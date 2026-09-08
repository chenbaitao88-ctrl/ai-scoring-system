"""
评分结果持久化服务 —— 从 scorer.py 抽出的评分结果写入数据库层。

职责：
- MachineScore 记录的创建与更新
- 维度分、评语、Flag、置信度等字段的持久化
- 旧评分的"采用"状态取消

设计约束：
- 不计算分数
- 不调用 LLM
- 不组装 Prompt
- 只负责将 ScoringResult 写入数据库
"""
from database import MachineScore


class ScoringPersistenceService:
    """评分结果持久化器"""

    def __init__(self, db):
        self.db = db

    def save_score(self, team_id: int, result, model_name: str = None, session_id: str = None):
        """保存评分到数据库（每次评分创建新记录，并将之前评分取消采用）"""
        from feature_flags import flags

        # 将之前的评分取消"采用"状态
        self.db.query(MachineScore).filter(
            MachineScore.team_id == team_id,
            MachineScore.is_adopted == True
        ).update({"is_adopted": False})

        score_data = {
            "team_id": team_id,
            "model_name": model_name or "unknown",
            "scoring_session": session_id or "",
            "is_adopted": True,
            "theme_score": result.theme_score,
            "presentation_score": result.presentation_score,
            "process_score": result.process_score,
            "ai_literacy_score": result.ai_literacy_score,
            "total_score": result.total_score,
            "calibrated_score": result.calibrated_score,
            "ai_theme_score": getattr(result, "ai_theme_score", 0),
            "ai_code_quality_score": getattr(result, "ai_code_quality_score", 0),
            "ai_completeness_score": getattr(result, "ai_completeness_score", 0),
            "ai_aigc_score": getattr(result, "ai_aigc_score", 0),
            "ai_total_score": getattr(result, "ai_total_score", 0),
            "inferred_presentation_score": getattr(result, "inferred_presentation_score", 0),
            "inferred_creativity_score": getattr(result, "inferred_creativity_score", 0),
            "inferred_process_score": getattr(result, "inferred_process_score", 0),
            "inferred_performance_score": getattr(result, "inferred_performance_score", 0),
            "inferred_total_score": getattr(result, "inferred_total_score", 0),
            "theme_comment": result.theme_comment,
            "presentation_comment": result.presentation_comment,
            "process_comment": result.process_comment,
            "ai_literacy_comment": result.ai_literacy_comment,
            "overall_comment": result.overall_comment,
            "defense_questions": result.defense_questions,
            "scoring_details": {
                "theme": {"score": result.theme_score, "max": 20, "comment": result.theme_comment},
                "presentation": {"score": result.presentation_score, "max": 30, "comment": result.presentation_comment},
                "process": {"score": result.process_score, "max": 30, "comment": result.process_comment},
                "ai_literacy": {"score": result.ai_literacy_score, "max": 20, "comment": result.ai_literacy_comment},
                "overall_comment": result.overall_comment,
                "used_llm": result.used_llm,
                "ai_dimensions": {
                    "theme": {"score": result.ai_theme_score, "max": 5, "comment": getattr(result, "ai_theme_comment", "")},
                    "code_quality": {"score": result.ai_code_quality_score, "max": 15, "comment": getattr(result, "ai_code_quality_comment", "")},
                    "completeness": {"score": result.ai_completeness_score, "max": 10, "comment": getattr(result, "ai_completeness_comment", "")},
                    "aigc": {"score": result.ai_aigc_score, "max": 10, "comment": getattr(result, "ai_aigc_comment", "")},
                    "total": result.ai_total_score,
                },
                "model_version": model_name or "default",
                "prompt_version": "P1-3",
                "anchor_version": "P1-4" if flags.ANCHOR_V2 else "P0-4",
                "deterministic": flags.DETERMINISTIC_MODE,
                "seed": result.score_seed,
                "temperature": result.score_temperature,
            },
            "code_quality_details": result.code_analysis,
            "aigc_analysis_details": result.aigc_analysis,
            "feature_detection_details": result.feature_detection,
            "is_completed": True,
            "confidence": result.confidence,
            "flags": result.flags,
        }

        new_score = MachineScore(**score_data)
        self.db.add(new_score)
        self.db.commit()

    def code_result_to_dict(self, result) -> dict:
        """代码分析结果转字典"""
        return {
            "language": result.language,
            "total_lines": result.total_lines,
            "code_lines": result.code_lines,
            "comment_lines": result.comment_lines,
            "comment_rate": result.comment_rate,
            "function_count": result.function_count,
            "class_count": result.class_count,
            "cyclomatic_complexity": result.cyclomatic_complexity,
            "features": result.features,
            "game_mechanics": result.game_mechanics,
            "quality_score": result.quality_score,
            "creativity_score": result.creativity_score,
            "issues": result.issues,
            "warnings": result.warnings,
        }

    def aigc_result_to_dict(self, result) -> dict:
        """AIGC分析结果转字典"""
        return {
            "tool_name": result.tool_name,
            "total_interactions": result.total_interactions,
            "user_messages": result.user_messages,
            "code_generation_count": result.code_generation_count,
            "debugging_count": result.debugging_count,
            "concept_count": result.concept_count,
            "optimization_count": result.optimization_count,
            "iteration_depth": result.iteration_depth,
            "duration_minutes": result.duration_minutes,
            "features": result.features,
            "process_score": result.process_score,
            "ai_literacy_score": result.ai_literacy_score,
            "warnings": result.warnings,
        }
