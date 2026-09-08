"""
AI自动评分服务
- 整合代码分析 + AIGC日志解析 + 文档内容分析
- 结合任务书进行LLM评分
- 区分小学组/初中组不同评分标准
- 自动生成四维度评语和总评语
- 按四大维度打分：主题立意(20) + 产品表现力(30) + 过程完整性(30) + AI素养(20)
"""
import os
import json
import asyncio
import time
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass, field

from database import SessionLocal, Team, Work, MachineScore
from services.code_analyzer import CodeAnalyzer
from services.aigc_parser import AIGCParser
from services.document_parser import document_parser
from services.llm_scoring_service import llm_scoring_service
from services.rule_scoring_service import RuleScoringService
from services.work_analysis_service import WorkAnalysisService
from services.scoring_context_service import ScoringContextService
from services.scoring_persistence_service import ScoringPersistenceService
from config import WORKS_DIR, get_scoring_dimensions

# P0-4 (2026-05-22): 锚点分层评分
# - compute_anchor_level: 评分前调用, 写 works.anchor_level, 返回 level
# - apply_anchor_cap:     维度评分汇总后、写库前调用, 硬截断 + 填 confidence
# - build_level_hint:     注入 LLM prompt 最前面 (软约束)
# 三个函数内部都判断 flags.ANCHOR_V2, OFF 时短路返回, 行为与 P0-4 前一致
from services.anchor import compute_anchor_level, apply_anchor_cap, build_level_hint

# P0-5 (2026-05-22): Flag 规则引擎
# - FlagEngine().check(result): 评分完成后打标异常, 返回 List[FlagRecord]
# - 内部判断 flags.MONITOR_GUARD, OFF 时返回 []，主流程零开销
from services.flag_engine import FlagEngine

# P2-3: 结构化日志（兼容旧代码的 log 调用）
from utils.logger import ScoringTrace, logger as scoring_logger

def log(msg):
    """兼容旧代码的 log 调用"""
    scoring_logger.info(msg)


@dataclass
class ScoringResult:
    """评分结果"""
    team_id: int
    team_name: str
    short_code: str
    group_type: str = ""

    # 各维度得分
    theme_score: float = 0       # 主题立意 (0-20)
    presentation_score: float = 0  # 产品表现力 (0-30)
    process_score: float = 0     # 过程完整性 (0-30)
    ai_literacy_score: float = 0  # AI素养 (0-20)
    total_score: float = 0       # 总分 (0-100)

    # 各维度评语
    theme_comment: str = ""
    presentation_comment: str = ""
    process_comment: str = ""
    ai_literacy_comment: str = ""
    overall_comment: str = ""
    defense_questions: List[str] = field(default_factory=list)

    # 评分详情
    code_analysis: Optional[dict] = None
    aigc_analysis: Optional[dict] = None
    feature_detection: Optional[dict] = None

    # 状态
    is_completed: bool = False
    error_message: str = ""
    used_llm: bool = False  # 是否使用了LLM评分

    # P0-4 (2026-05-22): 锚点分层结果
    anchor_level: str = "Lv0"     # Lv0/Lv1/Lv2/Lv3, ANCHOR_V2=off 时恒为 Lv0
    confidence: str = "UNKNOWN"   # HIGH/MEDIUM/LOW/UNKNOWN, 由 apply_anchor_cap 决定

    # P1-6 (2026-05-26): 校准后总分
    calibrated_score: float = 0.0  # 校准后的总分 (0-100)

    # P0-5 (2026-05-22): Flag 引擎所需字段 + 输出
    elapsed_sec: float = 0.0          # 评分总耗时 (秒), score_team_async 末尾用 time.monotonic 计算
    llm_called: bool = False          # LLM 是否真被调用 (= used_llm), 显式字段供 flag_engine 用
    flags: List[dict] = field(default_factory=list)  # FlagEngine 输出 [{code,level,msg}, ...]

    # P2-2 (2026-05-27): 确定性模式参数（透传到 scoring_details 用于审计）
    score_seed: Optional[int] = None
    score_temperature: Optional[float] = None

    # Phase 1.3: 双轨评分 AI 维度分
    ai_theme_score: float = 0
    ai_code_quality_score: float = 0
    ai_completeness_score: float = 0
    ai_aigc_score: float = 0
    ai_total_score: float = 0
    # 纯AI模式推断分
    human_presentation_score: float = 0
    human_creativity_score: float = 0
    human_process_score: float = 0
    human_performance_score: float = 0
    human_total_score: float = 0
    # Phase 2: 推断分字段（用于数据库存储，与 human_* 数值相同）
    inferred_presentation_score: float = 0
    inferred_creativity_score: float = 0
    inferred_process_score: float = 0
    inferred_performance_score: float = 0
    inferred_total_score: float = 0


class AutoScorer:
    """AI自动评分器"""

    def __init__(self, db=None):
        self.db = db or SessionLocal()
        self.code_analyzer = CodeAnalyzer()
        self.aigc_parser = AIGCParser()
        self._work_analysis = WorkAnalysisService()
        self._context_service = ScoringContextService()
        self._persistence = ScoringPersistenceService(self.db)
        self._rule_scoring = RuleScoringService()

    def score_team(self, team_id: int, model: str = None, session_id: str = None) -> ScoringResult:
        """对单个队伍进行自动评分（同步入口）

        Args:
            team_id: 队伍ID
            model: 指定LLM模型（可选）
            session_id: 评分批次ID（可选）
        """
        try:
            loop = asyncio.get_running_loop()
            # 如果事件循环已在运行，创建新任务
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self.score_team_async(team_id, model, session_id))
                return future.result()
        except RuntimeError:
            # 没有运行中的事件循环，使用新事件循环
            return asyncio.run(self.score_team_async(team_id, model, session_id))

    async def score_team_async(self, team_id: int, model: str = None, session_id: str = None) -> ScoringResult:
        """对单个队伍进行自动评分（异步）

        Args:
            team_id: 队伍ID
            model: 指定LLM模型（可选）
        """
        team = self.db.query(Team).filter(Team.id == team_id).first()
        if not team:
            return ScoringResult(team_id=team_id, team_name="", short_code="",
                               error_message="队伍不存在")

        result = ScoringResult(
            team_id=team_id,
            team_name=team.team_name,
            short_code=team.short_code or "",
            group_type=team.group_type or ""
        )

        # P2-3: 初始化评分追踪
        trace = ScoringTrace(team_id=team_id, team_name=result.team_name)
        trace.log("SCORE_START", model=model)

        # 获取作品
        work = team.work
        if not work or not work.is_parsed:
            result.error_message = "作品未上传或未解析"
            return result

        # 获取解压目录
        extract_dir = WORKS_DIR / Path(work.original_filename or "").stem
        if not extract_dir.exists():
            result.error_message = "作品文件不存在"
            return result

        try:
            # P0-5 (2026-05-22): 评分计时起点 (放在 try 内、所有 IO 之前)
            #   - 必须用 time.monotonic() 而非 time.time()(避免系统时钟跳变)
            #   - 异常分支也会到 finally(此处用 try/except 外加 except 兜底, 见下文)
            _t0 = time.monotonic()

            # 1. 代码分析
            code_result = self._analyze_code(work, extract_dir)
            result.code_analysis = self._code_result_to_dict(code_result)
            trace.log("CODE_ANALYSIS", result={"language": code_result.language, "lines": code_result.total_lines})

            # 2. AIGC日志分析
            aigc_result = self._analyze_aigc(work, extract_dir)
            result.aigc_analysis = self._aigc_result_to_dict(aigc_result)
            trace.log("AIGC_ANALYSIS", result={"log_count": len(work.aigc_log_files or [])})

            # 3. 文档内容分析
            doc_result = self._analyze_document(work, extract_dir)

            # 4. 功能检测
            feature_result = self._detect_features(work, extract_dir, code_result)
            result.feature_detection = feature_result

            # P0-4 (2026-05-22): 计算并持久化锚点等级
            # - 必须在所有维度评分之前调用(level_hint 注入 LLM prompt)
            # - 内部已做异常防护, commit 失败也只 warning 不抛异常
            # - ANCHOR_V2=off 时返回 'Lv0', 不写库, 行为同 P0-4 前
            anchor_level = compute_anchor_level(self.db, work.id)
            result.anchor_level = anchor_level
            trace.log("ANCHOR_LEVEL", result={"level": anchor_level})
            level_hint = build_level_hint(anchor_level)   # OFF 时为空字符串

            # 5. 收集代码内容用于LLM评分
            code_content = self._collect_code_content(work, extract_dir)
            aigc_summary = self._collect_aigc_summary(aigc_result)
            doc_content = doc_result.get("document_content", "")
            screenshot_count = len(work.screenshot_files or [])

            # 6. 尝试LLM评分（结合任务书）
            work_info = {
                "code_language": work.code_language or "未知",
                "code_line_count": work.code_line_count or 0,
                "comment_rate": work.comment_rate or 0,
                "source_files": work.source_files or [],
                "aigc_log_files": work.aigc_log_files or [],
            }

            # P2-2: 确定性模式
            from feature_flags import flags
            score_seed = 42 if flags.DETERMINISTIC_MODE else None
            score_temperature = 0.0 if flags.DETERMINISTIC_MODE else 0.3
            result.score_seed = score_seed
            result.score_temperature = score_temperature

            # Phase 1.3: 查询当前比赛评分模式
            scoring_mode = "mixed"
            try:
                from database import Competition
                comp = self.db.query(Competition).filter(Competition.is_active == True).first()
                if comp:
                    scoring_mode = comp.scoring_mode
                    log(f" P{team_id} 当前比赛模式: {scoring_mode}")
            except Exception as e:
                log(f" P{team_id} 查询比赛模式失败: {e}, 使用默认 mixed")

            llm_result = await llm_scoring_service.score_with_task(
                group_type=result.group_type,
                work_info=work_info,
                code_content=code_content,
                aigc_summary=aigc_summary,
                doc_content=doc_content,
                screenshot_count=screenshot_count,
                model=model,
                level_hint=level_hint,
                anchor_level=anchor_level,
                seed=score_seed,
                temperature=score_temperature,
                scoring_mode=scoring_mode,
            )

            if llm_result:
                # 使用LLM评分结果
                trace.log("LLM_SCORE", result={"total": result.total_score, "model": model or "default"})

                # Phase 1.3: 双轨评分维度解析
                result.ai_theme_score = min(5, max(0, llm_result.get("ai_theme_score", 0)))
                result.ai_code_quality_score = min(15, max(0, llm_result.get("ai_code_quality_score", 0)))
                result.ai_completeness_score = min(10, max(0, llm_result.get("ai_completeness_score", 0)))
                result.ai_aigc_score = min(10, max(0, llm_result.get("ai_aigc_score", 0)))
                result.ai_total_score = result.ai_theme_score + result.ai_code_quality_score + result.ai_completeness_score + result.ai_aigc_score

                if scoring_mode == "ai_only":
                    result.human_presentation_score = min(25, max(0, llm_result.get("human_presentation_score", 0)))
                    result.human_creativity_score = min(15, max(0, llm_result.get("human_creativity_score", 0)))
                    result.human_process_score = min(15, max(0, llm_result.get("human_process_score", 0)))
                    result.human_performance_score = min(5, max(0, llm_result.get("human_performance_score", 0)))
                    result.human_total_score = result.human_presentation_score + result.human_creativity_score + result.human_process_score + result.human_performance_score
                    # Phase 2: 同步写入推断分字段（数据库存储用）
                    result.inferred_presentation_score = result.human_presentation_score
                    result.inferred_creativity_score = result.human_creativity_score
                    result.inferred_process_score = result.human_process_score
                    result.inferred_performance_score = result.human_performance_score
                    result.inferred_total_score = result.human_total_score
                    result.theme_score = result.ai_theme_score
                    result.presentation_score = result.ai_code_quality_score
                    result.process_score = result.ai_completeness_score
                    result.ai_literacy_score = result.ai_aigc_score
                    result.total_score = result.ai_total_score + result.human_total_score
                else:
                    result.theme_score = min(20, max(0, llm_result.get("theme_score", 0)))
                    result.presentation_score = min(30, max(0, llm_result.get("presentation_score", 0)))
                    result.process_score = min(30, max(0, llm_result.get("process_score", 0)))
                    result.ai_literacy_score = min(20, max(0, llm_result.get("ai_literacy_score", 0)))
                    result.total_score = result.theme_score + result.presentation_score + result.process_score + result.ai_literacy_score

                result.theme_comment = llm_result.get("theme_comment", "")
                result.presentation_comment = llm_result.get("presentation_comment", "")
                result.process_comment = llm_result.get("process_comment", "")
                result.ai_literacy_comment = llm_result.get("ai_literacy_comment", "")
                result.overall_comment = llm_result.get("overall_comment", "")
                result.defense_questions = llm_result.get("defense_questions", [])
                result.used_llm = True

                if scoring_mode == "ai_only":
                    log(f" P{team_id} LLM评分成功(纯AI): ai_total={result.ai_total_score}, human_total={result.human_total_score}, total={result.total_score}")
                else:
                    log(f" P{team_id} LLM评分成功(混合): ai_theme={result.ai_theme_score}, code_quality={result.ai_code_quality_score}, completeness={result.ai_completeness_score}, aigc={result.ai_aigc_score}")
            else:
                # LLM不可用时，降级为规则评分
                result.theme_score, result.theme_comment = self._score_theme(code_result, aigc_result, doc_result)
                result.presentation_score, result.presentation_comment = self._score_presentation(code_result, aigc_result, feature_result, doc_result)
                result.process_score, result.process_comment = self._score_process(code_result, aigc_result, doc_result)
                result.ai_literacy_score, result.ai_literacy_comment = self._score_ai_literacy(code_result, aigc_result, doc_result)
                result.overall_comment = f"规则评分总评：主题立意{result.theme_score}分，表现力{result.presentation_score}分，过程完整性{result.process_score}分，AI素养{result.ai_literacy_score}分。总分{result.total_score}分。建议配置LLM API以获得更精准的评分和评语。"

            # 7. 总分 (P0-4: 先汇总, 然后用 apply_anchor_cap 硬截断)
            #    位置: 在所有维度评分汇总后, 写库前 — 与方案承诺一致
            #    ANCHOR_V2=off 时 apply_anchor_cap 直接返回原分 + 'UNKNOWN', 行为同 P0-4 前
            raw_dims = {
                "theme": result.theme_score,
                "presentation": result.presentation_score,
                "process": result.process_score,
                "ai_literacy": result.ai_literacy_score,
            }
            capped_dims, capped_total, confidence = apply_anchor_cap(raw_dims, anchor_level)
            result.theme_score = capped_dims["theme"]
            result.presentation_score = capped_dims["presentation"]
            result.process_score = capped_dims["process"]
            result.ai_literacy_score = capped_dims["ai_literacy"]
            result.total_score = capped_total
            result.confidence = confidence

            # P1-6 (2026-05-26): 评分校准
            #   - 在 apply_anchor_cap 之后、Flag 引擎之前插入
            #   - ANCHOR_V2=off 时跳过校准，calibrated_score = total_score
            from services.calibrator import get_calibrator
            from feature_flags import flags
            if flags.ANCHOR_V2:
                try:
                    calibrator = get_calibrator()
                    result.calibrated_score = calibrator.adjust(result.total_score, result.anchor_level)
                    log(f" P{team_id} 校准: raw={result.total_score:.1f} -> calibrated={result.calibrated_score:.1f} (level={result.anchor_level})")
                except Exception as cal_e:
                    log(f" P{team_id} 校准异常 (已吞): {cal_e}")
                    result.calibrated_score = result.total_score
            else:
                result.calibrated_score = result.total_score

            # P1-6 校准之后
            trace.log("CALIBRATION", result={"raw": result.total_score, "calibrated": result.calibrated_score})

            result.is_completed = True

            # P0-5 (2026-05-22): 计算评分耗时 + 同步 llm_called, 然后跑 Flag 引擎
            #   - elapsed_sec: time.monotonic() - _t0, 必须在 LLM 评分之后、写库之前
            #   - llm_called: result.used_llm 已经在 LLM/规则分支里正确设置
            #   - FlagEngine 内部判断 MONITOR_GUARD, OFF 时直接返回 [], 主流程零开销
            #   - check() 内部已 try/except 单条规则, 不会因为一条规则崩溃影响主流程
            result.elapsed_sec = time.monotonic() - _t0
            result.llm_called = result.used_llm
            try:
                _flag_records = FlagEngine().check(result)
                result.flags = [r.to_dict() for r in _flag_records]
                # P1 优化效果验证后新增：raw <= 5 的作品 AI 未能有效解析
                if result.total_score <= 5:
                    result.flags.append({
                        "code": "AI_SCORE_LOW_CONFIDENCE",
                        "level": "WARNING",
                        "message": "AI 原始评分 <= 5，未能有效解析作品，请以现场评审分为准"
                    })
                # P0-5 Flag 引擎之后
                trace.log("FLAG_CHECK", result={"flags": [f.code for f in _flag_records]})
                if result.flags:
                    log(f" P{team_id} Flag 命中 {len(result.flags)} 条: "
                        f"{[f['code'] for f in result.flags]}")
            except Exception as _e:
                # FlagEngine 内部已有规则级 try/except, 这里是 belt-and-suspenders
                log(f" P{team_id} FlagEngine 整体异常 (已吞): {_e}")
                result.flags = []

            # 8. 保存到数据库 (confidence/flags 字段已通过 ScoringResult 透传到 _save_score)
            self._save_score(team_id, result, model_name=model, session_id=session_id)

        except Exception as e:
            import traceback
            log(f" P{team_id} 异常: {str(e)}")
            log(f" P{team_id} 堆栈: {traceback.format_exc()}")
            result.error_message = f"评分失败: {str(e)}"

        # P2-3: 评分完成，记录汇总
        trace.finish(
            total_score=result.total_score,
            anchor_level=result.anchor_level,
            flags=[f.get("code") for f in result.flags] if result.flags else [],
        )
        return result

    def score_all(self) -> Dict:
        """对所有已解析作品进行评分"""
        works = self.db.query(Work).filter(Work.is_parsed == True).all()
        results = {"total": len(works), "success": 0, "failed": 0, "errors": []}

        for work in works:
            try:
                result = self.score_team(work.team_id)
                if result.is_completed:
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append(f"P{work.team_id:03d}: {result.error_message}")
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"P{work.team_id:03d}: {str(e)}")

        return results

    def _analyze_code(self, work: Work, extract_dir: Path):
        """分析代码（委托给 WorkAnalysisService）"""
        return self._work_analysis.analyze_code(work, extract_dir)

    def _analyze_aigc(self, work: Work, extract_dir: Path):
        """分析AIGC日志（委托给 WorkAnalysisService）"""
        return self._work_analysis.analyze_aigc(work, extract_dir)

    def _analyze_document(self, work: Work, extract_dir: Path) -> dict:
        """分析说明文档（委托给 WorkAnalysisService）"""
        return self._work_analysis.analyze_document(work, extract_dir)

    def _detect_features(self, work: Work, extract_dir: Path, code_result) -> dict:
        """功能检测（委托给 WorkAnalysisService）"""
        return self._work_analysis.detect_features(work, extract_dir, code_result)

    def _collect_code_content(self, work: Work, extract_dir: Path) -> str:
        """收集代码内容用于LLM分析（委托给 ScoringContextService）"""
        return self._context_service.collect_code_content(work, extract_dir)

    def _collect_aigc_summary(self, aigc_result) -> str:
        """收集AIGC交互摘要（委托给 ScoringContextService）"""
        return self._context_service.collect_aigc_summary(aigc_result)

    # ============ 规则评分（LLM不可用时的降级方案）============

    def _score_theme(self, code_result, aigc_result, doc_result: dict = None) -> tuple:
        """主题立意评分（委托给 RuleScoringService）"""
        return self._rule_scoring._score_theme(code_result, aigc_result, doc_result)

    def _score_presentation(self, code_result, aigc_result, feature_result: dict, doc_result: dict = None) -> tuple:
        """产品表现力评分（委托给 RuleScoringService）"""
        return self._rule_scoring._score_presentation(code_result, aigc_result, feature_result, doc_result)

    def _score_process(self, code_result, aigc_result, doc_result: dict = None) -> tuple:
        """过程完整性评分（委托给 RuleScoringService）"""
        return self._rule_scoring._score_process(code_result, aigc_result, doc_result)

    def _score_ai_literacy(self, code_result, aigc_result, doc_result: dict = None) -> tuple:
        """AI素养评分（委托给 RuleScoringService）"""
        return self._rule_scoring._score_ai_literacy(code_result, aigc_result, doc_result)

    # ============ 辅助方法 ============

    def _code_result_to_dict(self, result) -> dict:
        """代码分析结果转字典（委托给 ScoringPersistenceService）"""
        return self._persistence.code_result_to_dict(result)

    def _aigc_result_to_dict(self, result) -> dict:
        """AIGC分析结果转字典（委托给 ScoringPersistenceService）"""
        return self._persistence.aigc_result_to_dict(result)

    def _save_score(self, team_id: int, result, model_name: str = None, session_id: str = None):
        """保存评分到数据库（委托给 ScoringPersistenceService）"""
        self._persistence.save_score(team_id, result, model_name=model_name, session_id=session_id)
