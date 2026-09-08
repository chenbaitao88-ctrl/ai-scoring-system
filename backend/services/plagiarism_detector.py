"""
抄袭检测服务
- 代码相似度检测（基于token序列匹配）
- 结构相似度检测（基于AST）
- 生成抄袭报告
"""
import os
import re
import ast
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter

from database import SessionLocal, Team, Work


@dataclass
class SimilarityResult:
    """相似度检测结果"""
    team1_id: int
    team1_name: str
    team1_code: str

    team2_id: int
    team2_name: str
    team2_code: str

    token_similarity: float = 0.0  # token序列相似度
    structure_similarity: float = 0.0  # AST结构相似度
    overall_similarity: float = 0.0  # 综合相似度

    matched_tokens: int = 0
    total_tokens: int = 0

    is_suspicious: bool = False  # 是否可疑
    suspicion_level: str = "none"  # none/low/medium/high


@dataclass
class PlagiarismReport:
    """抄袭检测报告"""
    total_pairs: int = 0
    suspicious_pairs: int = 0
    high_risk: int = 0
    medium_risk: int = 0
    low_risk: int = 0

    results: List[SimilarityResult] = field(default_factory=list)

    # 按队伍汇总
    team_suspicion_scores: Dict[int, float] = field(default_factory=dict)


class Tokenizer:
    """代码分词器"""

    # Python关键字和常见模式
    PYTHON_KEYWORDS = {
        'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
        'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
        'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
        'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return',
        'try', 'while', 'with', 'yield'
    }

    @staticmethod
    def tokenize_python(code: str) -> List[str]:
        """Python代码分词"""
        tokens = []

        # 移除注释和字符串字面量
        code = re.sub(r'#.*', '', code)
        code = re.sub(r'""".*?"""', 'STRING', code, flags=re.DOTALL)
        code = re.sub(r"'''.*?'''", 'STRING', code, flags=re.DOTALL)
        code = re.sub(r'"[^"]*"', 'STRING', code)
        code = re.sub(r"'[^']*'", 'STRING', code)

        # 分词
        patterns = [
            (r'\b\d+\b', 'NUMBER'),  # 数字
            (r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', 'IDENT'),  # 标识符
            (r'[+\-*/%=<>!&|^~]', 'OP'),  # 运算符
            (r'[(){}\[\],;:.]', 'PUNCT'),  # 标点
        ]

        pos = 0
        while pos < len(code):
            matched = False
            for pattern, token_type in patterns:
                match = re.match(pattern, code[pos:])
                if match:
                    token = match.group()
                    # 关键字保留，标识符归一化
                    if token_type == 'IDENT':
                        if token in Tokenizer.PYTHON_KEYWORDS:
                            tokens.append(token)
                        else:
                            tokens.append('VAR')  # 变量名归一化
                    else:
                        tokens.append(token_type)
                    pos += len(token)
                    matched = True
                    break

            if not matched:
                pos += 1

        return tokens

    @staticmethod
    def tokenize_scratch(blocks: List[dict]) -> List[str]:
        """Scratch积木块分词"""
        tokens = []
        for block in blocks:
            opcode = block.get('opcode', '')
            # 提取类别
            if '.' in opcode:
                category = opcode.split('.')[0]
                tokens.append(category)
            tokens.append(opcode)
        return tokens


class SimilarityCalculator:
    """相似度计算器"""

    @staticmethod
    def jaccard_similarity(set1: set, set2: set) -> float:
        """Jaccard相似度"""
        if not set1 and not set2:
            return 1.0
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def sequence_similarity(seq1: List[str], seq2: List[str]) -> Tuple[float, int, int]:
        """序列相似度（基于最长公共子序列）"""
        if not seq1 and not seq2:
            return 1.0, 0, 0
        if not seq1 or not seq2:
            return 0.0, 0, max(len(seq1), len(seq2))

        # 使用n-gram匹配
        n = 3  # 3-gram
        ngrams1 = set()
        ngrams2 = set()

        for i in range(len(seq1) - n + 1):
            ngrams1.add(tuple(seq1[i:i+n]))
        for i in range(len(seq2) - n + 1):
            ngrams2.add(tuple(seq2[i:i+n]))

        similarity = SimilarityCalculator.jaccard_similarity(ngrams1, ngrams2)
        matched = len(ngrams1 & ngrams2)
        total = max(len(ngrams1), len(ngrams2))

        return similarity, matched, total

    @staticmethod
    def ast_similarity(code1: str, code2: str) -> float:
        """AST结构相似度"""
        def get_ast_structure(code: str) -> List[str]:
            try:
                tree = ast.parse(code)
                structure = []
                for node in ast.walk(tree):
                    structure.append(type(node).__name__)
                return structure
            except:
                return []

        struct1 = get_ast_structure(code1)
        struct2 = get_ast_structure(code2)

        if not struct1 and not struct2:
            return 1.0
        if not struct1 or not struct2:
            return 0.0

        # 统计节点类型频率
        counter1 = Counter(struct1)
        counter2 = Counter(struct2)

        all_types = set(counter1.keys()) | set(counter2.keys())
        similarity_sum = 0
        for node_type in all_types:
            c1 = counter1.get(node_type, 0)
            c2 = counter2.get(node_type, 0)
            # 归一化
            max_count = max(c1, c2)
            if max_count > 0:
                similarity_sum += min(c1, c2) / max_count

        return similarity_sum / len(all_types) if all_types else 0.0


class PlagiarismDetector:
    """抄袭检测器"""

    # 相似度阈值
    HIGH_RISK_THRESHOLD = 0.8
    MEDIUM_RISK_THRESHOLD = 0.6
    LOW_RISK_THRESHOLD = 0.4

    def __init__(self, db=None):
        self.db = db or SessionLocal()
        self.tokenizer = Tokenizer()
        self.similarity_calc = SimilarityCalculator()

    def detect_pair(self, code1: str, code2: str, team1: dict, team2: dict) -> SimilarityResult:
        """检测两个代码的相似度"""
        result = SimilarityResult(
            team1_id=team1['id'],
            team1_name=team1['name'],
            team1_code=code1[:200] + '...' if len(code1) > 200 else code1,
            team2_id=team2['id'],
            team2_name=team2['name'],
            team2_code=code2[:200] + '...' if len(code2) > 200 else code2,
        )

        # 1. Token序列相似度
        tokens1 = self.tokenizer.tokenize_python(code1)
        tokens2 = self.tokenizer.tokenize_python(code2)

        token_sim, matched, total = self.similarity_calc.sequence_similarity(tokens1, tokens2)
        result.token_similarity = token_sim
        result.matched_tokens = matched
        result.total_tokens = total

        # 2. AST结构相似度
        struct_sim = self.similarity_calc.ast_similarity(code1, code2)
        result.structure_similarity = struct_sim

        # 3. 综合相似度（加权平均）
        result.overall_similarity = 0.6 * token_sim + 0.4 * struct_sim

        # 4. 判断可疑程度
        if result.overall_similarity >= self.HIGH_RISK_THRESHOLD:
            result.is_suspicious = True
            result.suspicion_level = "high"
        elif result.overall_similarity >= self.MEDIUM_RISK_THRESHOLD:
            result.is_suspicious = True
            result.suspicion_level = "medium"
        elif result.overall_similarity >= self.LOW_RISK_THRESHOLD:
            result.is_suspicious = True
            result.suspicion_level = "low"
        else:
            result.is_suspicious = False
            result.suspicion_level = "none"

        return result

    def detect_all(self, language: str = "Python") -> PlagiarismReport:
        """对所有作品进行抄袭检测"""
        report = PlagiarismReport()

        # 获取所有已解析的作品
        works = self.db.query(Work).filter(
            Work.is_parsed == True,
            Work.code_language == language
        ).all()

        if len(works) < 2:
            return report

        # 提取代码
        code_map = {}  # team_id -> code
        team_map = {}  # team_id -> {id, name}

        for work in works:
            team = work.team
            if not team:
                continue

            # 读取代码
            code = self._get_code(work)
            if not code:
                continue

            code_map[team.id] = code
            team_map[team.id] = {'id': team.id, 'name': team.team_name}

        # 两两比较
        team_ids = list(code_map.keys())
        report.total_pairs = len(team_ids) * (len(team_ids) - 1) // 2

        for i in range(len(team_ids)):
            for j in range(i + 1, len(team_ids)):
                id1, id2 = team_ids[i], team_ids[j]

                result = self.detect_pair(
                    code_map[id1], code_map[id2],
                    team_map[id1], team_map[id2]
                )

                report.results.append(result)

                if result.is_suspicious:
                    report.suspicious_pairs += 1

                    if result.suspicion_level == "high":
                        report.high_risk += 1
                    elif result.suspicion_level == "medium":
                        report.medium_risk += 1
                    else:
                        report.low_risk += 1

                    # 更新队伍可疑分数
                    score = result.overall_similarity
                    report.team_suspicion_scores[id1] = max(
                        report.team_suspicion_scores.get(id1, 0), score
                    )
                    report.team_suspicion_scores[id2] = max(
                        report.team_suspicion_scores.get(id2, 0), score
                    )

        # 更新数据库中的抄袭标记
        self._update_plagiarism_flags(report)

        return report

    def _get_code(self, work: Work) -> str:
        """获取作品的代码内容"""
        from config import WORKS_DIR

        extract_dir = WORKS_DIR / Path(work.original_filename or "").stem
        if not extract_dir.exists():
            return ""

        source_files = work.source_files or []
        all_code = []

        for sf in source_files:
            if sf.get('ext') == '.py':
                file_path = extract_dir / sf['path']
                if file_path.exists():
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            all_code.append(f.read())
                    except:
                        pass

        return '\n\n'.join(all_code)

    def _update_plagiarism_flags(self, report: PlagiarismReport):
        """更新数据库中的抄袭标记"""
        for team_id, score in report.team_suspicion_scores.items():
            work = self.db.query(Work).filter(Work.team_id == team_id).first()
            if work:
                work.plagiarism_flag = True
                work.flag_reason = f"抄袭嫌疑（相似度{score:.1%}）"

        self.db.commit()

    def get_suspicious_teams(self) -> List[dict]:
        """获取有抄袭嫌疑的队伍列表"""
        works = self.db.query(Work).filter(Work.plagiarism_flag == True).all()

        results = []
        for work in works:
            team = work.team
            if team:
                results.append({
                    'team_id': team.id,
                    'short_code': team.short_code,
                    'team_name': team.team_name,
                    'school': team.school,
                    'flag_reason': work.flag_reason,
                })

        return results
