"""
文档解析服务
- 支持 .doc, .docx, .txt, .md 文件内容提取
- 用于读取"安装与使用说明文档"等文字材料
"""
import os
from pathlib import Path
from typing import List, Optional


class DocumentParser:
    """文档内容解析器"""

    def extract_text(self, file_path: Path) -> str:
        """从文档中提取纯文本内容

        支持格式：
        - .txt: 纯文本，直接读取
        - .md: Markdown，转纯文本（去除标记）
        - .doc: 二进制Word（通过python-docx）
        - .docx: Office Open XML（通过python-docx）
        """
        ext = file_path.suffix.lower()

        if ext == ".txt":
            return self._extract_txt(file_path)
        elif ext == ".md":
            return self._extract_md(file_path)
        elif ext == ".doc":
            return self._extract_doc(file_path)
        elif ext == ".docx":
            return self._extract_docx(file_path)
        else:
            return ""

    def _extract_txt(self, file_path: Path) -> str:
        """提取TXT文本"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            try:
                with open(file_path, "r", encoding="gbk", errors="ignore") as f:
                    return f.read()
            except Exception:
                return ""

    def _extract_md(self, file_path: Path) -> str:
        """提取Markdown文本（去除格式标记）"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # 去除常见Markdown标记
            import re
            # 去除代码块
            content = re.sub(r'```[\s\S]*?```', '', content)
            # 去除行内代码
            content = re.sub(r'`[^`]+`', '', content)
            # 去除图片
            content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
            # 去除链接
            content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
            # 去除标题标记
            content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
            # 去除加粗斜体
            content = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', content)
            content = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', content)
            # 去除水平线
            content = re.sub(r'^[-*_]{3,}$', '', content, flags=re.MULTILINE)
            # 去除列表标记
            content = re.sub(r'^[\s]*[-*+]\s+', '', content, flags=re.MULTILINE)
            content = re.sub(r'^[\s]*\d+\.\s+', '', content, flags=re.MULTILINE)
            return content.strip()
        except Exception:
            return ""

    def _extract_doc(self, file_path: Path) -> str:
        """提取旧版 .doc 二进制Word文档"""
        try:
            from docx import Document as DocxDocument
            # python-docx 只支持 .docx，不支持 .doc
            # 尝试用文本方式读取（部分 .doc 可以）
            with open(file_path, "rb") as f:
                content = f.read()
            # 尝试解码（部分 .doc 是文本格式）
            try:
                text = content.decode("utf-8", errors="ignore")
                # 检查是否包含可读文本
                if any(c.isalpha() for c in text):
                    # 过滤非ASCII字符过多的部分
                    import re
                    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', ' ', text)
                    return text.strip()
            except Exception:
                pass
            return "[.doc文件需要转换为.docx格式后上传]"
        except Exception:
            return ""

    def _extract_docx(self, file_path: Path) -> str:
        """提取 .docx Office Open XML文档"""
        try:
            from docx import Document
            doc = Document(str(file_path))
            paragraphs = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    paragraphs.append(text)
            return "\n".join(paragraphs)
        except Exception:
            return ""

    def analyze_document(self, doc_files: List[dict], extract_dir: Path) -> dict:
        """分析文档文件集合

        Args:
            doc_files: 文件信息列表，每项包含 name, path, ext
            extract_dir: 解压目录根路径
        """
        result = {
            "has_readme": False,
            "readme_content": "",
            "doc_files": [],
            "total_doc_count": 0,
            "total_char_count": 0,
        }

        if not doc_files:
            return result

        for doc_info in doc_files:
            # 兼容旧版document_files（可能没有path字段）
            doc_rel_path = doc_info.get("path", doc_info.get("name", ""))
            if not doc_rel_path:
                continue
            doc_path = extract_dir / doc_rel_path
            if not doc_path.exists():
                continue

            text = self.extract_text(doc_path)
            char_count = len(text.replace("\n", "").replace(" ", ""))

            file_info = {
                "name": doc_info.get("name", doc_rel_path or "unknown"),
                "path": doc_rel_path,
                "ext": doc_info.get("ext", ""),
                "char_count": char_count,
                "preview": text[:200] if text else "",  # 前200字符预览
            }
            result["doc_files"].append(file_info)
            result["total_char_count"] += char_count

            # 判断是否为README
            doc_name = doc_info.get("name", "")
            if doc_name and doc_name.lower() in ("readme.txt", "readme.md", "readme.doc", "readme.docx"):
                result["has_readme"] = True
                result["readme_content"] = text

        result["total_doc_count"] = len(result["doc_files"])
        return result


# 全局实例
document_parser = DocumentParser()
