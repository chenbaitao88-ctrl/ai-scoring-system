"""
任务书评分提示词模板
支持：
- 动态从数据库获取任务书
- 动态从数据库获取评审表维度
- 无任务书时的通用评分模式
- P1-2: Few-shot 示例注入（受 FEWSHOT 开关控制）
"""

from feature_flags import is_enabled

# 默认评分维度（用于初始化）
DEFAULT_DIMENSIONS = [
    {
        "name": "主题立意",
        "max_score": 20,
        "description": "1.思想性：主题真实有效，立意积极健康，具有清晰的用户或社会价值；\n2.原创性：解决方案新颖、巧妙，具备独特的创见。"
    },
    {
        "name": "产品表现力",
        "max_score": 30,
        "description": "1.技术性：产品架构完整，体系设计清晰，功能实现完整、稳定，程序逻辑严谨，代码算法准确，并具有一定的技术探索性；\n2.创新性：对传统场景，具有新颖思考，具有实际价值，设计创意巧妙，具有想象力，展现个性表现力；\n3.艺术性与用户体验：产品界面（UI）设计美观、协调，交互（UX）流程直观、流畅，设计风格和主题一致，体现以用户为中心的设计思想。"
    },
    {
        "name": "过程完整性",
        "max_score": 30,
        "description": "1.过程完整性：过程性文档等交付成果齐全、翔实、规范，完整体现0-1产品研发全流程；\n2.迭代性思维：从测试评估或AIGC交互中发现问题，有效改进迭代产品方案；\n3.AIGC交互日志：完整记录AIGC辅助实现产品研发的全过程，清晰地呈现学生与AI的互动轨迹。"
    },
    {
        "name": "AI素养",
        "max_score": 20,
        "description": "1.工具策略性：策略性地选择和使用AIGC工具以提升项目质量和效率；\n2.批判性思维：精准设计提示词，以专业视角引导大模型，确保其高效输出优质结果，AIGC交互日志体现对AI输出的深度分析、甄别与优化；\n3.社会伦理意识：正确认识AIGC的价值，负责任、合乎规范地使用AIGC工具；\n4.沟通与表达：路演讲解逻辑清晰、具有说服力，有效传达产品价值。"
    }
]

# 专家视角补充说明（基于2026年全国活动指南）
EXPERT_PERSPECTIVE = """
## 专家视角评价要点

### 核心理念
创意编程关注逻辑实体而非物理实体，核心是基于代码逻辑运行的交互系统。评价时应聚焦"作品四清"标准：
- 问题清：问题定义明确，用户情境清晰，成功标准可衡量
- 机制清：规则设计合理，算法逻辑自洽，能回答"灵魂三问"（规则为何这样定、规则如何运转、边界情况如何处理）
- 跑得清：主流程稳定运行，异常处理完善，演示过程流畅
- 过程清：需求文档、结构图、算法说明、迭代记录完整

### 典型问题警示
评价时需警惕三种典型问题作品：
1. 素材精美但逻辑简单：重形式轻内容，缺乏算法深度
2. 有演示无解释：功能可实现但无法说明原理，缺乏可解释性
3. 缺乏异常处理：主流程正常但边界情况崩溃，缺乏健壮性

### AI使用红线
作品应体现"可以辅助，不能替代"原则：
- 能解释核心规则：学生能清晰说明作品的核心逻辑和算法原理
- 能验证运行结果：能通过测试用例验证功能正确性
- 体现人类决策：关键的设计决策、规则制定应体现学生独立思考

### 能力证据要求
评价时应关注四类过程产出：
1. 选题卡：体现问题定义能力，包含用户情境和成功标准
2. 机制图：体现建模抽象能力，包含流程图、状态机等
3. 迭代记录：体现工程思维，记录bug修复、版本改进过程
4. 讲述稿：体现表达协作能力，能用"五句话模板"清晰表达（定坐标、亮核心、秀实现、点创意、谈反思）
"""

# 字段名映射（维度名 -> 数据库字段名）
DIMENSION_FIELD_MAP = {
    "主题立意": "theme",
    "产品表现力": "presentation",
    "过程完整性": "process",
    "AI素养": "ai_literacy"
}


# P1-3 (2026-05-27): 按锚点等级动态调整评分维度
LEVEL_DIMENSIONS = {
    "Lv0": {
        "dimensions": DEFAULT_DIMENSIONS,
        "extra_hint": "",
    },
    "Lv1": {
        "dimensions": [
            {"name": "主题立意", "max_score": 20, "description": DEFAULT_DIMENSIONS[0]["description"]},
            {"name": "产品表现力", "max_score": 30, "description": DEFAULT_DIMENSIONS[1]["description"]},
            {"name": "AI素养", "max_score": 20, "description": DEFAULT_DIMENSIONS[3]["description"]},
        ],
        "extra_hint": "\n【评审提示】本作品提交信息不完整，缺少过程性文档或AIGC交互记录。请在现有信息范围内评分，过程完整性维度不纳入评分。",
    },
    "Lv2": {
        "dimensions": [
            {"name": "主题立意", "max_score": 20, "description": DEFAULT_DIMENSIONS[0]["description"]},
            {"name": "产品表现力", "max_score": 30, "description": DEFAULT_DIMENSIONS[1]["description"]},
        ],
        "extra_hint": "\n【评审提示】本作品仅含代码描述或截图，缺少可执行源码。请仅对主题立意和产品表现力两个维度评分，其余维度标注为\"信息不足，无法评分\"。",
    },
    "Lv3": {
        "dimensions": [
            {"name": "主题立意", "max_score": 20, "description": DEFAULT_DIMENSIONS[0]["description"]},
        ],
        "extra_hint": "\n【评审提示】本作品信息严重不足（仅含AI对话链接或文字片段），请极度保守评分。仅对主题立意维度给出分数，其余维度直接给0分并标注\"信息不足\"。",
    },
}


def get_prompt_by_level(level: str) -> dict:
    """根据锚点等级获取评分维度和提示"""
    return LEVEL_DIMENSIONS.get(level, LEVEL_DIMENSIONS["Lv0"])


def build_scoring_prompt(
    task_book_content: str = None,
    dimensions: list = None,
    group_type: str = None,
    level: str = "Lv0"
) -> str:
    """构建评分提示词

    Args:
        task_book_content: 任务书内容（Markdown格式），为空则使用通用模式
        dimensions: 评分维度列表，为空则使用默认维度
        group_type: 组别（小学组/初中组），可选
        level: 锚点等级（Lv0/Lv1/Lv2/Lv3），控制评分维度

    Returns:
        完整的评分提示词
    """
    # P1-3: 按锚点等级调整维度
    level_config = get_prompt_by_level(level)
    if not dimensions:
        dimensions = level_config["dimensions"]
    extra_hint = level_config["extra_hint"]

    # 构建维度说明（包含详细评分标准）
    dimension_text = build_dimension_text(dimensions)

    # 构建JSON输出格式
    json_format = build_json_format(dimensions)

    # P1-2: Few-shot 示例文本
    few_shot_text = build_few_shot_text(group_type)

    # 基础提示词（含专家视角 + Few-shot）
    base_prompt = f"""你是一位专业的教育技术评审专家，负责对学生的创意编程作品进行评分。你熟悉2026年全国师生数字素养提升实践活动指南，具备计算思维教育专业视角。

{EXPERT_PERSPECTIVE}

评分维度与标准：
{dimension_text}
{few_shot_text}
请严格按照以上评分标准和专家视角要点，根据作品实际情况给出客观公正的评分。{extra_hint}

输出格式要求（JSON）：
```json
{json_format}
```

评分原则：
- 严格按照各维度的核心标准逐项评分
- 对照"作品四清"标准检查作品质量（问题清、机制清、跑得清、过程清）
- 警惕三种典型问题作品（素材精美但逻辑简单、有演示无解释、缺乏异常处理）
- 验证AI使用红线（能解释核心规则、能验证运行结果、体现人类决策）
- 评语要具体，指出作品亮点和可改进之处
- 避免空泛的表扬，给出有建设性的反馈
"""

    # 如果有任务书，添加任务书内容
    if task_book_content:
        group_text = f"\n当前评分组别：{group_type}" if group_type else ""
        return f"""{base_prompt}
{group_text}
任务书要求：
{task_book_content}

请严格按照以上任务书要求、评分标准和专家视角要点进行评分。"""

    # 无任务书时的通用模式
    return base_prompt


def build_dimension_text(dimensions: list) -> str:
    """构建维度说明文本（包含详细评分标准）"""
    lines = []
    for i, dim in enumerate(dimensions, 1):
        name = dim.get("name", f"维度{i}")
        max_score = dim.get("max_score", 0)
        description = dim.get("description", "")

        # 维度标题
        lines.append(f"## {i}. {name}（满分{max_score}分）")

        # 详细评分标准
        if description:
            lines.append(description)

        lines.append("")  # 空行分隔

    return "\n".join(lines)


def build_json_format(dimensions: list) -> str:
    """构建JSON输出格式"""
    lines = ["{"]

    for dim in dimensions:
        name = dim.get("name", "")
        max_score = dim.get("max_score", 0)

        # 获取字段名
        field_name = DIMENSION_FIELD_MAP.get(name, name.lower().replace(" ", "_"))

        lines.append(f'  "{field_name}_score": <0-{max_score}的数字>,')
        lines.append(f'  "{field_name}_comment": "<{name}评语，50-100字>",')

    lines.append('  "overall_comment": "<总评语，100-150字，总结亮点和改进建议>"')
    lines.append('  "defense_questions": ["问题1（聚焦作品核心机制，考察学生对作品逻辑的理解）", "问题2（聚焦算法原理，考察学生是否真正掌握代码逻辑）", "问题3（聚焦边界情况处理，考察程序的健壮性）", "问题4（聚焦AI工具使用策略，考察AI辅助的合理性）", "问题5（聚焦创新点论证，考察作品的独特价值）"]')
    lines.append("}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# P1-2: Few-shot 示例注入
# ─────────────────────────────────────────────
def build_few_shot_text(group_type: str = None) -> str:
    """构建 Few-shot 示例文本。

    Args:
        group_type: 组别（小学组/初中组），用于优先匹配同组别示例

    Returns:
        Few-shot 示例文本，FEWSHOT 开关关闭或示例为空时返回空字符串
    """
    if not is_enabled("FEWSHOT"):
        return ""

    from services.few_shot_examples import get_examples_by_group

    examples = get_examples_by_group(group_type)
    if not examples:
        return ""

    lines = ["=== 评分示例（仅供参考，展示不同质量作品的评分尺度） ===", ""]

    for i, ex in enumerate(examples, 1):
        level_label = {"high": "【高分示例】", "mid": "【中等示例】", "low": "【低分示例】"}.get(
            ex["level"], "【示例】"
        )
        lines.append(f"示例{i} {level_label}")
        lines.append(f"作品摘要：{ex['summary']}")
        lines.append(
            f"评分：主题{ex['theme_score']}/20，表现力{ex['presentation_score']}/30，"
            f"过程完整性{ex['process_score']}/30，AI素养{ex['ai_literacy_score']}/20，"
            f"总分{ex['total_score']}"
        )
        lines.append(f"主题评语：{ex['theme_comment']}")
        lines.append(f"表现力评语：{ex['presentation_comment']}")
        lines.append(f"过程评语：{ex['process_comment']}")
        lines.append(f"AI素养评语：{ex['ai_literacy_comment']}")
        lines.append(f"总评：{ex['overall_comment']}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("")

    return "\n".join(lines)


# ============ 兼容旧接口 ============
# 小学组任务书要求（已弃用，保留用于兼容）
PRIMARY_TASK = """
## 小学组任务：探秘千年古建

### 基础功能（必须完成）
1. **古建筑名片**（至少3处）
   - 展示中国古建筑（如故宫、长城、应县木塔等）
   - 每处名片包含：名称、图片、简介、建造年代、建筑特点

2. **古建故事**（动画反馈）
   - 选择一处古建筑，讲述它的历史故事或建造传说
   - 使用动画或交互方式呈现故事内容
   - 有明确的开始、发展和结尾

3. **知识小测验**（至少5道题）
   - 围绕中国古建筑知识设计题目
   - 包含选择题或判断题
   - 答题后有正确/错误反馈
   - 显示最终得分

### 创新功能（加分项）
- 搭建古城墙：用积木式搭建模拟城墙结构
- 小小建筑师：自由设计古建筑元素
- 古建找不同：对比两幅古建筑图片找差异

### 评分重点
- 主题立意：是否体现对中国传统文化的理解，是否有原创性
- 产品表现力：界面美观度、交互流畅性、动画效果
- 过程完整性：开发过程记录、迭代改进痕迹
- AI素养：AI工具使用策略、批判性思维
"""

# 初中组任务书要求（已弃用，保留用于兼容）
MIDDLE_TASK = """
## 初中组任务：古都数字博物馆

### 基础功能（必须完成）
1. **朝代时间轴**
   - 按时间顺序展示中国古代主要朝代
   - 每个朝代标注起止时间、都城、重要事件
   - 支持点击查看详情
   - 时间轴有动画效果

2. **文物鉴赏**（至少5件）
   - 展示不同朝代的代表性文物
   - 每件文物包含：名称、朝代、图片、介绍、收藏地
   - 支持放大查看细节
   - 有分类筛选功能

3. **知识闯关系统**
   - 设计至少3关历史知识闯关游戏
   - 每关包含3-5道题目
   - 有闯关进度和得分记录
   - 关卡难度递进

### 创新功能（加分项）
- 丝路探秘互动地图：展示丝绸之路沿线重要城市和文物
- 文物对比分析：对比不同朝代同类文物的特点
- 古都数据可视化：用图表展示历史数据（人口、疆域等）

### 评分重点
- 主题立意：历史准确性、文化深度、创新视角
- 产品表现力：信息架构、数据可视化、交互设计
- 过程完整性：需求分析、原型设计、迭代优化
- AI素养：AI辅助研究、内容验证、伦理意识
"""


def get_task_requirements(group_type: str) -> str:
    """获取任务书要求（兼容旧接口）"""
    if group_type == "小学组":
        return PRIMARY_TASK
    elif group_type == "初中组":
        return MIDDLE_TASK
    else:
        return "未知组别"


def get_scoring_prompt(group_type: str) -> str:
    """获取评分提示词（兼容旧接口）"""
    task = get_task_requirements(group_type)
    return build_scoring_prompt(
        task_book_content=task,
        dimensions=DEFAULT_DIMENSIONS,
        group_type=group_type
    )


def get_scoring_prompt_from_db(db, group_type: str = None, level: str = "Lv0") -> str:
    """从数据库获取评分提示词

    Args:
        db: 数据库会话
        group_type: 组别（可选）
        level: 锚点等级（Lv0/Lv1/Lv2/Lv3），控制评分维度

    Returns:
        完整的评分提示词
    """
    from database import TaskBook, ScoringForm
    from services.task_book_service import get_active_task_book
    from services.scoring_form_service import get_active_scoring_form, init_default_scoring_form

    # 获取激活的任务书
    task_book = get_active_task_book(db, group_type)
    task_content = task_book.content_md if task_book else None

    # 获取激活的评审表
    scoring_form = get_active_scoring_form(db)
    if not scoring_form:
        scoring_form = init_default_scoring_form(db)

    # P1-3: 非 Lv0 时，使用 level 对应的维度，忽略数据库评审表的维度
    if level == "Lv0":
        dimensions = scoring_form.dimensions if scoring_form else None
    else:
        dimensions = None  # 让 build_scoring_prompt 使用 level_config 的维度

    return build_scoring_prompt(
        task_book_content=task_content,
        dimensions=dimensions,
        group_type=group_type,
        level=level
    )


# ============ Phase 1.3: 双轨评分 Prompt ============
# 混合模式：AI 只评 4 个作品维度（代码质量、材料完整性、AIGC 规范性、主题契合）
# 纯AI模式：AI 评全部 8 个维度（4 AI + 4 推断）

# 混合模式 AI 维度定义
DUAL_TRACK_AI_DIMENSIONS = [
    {
        "name": "主题基础契合",
        "field": "ai_theme_score",
        "max_score": 5,
        "description": "1.作品主题与任务书/比赛要求的契合度；\n2.代码/文档中主题关键词的明确程度；\n3.主题表达的完整性和准确性。",
    },
    {
        "name": "代码质量",
        "field": "ai_code_quality_score",
        "max_score": 15,
        "description": "1.代码结构清晰，模块化设计合理；\n2.注释完整，命名规范，可读性好；\n3.算法逻辑正确，异常处理完善；\n4.代码复杂度适中，无冗余重复。",
    },
    {
        "name": "材料完整性",
        "field": "ai_completeness_score",
        "max_score": 10,
        "description": "1.必需文件齐全（源码、截图、文档、README等）；\n2.截图能展示作品核心功能和界面；\n3.README/说明文档清晰描述了作品功能和使用方法；\n4.作品包结构规范，无无关文件。",
    },
    {
        "name": "AIGC 规范性",
        "field": "ai_aigc_score",
        "max_score": 10,
        "description": "1.是否提交了 AIGC 交互日志/使用说明；\n2.AIGC 使用是否符合规范（标注 AI 辅助部分）；\n3.是否存在过度依赖 AI 的迹象（核心代码/创意是否由学生自主完成）；\n4.AIGC 使用策略是否合理。",
    },
]

# 纯AI模式推断维度定义（基于作品材料推断人工维度）
DUAL_TRACK_INFER_DIMENSIONS = [
    {
        "name": "产品表现力（推断）",
        "field": "human_presentation_score",
        "max_score": 25,
        "description": "【推断分】基于作品截图、代码结构、功能完整性，推断作品的交互体验、视觉设计和功能稳定性。注意：你未运行作品，此分数基于材料推断，可能不准确。",
    },
    {
        "name": "创意深度（推断）",
        "field": "human_creativity_score",
        "max_score": 15,
        "description": "【推断分】基于作品描述、功能设计和主题选择，推断作品的创新性、独特性和教育价值。注意：此分数基于材料推断。",
    },
    {
        "name": "过程深度（推断）",
        "field": "human_process_score",
        "max_score": 15,
        "description": "【推断分】基于 AIGC 日志、迭代记录和文档质量，推断学生的创作过程、问题解决能力和迭代思维。注意：此分数基于材料推断。",
    },
    {
        "name": "现场表现（推断）",
        "field": "human_performance_score",
        "max_score": 5,
        "description": "【推断分】基于文档质量和表达完整性，推断学生的演讲/演示能力。注意：此分数基于材料推断，准确性最低。",
    },
]

DUAL_TRACK_FIELD_MAP = {
    "主题基础契合": "ai_theme_score",
    "代码质量": "ai_code_quality_score",
    "材料完整性": "ai_completeness_score",
    "AIGC 规范性": "ai_aigc_score",
    "产品表现力（推断）": "human_presentation_score",
    "创意深度（推断）": "human_creativity_score",
    "过程深度（推断）": "human_process_score",
    "现场表现（推断）": "human_performance_score",
}


def build_dual_track_prompt(
    task_book_content: str = None,
    group_type: str = None,
    scoring_mode: str = "mixed",
    level: str = "Lv0"
) -> str:
    """构建双轨评分 Prompt

    Args:
        task_book_content: 任务书内容
        group_type: 组别
        scoring_mode: mixed（混合，只评4维）/ ai_only（纯AI，评8维）
        level: 锚点等级

    Returns:
        完整的双轨评分 Prompt
    """
    if scoring_mode == "mixed":
        dimensions = DUAL_TRACK_AI_DIMENSIONS
        mode_hint = "【混合模式】你只评价作品客观维度（代码质量、材料完整性、AIGC规范性、主题契合），合计40分。产品表现力、创意深度、现场表现等主观维度由现场评委评价，你不输出这些分数。"
    else:
        dimensions = DUAL_TRACK_AI_DIMENSIONS + DUAL_TRACK_INFER_DIMENSIONS
        mode_hint = "【纯AI模式】你需评价全部8个维度（4个作品客观维度 + 4个主观推断维度），合计100分。其中主观推断维度请基于作品材料保守评分，并在评语中标注'推断分，准确性有限'。"

    # 锚点等级提示
    level_hint = ""
    if level == "Lv1":
        level_hint = "\n【评审提示】本作品提交信息不完整，缺少过程性文档或AIGC交互记录。请在现有信息范围内评分。"
    elif level == "Lv2":
        level_hint = "\n【评审提示】本作品仅含代码描述或截图，缺少可执行源码。请仅对可评价的维度评分，无法评价的维度标注为'信息不足'。"
    elif level == "Lv3":
        level_hint = "\n【评审提示】本作品信息严重不足（仅含AI对话链接或文字片段），请极度保守评分。"

    # 构建维度说明
    dim_lines = []
    for i, dim in enumerate(dimensions, 1):
        dim_lines.append(f"### {i}. {dim['name']}（满分{dim['max_score']}分）")
        dim_lines.append(dim['description'])
        dim_lines.append("")

    dim_text = "\n".join(dim_lines)

    # 构建 JSON 输出格式
    json_lines = ["{"]
    for dim in dimensions:
        field = dim['field']
        max_score = dim['max_score']
        json_lines.append(f'  "{field}": <0-{max_score}的整数>,')
        json_lines.append(f'  "{field}_comment": "<{dim["name"]}评语，30-50字>",')
    json_lines.append('  "overall_comment": "<总评语，80-120字，总结亮点、问题和改进建议>"')
    if scoring_mode == "ai_only":
        json_lines.append('  "inference_note": "<说明：主观推断维度的准确性和局限性>"')
    json_lines.append('  "defense_questions": ["问题1", "问题2", "问题3"]')
    json_lines.append("}")

    json_format = "\n".join(json_lines)

    prompt = f"""你是一位专业的教育技术评审专家，负责对中小学生创意编程作品进行评分。

你的评价聚焦于作品的客观可量化维度。你具备代码分析、材料审查和 AIGC 检测能力。

{mode_hint}
{level_hint}

评分维度与标准：
{dim_text}

输出格式要求（JSON）：
```json
{json_format}
```

评分原则：
- 严格按各维度标准逐项评分，给出具体依据
- 代码质量维度：关注结构、注释、算法正确性、异常处理
- 材料完整性维度：检查必需文件、截图质量、README清晰度
- AIGC规范性维度：检查是否提交日志、使用策略是否合理
- 主题契合维度：检查代码/文档中主题表达是否明确
- 评语要具体，指出作品亮点和可改进之处
- 避免空泛表扬，给出有建设性的反馈
"""

    if task_book_content:
        group_text = f"\n当前评分组别：{group_type}" if group_type else ""
        prompt += f"""
{group_text}
任务书要求：
{task_book_content}

请严格按照任务书要求和评分标准进行评分。"""

    return prompt
