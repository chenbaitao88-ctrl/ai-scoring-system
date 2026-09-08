"""
LLM 评分服务（评分业务专用）
- 封装评分场景下的 Prompt 构建和 LLM 调用协调
- 依赖底层 llm_service 的通用 chat/chat_json 能力
- 处理空响应、异常时返回 None，由调用方决定降级策略
"""
from typing import Optional

from services.llm_service import llm_service


def log(msg):
    """强制print输出（避免日志缓冲/过滤问题）"""
    print(f"[LLM_SCORE] {msg}", flush=True)


class LLMScoringService:
    """LLM 评分专用服务"""

    async def score_with_task(self, group_type: str, work_info: dict,
                              code_content: str, aigc_summary: str,
                              doc_content: str, screenshot_count: int,
                              model: str = None, level_hint: str = "",
                              anchor_level: str = "Lv0",
                              seed: int = None,
                              temperature: float = 0.3,
                              scoring_mode: str = "mixed") -> Optional[dict]:
        """结合任务书进行AI评分+评语生成

        Args:
            group_type: 组别
            work_info: 作品信息
            code_content: 代码内容
            aigc_summary: AIGC交互摘要
            doc_content: 文档内容
            screenshot_count: 截图数量
            model: 指定模型（可选）
            level_hint: P0-4 锚点等级提示（注入 user_prompt 最前面, ANCHOR_V2=off 时为空字符串）
            anchor_level: P1-3 锚点等级（控制评分维度数量）
            seed: P2-2 确定性模式种子
            temperature: P2-2 确定性模式温度
            scoring_mode: Phase 1.3 双轨评分模式（mixed/ai_only）
        """
        log(f"[score_with_task] 开始评分，模型={model or '默认'}，组别={group_type}，模式={scoring_mode}")
        log(f"[score_with_task] 代码长度={len(code_content) if code_content else 0}，文档长度={len(doc_content) if doc_content else 0}")

        from services.task_prompts import (
            build_scoring_prompt, get_scoring_prompt_from_db,
            build_dual_track_prompt
        )
        db = None
        try:
            from database import SessionLocal
            db = SessionLocal()
            # Phase 1.3: 双轨评分模式优先使用新 Prompt
            if scoring_mode in ("mixed", "ai_only"):
                system_prompt = build_dual_track_prompt(
                    task_book_content=None,
                    group_type=group_type,
                    scoring_mode=scoring_mode,
                    level=anchor_level
                )
            else:
                system_prompt = get_scoring_prompt_from_db(db, group_type, anchor_level)
                # 如果数据库没有任务书，降级到默认提示词
                if not system_prompt or "任务书" not in system_prompt:
                    system_prompt = build_scoring_prompt(group_type=group_type, level=anchor_level)
        finally:
            if db:
                db.close()

        # 从数据库获取任务书内容用于user_prompt
        task_req_text = ""
        db2 = None
        try:
            from database import SessionLocal
            from services.task_book_service import get_active_task_book
            db2 = SessionLocal()
            active_book = get_active_task_book(db2, group_type)
            if active_book and active_book.content_md:
                task_req_text = active_book.content_md
        finally:
            if db2:
                db2.close()

        user_prompt = f"""{level_hint}## 任务书要求
{task_req_text if task_req_text else '（无任务书，请按通用创意编程评分标准评分）'}

## 作品信息
- 组别：{group_type}
- 编程语言：{work_info.get('code_language', '未知')}
- 代码行数：{work_info.get('code_line_count', 0)}
- 注释率：{work_info.get('comment_rate', 0)}%
- 源文件数：{len(work_info.get('source_files', []))}
- AIGC日志数：{len(work_info.get('aigc_log_files', []))}
- 截图/视频数：{screenshot_count}
- 说明文档：{'有' if doc_content else '无'}

## 代码内容（结构化摘要，最多8000字）
{code_content[:8000] if code_content else '无代码'}

## AIGC交互摘要
{aigc_summary[:3000] if aigc_summary else '无AIGC日志'}

## 说明文档内容（最多3000字）
{doc_content[:3000] if doc_content else '无说明文档'}

请严格按照任务书要求，对以上作品进行评分并生成评语。"""

        log(f"[score_with_task] system_prompt长度={len(system_prompt)}")
        log(f"[score_with_task] user_prompt长度={len(user_prompt)}")

        llm_result = await llm_service.chat_json(system_prompt, user_prompt, temperature=temperature, model=model, seed=seed)

        if llm_result:
            log(f"[score_with_task] LLM评分成功：{list(llm_result.keys())}")
            return llm_result
        else:
            log(f"[score_with_task] LLM评分失败，降级到规则评分")
            return None


# 全局实例
llm_scoring_service = LLMScoringService()
