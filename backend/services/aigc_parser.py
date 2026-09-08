"""
AIGC日志解析器
- 解析ChatGPT/Claude等AI工具的交互记录
- 统计交互次数、问题类型、工具使用情况
- P1-1 新增: OCR 分支，支持 .png/.jpg/.pdf 的文本提取
"""
import re
import json
import html
import plistlib
import logging
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from feature_flags import is_enabled

logger = logging.getLogger(__name__)


@dataclass
class AIGCInteraction:
    """单次AIGC交互"""
    role: str  # user / assistant
    content: str
    timestamp: Optional[datetime] = None
    question_type: str = ""  # code_generation / debugging / concept / optimization / other


@dataclass
class AIGCAnalysisResult:
    """AIGC日志分析结果"""
    tool_name: str = "Unknown"
    tool_version: str = ""

    total_interactions: int = 0
    user_messages: int = 0
    assistant_messages: int = 0

    # 问题类型统计
    code_generation_count: int = 0
    debugging_count: int = 0
    concept_count: int = 0
    optimization_count: int = 0
    other_count: int = 0

    # 工具使用
    tools_used: List[str] = field(default_factory=list)

    # 交互质量
    avg_question_length: float = 0.0
    avg_response_length: float = 0.0
    iteration_depth: int = 0  # 同一问题的迭代次数

    # 时间跨度
    first_interaction: Optional[datetime] = None
    last_interaction: Optional[datetime] = None
    duration_minutes: float = 0.0

    # 评分
    process_score: float = 0.0  # 过程完整性
    ai_literacy_score: float = 0.0  # AI素养

    # 特征
    features: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # 原始交互记录
    interactions: List[AIGCInteraction] = field(default_factory=list)


class AIGCParser:
    """AIGC日志解析器"""

    # P1-1: OCR 支持的文件扩展名
    _OCR_EXTS = {'.png', '.jpg', '.jpeg', '.pdf'}

    # 问题类型识别模式
    QUESTION_PATTERNS = {
        "code_generation": [
            r"帮我写|写一个|生成|创建|实现|编一个|做个",
            r"代码|程序|游戏|脚本",
            r"怎么实现|如何实现",
        ],
        "debugging": [
            r"报错|错误|bug|问题|不工作|运行不了",
            r"为什么|怎么回事|怎么解决",
            r"修复|改正|调试",
        ],
        "concept": [
            r"什么是|解释|讲解|介绍",
            r"原理|概念|理解|学习",
            r"怎么用|如何使用|用法",
        ],
        "optimization": [
            r"优化|改进|提升|更好",
            r"性能|效率|速度",
            r"美化|完善|调整",
        ],
    }

    # AI工具识别
    TOOL_SIGNATURES = {
        "ChatGPT": ["chatgpt", "gpt-4", "gpt-3.5", "openai"],
        "Claude": ["claude", "anthropic"],
        "Copilot": ["copilot", "github"],
        "DeepSeek": ["deepseek"],
        "Kimi": ["kimi", "moonshot"],
        "豆包": ["豆包", "doubao"],
        "文心一言": ["文心", "ernie", "百度"],
    }

    def __init__(self):
        """P1-1: 延迟初始化 OCR 引擎，避免未启用时加载开销"""
        self._ocr_engine = None
        if is_enabled("AIGC_OCR"):
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._ocr_engine = RapidOCR()
                logger.info("AIGC OCR 引擎初始化成功")
            except Exception as e:
                logger.warning(f"AIGC OCR 引擎初始化失败: {e}，图片/PDF 将跳过")

    def parse_file(self, file_path: Path) -> AIGCAnalysisResult:
        """解析AIGC日志文件"""
        ext = file_path.suffix.lower()

        if ext == '.html':
            return self._parse_html(file_path)
        elif ext == '.json':
            return self._parse_json(file_path)
        elif ext in ('.txt', '.md'):
            return self._parse_text(file_path)
        elif ext == '.textclipping':
            return self._parse_textClipping(file_path)
        elif ext in self._OCR_EXTS:
            # P1-1: OCR 分支
            if not is_enabled("AIGC_OCR"):
                result = AIGCAnalysisResult()
                result.warnings.append(f"OCR功能已关闭，跳过: {file_path.name}")
                return result
            return self._parse_image_or_pdf(file_path)
        else:
            result = AIGCAnalysisResult()
            result.warnings.append(f"不支持的日志格式: {ext}")
            return result

    def _parse_html(self, file_path: Path) -> AIGCAnalysisResult:
        """解析HTML格式的聊天记录（如ChatGPT导出）"""
        result = AIGCAnalysisResult()

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # 提取工具名称
            result.tool_name = self._detect_tool(content)

            # 解析对话内容
            interactions = self._extract_html_interactions(content)
            result.interactions = interactions

            # 统计分析
            self._analyze_interactions(result)

        except Exception as e:
            result.warnings.append(f"解析HTML失败: {str(e)}")

        return result

    def _parse_json(self, file_path: Path) -> AIGCAnalysisResult:
        """解析JSON格式的日志"""
        result = AIGCAnalysisResult()

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)

            # 适配不同格式
            if isinstance(data, list):
                # 直接是消息列表
                for msg in data:
                    role = msg.get('role', msg.get('sender', 'unknown'))
                    content = msg.get('content', msg.get('text', ''))
                    result.interactions.append(AIGCInteraction(
                        role=role,
                        content=content,
                        timestamp=self._parse_timestamp(msg.get('timestamp', msg.get('time')))
                    ))
            elif isinstance(data, dict):
                # 包含元数据
                result.tool_name = data.get('tool', data.get('model', 'Unknown'))
                messages = data.get('messages', data.get('conversations', []))
                for msg in messages:
                    result.interactions.append(AIGCInteraction(
                        role=msg.get('role', 'user'),
                        content=msg.get('content', ''),
                        timestamp=self._parse_timestamp(msg.get('timestamp'))
                    ))

            self._analyze_interactions(result)

        except Exception as e:
            result.warnings.append(f"解析JSON失败: {str(e)}")

        return result

    def _parse_text(self, file_path: Path) -> AIGCAnalysisResult:
        """解析纯文本格式的日志"""
        result = AIGCAnalysisResult()

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            result.tool_name = self._detect_tool(content)

            # 按行解析（简单格式）
            lines = content.split('\n')
            current_role = None
            current_content = []

            for line in lines:
                # 检测角色标记
                if re.match(r'^(用户|User|我|提问)[:：]', line, re.IGNORECASE):
                    if current_role and current_content:
                        result.interactions.append(AIGCInteraction(
                            role=current_role,
                            content='\n'.join(current_content)
                        ))
                    current_role = 'user'
                    current_content = [line.split(':', 1)[-1].strip() if ':' in line else line]
                elif re.match(r'^(AI|ChatGPT|Claude|助手|回答|GPT)[:：]', line, re.IGNORECASE):
                    if current_role and current_content:
                        result.interactions.append(AIGCInteraction(
                            role=current_role,
                            content='\n'.join(current_content)
                        ))
                    current_role = 'assistant'
                    current_content = [line.split(':', 1)[-1].strip() if ':' in line else line]
                elif current_role:
                    current_content.append(line)

            # 保存最后一条
            if current_role and current_content:
                result.interactions.append(AIGCInteraction(
                    role=current_role,
                    content='\n'.join(current_content)
                ))

            self._analyze_interactions(result)

        except Exception as e:
            result.warnings.append(f"解析文本失败: {str(e)}")

        return result

    def _parse_textClipping(self, file_path: Path) -> AIGCAnalysisResult:
        """解析Apple binary plist格式的剪贴板文件（如豆包AI助手导出）"""
        result = AIGCAnalysisResult()

        try:
            with open(file_path, 'rb') as f:
                plist = plistlib.load(f)

            # Apple textClipping 文件结构通常是 {'OSType-Data': ..., 'UTI-Data': {...}}
            # UTI-Data 包含多种格式：public.utf8-plain-text, public.rtf 等
            text_content = ""

            if isinstance(plist, str):
                text_content = plist

            elif isinstance(plist, dict):
                # 优先从 UTI-Data 中提取文本内容
                if 'UTI-Data' in plist and isinstance(plist['UTI-Data'], dict):
                    uti_data = plist['UTI-Data']

                    # 尝试多种格式，优先 utf8-plain-text
                    for key in ['public.utf8-plain-text', 'public.utf16-plain-text',
                                'com.apple.traditional-mac-plain-text', 'string', 'NSString', 'text', 'content']:
                        if key in uti_data:
                            value = uti_data[key]
                            if isinstance(value, str) and len(value) > 2:
                                text_content = value
                                break
                            elif isinstance(value, bytes) and len(value) > 2:
                                try:
                                    # 尝试解码
                                    text_content = value.decode('utf-8', errors='ignore')
                                    if len(text_content) > 2:
                                        break
                                except:
                                    pass

                    # 如果从纯文本格式没获取到，尝试从 RTF 提取
                    if not text_content and 'public.rtf' in uti_data:
                        rtf_content = uti_data['public.rtf']
                        if isinstance(rtf_content, str):
                            text_content = self._extract_text_from_rtf(rtf_content)
                        elif isinstance(rtf_content, bytes):
                            try:
                                text_content = self._extract_text_from_rtf(rtf_content.decode('utf-8', errors='ignore'))
                            except:
                                pass

                # 如果 UTI-Data 中没有，尝试直接从顶层字典提取
                if not text_content:
                    for key in ['string', 'NSString', 'text', 'content', 'public.utf8-plain-text']:
                        if key in plist:
                            text_content = str(plist[key])
                            break

                    if not text_content:
                        # 尝试将所有值转为字符串
                        for v in plist.values():
                            if isinstance(v, str) and len(v) > 10:
                                text_content += v + "\n"
                            elif isinstance(v, dict):
                                # 递归查找
                                for sub_v in v.values():
                                    if isinstance(sub_v, str) and len(sub_v) > 10:
                                        text_content += sub_v + "\n"

            elif isinstance(plist, list):
                for item in plist:
                    if isinstance(item, str):
                        text_content += item + "\n"

            if not text_content or len(text_content.strip()) < 3:
                result.warnings.append("无法从plist中提取文本内容，该文件可能只包含链接或文件名，不包含完整对话记录")
                return result

            # 检查提取的内容是否只是文件名或链接
            if len(text_content.strip()) < 50 and ('http' in text_content or '.com' in text_content):
                result.warnings.append(f"警告：提取的内容可能只是链接或文件名，不包含完整对话记录：{text_content[:100]}")

            # 将提取的文本按文本格式解析
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as tmp:
                tmp.write(text_content)
                tmp_path = Path(tmp.name)

            try:
                text_result = self._parse_text(tmp_path)
                # 合并结果
                result = text_result
                result.tool_name = "豆包"
            finally:
                tmp_path.unlink(missing_ok=True)

        except Exception as e:
            result.warnings.append(f"解析textClipping失败: {str(e)}")

        return result

    # ─────────────────────────────────────────────
    # P1-1: OCR 分支 — 图片/PDF 文本提取
    # ─────────────────────────────────────────────
    def _parse_image_or_pdf(self, file_path: Path) -> AIGCAnalysisResult:
        """通过 OCR 从图片或 PDF 中提取文本，作为伪 AIGC 交互记录。"""
        result = AIGCAnalysisResult()

        # 1. 检查 OCR 引擎是否可用
        if self._ocr_engine is None:
            result.warnings.append(f"OCR 引擎不可用，跳过: {file_path.name}")
            return result

        # 2. 准备待识别的图片路径列表
        image_paths: List[Path] = []
        ext = file_path.suffix.lower()

        try:
            if ext == '.pdf':
                # PyMuPDF 逐页转为图片
                import fitz
                doc = fitz.open(file_path)
                for page_num in range(len(doc)):
                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(dpi=200)
                    tmp_img = file_path.parent / f"__ocr_tmp_{file_path.stem}_p{page_num}.png"
                    pix.save(str(tmp_img))
                    image_paths.append(tmp_img)
                doc.close()
            else:
                # 图片直接识别
                image_paths.append(file_path)

            # 3. 逐张 OCR 识别
            all_texts: List[str] = []
            for img_path in image_paths:
                try:
                    ocr_result, _ = self._ocr_engine(str(img_path))
                    if ocr_result:
                        page_text = "\n".join(
                            line[1] for line in ocr_result
                            if isinstance(line, (list, tuple)) and len(line) >= 2 and line[1] and line[1].strip()
                        )
                        if page_text.strip():
                            all_texts.append(page_text)
                except Exception as e:
                    logger.warning(f"OCR 识别失败 {img_path.name}: {e}")
                finally:
                    # 清理临时图片（仅 PDF 产生的临时文件）
                    if ext == '.pdf' and img_path.exists():
                        img_path.unlink(missing_ok=True)

            # 4. 合并并截断
            full_text = "\n".join(all_texts).strip()
            if not full_text:
                result.warnings.append(f"OCR 未提取到有效文字: {file_path.name}")
                return result

            # 截断 + 日志
            if len(full_text) > 5000:
                logger.info(f"OCR extracted {len(full_text)} chars from {file_path.name} (truncated to 5000)")
                full_text = full_text[:5000]
            else:
                logger.info(f"OCR extracted {len(full_text)} chars from {file_path.name}")

            # 5. 构造伪交互记录（整段作为一条 user message）
            result.interactions.append(AIGCInteraction(
                role="user",
                content=full_text,
                question_type="other"
            ))
            result.tool_name = "OCR_Extracted"
            self._analyze_interactions(result)

        except Exception as e:
            result.warnings.append(f"OCR 处理失败 [{file_path.name}]: {str(e)}")
            logger.warning(f"OCR exception on {file_path.name}: {e}")

        return result

    def _extract_text_from_rtf(self, rtf_content: str) -> str:
        """从RTF格式中提取纯文本"""
        try:
            # 简单移除RTF标签
            import re
            # 移除RTF控制词
            text = re.sub(r'\\[a-z]+\d*\s?', '', rtf_content)
            # 移除花括号
            text = re.sub(r'[{}]', '', text)
            # 移除多余空白
            text = re.sub(r'\s+', ' ', text).strip()
            return text
        except:
            return ""

    def _extract_html_interactions(self, html_content: str) -> List[AIGCInteraction]:
        """从HTML提取交互记录"""
        interactions = []

        # 清理HTML标签，提取纯文本
        text = html.unescape(re.sub(r'<[^>]+>', ' ', html_content))
        text = re.sub(r'\s+', ' ', text).strip()

        # 查找对话模式
        # 模式1: 用户: xxx AI: xxx
        pattern1 = r'(用户|User|我)[:：](.*?)((AI|ChatGPT|Claude|助手|GPT)[:：](.*?))?'
        matches = re.findall(pattern1, text, re.IGNORECASE | re.DOTALL)

        for match in matches:
            user_content = match[1].strip()
            if user_content:
                interactions.append(AIGCInteraction(role='user', content=user_content))

            assistant_content = match[4].strip() if len(match) > 4 else ''
            if assistant_content:
                interactions.append(AIGCInteraction(role='assistant', content=assistant_content))

        # 如果没匹配到，尝试更宽松的模式
        if not interactions:
            # 查找所有可能的对话段落
            parts = re.split(r'(用户|User|AI|ChatGPT|Claude|助手)[:：]', text, flags=re.IGNORECASE)
            for i in range(1, len(parts), 2):
                if i + 1 < len(parts):
                    role = parts[i].lower()
                    content = parts[i + 1].strip()
                    if 'user' in role or '用户' in role or '我' in role:
                        interactions.append(AIGCInteraction(role='user', content=content))
                    else:
                        interactions.append(AIGCInteraction(role='assistant', content=content))

        return interactions

    def _detect_tool(self, content: str) -> str:
        """检测使用的AI工具"""
        content_lower = content.lower()
        for tool, signatures in self.TOOL_SIGNATURES.items():
            for sig in signatures:
                if sig in content_lower:
                    return tool
        return "Unknown"

    def _parse_timestamp(self, ts: any) -> Optional[datetime]:
        """解析时间戳"""
        if not ts:
            return None
        try:
            if isinstance(ts, datetime):
                return ts
            elif isinstance(ts, str):
                # 尝试多种格式
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y/%m/%d %H:%M']:
                    try:
                        return datetime.strptime(ts, fmt)
                    except:
                        continue
        except:
            pass
        return None

    def _classify_question(self, content: str) -> str:
        """分类问题类型"""
        content_lower = content.lower()

        for q_type, patterns in self.QUESTION_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, content_lower):
                    return q_type

        return "other"

    def _analyze_interactions(self, result: AIGCAnalysisResult):
        """分析交互记录"""
        if not result.interactions:
            return

        result.total_interactions = len(result.interactions)

        user_msgs = [i for i in result.interactions if i.role == 'user']
        assistant_msgs = [i for i in result.interactions if i.role == 'assistant']

        result.user_messages = len(user_msgs)
        result.assistant_messages = len(assistant_msgs)

        # 统计问题类型
        for msg in user_msgs:
            msg.question_type = self._classify_question(msg.content)
            if msg.question_type == 'code_generation':
                result.code_generation_count += 1
            elif msg.question_type == 'debugging':
                result.debugging_count += 1
            elif msg.question_type == 'concept':
                result.concept_count += 1
            elif msg.question_type == 'optimization':
                result.optimization_count += 1
            else:
                result.other_count += 1

        # 计算平均长度
        if user_msgs:
            result.avg_question_length = sum(len(i.content) for i in user_msgs) / len(user_msgs)
        if assistant_msgs:
            result.avg_response_length = sum(len(i.content) for i in assistant_msgs) / len(assistant_msgs)

        # 时间跨度
        timestamps = [i.timestamp for i in result.interactions if i.timestamp]
        if timestamps:
            result.first_interaction = min(timestamps)
            result.last_interaction = max(timestamps)
            if result.first_interaction and result.last_interaction:
                delta = result.last_interaction - result.first_interaction
                result.duration_minutes = delta.total_seconds() / 60

        # 检测迭代深度（同一主题的连续追问）
        result.iteration_depth = self._detect_iteration_depth(user_msgs)

        # 检测特征
        self._detect_features(result)

        # 计算评分
        self._calculate_scores(result)

    def _detect_iteration_depth(self, user_msgs: List[AIGCInteraction]) -> int:
        """检测迭代深度"""
        max_depth = 1
        current_topic = None
        current_depth = 0

        for msg in user_msgs:
            # 简单判断：如果问题包含"还是"、"不对"、"再"等，可能是迭代
            if any(kw in msg.content.lower() for kw in ['还是', '不对', '再', '修改', '调整', '优化']):
                current_depth += 1
                max_depth = max(max_depth, current_depth)
            else:
                current_depth = 1

        return max_depth

    def _detect_features(self, result: AIGCAnalysisResult):
        """检测AI使用特征"""
        if result.user_messages >= 5:
            result.features.append("多次交互")

        if result.iteration_depth >= 3:
            result.features.append("深度迭代")

        if result.debugging_count > 0:
            result.features.append("调试问题")

        if result.concept_count > 0:
            result.features.append("概念学习")

        if result.optimization_count > 0:
            result.features.append("优化改进")

        if result.code_generation_count > 0 and result.debugging_count > 0:
            result.features.append("生成+调试组合")

        if result.duration_minutes > 30:
            result.features.append("长时间交互")

        # 检测工具多样性
        if result.tool_name != "Unknown":
            result.tools_used.append(result.tool_name)

    def _calculate_scores(self, result: AIGCAnalysisResult):
        """计算评分"""
        # 过程完整性评分（基于交互数量和类型多样性）
        process_base = 30

        # 交互次数加分
        if result.user_messages >= 6:
            process_base += 20
        elif result.user_messages >= 3:
            process_base += 10

        # 问题类型多样性
        type_count = sum([
            result.code_generation_count > 0,
            result.debugging_count > 0,
            result.concept_count > 0,
            result.optimization_count > 0,
        ])
        process_base += type_count * 10

        # 迭代深度加分
        if result.iteration_depth >= 3:
            process_base += 10

        result.process_score = min(100, process_base)

        # AI素养评分（基于问题质量和工具策略）
        literacy_base = 20

        # 问题长度（表明思考深度）
        if result.avg_question_length > 100:
            literacy_base += 15
        elif result.avg_question_length > 50:
            literacy_base += 10

        # 类型组合（表明策略性使用）
        if result.code_generation_count > 0 and result.debugging_count > 0:
            literacy_base += 15  # 生成后调试，合理流程

        if result.concept_count > 0:
            literacy_base += 10  # 有学习过程

        result.ai_literacy_score = min(100, literacy_base)

    def analyze_logs(self, log_files: List[dict], extract_dir: Path) -> AIGCAnalysisResult:
        """分析多个AIGC日志文件"""
        if not log_files:
            return AIGCAnalysisResult()

        # 合并所有日志的分析结果
        combined = AIGCAnalysisResult()

        for lf in log_files:
            file_path = extract_dir / lf['path']
            if not file_path.exists():
                continue

            result = self.parse_file(file_path)

            # 合并统计
            combined.total_interactions += result.total_interactions
            combined.user_messages += result.user_messages
            combined.code_generation_count += result.code_generation_count
            combined.debugging_count += result.debugging_count
            combined.concept_count += result.concept_count
            combined.optimization_count += result.optimization_count
            combined.features.extend(result.features)
            combined.tools_used.extend(result.tools_used)
            combined.warnings.extend(result.warnings)

            # 更新工具名称
            if result.tool_name != "Unknown" and combined.tool_name == "Unknown":
                combined.tool_name = result.tool_name

        # 去重
        combined.features = list(set(combined.features))
        combined.tools_used = list(set(combined.tools_used))

        # 重新计算评分
        self._calculate_scores(combined)

        return combined
