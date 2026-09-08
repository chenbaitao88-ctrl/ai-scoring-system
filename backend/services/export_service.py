"""
导出服务层
- Excel / Word 文件生成
- 数据查询与汇总
- 备份管理
"""
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import json

from sqlalchemy.orm import Session
from config import DATA_DIR, BACKUPS_DIR, get_active_competition
from database import Team, MachineScore, HumanScore, FinalScore, Work
from utils.json_fields import json_list

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)



def get_final_results(db: Session) -> list:
    """获取所有队伍的综合评分结果"""
    teams = db.query(Team).all()
    results = []

    for team in teams:
        machine = db.query(MachineScore).filter(
            MachineScore.team_id == team.id,
            MachineScore.is_completed == True
        ).first()

        human_scores = db.query(HumanScore).filter(HumanScore.team_id == team.id).all()

        final = db.query(FinalScore).filter(FinalScore.team_id == team.id).first()
        work = db.query(Work).filter(Work.team_id == team.id).first()

        human_avg = None
        human_count = 0
        if human_scores:
            human_avg = sum(h.total_score for h in human_scores) / len(human_scores)
            human_count = len(human_scores)

        # 提取选手1姓名
        members = team.members or []
        member1_name = ""
        member2_name = ""
        if isinstance(members, list) and len(members) > 0:
            member1_name = members[0].get("name", "") if isinstance(members[0], dict) else ""
        if isinstance(members, list) and len(members) > 1:
            member2_name = members[1].get("name", "") if isinstance(members[1], dict) else ""

        results.append({
            "id": team.id,
            "short_code": team.short_code,
            "team_name": team.team_name,
            "school": team.school,
            "district": team.district,
            "group_type": team.group_type,
            "judge_group": team.judge_group,
            "member1_name": member1_name,
            "member2_name": member2_name,
            "machine_score": machine.total_score if machine else None,
            # P1-9 (2026-05-26): 新增校准分、锚点等级、置信度、Flag
            "calibrated_score": machine.calibrated_score if machine else None,
            "anchor_level": work.anchor_level if work else "Lv0",
            "confidence": machine.confidence if machine else "UNKNOWN",
            "flags": json_list(machine.flags) if machine else [],
            "machine_details": {
                "theme": machine.theme_score if machine else None,
                "presentation": machine.presentation_score if machine else None,
                "process": machine.process_score if machine else None,
                "ai_literacy": machine.ai_literacy_score if machine else None,
            } if machine else None,
            "defense_questions": machine.defense_questions if machine else None,
            "machine_comments": {
                "theme": machine.theme_comment if machine else None,
                "presentation": machine.presentation_comment if machine else None,
                "process": machine.process_comment if machine else None,
                "ai_literacy": machine.ai_literacy_comment if machine else None,
                "overall": machine.overall_comment if machine else None,
            } if machine else None,
            "human_score_avg": round(human_avg, 2) if human_avg else None,
            "human_judge_count": human_count,
            "human_scores": human_scores,
            "final_score": final.final_score if final else None,
            "ranking": final.ranking if final else None,
            "plagiarism_flag": work.plagiarism_flag if work else False,
        })

    # 按最终分数降序排列
    results.sort(key=lambda x: x["final_score"] or 0, reverse=True)
    return results



def _add_horizontal_line(doc):
    """添加水平分割线"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pPr = p._element.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '12')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), '4472C4')
    pBdr.append(bottom)
    pPr.append(pBdr)
    p.paragraph_format.space_after = Pt(6)



def _format_info_table(table):
    """格式化基本信息表格"""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(10)
                    run.font.name = "微软雅黑"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # 表头样式（第一列）
    for row in table.rows:
        for para in row.cells[0].paragraphs:
            for run in para.runs:
                run.font.bold = True



def _add_section_header(doc, text: str):
    """添加章节标题"""
    from docx.shared import Pt
    from docx.oxml.ns import qn

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.name = "微软雅黑"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")



def _add_subsection_header(doc, text: str):
    """添加子章节标题"""
    from docx.shared import Pt
    from docx.oxml.ns import qn

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    run.font.size = Pt(12)
    run.font.bold = True
    run.font.name = "微软雅黑"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")



def _add_page_header(doc, text: str):
    """添加页眉标题"""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.font.size = Pt(16)
    run.font.bold = True
    run.font.name = "微软雅黑"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    p.paragraph_format.space_after = Pt(12)

    _add_horizontal_line(doc)



def _format_ranking_table(table):
    """格式化排名表格"""
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    # 设置列宽
    widths = [Cm(1), Cm(1.5), Cm(2.5), Cm(2.5), Cm(2.5), Cm(2), Cm(1.5), Cm(2.5), Cm(1.5), Cm(1.5)]
    for i, width in enumerate(widths):
        for row in table.rows:
            row.cells[i].width = width

    # 格式化单元格
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in para.runs:
                    run.font.size = Pt(9)
                    run.font.name = "微软雅黑"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")



def _add_ranking_table(doc, teams: list):
    """添加排名表格到Word文档"""
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt
    if not teams:
        return

    table = doc.add_table(rows=1, cols=10)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 表头
    header_cells = table.rows[0].cells
    headers = ["排名", "编号", "选手", "团队", "学校", "AI评分", "锚点", "Flag", "人工分", "综合分"]
    for i, h in enumerate(headers):
        header_cells[i].text = h
        run = header_cells[i].paragraphs[0].runs[0]
        run.font.bold = True
        run.font.size = Pt(10)

    # 数据行
    for team in teams:
        # Flag 排序：MUST_REVIEW 在前，截断 >2
        flags = team.get("flags") or []
        flags_sorted = sorted(flags, key=lambda f: 0 if f.get("level") == "MUST_REVIEW" else 1)
        if len(flags_sorted) > 2:
            flag_text = " | ".join(f["code"] for f in flags_sorted[:2]) + " ..."
        else:
            flag_text = " | ".join(f["code"] for f in flags_sorted) if flags_sorted else "-"

        # AI 评分双行文本
        machine = team["machine_score"]
        calibrated = team.get("calibrated_score")
        if calibrated is not None and calibrated > 0 and calibrated != machine:
            ai_text = f"原始: {machine:.1f}\n校准: {calibrated:.1f}"
        else:
            ai_text = f"{machine:.1f}" if machine else "-"

        row_cells = table.add_row().cells
        row_cells[0].text = str(team["ranking"] or "-")
        row_cells[1].text = team["short_code"] or ""
        row_cells[2].text = team["member1_name"] or ""
        row_cells[3].text = team["team_name"] or ""
        row_cells[4].text = team["school"] or ""
        row_cells[5].text = ai_text
        row_cells[6].text = team.get("anchor_level") or "Lv0"
        row_cells[7].text = flag_text
        row_cells[8].text = f"{team['human_score_avg']:.1f}" if team["human_score_avg"] else "-"
        row_cells[9].text = f"{team['final_score']:.2f}" if team["final_score"] else "-"

        # MUST_REVIEW 整行浅红背景
        has_must = any(f.get("level") == "MUST_REVIEW" for f in flags)
        if has_must:
            from docx.oxml.ns import qn
            from docx.oxml import OxmlElement
            for cell in row_cells:
                tc = cell._tc
                tcPr = tc.get_or_add_tcPr()
                shading = OxmlElement('w:shd')
                shading.set(qn('w:fill'), 'FFDDDD')
                tcPr.append(shading)

    _format_ranking_table(table)
    doc.add_paragraph()



def _add_detailed_score_section(doc, team: dict):
    """添加单个队伍的详细评分信息"""
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn

    # 队伍标题卡片
    card_para = doc.add_paragraph()
    card_para.paragraph_format.space_before = Pt(6)
    card_para.paragraph_format.space_after = Pt(6)
    card_para.paragraph_format.left_indent = Cm(0.3)

    # 编号和团队名称
    code_run = card_para.add_run(f"【{team['short_code']}】")
    code_run.font.size = Pt(12)
    code_run.font.bold = True
    code_run.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)
    code_run.font.name = "微软雅黑"
    code_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    name_run = card_para.add_run(f" {team['team_name']}")
    name_run.font.size = Pt(11)
    name_run.font.bold = True
    name_run.font.name = "微软雅黑"
    name_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 基本信息（紧凑横排）
    info_para = doc.add_paragraph()
    info_para.paragraph_format.left_indent = Cm(0.5)
    info_para.paragraph_format.space_after = Pt(4)

    member = team['member1_name'] or '未填写'
    school = team['school'] or '未填写'
    group = team['group_type'] or '未填写'

    info_para.add_run(f"参赛选手：{member}").font.size = Pt(10)
    info_para.add_run("　　").font.size = Pt(10)
    info_para.add_run(f"学校：{school}").font.size = Pt(10)
    info_para.add_run("　　").font.size = Pt(10)
    info_para.add_run(f"组别：{group}").font.size = Pt(10)

    # P1-9 (2026-05-26): 锚点等级 + 置信度 + Flag
    anchor = team.get("anchor_level") or "Lv0"
    confidence = team.get("confidence") or "UNKNOWN"
    flags = team.get("flags") or []

    meta_para = doc.add_paragraph()
    meta_para.paragraph_format.left_indent = Cm(0.5)
    meta_para.paragraph_format.space_after = Pt(4)

    anchor_label = meta_para.add_run(f"锚点等级：{anchor} ({confidence})")
    anchor_label.font.size = Pt(10)
    anchor_label.font.bold = True
    anchor_label.font.name = "微软雅黑"
    anchor_label._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    # 锚点等级颜色
    if anchor == "Lv0":
        anchor_label.font.color.rgb = RGBColor(0x00, 0x80, 0x00)
    elif anchor == "Lv1":
        anchor_label.font.color.rgb = RGBColor(0x00, 0x66, 0xCC)
    elif anchor == "Lv2":
        anchor_label.font.color.rgb = RGBColor(0xCC, 0x66, 0x00)
    else:
        anchor_label.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)

    if flags:
        meta_para.add_run("　　").font.size = Pt(10)
        flags_sorted = sorted(flags, key=lambda f: 0 if f.get("level") == "MUST_REVIEW" else 1)
        flag_label = meta_para.add_run("Flag：")
        flag_label.font.size = Pt(10)
        flag_label.font.name = "微软雅黑"
        flag_label._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        for i, f in enumerate(flags_sorted):
            if i > 0:
                sep = meta_para.add_run(" | ")
                sep.font.size = Pt(10)
            f_run = meta_para.add_run(f"{f['code']}")
            f_run.font.size = Pt(10)
            f_run.font.bold = True
            f_run.font.name = "微软雅黑"
            f_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
            if f.get("level") == "MUST_REVIEW":
                f_run.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
            else:
                f_run.font.color.rgb = RGBColor(0x99, 0x66, 0x00)

    # MUST_REVIEW 红色警告条
    must_flags = [f for f in flags if f.get("level") == "MUST_REVIEW"]
    if must_flags:
        warn_para = doc.add_paragraph()
        warn_para.paragraph_format.left_indent = Cm(0.5)
        warn_para.paragraph_format.space_after = Pt(4)
        warn_icon = warn_para.add_run("⚠ ")
        warn_icon.font.size = Pt(11)
        warn_text = warn_para.add_run("硬规则标记：" + "；".join(f"{f['code']} — {f['msg']}" for f in must_flags))
        warn_text.font.size = Pt(10)
        warn_text.font.bold = True
        warn_text.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
        warn_text.font.name = "微软雅黑"
        warn_text._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 得分表格
    table = doc.add_table(rows=5, cols=5)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 表头
    headers = ["评分维度", "满分", "机器评分", "人工评分", "综合得分"]
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        cell.paragraphs[0].runs[0].font.bold = True
        cell.paragraphs[0].runs[0].font.size = Pt(10)

    # 数据行
    dimensions = [
        ("主题立意", "theme", 20),
        ("产品表现力", "presentation", 30),
        ("过程完整性", "process", 30),
        ("AI素养", "ai_literacy", 20),
    ]
    for row_idx, (name, key, max_score) in enumerate(dimensions, 1):
        table.rows[row_idx].cells[0].text = name
        table.rows[row_idx].cells[1].text = str(max_score)
        machine_val = team["machine_details"].get(key) if team["machine_details"] else None
        table.rows[row_idx].cells[2].text = f"{machine_val:.1f}" if machine_val else "-"
        table.rows[row_idx].cells[3].text = "-"  # 人工评分
        table.rows[row_idx].cells[4].text = f"{machine_val:.1f}" if machine_val else "-"

    # 格式化得分表格
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in para.runs:
                    run.font.size = Pt(9)
                    run.font.name = "微软雅黑"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    doc.add_paragraph()

    # P1-9 (2026-05-26): AI 评分总览
    summary_para = doc.add_paragraph()
    summary_para.paragraph_format.left_indent = Cm(0.5)
    summary_para.paragraph_format.space_after = Pt(6)

    machine_total = team.get("machine_score")
    calibrated_total = team.get("calibrated_score")
    anchor_lv = team.get("anchor_level") or "Lv0"
    conf = team.get("confidence") or "UNKNOWN"

    s_label = summary_para.add_run("▶ AI评分总览：")
    s_label.font.size = Pt(11)
    s_label.font.bold = True
    s_label.font.name = "微软雅黑"
    s_label._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    if machine_total is not None:
        s_text = summary_para.add_run(f" 原始总分 {machine_total:.1f} 分")
        s_text.font.size = Pt(10)
        s_text.font.name = "微软雅黑"
        s_text._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    if calibrated_total is not None and calibrated_total > 0 and calibrated_total != machine_total:
        s_cal = summary_para.add_run(f"　校准总分 {calibrated_total:.1f} 分")
        s_cal.font.size = Pt(10)
        s_cal.font.bold = True
        s_cal.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)
        s_cal.font.name = "微软雅黑"
        s_cal._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    s_anchor = summary_para.add_run(f"　锚点 {anchor_lv} ({conf})")
    s_anchor.font.size = Pt(10)
    s_anchor.font.name = "微软雅黑"
    s_anchor._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # AI评语
    if team["machine_comments"]:
        comments = team["machine_comments"]

        # 评语标题
        comment_title = doc.add_paragraph()
        comment_title.paragraph_format.space_before = Pt(4)
        comment_title.paragraph_format.space_after = Pt(4)
        ct_run = comment_title.add_run("▶ AI评分评语")
        ct_run.font.size = Pt(11)
        ct_run.font.bold = True
        ct_run.font.name = "微软雅黑"
        ct_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

        # 各项评语
        comment_items = [
            ("主题立意", comments.get("theme")),
            ("产品表现力", comments.get("presentation")),
            ("过程完整性", comments.get("process")),
            ("AI素养", comments.get("ai_literacy")),
        ]

        for dim_name, dim_comment in comment_items:
            if dim_comment:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.5)
                p.paragraph_format.space_after = Pt(3)

                label = p.add_run(f"【{dim_name}】")
                label.font.size = Pt(10)
                label.font.bold = True
                label.font.name = "微软雅黑"
                label._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
                label.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)

                content = p.add_run(f" {dim_comment}")
                content.font.size = Pt(10)
                content.font.name = "微软雅黑"
                content._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

        # 总评
        if comments.get("overall"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)

            label = p.add_run("【综合评价】")
            label.font.size = Pt(10)
            label.font.bold = True
            label.font.name = "微软雅黑"
            label._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
            label.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

            content = p.add_run(f" {comments['overall']}")
            content.font.size = Pt(10)
            content.font.name = "微软雅黑"
            content._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

        # AI生成答辩题目
        if team.get("defense_questions"):
            q_title = doc.add_paragraph()
            q_title.paragraph_format.space_before = Pt(6)
            q_title.paragraph_format.space_after = Pt(4)
            qt_run = q_title.add_run("▶ AI生成答辩题目")
            qt_run.font.size = Pt(11)
            qt_run.font.bold = True
            qt_run.font.name = "微软雅黑"
            qt_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
            qt_run.font.color.rgb = RGBColor(0x00, 0x70, 0x00)

            questions = team["defense_questions"]
            if isinstance(questions, list):
                for i, q in enumerate(questions, 1):
                    if q:
                        qp = doc.add_paragraph()
                        qp.paragraph_format.left_indent = Cm(0.5)
                        qp.paragraph_format.space_after = Pt(3)
                        num_run = qp.add_run(f"  {i}. ")
                        num_run.font.size = Pt(10)
                        num_run.font.bold = True
                        num_run.font.name = "微软雅黑"
                        num_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
                        content_run = qp.add_run(q)
                        content_run.font.size = Pt(10)
                        content_run.font.name = "微软雅黑"
                        content_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 分隔线
    sep_para = doc.add_paragraph()
    sep_para.paragraph_format.space_before = Pt(8)
    sep_para.paragraph_format.space_after = Pt(8)
    sep_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    from docx.oxml import OxmlElement
    pPr = sep_para._element.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), 'AAAAAA')
    pBdr.append(bottom)
    pPr.append(pBdr)



def _format_dimension_table(table):
    """格式化维度说明表格"""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in para.runs:
                    run.font.size = Pt(10)
                    run.font.name = "微软雅黑"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 表头加粗
    for cell in table.rows[0].cells:
        for para in cell.paragraphs:
            for run in para.runs:
                run.font.bold = True



def build_excel_export(db: Session):
    """生成评分汇总 Excel，返回 (文件路径, 文件名)"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    results = get_final_results(db)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "评分汇总"

    # 表头样式
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # 表头
    headers = [
        "排名", "短编号", "选手1姓名", "团队名称", "学校", "区县", "组别", "评审组",
        "机器评分", "AI校准分", "主题", "表现力", "过程", "AI素养",
        "人工均分", "评委数",
        "锚点等级", "Flag",
        "综合得分", "抄袭标记"
    ]
    ws.append(headers)
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = thin_border

    # 数据行
    row_fill_1 = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")
    row_fill_2 = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    for idx, r in enumerate(results, 1):
        row_num = idx + 1
        # Flag 排序截断
        flags = r.get("flags") or []
        flags_sorted = sorted(flags, key=lambda f: 0 if f.get("level") == "MUST_REVIEW" else 1)
        flag_text = " | ".join(f["code"] for f in flags_sorted[:2]) + (" ..." if len(flags_sorted) > 2 else "") if flags_sorted else ""

        ws.append([
            r["ranking"] or "-",
            r["short_code"] or "",
            r["member1_name"] or "",
            r["team_name"] or "",
            r["school"] or "",
            r["district"] or "",
            r["group_type"] or "",
            f"第{r['judge_group']}组" if r["judge_group"] else "-",
            r["machine_score"] or "-",
            r["calibrated_score"] if r.get("calibrated_score") is not None else "-",
            r["machine_details"]["theme"] if r["machine_details"] else "-",
            r["machine_details"]["presentation"] if r["machine_details"] else "-",
            r["machine_details"]["process"] if r["machine_details"] else "-",
            r["machine_details"]["ai_literacy"] if r["machine_details"] else "-",
            r["human_score_avg"] or "-",
            r["human_judge_count"] or 0,
            r.get("anchor_level") or "Lv0",
            flag_text,
            r["final_score"] or "-",
            "疑似抄袭" if r["plagiarism_flag"] else "",
        ])

        fill = row_fill_1 if idx % 2 == 1 else row_fill_2
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=row_num, column=col)
            cell.border = thin_border
            cell.fill = fill
            if col == 1:  # 排名
                cell.alignment = Alignment(horizontal="center")
            elif col in [9, 10, 11, 12, 13, 14, 15, 16, 17, 19]:  # 分数列 + 锚点等级
                cell.alignment = Alignment(horizontal="center")

    # 列宽自适应
    column_widths = [8, 10, 12, 20, 20, 12, 10, 10, 10, 10, 8, 8, 8, 8, 10, 8, 8, 12, 10, 10]
    for i, width in enumerate(column_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

    # 冻结首行
    ws.freeze_panes = "A2"

    # 保存
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"评分汇总表_{timestamp}.xlsx"
    export_path = DATA_DIR / filename
    ensure_dir(DATA_DIR)
    wb.save(export_path)

    return export_path, filename

def build_word_export(db: Session):
    """生成评分汇总 Word，返回 (文件路径, 文件名)"""
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor, Twips
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    results = get_final_results(db)

    # GENERIC: 获取当前活跃赛事名称
    comp = get_active_competition(db)
    competition_name = comp.name if comp else "创意编程评分系统"

    doc = Document()

    # ========== 页面设置 ==========
    section = doc.sections[0]
    section.page_width = Cm(21)  # A4竖版
    section.page_height = Cm(29.7)
    section.left_margin = Cm(2)
    section.right_margin = Cm(2)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    # ========== 标题样式 ==========
    # 主标题
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_para.add_run(competition_name)
    title_run.font.size = Pt(18)
    title_run.font.bold = True
    title_run.font.name = "微软雅黑"
    title_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 副标题
    subtitle_para = doc.add_paragraph()
    subtitle_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle_run = subtitle_para.add_run("创意编程赛项评分汇总表")
    subtitle_run.font.size = Pt(16)
    subtitle_run.font.bold = True
    subtitle_run.font.name = "微软雅黑"
    subtitle_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    subtitle_para.space_after = Pt(12)

    # 分割线
    _add_horizontal_line(doc)

    # ========== 基本信息表 ==========
    info_table = doc.add_table(rows=3, cols=4)
    info_table.style = "Table Grid"
    info_table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 设置列宽
    for row in info_table.rows:
        row.cells[0].width = Cm(4)
        row.cells[1].width = Cm(6)
        row.cells[2].width = Cm(4)
        row.cells[3].width = Cm(6)

    # 第一行
    info_table.cell(0, 0).text = "赛事名称"
    info_table.cell(0, 1).text = competition_name
    info_table.cell(0, 2).text = "赛项"
    info_table.cell(0, 3).text = "创意编程"

    # 第二行
    info_table.cell(1, 0).text = "导出时间"
    info_table.cell(1, 1).text = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    info_table.cell(1, 2).text = "参赛队伍"
    info_table.cell(1, 3).text = f"{len(results)} 支"

    # 第三行
    info_table.cell(2, 0).text = "有效评分"
    scored_count = sum(1 for r in results if r['final_score'] is not None)
    info_table.cell(2, 1).text = f"{scored_count} 支"
    info_table.cell(2, 2).text = "评分模型"
    info_table.cell(2, 3).text = "AI机器评分 + 评委人工评分"

    # 格式化基本信息表格
    _format_info_table(info_table)

    doc.add_paragraph()

    # ========== 小学组排名 ==========
    primary_teams = [r for r in results if r["group_type"] in ("小学", "小学组") and r["final_score"] is not None]
    if primary_teams:
        _add_section_header(doc, f"一、小学组排名（共{len(primary_teams)}支）")
        _add_ranking_table(doc, primary_teams[:20])

    # ========== 初中组排名 ==========
    middle_teams = [r for r in results if r["group_type"] in ("初中", "初中组") and r["final_score"] is not None]
    if middle_teams:
        _add_section_header(doc, f"二、初中组排名（共{len(middle_teams)}支）")
        _add_ranking_table(doc, middle_teams[:20])

    # ========== 异常情况 ==========
    flagged = [r for r in results if r["plagiarism_flag"]]
    if flagged:
        _add_section_header(doc, "三、异常情况记录")
        for r in flagged:
            p = doc.add_paragraph()
            p.add_run(f"▶ {r['short_code']} {r['team_name']}（{r['school']}）— 疑似抄袭")
            p.paragraph_format.left_indent = Cm(0.5)

    # ========== 详细评分（分页） ==========
    doc.add_page_break()
    _add_page_header(doc, "详细评分记录")

    if primary_teams:
        # 按编号排序
        primary_teams_sorted = sorted(primary_teams, key=lambda x: x.get("short_code", ""))
        _add_subsection_header(doc, f"（一）小学组详细评分（共{len(primary_teams)}支）")
        for team in primary_teams_sorted:
            _add_detailed_score_section(doc, team)

    if middle_teams:
        # 按编号排序
        middle_teams_sorted = sorted(middle_teams, key=lambda x: x.get("short_code", ""))
        _add_subsection_header(doc, f"（二）初中组详细评分（共{len(middle_teams)}支）")
        for team in middle_teams_sorted:
            _add_detailed_score_section(doc, team)

    # ========== 评分说明 ==========
    doc.add_page_break()
    _add_page_header(doc, "评分维度说明")

    dim_table = doc.add_table(rows=5, cols=3)
    dim_table.style = "Table Grid"
    dim_table.alignment = WD_TABLE_ALIGNMENT.CENTER

    headers = ["评分维度", "满分值", "评分说明"]
    for i, h in enumerate(headers):
        dim_table.rows[0].cells[i].text = h
        dim_table.rows[0].cells[i].paragraphs[0].runs[0].font.bold = True

    dim_data = [
        ("主题立意", "20分", "作品的原创性、实用价值、问题定义清晰度"),
        ("产品表现力", "30分", "代码结构、界面交互、用户体验、功能完整性"),
        ("过程完整性", "30分", "选题卡、机制图、迭代记录、AIGC使用证据"),
        ("AI素养", "20分", "AI工具使用策略、批判性思维、人机协作能力"),
    ]
    for i, (dim, score, desc) in enumerate(dim_data, 1):
        dim_table.rows[i].cells[0].text = dim
        dim_table.rows[i].cells[1].text = score
        dim_table.rows[i].cells[2].text = desc

    _format_dimension_table(dim_table)

    # 签章区
    doc.add_paragraph()
    doc.add_paragraph()
    sign_table = doc.add_table(rows=1, cols=3)
    sign_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    sign_table.rows[0].cells[0].text = "评委签字：___________"
    sign_table.rows[0].cells[1].text = ""
    sign_table.rows[0].cells[2].text = "日期：______年______月______日"

    # 保存
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"评分汇总表_{timestamp}.docx"
    export_path = DATA_DIR / filename
    ensure_dir(DATA_DIR)
    doc.save(export_path)

    return export_path, filename

def get_score_summary_dict(db: Session) -> dict:
    """获取评分汇总数据（前端展示用）"""
    results = get_final_results(db)

    total = len(results)
    with_final = sum(1 for r in results if r["final_score"] is not None)
    with_machine = sum(1 for r in results if r["machine_score"] is not None)
    with_human = sum(1 for r in results if r["human_score_avg"] is not None)
    flagged = sum(1 for r in results if r["plagiarism_flag"])

    primary = [r for r in results if r["group_type"] in ("小学", "小学组")]
    middle = [r for r in results if r["group_type"] in ("初中", "初中组")]

    # 各分数段统计
    score_ranges = {
        "90-100": 0,
        "80-89": 0,
        "70-79": 0,
        "60-69": 0,
        "60以下": 0,
    }
    for r in results:
        if r["final_score"] is not None:
            s = r["final_score"]
            if s >= 90:
                score_ranges["90-100"] += 1
            elif s >= 80:
                score_ranges["80-89"] += 1
            elif s >= 70:
                score_ranges["70-79"] += 1
            elif s >= 60:
                score_ranges["60-69"] += 1
            else:
                score_ranges["60以下"] += 1

    return {
        "total_teams": total,
        "with_final_score": with_final,
        "with_machine_score": with_machine,
        "with_human_score": with_human,
        "flagged_count": flagged,
        "primary_count": len(primary),
        "middle_count": len(middle),
        "score_ranges": score_ranges,
        "results": results,
    }


def create_backup() -> dict:
    """备份数据库和上传的文件"""
    ensure_dir(BACKUPS_DIR)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{timestamp}"
    backup_dir = BACKUPS_DIR / backup_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 备份数据库
    db_path = DATA_DIR / "teams.db"
    if db_path.exists():
        shutil.copy2(db_path, backup_dir / "teams.db")

    # 备份上传的works
    works_dir = DATA_DIR / "works"
    if works_dir.exists():
        shutil.copytree(works_dir, backup_dir / "works")

    # 创建压缩包
    archive_path = BACKUPS_DIR / f"{backup_name}.zip"
    shutil.make_archive(
        base_dir=backup_dir,
        base_name=str(backup_dir),
        format="zip",
        root_dir=BACKUPS_DIR
    )

    # 清理未压缩的文件夹
    shutil.rmtree(backup_dir)

    return {
        "message": "备份成功",
        "backup_file": f"{backup_name}.zip",
        "path": str(archive_path),
    }
