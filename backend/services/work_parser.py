"""
作品解析服务
- ZIP解压
- 文件结构识别（source/aigc_log/screenshots/document）
- 代码文件提取
- AIGC日志解析
- 文档内容提取
- 图形化编程文件二次解压与JSON分析（.sb3/.bcm3/.bcm4/.bcmkn）
"""
import os
import re
import json
import zipfile
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import UploadFile
from sqlalchemy.orm import Session

from database import Team, Work
from config import WORKS_DIR, get_naming_pattern
from services.document_parser import document_parser


class WorkParser:
    """作品解析器"""

    # 支持的文件扩展名
    CODE_EXTENSIONS = {".py", ".sb3", ".sb2", ".js", ".html", ".css", ".bcm", ".bcm3", ".bcm4", ".bcmkn", ".kitten", ".mpcode"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
    AIGC_EXTENSIONS = {".html", ".json", ".txt", ".md", ".csv"}
    DOC_EXTENSIONS = {".txt", ".md", ".doc", ".docx", ".pdf", ".pptx", ".jpg"}

    def __init__(self, db: Session):
        self.db = db

    async def upload_and_parse(self, file: UploadFile) -> dict:
        """上传ZIP文件并解析"""
        filename = file.filename or ""
        content = await file.read()

        # 1. 从文件名解析队伍信息
        team_info = self._parse_filename(filename)

        # 如果无法解析文件名，返回错误
        if not team_info:
            return {"success": False, "error": "上传失败：作品命名不符合规范。正确格式为：组别+姓名（例如：小学组+张三、李四）"}

        # 2. 保存ZIP文件
        save_path = WORKS_DIR / filename
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(content)

        # 3. 解压ZIP
        extract_dir = WORKS_DIR / Path(filename).stem
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        try:
            with zipfile.ZipFile(save_path, 'r') as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            return {"success": False, "error": "无效的ZIP文件"}

        # 4. 解析文件结构
        parse_result = self._analyze_structure(extract_dir)

        # 5. 匹配或创建队伍，保存作品到数据库
        team_id = None
        if team_info:
            team = self._find_team_or_create(team_info)
            if team:
                team_id = team.id
                self._save_work(team.id, filename, str(save_path), len(content), parse_result)

        return {
            "success": True,
            "filename": filename,
            "team_info": team_info,
            "team_id": team_id,
            "parse_result": parse_result
        }

    async def upload_folder(self, files: List[UploadFile], folder_name: str) -> dict:
        """上传文件夹并解析（单个文件夹）"""
        from config import WORKS_DIR
        import tempfile
        import shutil

        # 1. 从文件夹名解析队伍信息
        team_info = self._parse_filename(folder_name)

        # 如果无法解析文件夹名，返回错误
        if not team_info:
            return {"success": False, "error": "上传失败：文件夹命名不符合规范。正确格式为：组别+姓名（例如：小学组+张三、李四）"}

        # 2. 创建保存目录
        save_dir = WORKS_DIR / folder_name
        if save_dir.exists():
            shutil.rmtree(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        # 3. 保存所有文件
        files_count = 0
        for file in files:
            if not file.filename:
                continue
            # 处理路径（去除文件夹前缀）
            rel_path = file.filename
            # 如果路径包含文件夹名，去除它
            if rel_path.startswith(folder_name + "/"):
                rel_path = rel_path[len(folder_name) + 1:]
            elif rel_path.startswith(folder_name + "\\"):
                rel_path = rel_path[len(folder_name) + 1:]

            if not rel_path:
                continue

            file_path = save_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            content = await file.read()
            with open(file_path, "wb") as f:
                f.write(content)
            files_count += 1

        # 4. 解析文件结构
        parse_result = self._analyze_structure(save_dir)

        # 5. 匹配或创建队伍，保存作品到数据库
        team_id = None
        if team_info:
            team = self._find_team_or_create(team_info)
            if team:
                team_id = team.id
                # 保存作品（文件夹格式）
                self._save_work(team.id, folder_name, str(save_dir), 0, parse_result)

        return {
            "success": True,
            "folder_name": folder_name,
            "team_info": team_info,
            "team_id": team_id,
            "files_count": files_count,
            "parse_result": parse_result
        }

    async def batch_upload_folders(self, files: List[UploadFile], max_concurrency: int = 10) -> dict:
        """批量上传多个文件夹（并发处理）"""
        from collections import defaultdict
        import asyncio

        # 按文件夹分组
        folders = defaultdict(list)
        for file in files:
            if not file.filename:
                continue
            # 提取文件夹名（第一级目录）
            parts = file.filename.replace("\\", "/").split("/")
            if len(parts) > 1:
                folder_name = parts[0]
                folders[folder_name].append(file)

        results = {
            "total_folders": len(folders),
            "success": 0,
            "failed": 0,
            "details": []
        }

        # 使用信号量限制并发数
        semaphore = asyncio.Semaphore(max_concurrency)

        async def process_single_folder(folder_name: str, folder_files: List[UploadFile]) -> dict:
            """处理单个文件夹（带并发限制）"""
            async with semaphore:
                try:
                    result = await self.upload_folder(folder_files, folder_name)
                    return {
                        "folder": folder_name,
                        "success": result.get("success", False),
                        "team_id": result.get("team_id"),
                        "files_count": result.get("files_count"),
                        "error": result.get("error") if not result.get("success") else None
                    }
                except Exception as e:
                    return {
                        "folder": folder_name,
                        "success": False,
                        "error": str(e)
                    }

        # 创建所有任务
        tasks = [
            process_single_folder(folder_name, folder_files)
            for folder_name, folder_files in folders.items()
        ]

        # 并发执行所有任务
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 整理结果
        for result in task_results:
            if isinstance(result, Exception):
                results["failed"] += 1
                results["details"].append({"folder": "unknown", "error": str(result)})
            elif result.get("success"):
                results["success"] += 1
                results["details"].append({
                    "folder": result["folder"],
                    "team_id": result.get("team_id"),
                    "files_count": result.get("files_count")
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "folder": result.get("folder", "unknown"),
                    "error": result.get("error", "未知错误")
                })

        return results

    def parse_existing(self, work_id: int) -> dict:
        """解析已上传的作品"""
        work = self.db.query(Work).filter(Work.id == work_id).first()
        if not work:
            return {"success": False, "error": "作品不存在"}

        if not work.file_path or not os.path.exists(work.file_path):
            return {"success": False, "error": "作品文件不存在"}

        # 获取解压目录
        extract_dir = WORKS_DIR / Path(work.original_filename or "").stem
        if not extract_dir.exists():
            # 重新解压
            try:
                with zipfile.ZipFile(work.file_path, 'r') as zf:
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile:
                return {"success": False, "error": "无效的ZIP文件"}

        # 解析文件结构
        parse_result = self._analyze_structure(extract_dir)

        # 更新数据库
        self._update_work(work, parse_result)

        return {"success": True, "work_id": work_id, "parse_result": parse_result}

    def batch_parse_all(self) -> dict:
        """批量解析所有已上传但未解析的作品"""
        works = self.db.query(Work).filter(Work.is_parsed == False).all()
        results = {"total": len(works), "success": 0, "failed": 0, "errors": []}

        for work in works:
            try:
                result = self.parse_existing(work.id)
                if result.get("success"):
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append(f"作品ID {work.id}: {result.get('error', '未知错误')}")
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"作品ID {work.id}: {str(e)}")

        return results

    def _parse_filename(self, filename: str) -> Optional[dict]:
        """
        从文件名解析队伍信息
        只接受规范格式（+ 分隔）：
        - 新格式：组别+姓名（例如：小学组+张三、李四）
        - 旧格式（兼容）：组别+编号+姓名（例如：小学组+001+张三、李四）
        """
        name = Path(filename).stem

        # 只接受包含加号的格式
        if "+" not in name:
            return None

        parts = name.split("+")

        # 第一部分必须是有效组别
        if parts[0] not in ("小学", "小学组", "初中", "初中组"):
            return None

        # 新格式：组别+姓名（2部分）
        if len(parts) == 2:
            team_name = parts[1]
            if not team_name:  # 姓名为空
                return None
            # 如果包含顿号，拆分、排序、重新组装（确保顺序一致，避免"学生1、学生2"和"学生2、学生1"不匹配）
            if '、' in team_name:
                members = [m.strip() for m in team_name.split('、') if m.strip()]
                members.sort()
                team_name = '、'.join(members)
            return {
                "match_type": "new_format",
                "group_type": parts[0],
                "team_code": "",
                "team_name": team_name
            }

        # 旧格式：组别+编号+姓名（3+部分）
        elif len(parts) >= 3:
            team_name = parts[2] if len(parts) == 3 else "+".join(parts[2:])
            if not team_name:  # 姓名为空
                return None
            # 如果包含顿号，同样排序重组
            if '、' in team_name:
                members = [m.strip() for m in team_name.split('、') if m.strip()]
                members.sort()
                team_name = '、'.join(members)
            return {
                "match_type": "new_format",
                "group_type": parts[0],
                "team_code": parts[1],
                "team_name": team_name
            }

        # 其他所有情况一律拒绝
        return None

    def _find_team(self, team_info: dict) -> Optional[Team]:
        """根据队伍信息查找队伍（多层匹配策略）"""
        match_type = team_info.get("match_type", "")

        # 新格式: 组别+姓名 或 组别+编号+姓名（优先使用编号匹配，无编号时直接使用队名匹配）
        if match_type == "new_format":
            # 首先尝试使用编号精确匹配
            if team_info.get("team_code"):
                team = self.db.query(Team).filter(
                    Team.team_code == team_info["team_code"]
                ).first()
                if team:
                    return team

            # 如果编号匹配失败，尝试使用队名匹配
            if team_info.get("team_name"):
                team = self.db.query(Team).filter(
                    Team.team_name == team_info["team_name"],
                    Team.group_type == team_info["group_type"]
                ).first()
                if team:
                    return team

            # 如果仍然没有找到，尝试使用队名模糊匹配（可能队名中包含姓名）
            if team_info.get("team_name"):
                # 尝试从队名中提取姓名（可能包含顿号分隔的多个人名）
                team_name = team_info["team_name"]
                # 尝试使用整个队名匹配
                teams = self.db.query(Team).filter(
                    Team.team_name.contains(team_name)
                ).all()
                if len(teams) == 1:
                    return teams[0]
                # 如果多个队伍匹配，尝试使用组别过滤
                if len(teams) > 1 and team_info.get("group_type"):
                    teams = [t for t in teams if t.group_type == team_info["group_type"]]
                    if len(teams) == 1:
                        return teams[0]

            # 如果队名看起来像多个学生姓名（包含顿号），尝试在members中搜索
            if team_info.get("team_name") and '、' in team_info["team_name"]:
                # 拆分学生姓名
                student_names = [n.strip() for n in team_info["team_name"].split('、') if n.strip()]
                if len(student_names) >= 1:
                    # 搜索所有队伍，在members中匹配所有学生姓名
                    all_teams = self.db.query(Team).filter(
                        Team.group_type == team_info["group_type"]
                    ).all()
                    for team in all_teams:
                        members = team.members or []
                        member_names = []
                        for m in members:
                            if isinstance(m, dict) and m.get('name'):
                                member_names.append(m['name'])
                            elif isinstance(m, str):
                                member_names.append(m)
                        # 检查是否所有学生姓名都在这个队伍的members中
                        matched_names = [n for n in student_names if any(n in mn or mn in n for mn in member_names)]
                        if len(matched_names) == len(student_names):
                            return team

            # 如果队名匹配失败，尝试在members中搜索单个学生姓名
            if team_info.get("team_name") and not '、' in team_info["team_name"]:
                # 单个学生姓名，搜索members字段
                student_name = team_info["team_name"].strip()
                all_teams = self.db.query(Team).filter(
                    Team.group_type == team_info["group_type"]
                ).all()
                for team in all_teams:
                    members = team.members or []
                    for m in members:
                        if isinstance(m, dict) and m.get('name'):
                            if student_name in m['name'] or m['name'] in student_name:
                                return team
                        elif isinstance(m, str) and (student_name in m or m in student_name):
                            return team

            return None

        # 优先级1: 短编号精确匹配（P002）
        if match_type == "short_code" and team_info.get("short_code"):
            team = self.db.query(Team).filter(
                Team.short_code == team_info["short_code"]
            ).first()
            if team:
                return team

        # 优先级2: 原始编号精确匹配
        if team_info.get("team_code"):
            team = self.db.query(Team).filter(
                Team.team_code == team_info["team_code"]
            ).first()
            if team:
                return team
            # 尝试带后缀匹配（编号可能带 -01/-02 后缀）
            team = self.db.query(Team).filter(
                Team.team_code.startswith(team_info["team_code"])
            ).first()
            if team:
                return team

        # 优先级3: 组别+队名联合匹配
        if team_info.get("team_name") and team_info.get("group_type"):
            team = self.db.query(Team).filter(
                Team.team_name == team_info["team_name"],
                Team.group_type == team_info["group_type"]
            ).first()
            if team:
                return team

        # 优先级4: 纯队名匹配（可能不唯一）
        if team_info.get("team_name"):
            teams = self.db.query(Team).filter(
                Team.team_name == team_info["team_name"]
            ).all()
            if len(teams) == 1:
                return teams[0]
            # 多个同名，无法确定
            if len(teams) > 1:
                return None

        return None

    def _find_team_or_create(self, team_info: dict) -> Optional[Team]:
        """根据队伍信息查找队伍（仅匹配，不自动创建）"""
        # 只查找，不创建
        return self._find_team(team_info)

    def _analyze_scratch_json(self, json_path: Path) -> Dict[str, Any]:
        """分析Scratch/编程猫的JSON项目文件，提取积木数量等信息"""
        try:
            with open(json_path, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)

            result: Dict[str, Any] = {
                "block_count": 0,
                "sprite_count": 0,
                "has_variables": False,
                "has_pen": False,
                "has_music": False,
            }

            # Scratch 3.0 项目结构
            if isinstance(data, dict) and "targets" in data:
                targets = data.get("targets", [])
                result["sprite_count"] = len([t for t in targets if isinstance(t, dict) and not t.get("isStage")])

                # 统计积木数量
                for target in targets:
                    if isinstance(target, dict) and "blocks" in target:
                        blocks = target["blocks"]
                        if isinstance(blocks, dict):
                            result["block_count"] += len(blocks)
                        elif isinstance(blocks, list):
                            result["block_count"] += len(blocks)

                    # 检查扩展（画笔、音乐等）
                    if isinstance(target, dict) and "blocks" in target:
                        blocks = target["blocks"]
                        if isinstance(blocks, dict):
                            block_ops = ' '.join(str(b) for b in blocks.values() if b)
                            if 'pen' in block_ops.lower():
                                result["has_pen"] = True
                            if 'music' in block_ops.lower():
                                result["has_music"] = True

                # 检查是否有变量
                for target in targets:
                    if isinstance(target, dict) and "variables" in target:
                        if target["variables"]:
                            result["has_variables"] = True

            return result
        except Exception:
            return {}

    def _analyze_bcm4_json(self, json_path: Path) -> Dict[str, Any]:
        """分析编程猫 KITTEN4 .bcm4/.bcmkn JSON 项目文件，提取积木数量等信息。
        注意: KITTEN4 的积木在每个 actor 的 block_data_json.blocks 中，不在顶层。
        """
        try:
            with open(json_path, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)

            result: Dict[str, Any] = {
                "block_count": 0,
                "sprite_count": 0,
                "has_variables": False,
                "has_pen": False,
                "has_music": False,
            }

            theatre = data.get("theatre", {})

            # 统计角色数（actors 数量）
            actors = theatre.get("actors", {})
            if isinstance(actors, dict):
                result["sprite_count"] = len(actors)

            # 统计积木数量 + 类型 —— 遍历每个 actor 的 block_data_json.blocks
            for actor in actors.values():
                if not isinstance(actor, dict):
                    continue
                abd = actor.get("block_data_json", {})
                if not isinstance(abd, dict):
                    continue
                blocks = abd.get("blocks", {})
                if isinstance(blocks, dict):
                    result["block_count"] += len(blocks)
                    block_ops = ' '.join(str(b) for b in blocks.values() if b)
                    if 'pen' in block_ops.lower():
                        result["has_pen"] = True
                    if 'music' in block_ops.lower():
                        result["has_music"] = True

            # 检查变量（顶层 + 各 actor）
            variables = data.get("variables", {})
            if variables:
                result["has_variables"] = True
            for actor in actors.values():
                if isinstance(actor, dict) and actor.get("variables"):
                    result["has_variables"] = True
                    break

            return result
        except Exception:
            return {}

    def _analyze_structure(self, extract_dir: Path) -> dict:
        """分析解压后的文件结构"""
        result = {
            "has_source": False,
            "has_aigc_log": False,
            "has_screenshots": False,
            "has_readme": False,
            "source_files": [],
            "aigc_log_files": [],
            "screenshot_files": [],
            "document_files": [],
            "readme_content": "",
            "code_language": None,
            "code_line_count": 0,
            "comment_rate": 0.0,
            "all_files": [],
            "warnings": []
        }

        # 第一步：解压所有 ZIP 文件到临时目录（支持嵌套ZIP）
        all_dirs = [extract_dir]
        temp_dirs = []

        def extract_all_zips(base_dir: Path):
            """递归解压base_dir中所有ZIP到临时目录，并继续扫描解压后的内容"""
            for root, dirs, files in os.walk(base_dir):
                for f in files:
                    file_path = Path(root) / f
                    if file_path.suffix.lower() == '.zip':
                        try:
                            zip_temp = Path(tempfile.mkdtemp())
                            temp_dirs.append(zip_temp)
                            with zipfile.ZipFile(file_path, 'r') as zf:
                                zf.extractall(zip_temp)
                            # 递归处理解压出来的内容（支持ZIP嵌套）
                            extract_all_zips(zip_temp)
                        except Exception:
                            pass

        extract_all_zips(extract_dir)
        all_dirs.extend(temp_dirs)

        # 用于去重（相同路径只分析一次）
        seen_rel_paths = set()

        # 第二步：遍历所有目录（包括ZIP解压内容）
        for scan_dir in all_dirs:
            for root, dirs, files in os.walk(scan_dir):
                for f in files:
                    file_path = Path(root) / f

                    # 来自ZIP的文件标记路径（用于区分来源）
                    if scan_dir in temp_dirs:
                        # 从临时目录追溯原始ZIP内的相对路径
                        rel_path = f"[ZIP] {f}"
                    else:
                        rel_path = str(file_path.relative_to(extract_dir))

                    # 避免重复扫描同一路径
                    if rel_path in seen_rel_paths:
                        continue
                    seen_rel_paths.add(rel_path)

                    ext = file_path.suffix.lower()

                    file_info = {
                        "name": f,
                        "path": rel_path,
                        "ext": ext,
                        "size": file_path.stat().st_size
                    }
                    result["all_files"].append(file_info)

                    rel_lower = rel_path.lower()

                    # 源文件
                    if "source" in rel_lower or ext in self.CODE_EXTENSIONS:
                        if ext in self.CODE_EXTENSIONS:
                            result["has_source"] = True
                            result["source_files"].append(file_info)

                            # 识别编程语言
                            if ext == ".py" and not result["code_language"]:
                                result["code_language"] = "Python"
                            elif ext in (".sb3", ".sb2") and not result["code_language"]:
                                result["code_language"] = "Scratch"
                            elif ext in (".bcm", ".bcm3", ".bcm4", ".bcmkn", ".kitten") and not result["code_language"]:
                                result["code_language"] = "编程猫"
                            elif ext == ".mpcode" and not result["code_language"]:
                                result["code_language"] = "Mind+"

                    # 二次解压分析 .sb3/.bcm 等图形化文件（仅对原始目录中的文件操作）
                    # P1-12 (2026-05-25): .bcm4/.bcmkn 是纯文本 JSON，不走 ZIP 解压
                    if scan_dir == extract_dir and ext in (".sb3", ".sb2", ".bcm", ".bcm3") and not result.get("scratch_analyzed"):
                        try:
                            with zipfile.ZipFile(file_path, 'r') as zf:
                                temp_dir2 = Path(tempfile.mkdtemp())
                                zf.extractall(temp_dir2)

                                found = False
                                for root2, dirs2, files2 in os.walk(temp_dir2):
                                    for f2 in files2:
                                        if f2.lower().endswith('.json'):
                                            json_path = Path(root2) / f2
                                            analysis = self._analyze_scratch_json(json_path)
                                            if analysis and analysis.get("block_count", 0) > 0:
                                                result["scratch_analyzed"] = True
                                                result["scratch_block_count"] = analysis.get("block_count", 0)
                                                result["scratch_sprite_count"] = analysis.get("sprite_count", 0)
                                                result["scratch_has_variables"] = analysis.get("has_variables", False)
                                                result["scratch_has_pen"] = analysis.get("has_pen", False)
                                                result["scratch_has_music"] = analysis.get("has_music", False)
                                                found = True
                                                break
                                    if found:
                                        break

                                shutil.rmtree(temp_dir2, ignore_errors=True)
                        except Exception:
                            pass

                    # P1-12 (2026-05-25): 编程猫 KITTEN4 .bcm4/.bcmkn 是纯文本 JSON，直接解析
                    if scan_dir == extract_dir and ext in (".bcm4", ".bcmkn") and not result.get("scratch_analyzed"):
                        try:
                            analysis = self._analyze_bcm4_json(file_path)
                            if analysis:
                                result["scratch_analyzed"] = True
                                result["scratch_block_count"] = analysis.get("block_count", 0)
                                result["scratch_sprite_count"] = analysis.get("sprite_count", 0)
                                result["scratch_has_variables"] = analysis.get("has_variables", False)
                                result["scratch_has_pen"] = analysis.get("has_pen", False)
                                result["scratch_has_music"] = analysis.get("has_music", False)
                        except Exception:
                            pass

                    # AIGC日志
                    if "aigc" in rel_lower or "log" in rel_lower or "chat" in rel_lower:
                        result["has_aigc_log"] = True
                        result["aigc_log_files"].append(file_info)
                    elif ext == ".html" and ("chat" in f.lower() or "log" in f.lower()):
                        result["has_aigc_log"] = True
                        result["aigc_log_files"].append(file_info)

                    # 截图/视频
                    if "screenshot" in rel_lower or "image" in rel_lower or "截图" in rel_path:
                        if ext in self.IMAGE_EXTENSIONS or ext in self.VIDEO_EXTENSIONS:
                            result["has_screenshots"] = True
                            result["screenshot_files"].append(file_info)
                    elif ext in self.IMAGE_EXTENSIONS:
                        result["has_screenshots"] = True
                        result["screenshot_files"].append(file_info)
                    elif ext in self.VIDEO_EXTENSIONS:
                        result["has_screenshots"] = True
                        result["screenshot_files"].append(file_info)

                    # 说明文档
                    if ext in self.DOC_EXTENSIONS or f.lower().startswith("readme"):
                        result["has_readme"] = True
                        result["document_files"].append(file_info)

        # 清理临时目录
        for td in temp_dirs:
            shutil.rmtree(td, ignore_errors=True)

        # 解析文档内容
        if result["document_files"]:
            doc_analysis = document_parser.analyze_document(result["document_files"], extract_dir)
            result["readme_content"] = doc_analysis.get("readme_content", "")
            result["document_files"] = doc_analysis.get("doc_files", [])

        # 分析代码质量（Python）
        if result["code_language"] == "Python" and result["source_files"]:
            code_analysis = self._analyze_python_code(extract_dir, result["source_files"])
            result.update(code_analysis)

        # 生成警告
        if not result["has_source"]:
            result["warnings"].append("未找到源代码文件")
        if not result["has_aigc_log"]:
            result["warnings"].append("未找到AIGC交互日志")
        if not result["has_screenshots"]:
            result["warnings"].append("未找到截图或演示视频")

        return result

    def _analyze_python_code(self, extract_dir: Path, source_files: List[dict]) -> dict:
        """分析Python代码质量"""
        total_lines = 0
        comment_lines = 0
        blank_lines = 0

        for sf in source_files:
            if sf["ext"] != ".py":
                continue
            file_path = extract_dir / sf["path"]
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        total_lines += 1
                        stripped = line.strip()
                        if not stripped:
                            blank_lines += 1
                        elif stripped.startswith("#"):
                            comment_lines += 1
            except Exception:
                continue

        comment_rate = (comment_lines / total_lines * 100) if total_lines > 0 else 0.0

        return {
            "code_line_count": total_lines,
            "comment_lines": comment_lines,
            "blank_lines": blank_lines,
            "comment_rate": round(comment_rate, 1)
        }

    def _save_work(self, team_id: int, filename: str, file_path: str, file_size: int, parse_result: dict):
        """保存作品信息到数据库"""
        # 检查是否已有记录
        work = self.db.query(Work).filter(Work.team_id == team_id).first()
        if work:
            # 更新
            work.original_filename = filename
            work.file_path = file_path
            work.file_size = file_size
            work.has_source = parse_result.get("has_source", False)
            work.has_aigc_log = parse_result.get("has_aigc_log", False)
            work.has_screenshots = parse_result.get("has_screenshots", False)
            work.has_readme = parse_result.get("has_readme", False)
            work.source_files = parse_result.get("source_files", [])
            work.aigc_log_files = parse_result.get("aigc_log_files", [])
            work.screenshot_files = parse_result.get("screenshot_files", [])
            work.document_files = parse_result.get("document_files", [])  # 新增
            work.readme_content = parse_result.get("readme_content", "")  # 新增
            work.code_language = parse_result.get("code_language")
            work.code_line_count = parse_result.get("code_line_count", 0)
            work.comment_rate = parse_result.get("comment_rate", 0.0)
            work.is_parsed = True
        else:
            # 新建
            work = Work(
                team_id=team_id,
                original_filename=filename,
                file_path=file_path,
                file_size=file_size,
                has_source=parse_result.get("has_source", False),
                has_aigc_log=parse_result.get("has_aigc_log", False),
                has_screenshots=parse_result.get("has_screenshots", False),
                has_readme=parse_result.get("has_readme", False),
                source_files=parse_result.get("source_files", []),
                aigc_log_files=parse_result.get("aigc_log_files", []),
                screenshot_files=parse_result.get("screenshot_files", []),
                document_files=parse_result.get("document_files", []),  # 新增
                readme_content=parse_result.get("readme_content", ""),  # 新增
                code_language=parse_result.get("code_language"),
                code_line_count=parse_result.get("code_line_count", 0),
                comment_rate=parse_result.get("comment_rate", 0.0),
                is_parsed=True
            )
            self.db.add(work)

        self.db.commit()

    def _update_work(self, work: Work, parse_result: dict):
        """更新作品解析结果"""
        work.has_source = parse_result.get("has_source", False)
        work.has_aigc_log = parse_result.get("has_aigc_log", False)
        work.has_screenshots = parse_result.get("has_screenshots", False)
        work.has_readme = parse_result.get("has_readme", False)
        work.source_files = parse_result.get("source_files", [])
        work.aigc_log_files = parse_result.get("aigc_log_files", [])
        work.screenshot_files = parse_result.get("screenshot_files", [])
        work.document_files = parse_result.get("document_files", [])  # 新增
        work.readme_content = parse_result.get("readme_content", "")  # 新增
        work.code_language = parse_result.get("code_language")
        work.code_line_count = parse_result.get("code_line_count", 0)
        work.comment_rate = parse_result.get("comment_rate", 0.0)
        work.is_parsed = True
        self.db.commit()

    def get_work_by_team(self, team_id: int) -> Optional[dict]:
        """获取队伍的作品信息"""
        work = self.db.query(Work).filter(Work.team_id == team_id).first()
        if not work:
            return None
        return {
            "id": work.id,
            "team_id": work.team_id,
            "original_filename": work.original_filename,
            "file_size": work.file_size,
            "has_source": work.has_source,
            "has_aigc_log": work.has_aigc_log,
            "has_screenshots": work.has_screenshots,
            "has_readme": work.has_readme,
            "source_files": work.source_files or [],
            "aigc_log_files": work.aigc_log_files or [],
            "screenshot_files": work.screenshot_files or [],
            "document_files": work.document_files or [],  # 新增
            "readme_content": work.readme_content or "",  # 新增
            "code_language": work.code_language,
            "code_line_count": work.code_line_count,
            "comment_rate": work.comment_rate,
            "is_parsed": work.is_parsed,
            "is_flagged": work.is_flagged,
            "flag_reason": work.flag_reason,
            "plagiarism_flag": work.plagiarism_flag
        }

    def get_all_works_status(self) -> dict:
        """获取所有作品的状态统计"""
        total_teams = self.db.query(Team).count()
        works_uploaded = self.db.query(Work).count()
        works_parsed = self.db.query(Work).filter(Work.is_parsed == True).count()
        works_with_source = self.db.query(Work).filter(Work.has_source == True).count()
        works_with_aigc = self.db.query(Work).filter(Work.has_aigc_log == True).count()
        works_with_screenshots = self.db.query(Work).filter(Work.has_screenshots == True).count()
        works_flagged = self.db.query(Work).filter(Work.is_flagged == True).count()

        return {
            "total_teams": total_teams,
            "works_uploaded": works_uploaded,
            "works_parsed": works_parsed,
            "works_with_source": works_with_source,
            "works_with_aigc": works_with_aigc,
            "works_with_screenshots": works_with_screenshots,
            "works_flagged": works_flagged,
            "upload_progress": f"{works_uploaded}/{total_teams}"
        }
