"""
代码质量分析器
- Python代码：AST解析、复杂度、规范检查
- Scratch项目：积木块统计、逻辑结构分析
"""
import os
import ast
import re
import json
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class CodeAnalysisResult:
    """代码分析结果"""
    language: str = "Unknown"
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    comment_rate: float = 0.0

    # Python特有
    function_count: int = 0
    class_count: int = 0
    import_count: int = 0
    cyclomatic_complexity: int = 1
    max_nesting_depth: int = 0

    # Scratch特有
    block_count: int = 0
    sprite_count: int = 0
    variable_count: int = 0
    custom_block_count: int = 0

    # 功能特征
    features: List[str] = field(default_factory=list)
    game_mechanics: List[str] = field(default_factory=list)

    # 质量评分
    quality_score: float = 0.0
    creativity_score: float = 0.0
    complexity_score: float = 0.0

    # 问题
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class PythonAnalyzer:
    """Python代码分析器"""

    # 游戏开发特征模式
    GAME_PATTERNS = {
        "game_loop": r"while\s+True|while\s+running",
        "event_handling": r"pygame\.event\.get|for\s+event\s+in",
        "key_input": r"pygame\.key\.get_pressed|pygame\.K_",
        "mouse_input": r"pygame\.mouse",
        "sprite": r"pygame\.sprite|Sprite|class\s+\w*\(Sprite",
        "collision": r"collide|collision|pygame\.sprite\.collide",
        "sound": r"pygame\.mixer|\.play\(\)|play_sound",
        "animation": r"pygame\.time\.Clock|clock\.tick|frame",
        "drawing": r"pygame\.draw\.|screen\.blit",
        "text_render": r"pygame\.font|render\(",
        "image_load": r"pygame\.image\.load|\.convert\(\)",
        "random": r"random\.|randint|choice|shuffle",
        "math": r"math\.|sin\(|cos\(|sqrt\(|abs\(",
        "data_structure": r"\[\]|dict\(|set\(|list\(",
    }

    def analyze(self, code: str, filename: str = "main.py") -> CodeAnalysisResult:
        """分析Python代码"""
        result = CodeAnalysisResult(language="Python")

        # 1. 基础统计
        lines = code.split('\n')
        result.total_lines = len(lines)

        for line in lines:
            stripped = line.strip()
            if not stripped:
                result.blank_lines += 1
            elif stripped.startswith('#'):
                result.comment_lines += 1
            else:
                result.code_lines += 1

        result.comment_rate = (result.comment_lines / result.code_lines * 100) if result.code_lines > 0 else 0

        # 2. AST分析
        try:
            tree = ast.parse(code)
            self._analyze_ast(tree, result)
        except SyntaxError as e:
            result.issues.append(f"语法错误: {e}")
            return result

        # 3. 特征检测
        self._detect_features(code, result)

        # 4. 计算评分
        self._calculate_scores(result)

        return result

    def _analyze_ast(self, tree: ast.AST, result: CodeAnalysisResult):
        """AST结构分析"""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                result.function_count += 1
                # 计算函数复杂度
                complexity = self._calc_function_complexity(node)
                result.cyclomatic_complexity += complexity - 1

            elif isinstance(node, ast.ClassDef):
                result.class_count += 1

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                result.import_count += 1

            elif isinstance(node, (ast.If, ast.For, ast.While)):
                result.max_nesting_depth = max(result.max_nesting_depth, self._get_nesting_depth(node))

    def _calc_function_complexity(self, node: ast.FunctionDef) -> int:
        """计算函数圈复杂度"""
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
        return complexity

    def _get_nesting_depth(self, node: ast.AST, depth: int = 0) -> int:
        """获取嵌套深度"""
        max_depth = depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.For, ast.While)):
                max_depth = max(max_depth, self._get_nesting_depth(child, depth + 1))
        return max_depth

    def _detect_features(self, code: str, result: CodeAnalysisResult):
        """检测代码特征"""
        for feature, pattern in self.GAME_PATTERNS.items():
            if re.search(pattern, code):
                result.game_mechanics.append(feature)

        # 检测 pygame 使用
        if 'pygame' in code:
            result.features.append("pygame游戏")

        # 检测面向对象
        if result.class_count > 0:
            result.features.append("面向对象")

        # 检测模块化
        if result.function_count >= 3:
            result.features.append("函数模块化")

        # 检测异常处理
        if 'try:' in code or 'except' in code:
            result.features.append("异常处理")

        # 检测文件操作
        if 'open(' in code or 'with open' in code:
            result.features.append("文件操作")

    def _calculate_scores(self, result: CodeAnalysisResult):
        """计算质量评分"""
        # 基础质量分（注释率 + 模块化）
        base_score = 50

        # 注释率加分（理想10-30%）
        if 10 <= result.comment_rate <= 30:
            base_score += 15
        elif result.comment_rate > 0:
            base_score += min(10, result.comment_rate / 3)

        # 模块化加分
        if result.function_count >= 5:
            base_score += 10
        elif result.function_count >= 2:
            base_score += 5

        # 面向对象加分
        if result.class_count > 0:
            base_score += 10

        # 复杂度评分
        if result.cyclomatic_complexity <= 5:
            base_score += 10  # 简洁
        elif result.cyclomatic_complexity <= 15:
            base_score += 5   # 适中
        else:
            result.warnings.append(f"圈复杂度过高({result.cyclomatic_complexity})，建议重构")

        result.quality_score = min(100, base_score)

        # 创意评分（基于游戏机制数量）
        creativity = len(result.game_mechanics) * 10
        if 'pygame游戏' in result.features:
            creativity += 20
        result.creativity_score = min(100, creativity)

        # 复杂度评分
        result.complexity_score = min(100, result.total_lines / 2 + result.function_count * 5)


class ScratchAnalyzer:
    """Scratch项目分析器"""

    def analyze(self, file_path: Path) -> CodeAnalysisResult:
        """分析Scratch项目（.sb3文件）"""
        result = CodeAnalysisResult(language="Scratch")

        try:
            # sb3是ZIP格式，包含project.json
            with zipfile.ZipFile(file_path, 'r') as zf:
                if 'project.json' not in zf.namelist():
                    result.issues.append("无效的Scratch项目文件")
                    return result

                project = json.loads(zf.read('project.json').decode('utf-8'))
                self._analyze_project(project, result)
        except Exception as e:
            result.issues.append(f"解析错误: {str(e)}")

        return result

    def _analyze_project(self, project: dict, result: CodeAnalysisResult):
        """分析Scratch项目结构"""
        targets = project.get('targets', [])

        for target in targets:
            if target.get('isStage'):
                continue

            result.sprite_count += 1

            # 统计积木块
            blocks = target.get('blocks', {})
            for block_id, block in blocks.items():
                if isinstance(block, dict):
                    opcode = block.get('opcode', '')
                    result.block_count += 1

                    # 检测自定义积木
                    if 'procedures_definition' in opcode:
                        result.custom_block_count += 1

                    # 检测变量
                    if 'data_setvariableto' in opcode or 'data_changevariableby' in opcode:
                        result.variable_count += 1

        # 检测功能特征
        self._detect_scratch_features(project, result)

        # 计算评分
        self._calculate_scores(result)

    def _detect_scratch_features(self, project: dict, result: CodeAnalysisResult):
        """检测Scratch项目特征"""
        blocks_text = json.dumps(project)

        if 'motion_goto' in blocks_text or 'motion_glideto' in blocks_text:
            result.game_mechanics.append("角色移动")

        if 'sensing_touchingobject' in blocks_text or 'sensing_touchingcolor' in blocks_text:
            result.game_mechanics.append("碰撞检测")

        if 'control_if' in blocks_text:
            result.features.append("条件判断")

        if 'control_repeat_until' in blocks_text or 'control_forever' in blocks_text:
            result.features.append("循环结构")

        if 'event_whenkeypressed' in blocks_text:
            result.features.append("键盘交互")

        if 'sound_play' in blocks_text:
            result.features.append("音效")

        if 'looks_say' in blocks_text or 'looks_think' in blocks_text:
            result.features.append("对话显示")

        if result.custom_block_count > 0:
            result.features.append("自定义积木")

    def _calculate_scores(self, result: CodeAnalysisResult):
        """计算Scratch项目评分"""
        # 基础分
        base_score = 50

        # 积木数量评分
        if result.block_count >= 50:
            base_score += 20
        elif result.block_count >= 30:
            base_score += 10

        # 角色数量
        if result.sprite_count >= 3:
            base_score += 10

        # 自定义积木
        if result.custom_block_count > 0:
            base_score += 10

        # 功能多样性
        base_score += min(10, len(result.features) * 2)

        result.quality_score = min(100, base_score)
        result.creativity_score = min(100, len(result.game_mechanics) * 15 + 20)
        result.complexity_score = min(100, result.block_count / 2)


class CodeAnalyzer:
    """代码分析器入口"""

    def __init__(self):
        self.python_analyzer = PythonAnalyzer()
        self.scratch_analyzer = ScratchAnalyzer()

    def analyze_file(self, file_path: Path) -> CodeAnalysisResult:
        """分析单个代码文件"""
        ext = file_path.suffix.lower()

        if ext == '.py':
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    code = f.read()
                return self.python_analyzer.analyze(code, file_path.name)
            except Exception as e:
                result = CodeAnalysisResult(language="Python")
                result.issues.append(f"读取文件失败: {e}")
                return result

        elif ext in ('.sb3', '.sb2'):
            return self.scratch_analyzer.analyze(file_path)

        else:
            result = CodeAnalysisResult(language="Unknown")
            result.warnings.append(f"不支持的文件类型: {ext}")
            return result

    def analyze_project(self, source_files: List[dict], extract_dir: Path) -> CodeAnalysisResult:
        """分析整个项目（多个文件）"""
        if not source_files:
            return CodeAnalysisResult()

        # 找主要代码文件
        main_result = None
        total_result = CodeAnalysisResult()

        for sf in source_files:
            file_path = extract_dir / sf['path']
            if not file_path.exists():
                continue

            result = self.analyze_file(file_path)

            # 合并结果
            total_result.total_lines += result.total_lines
            total_result.code_lines += result.code_lines
            total_result.function_count += result.function_count
            total_result.class_count += result.class_count
            total_result.features.extend(result.features)
            total_result.game_mechanics.extend(result.game_mechanics)

            # 选择主要语言
            if main_result is None or result.total_lines > main_result.total_lines:
                main_result = result

        if main_result:
            total_result.language = main_result.language
            total_result.comment_rate = main_result.comment_rate
            total_result.cyclomatic_complexity = main_result.cyclomatic_complexity
            total_result.quality_score = main_result.quality_score
            total_result.creativity_score = main_result.creativity_score
            total_result.complexity_score = main_result.complexity_score

        # 去重
        total_result.features = list(set(total_result.features))
        total_result.game_mechanics = list(set(total_result.game_mechanics))

        return total_result
