"""
作品分析服务 —— 从 scorer.py 抽出的作品/文档分析层。

职责：
- 代码分析（薄包装，调用 CodeAnalyzer）
- AIGC 日志分析（薄包装，调用 AIGCParser）
- 说明文档分析（README / document_files 内容质量评估）
- 功能特性检测（基于 code_analyzer 输出）

设计约束：
- 不依赖 LLM 调用
- 不修改数据库
- 不组装评分结果
"""
from pathlib import Path
from typing import Dict

from services.code_analyzer import CodeAnalyzer, CodeAnalysisResult
from services.aigc_parser import AIGCParser, AIGCAnalysisResult
from services.document_parser import document_parser
from database import Work


class WorkAnalysisService:
    """作品分析器：为评分流程提供作品元数据与结构化分析"""

    def __init__(self):
        self.code_analyzer = CodeAnalyzer()
        self.aigc_parser = AIGCParser()

    def analyze_code(self, work: Work, extract_dir: Path) -> CodeAnalysisResult:
        """分析代码"""
        source_files = work.source_files or []
        if not source_files:
            return CodeAnalysisResult()
        return self.code_analyzer.analyze_project(source_files, extract_dir)

    def analyze_aigc(self, work: Work, extract_dir: Path) -> AIGCAnalysisResult:
        """分析 AIGC 日志"""
        aigc_files = work.aigc_log_files or []
        if not aigc_files:
            return AIGCAnalysisResult()
        return self.aigc_parser.analyze_logs(aigc_files, extract_dir)

    def analyze_document(self, work: Work, extract_dir: Path) -> dict:
        """分析说明文档，返回文档质量评估结果"""
        doc_result = {
            "has_document": False,
            "document_char_count": 0,
            "document_content": "",
            "document_quality": 0,
        }

        if work.readme_content:
            doc_result["has_document"] = True
            doc_result["document_content"] = work.readme_content
            doc_result["document_char_count"] = len(
                work.readme_content.replace("\n", "").replace(" ", "")
            )

        if not doc_result["has_document"]:
            document_files = work.document_files or []
            if document_files:
                analysis = document_parser.analyze_document(document_files, extract_dir)
                doc_result["has_document"] = (
                    analysis["has_readme"] or analysis["total_doc_count"] > 0
                )
                doc_result["document_content"] = analysis.get("readme_content", "")
                doc_result["document_char_count"] = analysis.get("total_char_count", 0)

        char_count = doc_result["document_char_count"]
        if char_count >= 500:
            doc_result["document_quality"] = 90
        elif char_count >= 200:
            doc_result["document_quality"] = 70
        elif char_count >= 100:
            doc_result["document_quality"] = 50
        elif char_count >= 50:
            doc_result["document_quality"] = 30
        elif char_count > 0:
            doc_result["document_quality"] = 10

        return doc_result

    def detect_features(self, work: Work, extract_dir: Path, code_result) -> dict:
        """功能检测：基于 code_analyzer 输出检测作品功能特性"""
        features = {
            "has_game_loop": False,
            "has_user_input": False,
            "has_scoring": False,
            "has_levels": False,
            "has_animation": False,
            "has_sound": False,
            "has_menu": False,
            "has_save": False,
            "detected_features": [],
            "completeness_score": 0,
        }

        game_mechanics = getattr(code_result, "game_mechanics", [])
        code_features = getattr(code_result, "features", [])

        if "game_loop" in game_mechanics:
            features["has_game_loop"] = True
            features["detected_features"].append("游戏循环")
        if "event_handling" in game_mechanics or "key_input" in game_mechanics:
            features["has_user_input"] = True
            features["detected_features"].append("用户输入")
        if "collision" in game_mechanics:
            features["has_scoring"] = True
            features["detected_features"].append("碰撞/计分")
        if "animation" in game_mechanics:
            features["has_animation"] = True
            features["detected_features"].append("动画效果")
        if "sound" in game_mechanics:
            features["has_sound"] = True
            features["detected_features"].append("音效")
        if "text_render" in game_mechanics:
            features["has_menu"] = True
            features["detected_features"].append("文字/菜单")
        if "random" in game_mechanics:
            features["detected_features"].append("随机性")
        if "math" in game_mechanics:
            features["detected_features"].append("数学运算")

        feature_count = len(features["detected_features"])
        if feature_count >= 5:
            features["completeness_score"] = 90
        elif feature_count >= 3:
            features["completeness_score"] = 70
        elif feature_count >= 2:
            features["completeness_score"] = 50
        elif feature_count >= 1:
            features["completeness_score"] = 30
        else:
            features["completeness_score"] = 10

        return features
