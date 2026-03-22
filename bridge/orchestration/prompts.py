"""
Bridge 提示词构建
================
所有 build_*_prompt 函数 + prompt 配置管理。

Universal DI Rule:
  凡是内部调用了出现在 bridge.py re-export 清单中的函数，
  该调用通过 _xxx=None 可选参数接收，默认值指向本模块 import 的原始实现。
  bridge.py 定义同签名薄 wrapper，从自身 globals() 注入。

DI 参数清单:
  - build_claude_first_prompt:              _detect_context=None, _adapter=None
  - _build_diff_section:                    _capture_diff=None
  - build_codex_post_review_prompt:         _capture_diff=None (透传)
  - build_codex_post_review_followup_prompt: _capture_diff=None (透传)

Step 7: detect_claude_md → detect_project_context (adapter-aware)
        模板变量增加 {planner_name} / {reviewer_name}
"""

import json
import re
from pathlib import Path

from bridge.git import capture_execution_diff as _default_capture_diff

# ═════════════════════════════════════════════════════════════════
# Prompt Configuration (全局共享)
# ═════════════════════════════════════════════════════════════════
_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_FILE = _ROOT / "prompts.json"


def load_prompts():
    """从 prompts.json 加载提示词模板。"""
    if PROMPTS_FILE.exists():
        return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_prompts(data):
    """保存提示词模板到 prompts.json。"""
    PROMPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


prompt_config = load_prompts()

_PROJECT_CONTEXT_CHAR_LIMIT = 2000
_CLAUDE_BLOCKED_TOKENS = (
    "请确认我的理解是否准确",
    "【🫡结论】",
    "【方案】",
    "【反驳】",
    "【需要澄清】",
    "我是linus",
    "每次回复必须",
    "只选一个",
)

_PLANNER_FIRST_OUTPUT_CONTRACT = """## 最终输出要求（必须遵守）
- 项目开发规范（包括 CLAUDE.md）只影响分析原则、代码品味和审查方式，不改变本轮最终输出格式。
- 不要只输出“【结论】”、不要先请求用户确认、不要只列选项或澄清问题。
- 直接输出一份完整的 Markdown 方案文档。
- 文档至少包含：根因分析、数据流分析（数据从哪里来/到哪里去/中间经历了什么变换）、详细实施步骤、风险点、验证方法。
- 如果存在不确定项，直接写入风险点或假设，不要把整份输出退化成请求补充信息。"""

_PLANNER_REVISE_OUTPUT_CONTRACT = """## 最终输出要求（必须遵守）
- 项目开发规范（包括 CLAUDE.md）只影响分析原则、代码品味和审查方式，不改变本轮最终输出格式。
- 不要只回复“接纳/不接纳”列表，也不要只输出结论或澄清问题。
- 先逐条回应审查反馈：对每条标记 接纳 ✓ / 部分接纳 △ / 不接纳 ✗，并说明理由。
- 然后输出“修订后的完整方案”，并保持完整 Markdown 文档结构。
- 修订后的完整方案至少包含：根因分析、数据流分析（数据从哪里来/到哪里去/中间经历了什么变换）、详细实施步骤、风险点、验证方法。"""


# ═════════════════════════════════════════════════════════════════
# Prompt Templates
# ═════════════════════════════════════════════════════════════════


def _clip_context(text, limit=_PROJECT_CONTEXT_CHAR_LIMIT):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(truncated)\n"


def _trim_blank_lines(lines):
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _parse_markdown_sections(text):
    sections = []
    stack = []
    current_path = None
    current_lines = []

    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if current_path is not None:
                sections.append((tuple(current_path), "\n".join(current_lines).strip()))
            level = len(match.group(1))
            title = match.group(2).strip()
            stack = stack[:level - 1] + [title]
            current_path = list(stack)
            current_lines = []
            continue
        if current_path is not None:
            current_lines.append(line)

    if current_path is not None:
        sections.append((tuple(current_path), "\n".join(current_lines).strip()))
    return sections


def _filter_context_body(body):
    if not body:
        return ""
    kept = []
    for line in body.splitlines():
        stripped = line.strip()
        if any(token in stripped for token in _CLAUDE_BLOCKED_TOKENS):
            continue
        kept.append(line)
    trimmed = _trim_blank_lines(kept)
    return "\n".join(trimmed).strip()


def _render_context_group(title, entries):
    if not entries:
        return ""
    lines = [f"### {title}"]
    for subtitle, body in entries:
        lines.append(f"#### {subtitle}")
        if body:
            lines.append(body)
    return "\n".join(lines).strip()


def _normalize_claude_md(content):
    groups = {
        "核心哲学": [],
        "协作风格": [],
        "思考维度": [],
    }

    for path, body in _parse_markdown_sections(content):
        cleaned = _filter_context_body(body)
        if len(path) >= 2 and path[-2] == "核心哲学":
            groups["核心哲学"].append((path[-1], cleaned))
        elif len(path) >= 2 and path[-2] == "沟通协作原则" and path[-1] == "基础交流规范":
            groups["协作风格"].append((path[-1], cleaned))
        elif len(path) >= 2 and path[-2] == "需求确认流程" and path[-1].startswith("2. 思考维度分析"):
            groups["思考维度"].append((path[-1], cleaned))

    parts = [
        "以下内容只保留分析原则、协作风格和思考维度；已过滤固定输出模板、确认流程和验证口令。",
    ]
    for group_name in ("核心哲学", "协作风格", "思考维度"):
        section = _render_context_group(group_name, groups[group_name])
        if section:
            parts.append(section)
    return "\n\n".join(part for part in parts if part).strip()


def _normalize_context_file(filename, content):
    if filename == "CLAUDE.md":
        return _normalize_claude_md(content)
    return content.strip()

def detect_project_context(cwd, adapter=None):
    """检测项目上下文文件。

    adapter 提供时使用其 context_files 属性，否则 fallback 到 CLAUDE.md。
    """
    filenames = getattr(adapter, 'context_files', None) or ["CLAUDE.md"]
    context = ""
    for filename in filenames:
        p = Path(cwd) / filename
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            content = _clip_context(_normalize_context_file(filename, raw))
            if content:
                context += f"\n\n## 项目开发规范 ({filename})\n{content}"
    return context


# 向后兼容别名
detect_claude_md = detect_project_context


def _join_prompt_sections(*sections):
    return "\n\n".join(s.strip() for s in sections if s and s.strip())


def build_claude_first_prompt(task, cwd, planner_name="Claude Code",
                               _detect_context=None, _adapter=None):
    """构建 Planner 首轮方案提示。

    注意：claude_* / codex_* 是历史 key，当前分别服务 planner / reviewer 阶段。
    DI: _detect_context — bridge.py wrapper 注入以支持 monkey-patch。
    """
    if _detect_context is None:
        _detect_context = detect_project_context
    context = _detect_context(cwd, _adapter)
    tpl = prompt_config.get("claude_first", "## 任务\n{task}")
    try:
        body = tpl.format(task=task, planner_name=planner_name)
    except KeyError:
        body = tpl.format(task=task)
    return _join_prompt_sections(context, body, _PLANNER_FIRST_OUTPUT_CONTRACT)


def build_claude_revise_prompt(codex_feedback, user_injects=None, cwd=None,
                               _detect_context=None, _adapter=None):
    """构建 Planner 修订提示。"""
    context = ""
    if cwd:
        if _detect_context is None:
            _detect_context = detect_project_context
        context = _detect_context(cwd, _adapter)
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_claude", "用户补充的约束和意见（必须优先考虑）")
        inject_section = f"\n\n## {label}\n{joined}"
    tpl = prompt_config.get("claude_revise",
        "以上是你之前的方案。\n\n## 审查者反馈\n{codex_feedback}{inject_section}\n\n请修订方案。")
    body = tpl.format(codex_feedback=codex_feedback, inject_section=inject_section)
    return _join_prompt_sections(context, body, _PLANNER_REVISE_OUTPUT_CONTRACT)


def build_codex_first_prompt(task, claude_plan):
    """构建 Reviewer 首轮审查提示。历史 key: codex_first。"""
    tpl = prompt_config.get("codex_first",
        "对于以下方案有什么看法？\n\n## 原始任务\n{task}\n\n## Planner 的方案\n{claude_plan}")
    return tpl.format(task=task, claude_plan=claude_plan)


def build_codex_review_prompt(claude_revision, user_injects=None):
    """构建 Reviewer 后续审查提示。历史 key: codex_review。"""
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_codex", "用户补充的约束和意见（审查时必须考虑）")
        inject_section = f"\n\n## {label}\n{joined}"
    tpl = prompt_config.get("codex_review",
        "Planner 修订了方案。\n\n## Planner 的修订方案\n{claude_revision}{inject_section}")
    return tpl.format(claude_revision=claude_revision, inject_section=inject_section)


def build_execution_prompt(task, final_plan="", approved=True):
    """构建执行提示。"""
    plan_section = ""
    if final_plan:
        plan_section = f"\n\n## 最终方案\n{final_plan}"

    if approved:
        tpl = prompt_config.get("execution",
            "以上方案已经过严格多轮审查并获得 APPROVED。{plan_section}\n\n"
            "请严格按照方案执行所有代码修改。完成后总结你执行的所有变更。\n\n原始任务: {task}")
    else:
        tpl = prompt_config.get("execution_unapproved",
            "以上方案经过多轮协商但未获得审查者的明确认可，用户选择继续执行。{plan_section}\n\n"
            "请按照方案执行代码修改，对不确定的部分保持审慎。完成后总结你执行的所有变更。\n\n原始任务: {task}")

    try:
        return tpl.format(task=task, plan_section=plan_section)
    except KeyError:
        return tpl.replace("{task}", task) + plan_section


# ═════════════════════════════════════════════════════════════════
# Diff Formatting + Post-Review Prompts
# ═════════════════════════════════════════════════════════════════

def _build_diff_section(cwd, baseline_ref, is_git_repo, baseline_untracked=None,
                        _capture_diff=None):
    """格式化 diff 为 markdown 区块嵌入 prompt。"""
    if _capture_diff is None:
        _capture_diff = _default_capture_diff
    if not is_git_repo:
        return "（注意：本项目不在 git 仓库中，无法提供 diff。请自行读取相关文件验证实际变更。）\n\n"
    diff = _capture_diff(cwd, baseline_ref, baseline_untracked)
    if diff is None:
        return "（获取 diff 失败，请自行读取相关文件验证。）\n\n"
    return f"## 本次执行的代码变更 (git diff)\n```\n{diff}\n```\n\n"


def build_codex_post_review_prompt(sess, task, approved_plan, execution_result,
                                   _capture_diff=None):
    """构建执行后审查提示 (带 git diff)。"""
    diff_section = _build_diff_section(
        sess.project_path, sess.exec_baseline_ref, sess.is_git_repo,
        sess.exec_baseline_untracked, _capture_diff=_capture_diff)
    tpl = prompt_config.get("codex_post_review", "请审查执行结果...")
    return tpl.format(task=task, approved_plan=approved_plan,
                      execution_result=execution_result, diff_section=diff_section)


def build_claude_post_fix_prompt(review_feedback):
    """构建修复提示。"""
    tpl = prompt_config.get("claude_post_fix", "请修复以下问题...")
    return tpl.format(review_feedback=review_feedback)


def build_codex_post_review_followup_prompt(sess, fix_result, _capture_diff=None):
    """构建再评审提示。"""
    diff_section = _build_diff_section(
        sess.project_path, sess.exec_baseline_ref, sess.is_git_repo,
        sess.exec_baseline_untracked, _capture_diff=_capture_diff)
    tpl = prompt_config.get("codex_post_review_followup", "请重新审查...")
    return tpl.format(fix_result=fix_result, diff_section=diff_section)
