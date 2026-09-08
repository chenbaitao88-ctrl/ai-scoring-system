"""
评分流程测试（5个用例）
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Team, Work, Competition
from services.scorer import AutoScorer
from services.llm_service import LLMService


@pytest.mark.scorer
class TestScorer:

    def test_score_team_basic(self, db, mock_llm_service, mock_scorer_methods, mock_works_dir):
        """基础评分流程：创建队伍 -> 创建作品 -> 评分"""
        # 创建测试数据
        team = Team(
            team_code="TEST001",
            team_name="测试队伍",
            short_code="T001",
            group_type="小学组",
            school="测试学校"
        )
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=200,
            source_files=[{"ext": ".py", "name": "main.py"}],
            aigc_log_files=[],
            is_parsed=True,
            original_filename="test.zip",
        )
        db.add(work)
        db.commit()

        # 创建作品解压目录（模拟文件系统）
        extract_dir = mock_works_dir / "test"
        extract_dir.mkdir(exist_ok=True)

        # 评分
        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.total_score > 0
        assert result.anchor_level == "Lv0"

    def test_score_team_empty_work(self, db, mock_llm_service, mock_scorer_methods, mock_works_dir):
        """空作品评分：应标记为 Lv3"""
        team = Team(team_code="TEST002", team_name="空作品队", short_code="T002", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            source_files=[],
            aigc_log_files=[],
            is_parsed=True,
            original_filename="empty.zip",
        )
        db.add(work)
        db.commit()

        extract_dir = mock_works_dir / "empty"
        extract_dir.mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.anchor_level == "Lv3"

    def test_score_team_with_files(self, db, mock_llm_service, mock_scorer_methods, mock_works_dir):
        """有文件作品的评分流程"""
        team = Team(team_code="TEST003", team_name="有文件队", short_code="T003", group_type="初中组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=500,
            source_files=[
                {"ext": ".py", "name": "main.py"},
                {"ext": ".png", "name": "bg.png"},
            ],
            aigc_log_files=[{"name": "chat1.txt"}],
            is_parsed=True,
            original_filename="files.zip",
        )
        db.add(work)
        db.commit()

        extract_dir = mock_works_dir / "files"
        extract_dir.mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.calibrated_score > 0  # P1-6 校准

    def test_anchor_cap_lv0(self, db, mock_llm_service, mock_scorer_methods, mock_works_dir):
        """Lv0 作品不截断，confidence=HIGH"""
        team = Team(team_code="TEST004", team_name="Lv0队", short_code="T004", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="lv0.zip",
        )
        db.add(work)
        db.commit()

        extract_dir = mock_works_dir / "lv0"
        extract_dir.mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.anchor_level == "Lv0"
        assert result.confidence == "HIGH"

    def test_score_team_no_llm(self, db, mock_scorer_methods, mock_works_dir):
        """LLM 不可用时的降级评分（不 mock chat_json，让 LLM 调用失败走规则评分分支）"""
        team = Team(team_code="TEST005", team_name="无LLM队", short_code="T005", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="nollm.zip",
        )
        db.add(work)
        db.commit()

        extract_dir = mock_works_dir / "nollm"
        extract_dir.mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.total_score > 0

    # ========== 新增测试：拆分前契约保护 ==========

    def test_score_all_batch(self, db, mock_llm_service, mock_scorer_methods, mock_works_dir):
        """批量评分：验证 success/failed 统计和错误收集"""
        # 正常队伍
        team1 = Team(team_code="B001", team_name="正常队", short_code="B001", group_type="小学组")
        db.add(team1)
        db.commit()
        work1 = Work(
            team_id=team1.id,
            code_language="Python",
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="b1.zip",
        )
        db.add(work1)
        db.commit()
        (mock_works_dir / "b1").mkdir(exist_ok=True)

        # 队伍不存在（work 指向不存在的 team_id）
        work_orphan = Work(
            team_id=9999,
            code_language="Python",
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="orphan.zip",
        )
        db.add(work_orphan)
        db.commit()

        scorer = AutoScorer(db)
        results = scorer.score_all()

        assert results["total"] == 2
        assert results["success"] == 1
        assert results["failed"] == 1
        assert len(results["errors"]) == 1
        assert "队伍不存在" in results["errors"][0]

    def test_score_team_ai_only_mode(self, db, monkeypatch, mock_scorer_methods, mock_works_dir):
        """ai_only 评分模式：验证双轨维度映射和推断分字段"""
        # 创建 ai_only 比赛配置
        comp = Competition(
            name="AI测试赛",
            scoring_mode="ai_only",
            ai_weight=0.4,
            human_weight=0.6,
            is_active=True,
        )
        db.add(comp)
        db.commit()

        # Mock LLM 返回 ai_only 字段
        async def mock_chat_json_ai_only(self, system_prompt, user_prompt, temperature=0.2, model=None, seed=None):
            return {
                "ai_theme_score": 4,
                "ai_code_quality_score": 12,
                "ai_completeness_score": 8,
                "ai_aigc_score": 7,
                "human_presentation_score": 20,
                "human_creativity_score": 10,
                "human_process_score": 12,
                "human_performance_score": 4,
                "theme_comment": "AI主题评语",
                "presentation_comment": "AI表现力评语",
                "process_comment": "AI过程评语",
                "ai_literacy_comment": "AI素养评语",
                "overall_comment": "AI总评",
                "defense_questions": ["Q1"],
            }

        monkeypatch.setattr(LLMService, "chat_json", mock_chat_json_ai_only)

        team = Team(team_code="AI001", team_name="AI队", short_code="AI001", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=300,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="ai.zip",
        )
        db.add(work)
        db.commit()

        (mock_works_dir / "ai").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.used_llm is True
        # AI 维度分
        assert result.ai_theme_score == 4
        assert result.ai_code_quality_score == 12
        assert result.ai_completeness_score == 8
        assert result.ai_aigc_score == 7
        assert result.ai_total_score == 31
        # 推断人工维度分
        assert result.human_presentation_score == 20
        assert result.human_creativity_score == 10
        assert result.human_process_score == 12
        assert result.human_performance_score == 4
        assert result.human_total_score == 46
        # 映射到旧字段（ai_only 模式下旧字段 = AI 维度分）
        assert result.theme_score == result.ai_theme_score
        assert result.presentation_score == result.ai_code_quality_score
        assert result.process_score == result.ai_completeness_score
        assert result.ai_literacy_score == result.ai_aigc_score
        # total_score 经 apply_anchor_cap 后按旧 4 维重新汇总
        assert result.total_score == (
            result.theme_score + result.presentation_score +
            result.process_score + result.ai_literacy_score
        )
        # 推断分字段同步
        assert result.inferred_presentation_score == result.human_presentation_score
        assert result.inferred_total_score == result.human_total_score

    def test_analyze_document_with_readme(self):
        """_analyze_document：work.readme_content 存在时的分支"""
        work = type("Work", (), {
            "readme_content": "这是一份详细的作品说明文档。" * 30,  # 约 420 字符
            "document_files": [],
        })()
        scorer = AutoScorer(db=None)
        result = scorer._analyze_document(work, Path("/tmp"))

        assert result["has_document"] is True
        assert result["document_char_count"] > 400
        assert result["document_quality"] == 70  # 420 字符 → >=200 且 <500 → 70 分
        assert "这是一份详细的作品说明文档" in result["document_content"]

    def test_analyze_document_without_readme(self):
        """_analyze_document：无 readme 且无 document_files 时的默认分支"""
        work = type("Work", (), {
            "readme_content": None,
            "document_files": [],
        })()
        scorer = AutoScorer(db=None)
        result = scorer._analyze_document(work, Path("/tmp"))

        assert result["has_document"] is False
        assert result["document_char_count"] == 0
        assert result["document_quality"] == 0
        assert result["document_content"] == ""

    # ========== LLM 调用层契约保护（3C-0） ==========

    def test_llm_empty_response_uses_fallback(self, db, monkeypatch, mock_scorer_methods, mock_works_dir):
        """LLM 返回空响应时，chat_json → None，应降级到规则评分"""
        async def mock_chat_empty(self, system_prompt, user_prompt, temperature=0.3, model=None, seed=None):
            return ""

        monkeypatch.setattr(LLMService, "chat", mock_chat_empty)

        team = Team(team_code="LLM001", team_name="空响应队", short_code="L001", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="empty_llm.zip",
        )
        db.add(work)
        db.commit()

        (mock_works_dir / "empty_llm").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.used_llm is False
        assert result.total_score > 0
        assert "规则评分" in result.overall_comment

    def test_llm_malformed_json_uses_fallback(self, db, monkeypatch, mock_scorer_methods, mock_works_dir):
        """LLM 返回非 JSON 内容时，chat_json 三层解析均失败，应降级到规则评分"""
        async def mock_chat_garbage(self, system_prompt, user_prompt, temperature=0.3, model=None, seed=None):
            return "抱歉，我无法理解您的请求，这不是一个有效的 JSON 响应。"

        monkeypatch.setattr(LLMService, "chat", mock_chat_garbage)

        team = Team(team_code="LLM002", team_name="乱码队", short_code="L002", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="garbage.zip",
        )
        db.add(work)
        db.commit()

        (mock_works_dir / "garbage").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.used_llm is False
        assert result.total_score > 0
        assert "规则评分" in result.overall_comment

    def test_llm_parameter_propagation(self, db, monkeypatch, mock_scorer_methods, mock_works_dir):
        """验证 model/seed/temperature 等参数正确传递到 LLMService.chat_json"""
        captured = {}

        async def mock_chat_json_capture(self, system_prompt, user_prompt, temperature=0.2, model=None, seed=None):
            captured["temperature"] = temperature
            captured["model"] = model
            captured["seed"] = seed
            captured["system_prompt_length"] = len(system_prompt)
            return {
                "theme_score": 10,
                "presentation_score": 15,
                "process_score": 12,
                "ai_literacy_score": 8,
                "theme_comment": "",
                "presentation_comment": "",
                "process_comment": "",
                "ai_literacy_comment": "",
                "overall_comment": "",
                "defense_questions": [],
            }

        monkeypatch.setattr(LLMService, "chat_json", mock_chat_json_capture)

        team = Team(team_code="LLM003", team_name="参数队", short_code="L003", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="param.zip",
        )
        db.add(work)
        db.commit()

        (mock_works_dir / "param").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id, model="kimi-test")

        assert result.is_completed
        assert result.used_llm is True
        assert captured["model"] == "kimi-test"
        assert captured["temperature"] == 0.3  # DETERMINISTIC_MODE=False 默认
        assert captured["seed"] is None
        assert captured["system_prompt_length"] > 0

    # ========== 规则评分/锚点/总分边界测试（3D-0） ==========

    def test_rule_scoring_rich_inputs(self):
        """规则评分：丰富特征输入应返回接近满分"""
        scorer = AutoScorer(db=None)

        rich_code = type("obj", (object,), {
            "game_mechanics": ["m1", "m2", "m3", "m4"],
            "features": ["f1", "f2", "f3", "异常处理"],
            "cyclomatic_complexity": 10,
            "quality_score": 85,
            "comment_rate": 20,
            "function_count": 6,
        })()
        rich_aigc = type("obj", (object,), {
            "optimization_count": 2,
            "user_messages": 10,
            "iteration_depth": 4,
            "code_generation_count": 2,
            "debugging_count": 2,
            "concept_count": 2,
            "duration_minutes": 90,
            "avg_question_length": 150,
        })()
        rich_feature = {
            "completeness_score": 85,
            "has_user_input": True,
            "has_animation": True,
            "has_sound": True,
            "has_menu": True,
        }
        rich_doc = {"document_quality": 85, "document_char_count": 600}

        theme_score, theme_cmt = scorer._score_theme(rich_code, rich_aigc, rich_doc)
        pres_score, pres_cmt = scorer._score_presentation(rich_code, rich_aigc, rich_feature, rich_doc)
        proc_score, proc_cmt = scorer._score_process(rich_code, rich_aigc, rich_doc)
        ai_score, ai_cmt = scorer._score_ai_literacy(rich_code, rich_aigc, rich_doc)

        assert theme_score == 20
        assert "游戏机制丰富" in theme_cmt
        assert pres_score == 30
        assert "代码质量高" in pres_cmt
        assert proc_score == 30
        assert "AIGC交互次数多" in proc_cmt
        assert ai_score == 20
        assert "使用AI生成+调试代码" in ai_cmt

    def test_rule_scoring_poor_and_none_inputs(self):
        """规则评分：贫乏/None 输入应返回基础分，不崩溃"""
        scorer = AutoScorer(db=None)

        # None 输入
        t_score, t_cmt = scorer._score_theme(None, None, None)
        assert t_score == 5
        assert "基础分5分" in t_cmt

        p_score, p_cmt = scorer._score_presentation(None, None, {}, None)
        assert p_score == 8
        assert "基础分8分" in p_cmt

        pr_score, pr_cmt = scorer._score_process(None, None, None)
        assert pr_score == 5
        assert "基础分5分" in pr_cmt

        a_score, a_cmt = scorer._score_ai_literacy(None, None, None)
        assert a_score == 4
        assert "基础分4分" in a_cmt

    def test_anchor_cap_all_levels(self, monkeypatch):
        """验证 Lv0-Lv3 各等级的锚点截断行为"""
        monkeypatch.setattr("services.anchor.flags", type("F", (), {"ANCHOR_V2": True})())
        from services.anchor import apply_anchor_cap

        raw = {"theme": 20.0, "presentation": 30.0, "process": 30.0, "ai_literacy": 20.0}

        # Lv0: 恒等
        capped, total, conf = apply_anchor_cap(raw.copy(), "Lv0")
        assert total == 100.0
        assert conf == "HIGH"
        assert capped == raw

        # Lv1: 维度截断 + 总分截断
        capped, total, conf = apply_anchor_cap(raw.copy(), "Lv1")
        assert capped["theme"] == 18.0
        assert capped["presentation"] == 22.0
        assert capped["process"] == 22.0
        assert capped["ai_literacy"] == 18.0
        assert total == 75.0
        assert conf == "MEDIUM"

        # Lv2
        capped, total, conf = apply_anchor_cap(raw.copy(), "Lv2")
        assert capped["theme"] == 14.0
        assert total == 50.0
        assert conf == "LOW"

        # Lv3
        capped, total, conf = apply_anchor_cap(raw.copy(), "Lv3")
        assert capped["theme"] == 10.0
        assert total == 40.0
        assert conf == "UNKNOWN"

    def test_fallback_scoring_exact_values(self, db, mock_scorer_methods, mock_works_dir):
        """LLM 不可用时走规则降级，验证各维度分精确值"""
        team = Team(team_code="FB001", team_name="降级队", short_code="FB001", group_type="小学组")
        db.add(team)
        db.commit()

        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="fallback.zip",
        )
        db.add(work)
        db.commit()

        (mock_works_dir / "fallback").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.used_llm is False
        assert result.anchor_level == "Lv0"
        assert result.confidence == "HIGH"
        # 基于 mock_scorer_methods 默认值的预期规则评分
        # 基于 mock_scorer_methods 默认值的预期规则评分
        # theme: base=5, mechanics=[], features=1项(不满足>=2), complexity=3(>1 +1), opt=0 → 6
        assert result.theme_score == 6
        # presentation: base=8, quality=7(<40), comment_rate=0.2(>0 +2), completeness=0, ux=0, func=2(<5), doc=0 → 10
        assert result.presentation_score == 10
        # process: base=5, user_msgs=0, iter=0, type_div=0, duration=0, comment_rate=0.2(>0 +1), doc=0 → 6
        assert result.process_score == 6
        # ai_literacy: base=4, 无加分项 → 4
        assert result.ai_literacy_score == 4
        assert result.total_score == 26
        assert "规则评分" in result.overall_comment

    def test_ai_only_total_score_cap_override(self, db, monkeypatch, mock_scorer_methods, mock_works_dir):
        """ai_only 模式下 apply_anchor_cap 用旧 4 维重新汇总 total_score，覆盖 ai+human 之和"""
        from services.llm_service import LLMService

        comp = Competition(
            name="AIO2", scoring_mode="ai_only", ai_weight=0.4,
            human_weight=0.6, is_active=True,
        )
        db.add(comp)
        db.commit()

        async def mock_ai_only(self, system_prompt, user_prompt, temperature=0.2, model=None, seed=None):
            return {
                "ai_theme_score": 5,
                "ai_code_quality_score": 15,
                "ai_completeness_score": 10,
                "ai_aigc_score": 10,
                "human_presentation_score": 25,
                "human_creativity_score": 15,
                "human_process_score": 15,
                "human_performance_score": 5,
                "theme_comment": "", "presentation_comment": "",
                "process_comment": "", "ai_literacy_comment": "",
                "overall_comment": "", "defense_questions": [],
            }

        monkeypatch.setattr(LLMService, "chat_json", mock_ai_only)

        team = Team(team_code="AO2", team_name="AO2", short_code="AO2", group_type="小学组")
        db.add(team)
        db.commit()
        work = Work(
            team_id=team.id,
            code_language="Python",
            code_line_count=100,
            source_files=[{"ext": ".py", "name": "main.py"}],
            is_parsed=True,
            original_filename="ao2.zip",
        )
        db.add(work)
        db.commit()
        (mock_works_dir / "ao2").mkdir(exist_ok=True)

        scorer = AutoScorer(db)
        result = scorer.score_team(team.id)

        assert result.is_completed
        assert result.used_llm is True
        # AI 维度分
        assert result.ai_total_score == 40  # 5+15+10+10
        # 推断人工维度分
        assert result.human_total_score == 60  # 25+15+15+5
        # 历史行为：apply_anchor_cap 后 total_score 按旧 4 维重新汇总
        assert result.total_score == (
            result.theme_score + result.presentation_score +
            result.process_score + result.ai_literacy_score
        )
        # 明确记录：total_score 不等于 ai_total + human_total
        assert result.total_score != result.ai_total_score + result.human_total_score
