"""
P1-2: Few-shot 评分示例
基于金标准数据集（30件）精选的6件示例，用于校准 LLM 评分尺度。

选取标准：
- 高分2件（human≥85）：展示优秀作品标准
- 中等2件（human 60-84）：展示中等作品特征
- 低分2件（human<60）：展示问题作品特征

所有示例已脱敏：不含学生姓名、学校名称等敏感信息。
"""

# 6件 Few-shot 示例
FEW_SHOT_EXAMPLES = [
    {
        "level": "high",
        "group_type": "小学组",
        "summary": "作品《红色文化之旅》，编程猫KITTEN4平台，小学组。红色文化主题，包含角色移动、场景切换、对话交互，代码结构清晰，有迭代记录和AIGC交互日志。",
        "theme_score": 18,
        "presentation_score": 28,
        "process_score": 28,
        "ai_literacy_score": 18,
        "total_score": 92,
        "theme_comment": "主题立意深刻，紧扣陕北红色文化，具有清晰的教育价值和社会意义。",
        "presentation_comment": "作品架构完整，交互设计流畅，界面美观协调，功能实现稳定。",
        "process_comment": "开发过程记录详尽，体现迭代思维和工程能力，AIGC交互日志完整。",
        "ai_literacy_comment": "AIGC使用策略清晰，体现批判性思维，能精准引导模型输出优质结果。",
        "overall_comment": "优秀作品，主题、技术、过程、AI素养四维度均衡发展，体现学生独立思考和创新能力。",
    },
    {
        "level": "high",
        "group_type": "初中组",
        "summary": "作品《红色革命探索》，Python/Pygame平台，初中组。红色革命主题，包含地图导航、历史事件触发、知识问答，代码模块化，有异常处理和版本迭代记录。",
        "theme_score": 18,
        "presentation_score": 27,
        "process_score": 28,
        "ai_literacy_score": 18,
        "total_score": 91,
        "theme_comment": "主题立意深刻，历史准确性强，以革命路线串联红色文化，视角独特。",
        "presentation_comment": "技术架构清晰，Pygame运用成熟，地图交互和事件触发设计巧妙，用户体验良好。",
        "process_comment": "需求分析、原型设计、迭代优化全流程记录完整，版本管理规范。",
        "ai_literacy_comment": "AI辅助研究历史资料，提示词设计精准，对AI输出有甄别和优化能力。",
        "overall_comment": "高质量作品，技术深度和主题深度兼具，体现初中生的计算思维和工程素养。",
    },
    {
        "level": "mid",
        "group_type": "小学组",
        "summary": "作品《文物会说话》，Scratch平台，小学组。文物介绍主题，包含点击展示、语音播放、简单动画，代码逻辑基本清晰，有部分过程记录。",
        "theme_score": 14,
        "presentation_score": 20,
        "process_score": 20,
        "ai_literacy_score": 14,
        "total_score": 68,
        "theme_comment": "主题明确，但文化深度有限，主要通过展示传达信息，缺乏独特视角。",
        "presentation_comment": "功能实现完整但创新性不足，界面设计较为常规，交互流程基本顺畅。",
        "process_comment": "有基本的开发过程记录，但迭代痕迹不明显，工程思维体现有限。",
        "ai_literacy_comment": "使用AI工具辅助内容生成，但策略性不强，提示词设计较为简单。",
        "overall_comment": "中等水平作品，完成了基本要求，但在创新性、深度和过程完整性上有提升空间。",
    },
    {
        "level": "mid",
        "group_type": "初中组",
        "summary": "作品《历史知识问答》，Scratch平台，初中组。问答闯关主题，包含题目展示、答题判断、得分统计，代码结构较清晰，有部分文档记录。",
        "theme_score": 14,
        "presentation_score": 20,
        "process_score": 20,
        "ai_literacy_score": 14,
        "total_score": 68,
        "theme_comment": "主题明确，历史知识覆盖较广，但缺乏独特视角和深度挖掘。",
        "presentation_comment": "功能实现完整，问答逻辑清晰，但界面设计较为基础，缺乏视觉吸引力。",
        "process_comment": "有开发过程记录，但迭代深度不足，测试和优化痕迹不明显。",
        "ai_literacy_comment": "使用AI辅助题目生成和内容整理，但交互深度有限，策略性一般。",
        "overall_comment": "中等水平作品，功能完整但缺乏亮点，在创新性和表现力上有较大提升空间。",
    },
    {
        "level": "low",
        "group_type": "小学组",
        "summary": "作品《简单动画》，编程猫KITTEN4平台，小学组。基础动画主题，仅有简单角色移动和背景切换，代码量极少，无迭代记录和AIGC日志。",
        "theme_score": 10,
        "presentation_score": 16,
        "process_score": 15,
        "ai_literacy_score": 10,
        "total_score": 51,
        "theme_comment": "主题模糊，未明确表达核心观点，文化价值和教育意义有限。",
        "presentation_comment": "交互设计缺失，用户参与度低，功能单一，技术探索性不足。",
        "process_comment": "无迭代记录，开发过程不可追溯，工程思维和文档意识薄弱。",
        "ai_literacy_comment": "未体现AIGC工具使用策略，缺乏AI辅助创作的痕迹。",
        "overall_comment": "作品完成度较低，主题、技术、过程和AI素养四维度均存在明显不足，需大幅改进。",
    },
    {
        "level": "low",
        "group_type": "小学组",
        "summary": "作品《基础互动》，Scratch平台，小学组。简单互动主题，仅有基本点击反馈和角色对话，代码结构混乱，无模块化思维，无过程文档。",
        "theme_score": 6,
        "presentation_score": 8,
        "process_score": 8,
        "ai_literacy_score": 6,
        "total_score": 28,
        "theme_comment": "主题立意模糊，未明确表达核心观点，缺乏用户情境和成功标准。",
        "presentation_comment": "交互设计缺失，用户参与度低；代码结构混乱，缺乏模块化思维。",
        "process_comment": "无迭代记录，开发过程不可追溯；缺乏需求分析、原型设计和测试验证。",
        "ai_literacy_comment": "未体现AIGC工具使用，无AI辅助创作的痕迹，AI素养薄弱。",
        "overall_comment": "作品完成度很低，主题模糊、技术薄弱、过程缺失、AI素养不足，需从零重构。",
    },
]


def get_examples_by_group(group_type: str = None) -> list:
    """按组别筛选示例，不匹配时返回全部6件。"""
    if not group_type:
        return FEW_SHOT_EXAMPLES
    filtered = [ex for ex in FEW_SHOT_EXAMPLES if ex["group_type"] == group_type]
    # 如果组别不匹配（如未匹配到），fallback 到全部6件
    return filtered if filtered else FEW_SHOT_EXAMPLES
