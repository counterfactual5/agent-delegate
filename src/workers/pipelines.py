"""
Workers - 流水线 Agent 定义

每个 Worker 是一个独立的多阶段流水线。
这里定义的是阶段模板和执行逻辑，不绑定具体 runtime。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StageStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Stage:
    """流水线中的一个阶段"""
    name: str
    role_prompt: str               # 该阶段的 system prompt 模板
    input_gates: list[str]         # 前置产物（必须存在的文件名）
    output_artifacts: list[str]    # 该阶段的产出文件
    model_tier: str = "heavy"      # light / standard / heavy
    timeout_seconds: int = 600
    max_retries: int = 2
    status: StageStatus = StageStatus.PENDING
    error: Optional[str] = None


@dataclass
class Pipeline:
    """完整的流水线定义"""
    name: str
    description: str
    stages: list[Stage] = field(default_factory=list)
    
    def next_pending(self) -> Optional[Stage]:
        """返回下一个待执行的阶段（前提：input_gates 已满足）"""
        for stage in self.stages:
            if stage.status == StageStatus.PENDING:
                return stage
        return None
    
    def completed_artifacts(self) -> list[str]:
        """返回已完成阶段的所有产出"""
        artifacts = []
        for s in self.stages:
            if s.status == StageStatus.COMPLETED:
                artifacts.extend(s.output_artifacts)
        return artifacts
    
    def validate_gates(self, stage: Stage, existing_files: set) -> bool:
        """检查阶段的前置产物是否都已存在"""
        return all(f in existing_files for f in stage.input_gates)


# ─── 预定义流水线 ───────────────────────────────────────

CODING_PIPELINE = Pipeline(
    name="coding",
    description="4 阶段编码流水线：Planner → Builder → Reviewer → Consultant",
    stages=[
        Stage(
            name="Planner",
            role_prompt="你是一名高级架构师。分析需求，产出 PLAN.md，包含：技术方案、文件结构、关键决策、风险点。",
            input_gates=[],
            output_artifacts=["PLAN.md"],
            model_tier="heavy",
            timeout_seconds=300,
        ),
        Stage(
            name="Builder",
            role_prompt="你是一名全栈工程师。根据 PLAN.md 实现完整代码，注释详尽，使用绝对路径保存。",
            input_gates=["PLAN.md"],
            output_artifacts=["src/"],  # 目录
            model_tier="heavy",
            timeout_seconds=600,
        ),
        Stage(
            name="Reviewer",
            role_prompt="你是一名代码审查专家。审查 Builder 的产出：安全性、性能、代码规范、边界处理。输出 REVIEW.md。",
            input_gates=["PLAN.md", "src/"],
            output_artifacts=["REVIEW.md"],
            model_tier="heavy",
            timeout_seconds=300,
        ),
        Stage(
            name="Consultant",
            role_prompt="你是一名技术顾问。阅读 REVIEW.md 中的问题，决定是否需要 Builder 修复，最终产出 CONSULTANT.md。",
            input_gates=["PLAN.md", "REVIEW.md"],
            output_artifacts=["CONSULTANT.md"],
            model_tier="standard",
            timeout_seconds=300,
        ),
    ],
)

RESEARCH_PIPELINE = Pipeline(
    name="research",
    description="4 阶段调研流水线：Searcher → Synthesizer → Fact-Checker → Reporter",
    stages=[
        Stage(
            name="Searcher",
            role_prompt="你是一名信息检索专家。对给定主题进行多轮搜索，收集原始资料，保存为 SOURCES.md。",
            input_gates=[],
            output_artifacts=["SOURCES.md"],
            model_tier="standard",
            timeout_seconds=300,
        ),
        Stage(
            name="Synthesizer",
            role_prompt="你是一名信息综合分析师。阅读 SOURCES.md，去重、归纳、提取关键观点，产出 SYNTHESIS.md。",
            input_gates=["SOURCES.md"],
            output_artifacts=["SYNTHESIS.md"],
            model_tier="heavy",
            timeout_seconds=300,
        ),
        Stage(
            name="Fact-Checker",
            role_prompt="你是一名事实核查员。验证 SYNTHESIS.md 中的关键论断，标注可信度，产出 FACT_CHECK.md。",
            input_gates=["SYNTHESIS.md"],
            output_artifacts=["FACT_CHECK.md"],
            model_tier="standard",
            timeout_seconds=300,
        ),
        Stage(
            name="Reporter",
            role_prompt="你是一名专业报告撰写者。综合 SYNTHESIS.md 和 FACT_CHECK.md，撰写最终 REPORT.md。",
            input_gates=["SYNTHESIS.md", "FACT_CHECK.md"],
            output_artifacts=["REPORT.md"],
            model_tier="heavy",
            timeout_seconds=600,
        ),
    ],
)

DOC_PIPELINE = Pipeline(
    name="doc",
    description="9 阶段文档流水线：Scanner → Planner → Expander → Editor → Quality → Kami → PDF/PPTX",
    stages=[
        Stage(name="Scanner", role_prompt="扫描本地项目结构，提取代码和文档元信息。",
              input_gates=[], output_artifacts=["SCAN.md"], model_tier="standard"),
        Stage(name="Planner", role_prompt="基于扫描结果，生成 WRITING_PLAN.md，规划章节、段落、代码/公式分配。",
              input_gates=["SCAN.md"], output_artifacts=["WRITING_PLAN.md"], model_tier="heavy"),
        Stage(name="Section Expander", role_prompt="逐章展开，写入 sections/section-XX.md。",
              input_gates=["WRITING_PLAN.md"], output_artifacts=["sections/"], model_tier="heavy"),
        Stage(name="Merger", role_prompt="合并章节为完整报告 WHITEPAPER.md。",
              input_gates=["sections/"], output_artifacts=["WHITEPAPER.md"], model_tier="standard"),
        Stage(name="Quality Gate", role_prompt="检查长度、薄章节、代码块、空话词。产出 QUALITY_REPORT.md。",
              input_gates=["WHITEPAPER.md"], output_artifacts=["QUALITY_REPORT.md"], model_tier="standard"),
        Stage(name="Editor", role_prompt="根据质量报告修复问题。",
              input_gates=["WHITEPAPER.md", "QUALITY_REPORT.md"], output_artifacts=["WHITEPAPER.md"], model_tier="heavy"),
        Stage(name="Kami Brief", role_prompt="生成排版指令 KAMI_BRIEF.md 和 PPT_OUTLINE.md。",
              input_gates=["WHITEPAPER.md"], output_artifacts=["KAMI_BRIEF.md", "PPT_OUTLINE.md"], model_tier="standard"),
        Stage(name="Render", role_prompt="调用 Kami 渲染器，生成 HTML/PDF/PPTX。",
              input_gates=["KAMI_BRIEF.md"], output_artifacts=["kami/"], model_tier="light"),
    ],
)


# 流水线注册表
PIPELINES: dict[str, Pipeline] = {
    "coding": CODING_PIPELINE,
    "research": RESEARCH_PIPELINE,
    "doc": DOC_PIPELINE,
}
