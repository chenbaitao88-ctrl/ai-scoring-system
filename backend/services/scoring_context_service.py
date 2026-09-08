"""
评分上下文收集服务 —— 从 scorer.py 抽出的 Prompt 素材收集层。

职责：
- 代码内容收集（黑名单/小文件优先 + 旧版回滚）
- AIGC 交互摘要收集
- 单文件格式化（代码/文本/二进制占位/bcm4/mp/未知）
- 编程猫 KITTEN4 / Mind+ 项目结构化解析

设计约束：
- 不生成 Prompt 模板文案（那是 task_prompts.py 的职责）
- 不调用 LLM
- 不修改数据库
- 不组装评分结果
"""
import json
import shutil
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Dict, List

from database import Work


class ScoringContextService:
    """评分上下文收集器：为 LLM Prompt 提供素材篮子"""

    # ============ P0-1: 黑名单 / 分类常量 ============
    _MEDIA_BLACKLIST = {
        ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".flac", ".aac",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff",
        ".ttf", ".woff", ".woff2", ".otf", ".eot",
        ".mov", ".avi", ".mkv", ".webm",
        # 系统垃圾
        ".ds_store",
    }
    _SYSTEM_PATH_FRAGMENTS = (
        "__macosx/", "__pycache__/", ".idea/", ".vscode/",
        "node_modules/", ".git/", ".ds_store",
    )
    _CODE_EXTS = {".py", ".js", ".html", ".css", ".sb3", ".sb2"}
    _TEXT_EXTS = {".txt", ".md", ".json", ".xml", ".csv", ".yaml", ".yml", ".textclipping"}
    _BCM4_EXTS = {".bcm4", ".bcmkn"}
    _MP_EXTS = {".mp"}
    _BINARY_PROJECT_EXTS = {".bcm", ".kitten"}

    # --- 编程猫 KITTEN4 积木类型翻译表 ---
    _BCM4_TRIGGERS = {
        "start_on_click": "[点击]",
        "sprite_on_tap": "[触摸]",
        "on_running_group_activated": "[启动]",
        "self_listen": "[监听]",
        "procedures_2_defnoreturn": "[函数]",
        "procedures_defnoreturn": "[函数]",
    }
    _BCM4_ACTIONS = {
        "self_disappear": "消失",
        "self_appear": "出现",
        "self_move_to": "移动",
        "self_gradually_show_hide": "渐显/渐隐",
        "create_stage_dialog": "显示对话",
        "self_dialog": "说话",
        "self_broadcast": "广播消息",
        "switch_to_screen": "切换场景",
        "controls_if": "如果...则",
        "controls_if_else": "如果...否则",
        "repeat_forever": "无限循环",
        "repeat_times": "循环",
        "ask_and_choose": "询问选择",
        "get_choice_or_index": "获取选择",
        "wait": "等待",
        "change_variable": "改变变量",
        "show_hide_variable": "显隐变量",
        "set_entity_show_hide": "显隐实体",
        "lists_get": "列表取值",
        "lists_get_value": "列表取值",
        "lists_set": "列表赋值",
        "logic_compare": "比较",
        "get_current_scene": "当前场景",
        "math_number": "",
        "text": "",
        "math_arithmetic": "",
    }

    def collect_code_content(self, work: Work, extract_dir: Path) -> str:
        """收集代码内容用于LLM分析（智能截断）"""
        source_files = work.source_files or []
        all_code = []
        MAX_CHARS = 8000

        from feature_flags import flags
        if flags.FEATURE_NEW_PARSER:
            self.collect_new(source_files, extract_dir, all_code, MAX_CHARS)
        else:
            self.collect_legacy(source_files, extract_dir, all_code, MAX_CHARS)

        result = "\n".join(all_code)
        return result[:MAX_CHARS] if len(result) > MAX_CHARS else result

    def collect_new(self, source_files, extract_dir: Path, all_code: list, MAX_CHARS: int) -> None:
        """P0-1 新版: 黑名单 + 小文件优先。"""
        def _size_of(sf):
            try:
                p = extract_dir / sf.get("path", sf.get("name", ""))
                return p.stat().st_size if p.exists() else 1 << 30
            except Exception:
                return 1 << 30
        ordered = sorted(source_files, key=_size_of)

        total_chars = 0
        for sf in ordered:
            if total_chars >= MAX_CHARS:
                break
            file_rel_path = sf.get("path", sf.get("name", ""))
            if not file_rel_path:
                continue
            rel_lower = file_rel_path.replace("\\", "/").lower()
            if any(frag in rel_lower for frag in self._SYSTEM_PATH_FRAGMENTS):
                continue
            ext = (sf.get("ext") or "").lower()
            if ext in self._MEDIA_BLACKLIST:
                continue
            file_path = extract_dir / file_rel_path
            if not file_path.exists():
                continue
            try:
                if ext in self._CODE_EXTS:
                    chunk = self.format_code_file(sf, file_path)
                elif ext in self._TEXT_EXTS:
                    chunk = self.format_text_file(sf, file_path, ext)
                elif ext in self._BCM4_EXTS:
                    chunk = self.format_bcm4(sf, file_path, ext)
                elif ext in self._MP_EXTS:
                    chunk = self.format_mp(sf, file_path, ext)
                elif ext in self._BINARY_PROJECT_EXTS:
                    chunk = self.format_binary_placeholder(sf, file_path, ext)
                else:
                    chunk = self.format_unknown_file(sf, file_path, ext)
                if chunk:
                    all_code.append(chunk)
                    total_chars += len(chunk)
            except Exception:
                continue

    def collect_legacy(self, source_files, extract_dir: Path, all_code: list, MAX_CHARS: int) -> None:
        """FEATURE_NEW_PARSER=false 时的回滚路径。"""
        total_chars = 0
        for sf in source_files:
            if total_chars >= MAX_CHARS:
                break
            file_rel_path = sf.get("path", sf.get("name", ""))
            if not file_rel_path:
                continue
            file_path = extract_dir / file_rel_path
            if file_path.exists() and sf.get("ext") in (".py", ".js", ".html", ".css", ".sb3", ".sb2"):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if len(content) > 3000:
                        import re
                        functions = re.findall(r'(?:def |class |function )(\w+)', content[:2000])
                        ending = content[-500:] if len(content) > 500 else content
                        lines = content.count('\n')
                        structured = f"""### {sf['name']} (共{lines}行, 摘要)
【结构】函数/类: {', '.join(functions[:20])}{'...' if len(functions) > 20 else ''}
【开头】
{content[:500]}
...
【结尾】
{ending}
"""
                        all_code.append(structured)
                        total_chars += len(structured)
                    else:
                        all_code.append(f"### {sf['name']}\n```\n{content}\n```\n")
                        total_chars += len(content)
                except Exception:
                    continue

    # ============ 单文件格式化 ============

    def format_code_file(self, sf, file_path: Path) -> str:
        """代码类: >3000 字符走结构化摘要。"""
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        if len(content) > 3000:
            import re
            functions = re.findall(r'(?:def |class |function )(\w+)', content[:2000])
            ending = content[-500:] if len(content) > 500 else content
            lines = content.count('\n')
            return (
                f"### {sf.get('name','?')} (共{lines}行, 摘要)\n"
                f"【结构】函数/类: {', '.join(functions[:20])}"
                f"{'...' if len(functions) > 20 else ''}\n"
                f"【开头】\n{content[:500]}\n...\n"
                f"【结尾】\n{ending}\n"
            )
        return f"### {sf.get('name','?')}\n```\n{content}\n```\n"

    def format_text_file(self, sf, file_path: Path, ext: str) -> str:
        """文本/AIGC 记录: 截断到 1000 字符。"""
        content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        if len(content) < 10:
            return f"### {sf.get('name','?')} [空文件或仅含链接]\n"
        truncated = content[:1000]
        more = f"\n...(共 {len(content)} 字符,已截断)" if len(content) > 1000 else ""
        return f"### {sf.get('name','?')} [{ext}]\n```\n{truncated}{more}\n```\n"

    def format_binary_placeholder(self, sf, file_path: Path, ext: str) -> str:
        """二进制工程文件占位。"""
        size_kb = file_path.stat().st_size / 1024
        name = sf.get("name", file_path.name)
        return (
            f"### {name}\n"
            f"[BINARY_PLACEHOLDER: {name} ({ext} 工程文件), {size_kb:.1f}KB, 待 P0-2 解析器实现]\n"
        )

    def format_unknown_file(self, sf, file_path: Path, ext: str) -> str:
        """未知扩展: 尝试 UTF-8 strict 读前 500 字符。"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="strict")
            return f"### {sf.get('name','?')} [{ext}, 未知类型]\n```\n{content[:500]}\n```\n"
        except Exception:
            size_kb = file_path.stat().st_size / 1024
            return f"### {sf.get('name','?')} [{ext}, 二进制, {size_kb:.1f}KB]\n"

    # ============ P1-12: .bcm4 / .mp 真实解析 ============

    def translate_bcm4_block(self, btype: str, fields: dict) -> str:
        """将单个积木翻译为可读动作; 参数类积木返回空串。"""
        if btype in self._BCM4_TRIGGERS:
            return self._BCM4_TRIGGERS[btype]
        if btype == "self_gradually_show_hide":
            return "渐显" if fields.get("is_show") == "show" else "渐隐"
        return self._BCM4_ACTIONS.get(btype, "")

    def extract_event_chains(self, actor: dict) -> List[dict]:
        """从单个 actor 提取所有事件链(沿 connections['next'] 遍历)。"""
        abd = actor.get("block_data_json", {})
        if not isinstance(abd, dict):
            return []
        blocks = abd.get("blocks", {})
        connections = abd.get("connections", {})
        if not isinstance(blocks, dict):
            return []

        chains = []
        roots = [bid for bid, b in blocks.items()
                 if isinstance(b, dict) and b.get("parent_id") is None]
        for root in roots:
            chain_actions = []
            cur = root
            seen = set()
            while cur and cur not in seen:
                seen.add(cur)
                b = blocks.get(cur, {})
                if isinstance(b, dict):
                    btype = b.get("type", "")
                    fields = b.get("fields", {})
                    trans = self.translate_bcm4_block(btype, fields)
                    if trans:
                        chain_actions.append(trans)
                conns = connections.get(cur, {})
                nxt = None
                for nbid, cinfo in conns.items():
                    if isinstance(cinfo, dict) and cinfo.get("type") == "next":
                        nxt = nbid
                        break
                cur = nxt
            if len(chain_actions) >= 1:
                chains.append({
                    "actor_name": actor.get("name", "?"),
                    "actions": chain_actions,
                    "length": len(chain_actions),
                })
        return chains

    def detect_bcm4_concepts(self, actors: dict) -> dict:
        """全局编程概念检测。"""
        concepts = {"变量": False, "广播": False, "循环": False,
                    "条件": False, "列表": False, "场景切换": False}
        for actor in actors.values():
            if not isinstance(actor, dict):
                continue
            abd = actor.get("block_data_json", {})
            if not isinstance(abd, dict):
                continue
            blocks = abd.get("blocks", {})
            if not isinstance(blocks, dict):
                continue
            for b in blocks.values():
                if not isinstance(b, dict):
                    continue
                bt = b.get("type", "")
                if bt in ("change_variable", "set_variable", "show_hide_variable"):
                    concepts["变量"] = True
                if bt in ("self_broadcast", "broadcast_input", "on_running_group_activated"):
                    concepts["广播"] = True
                if bt in ("repeat_forever", "repeat_times", "controls_repeat"):
                    concepts["循环"] = True
                if bt in ("controls_if", "controls_if_else"):
                    concepts["条件"] = True
                if bt in ("lists_get", "lists_set", "lists_get_value"):
                    concepts["列表"] = True
                if bt == "switch_to_screen":
                    concepts["场景切换"] = True
            if actor.get("variables"):
                concepts["变量"] = True
        return concepts

    def format_bcm4(self, sf, file_path: Path, ext: str) -> str:
        """解析编程猫 KITTEN4 .bcm4/.bcmkn 为 LLM 可读的事件流驱动摘要。"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
        except Exception:
            return self.format_text_file(sf, file_path, ext)

        project_name = data.get("project_name", "未命名") or "未命名"
        theatre = data.get("theatre", {})
        scenes = theatre.get("scenes", {})
        scene_count = len(scenes) if isinstance(scenes, dict) else 0
        scenes_order = theatre.get("scenes_order", [])
        if isinstance(scenes_order, list) and len(scenes_order) > scene_count:
            scene_count = len(scenes_order)
        actors = theatre.get("actors", {})
        actor_count = len(actors) if isinstance(actors, dict) else 0

        block_types = Counter()
        block_count = 0
        for actor in actors.values():
            if not isinstance(actor, dict):
                continue
            abd = actor.get("block_data_json", {})
            if not isinstance(abd, dict):
                continue
            blocks = abd.get("blocks", {})
            if isinstance(blocks, dict):
                block_count += len(blocks)
                for binfo in blocks.values():
                    if isinstance(binfo, dict):
                        block_types[binfo.get("type", "unknown")] += 1

        concepts = self.detect_bcm4_concepts(actors)
        concept_str = " ".join(
            f"{k}{'✓' if v else '✗'}" for k, v in concepts.items()
        )

        all_chains = []
        for actor in actors.values():
            if not isinstance(actor, dict):
                continue
            chains = self.extract_event_chains(actor)
            all_chains.extend(chains)
        all_chains.sort(key=lambda c: c["length"], reverse=True)
        top_chains = all_chains[:5]

        lines = [
            f"### {sf.get('name', '?')} [编程猫 KITTEN4]",
            f"作品: {project_name} | {scene_count}场景/{actor_count}角色/{block_count}积木",
            f"编程概念: {concept_str}",
            "",
            "事件流 (核心玩法):",
        ]
        for i, chain in enumerate(top_chains, 1):
            actions = " → ".join(chain["actions"])
            if len(actions) > 120:
                actions = actions[:117] + "..."
            lines.append(f"{i}. [{chain['actor_name']}] {actions}")

        if block_types:
            top10 = block_types.most_common(10)
            type_summary = ", ".join(f"{t}({c})" for t, c in top10)
            lines.append("")
            lines.append(f"高频积木: {type_summary}")

        summary = "\n".join(lines) + "\n"
        if len(summary) > 2000:
            summary = summary[:1997] + "...\n"
        return summary

    def format_mp(self, sf, file_path: Path, ext: str) -> str:
        """解析 Mind+ .mp 为 LLM 可读的结构化摘要。"""
        temp_dir = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                zf.extractall(temp_dir)

            project_json = temp_dir / "project.json"
            if not project_json.exists():
                raise FileNotFoundError("project.json not found")

            with open(project_json, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)

            pages = []
            page_keys = sorted([k for k in data.keys() if k.startswith("page")])
            for pk in page_keys:
                page_data = data.get(pk, {})
                if not isinstance(page_data, dict):
                    continue
                targets = page_data.get("targets", [])
                target_count = len(targets) if isinstance(targets, list) else 0
                block_count = 0
                block_types = Counter()
                for t in targets:
                    if not isinstance(t, dict):
                        continue
                    blocks = t.get("blocks", {})
                    if isinstance(blocks, dict):
                        block_count += len(blocks)
                        for binfo in blocks.values():
                            if isinstance(binfo, dict):
                                block_types[binfo.get("opcode", "unknown")] += 1
                pages.append({
                    "name": pk,
                    "target_count": target_count,
                    "block_count": block_count,
                    "block_types": block_types,
                })

            total_blocks = sum(p["block_count"] for p in pages)
            total_targets = sum(p["target_count"] for p in pages)
            all_block_types = Counter()
            for p in pages:
                all_block_types.update(p["block_types"])

            lines = [
                f"### {sf.get('name', '?')} [Mind+ 项目]",
                f"页面数量: {len(pages)}",
                f"角色总数: {total_targets}",
                f"积木总数: {total_blocks}",
                "",
                "页面详情:",
            ]
            for p in pages:
                lines.append(f"  - {p['name']}: {p['target_count']} 角色, {p['block_count']} 积木")

            if all_block_types:
                lines.append("")
                lines.append("积木类型分布 (Top 15):")
                for btype, cnt in all_block_types.most_common(15):
                    lines.append(f"  - {btype}: {cnt}")

            summary = "\n".join(lines) + "\n"
            if len(summary) > 2500:
                summary = summary[:2497] + "...\n"
            return summary
        except Exception:
            return self.format_binary_placeholder(sf, file_path, ext)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ============ AIGC 摘要收集 ============

    def collect_aigc_summary(self, aigc_result) -> str:
        """收集AIGC交互摘要"""
        summary_parts = []
        if hasattr(aigc_result, 'tool_name') and aigc_result.tool_name:
            summary_parts.append(f"使用工具: {aigc_result.tool_name}")
        if hasattr(aigc_result, 'total_interactions'):
            summary_parts.append(f"总交互次数: {aigc_result.total_interactions}")
        if hasattr(aigc_result, 'user_messages'):
            summary_parts.append(f"用户消息数: {aigc_result.user_messages}")
        if hasattr(aigc_result, 'code_generation_count'):
            summary_parts.append(f"代码生成次数: {aigc_result.code_generation_count}")
        if hasattr(aigc_result, 'debugging_count'):
            summary_parts.append(f"调试次数: {aigc_result.debugging_count}")
        if hasattr(aigc_result, 'optimization_count'):
            summary_parts.append(f"优化改进次数: {aigc_result.optimization_count}")
        if hasattr(aigc_result, 'iteration_depth'):
            summary_parts.append(f"迭代深度: {aigc_result.iteration_depth}")
        if hasattr(aigc_result, 'duration_minutes'):
            summary_parts.append(f"交互时长: {aigc_result.duration_minutes:.0f}分钟")
        return "\n".join(summary_parts) if summary_parts else "无AIGC交互记录"
