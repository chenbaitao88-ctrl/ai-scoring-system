"""
规则评分降级服务
- LLM 不可用时提供四维度规则评分
- 保持与 scorer.py 完全一致的分值阈值和默认值
"""
from typing import Dict, Tuple


class RuleScoringService:
    """规则评分降级服务"""

    def score_fallback(self, code_result, aigc_result, feature_result: dict, doc_result: dict = None) -> Dict:
        """执行完整的规则评分降级，返回四维度分+评语"""
        theme_score, theme_comment = self._score_theme(code_result, aigc_result, doc_result)
        presentation_score, presentation_comment = self._score_presentation(code_result, aigc_result, feature_result, doc_result)
        process_score, process_comment = self._score_process(code_result, aigc_result, doc_result)
        ai_literacy_score, ai_literacy_comment = self._score_ai_literacy(code_result, aigc_result, doc_result)
        return {
            "theme_score": theme_score,
            "theme_comment": theme_comment,
            "presentation_score": presentation_score,
            "presentation_comment": presentation_comment,
            "process_score": process_score,
            "process_comment": process_comment,
            "ai_literacy_score": ai_literacy_score,
            "ai_literacy_comment": ai_literacy_comment,
        }

    def _score_theme(self, code_result, aigc_result, doc_result: dict = None) -> Tuple[float, str]:
        """主题立意评分 (0-20) - 规则降级，返回 (score, comment)"""
        doc_result = doc_result or {}
        score = 5
        reasons = []

        mechanics = getattr(code_result, 'game_mechanics', [])
        if len(mechanics) >= 4:
            score += 6
            reasons.append(f"游戏机制丰富({len(mechanics)}种)")
        elif len(mechanics) >= 2:
            score += 4
            reasons.append(f"游戏机制较多({len(mechanics)}种)")
        elif len(mechanics) >= 1:
            score += 2
            reasons.append("有基础游戏机制")

        features = getattr(code_result, 'features', [])
        if len(features) >= 3:
            score += 4
            reasons.append(f"功能特性丰富({len(features)}项)")
        elif len(features) >= 2:
            score += 2
            reasons.append(f"功能特性较多({len(features)}项)")

        complexity = getattr(code_result, 'cyclomatic_complexity', 1)
        if 5 <= complexity <= 20:
            score += 3
            reasons.append("代码复杂度适中")
        elif complexity > 20:
            score += 2
            reasons.append("代码复杂度较高")
        elif complexity > 1:
            score += 1

        opt_count = getattr(aigc_result, 'optimization_count', 0)
        if opt_count > 0:
            score += 2
            reasons.append("有AI辅助优化记录")

        comment = f"规则评分（主题立意）：基础分5分，{'. '.join(reasons)}。" if reasons else "规则评分（主题立意）：基础分5分，无明显亮点。"
        return min(20, score), comment

    def _score_presentation(self, code_result, aigc_result, feature_result: dict, doc_result: dict = None) -> Tuple[float, str]:
        """产品表现力评分 (0-30) - 规则降级，返回 (score, comment)"""
        doc_result = doc_result or {}
        score = 8
        reasons = []

        quality = getattr(code_result, 'quality_score', 0)
        if quality >= 80:
            score += 6
            reasons.append(f"代码质量高({quality}分)")
        elif quality >= 60:
            score += 4
            reasons.append(f"代码质量中等({quality}分)")
        elif quality >= 40:
            score += 2

        comment_rate = getattr(code_result, 'comment_rate', 0)
        if 10 <= comment_rate <= 30:
            score += 4
            reasons.append(f"注释率合理({comment_rate}%)")
        elif comment_rate > 0:
            score += 2
            reasons.append(f"有注释({comment_rate}%)")

        completeness = feature_result.get('completeness_score', 0)
        if completeness >= 80:
            score += 6
            reasons.append("功能完整度高")
        elif completeness >= 60:
            score += 4
            reasons.append("功能完整度中等")
        elif completeness >= 40:
            score += 2

        ux_features = sum([
            feature_result.get('has_user_input', False),
            feature_result.get('has_animation', False),
            feature_result.get('has_sound', False),
            feature_result.get('has_menu', False),
        ])
        score += min(4, ux_features)
        if ux_features > 0:
            reasons.append(f"用户体验特性({ux_features}项)")

        func_count = getattr(code_result, 'function_count', 0)
        if func_count >= 5:
            score += 2
            reasons.append(f"函数数量多({func_count}个)")

        doc_quality = doc_result.get('document_quality', 0)
        if doc_quality >= 80:
            score += 3
            reasons.append("文档质量高")
        elif doc_quality >= 60:
            score += 2
            reasons.append("文档质量中等")
        elif doc_quality >= 40:
            score += 1

        comment = f"规则评分（表现力）：基础分8分，{'. '.join(reasons)}。" if reasons else "规则评分（表现力）：基础分8分，无明显亮点。"
        return min(30, score), comment

    def _score_process(self, code_result, aigc_result, doc_result: dict = None) -> Tuple[float, str]:
        """过程完整性评分 (0-30) - 规则降级，返回 (score, comment)"""
        doc_result = doc_result or {}
        score = 5
        reasons = []

        user_msgs = getattr(aigc_result, 'user_messages', 0)
        if user_msgs >= 8:
            score += 8
            reasons.append(f"AIGC交互次数多({user_msgs}次)")
        elif user_msgs >= 5:
            score += 6
            reasons.append(f"AIGC交互次数较多({user_msgs}次)")
        elif user_msgs >= 3:
            score += 4
            reasons.append(f"AIGC交互次数一般({user_msgs}次)")
        elif user_msgs >= 1:
            score += 2
            reasons.append(f"有AIGC交互({user_msgs}次)")

        iter_depth = getattr(aigc_result, 'iteration_depth', 0)
        if iter_depth >= 3:
            score += 5
            reasons.append(f"迭代深度深({iter_depth}轮)")
        elif iter_depth >= 2:
            score += 3
            reasons.append(f"迭代深度中等({iter_depth}轮)")

        type_diversity = sum([
            getattr(aigc_result, 'code_generation_count', 0) > 0,
            getattr(aigc_result, 'debugging_count', 0) > 0,
            getattr(aigc_result, 'concept_count', 0) > 0,
            getattr(aigc_result, 'optimization_count', 0) > 0,
        ])
        score += type_diversity * 3
        if type_diversity > 0:
            reasons.append(f"AIGC交互类型多样({type_diversity}种)")

        duration = getattr(aigc_result, 'duration_minutes', 0)
        if duration >= 60:
            score += 4
            reasons.append(f"AIGC使用时长({duration:.0f}分钟)")
        elif duration >= 30:
            score += 2
            reasons.append(f"AIGC使用时长中等({duration:.0f}分钟)")

        comment_rate = getattr(code_result, 'comment_rate', 0)
        if comment_rate >= 15:
            score += 3
            reasons.append(f"代码注释充分({comment_rate}%)")
        elif comment_rate > 0:
            score += 1

        doc_char_count = doc_result.get('document_char_count', 0)
        if doc_char_count >= 500:
            score += 3
            reasons.append(f"文档详细({doc_char_count}字)")
        elif doc_char_count >= 200:
            score += 2
            reasons.append(f"文档较详细({doc_char_count}字)")
        elif doc_char_count >= 50:
            score += 1
            reasons.append(f"有基础文档({doc_char_count}字)")

        comment = f"规则评分（过程完整性）：基础分5分，{'. '.join(reasons)}。" if reasons else "规则评分（过程完整性）：基础分5分，无明显亮点。"
        return min(30, score), comment

    def _score_ai_literacy(self, code_result, aigc_result, doc_result: dict = None) -> Tuple[float, str]:
        """AI素养评分 (0-20) - 规则降级，返回 (score, comment)"""
        score = 4
        reasons = []

        gen_count = getattr(aigc_result, 'code_generation_count', 0)
        debug_count = getattr(aigc_result, 'debugging_count', 0)
        if gen_count > 0 and debug_count > 0:
            score += 5
            reasons.append(f"使用AI生成+调试代码(生成{gen_count}次,调试{debug_count}次)")
        elif gen_count > 0:
            score += 2
            reasons.append(f"使用AI生成代码({gen_count}次)")

        avg_len = getattr(aigc_result, 'avg_question_length', 0)
        if avg_len > 100:
            score += 3
            reasons.append(f"AI提问详细(平均{avg_len:.0f}字)")
        elif avg_len > 50:
            score += 2
            reasons.append(f"AI提问较详细(平均{avg_len:.0f}字)")

        concept_count = getattr(aigc_result, 'concept_count', 0)
        if concept_count > 0:
            score += 3
            reasons.append(f"涉及AI概念({concept_count}个)")

        opt_count = getattr(aigc_result, 'optimization_count', 0)
        if opt_count > 0:
            score += 3
            reasons.append(f"使用AI优化代码({opt_count}次)")

        features = getattr(code_result, 'features', [])
        if '异常处理' in features:
            score += 2
            reasons.append("代码有异常处理")

        comment = f"规则评分（AI素养）：基础分4分，{'. '.join(reasons)}。" if reasons else "规则评分（AI素养）：基础分4分，无明显亮点。"
        return min(20, score), comment
