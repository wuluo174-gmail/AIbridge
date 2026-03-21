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


# ═════════════════════════════════════════════════════════════════
# Prompt Templates
# ═════════════════════════════════════════════════════════════════

def detect_project_context(cwd, adapter=None):
    """检测项目上下文文件。

    adapter 提供时使用其 context_files 属性，否则 fallback 到 CLAUDE.md。
    """
    filenames = getattr(adapter, 'context_files', None) or ["CLAUDE.md"]
    context = ""
    for filename in filenames:
        p = Path(cwd) / filename
        if p.exists():
            content = p.read_text(encoding="utf-8")[:2000]
            context += f"\n\n## 项目开发规范 ({filename})\n{content}"
    return context


# 向后兼容别名
detect_claude_md = detect_project_context


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
    return f"{context}\n\n{body}"


def collect_user_injects(history):
    """从 history 末尾收集连续 user 注入。"""
    injects = []
    for h in reversed(history):
        if h["role"] == "user":
            injects.append(h["content"])
        else:
            break
    injects.reverse()
    return injects


def build_claude_revise_prompt(codex_feedback, user_injects=None):
    """构建 Planner 修订提示。"""
    inject_section = ""
    if user_injects:
        joined = "\n".join(f"- {m}" for m in user_injects)
        label = prompt_config.get("user_inject_label_claude", "用户补充的约束和意见（必须优先考虑）")
        inject_section = f"\n\n## {label}\n{joined}"
    tpl = prompt_config.get("claude_revise",
        "以上是你之前的方案。\n\n## 审查者反馈\n{codex_feedback}{inject_section}\n\n请修订方案。")
    return tpl.format(codex_feedback=codex_feedback, inject_section=inject_section)


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
