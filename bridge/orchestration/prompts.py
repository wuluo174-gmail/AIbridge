"""
Bridge 提示词构建
================
所有 build_*_prompt 函数的骨架声明。

当前阶段：函数签名。
Step 5 时从 bridge.py L440-606 迁入完整实现。

11 个提示词配置键 (与 protocol.PROMPT_KEYS 一致):
  1. claude_first          — Claude 首轮方案
  2. claude_revise         — Claude 修订
  3. codex_first           — Codex 首轮审查
  4. codex_review          — Codex 后续审查
  5. execution             — 执行 (APPROVED)
  6. execution_unapproved  — 执行 (未 APPROVED)
  7. codex_post_review     — 执行后 Codex 审查
  8. claude_post_fix       — Claude 修复
  9. codex_post_review_followup — Codex 再评审
  10. user_inject_label_claude  — 用户注入标签 (Claude)
  11. user_inject_label_codex   — 用户注入标签 (Codex)
"""


def detect_claude_md(cwd: str) -> str:
    """检测项目 CLAUDE.md 并返回内容 (前 2000 字符)。

    对应 bridge.py L442-447。
    """
    raise NotImplementedError("骨架声明，Step 5 迁入实现")


def build_claude_first_prompt(task: str, cwd: str) -> str:
    """构建 Claude 首轮方案提示。对应 L450-454。"""
    raise NotImplementedError

def build_claude_revise_prompt(codex_feedback: str, user_injects: list = None) -> str:
    """构建 Claude 修订提示。对应 L468-476。"""
    raise NotImplementedError

def build_codex_first_prompt(task: str, claude_plan: str) -> str:
    """构建 Codex 首轮审查提示。对应 L479-482。"""
    raise NotImplementedError

def build_codex_review_prompt(claude_revision: str, user_injects: list = None) -> str:
    """构建 Codex 后续审查提示。对应 L485-493。"""
    raise NotImplementedError

def build_execution_prompt(task: str, final_plan: str = "", approved: bool = True) -> str:
    """构建执行提示。对应 L496-513。"""
    raise NotImplementedError

def collect_user_injects(history: list) -> list:
    """从 history 末尾收集连续 user 注入。对应 L457-465。"""
    raise NotImplementedError

def build_codex_post_review_prompt(sess, task: str, approved_plan: str, execution_result: str) -> str:
    """构建执行后 Codex 审查提示 (带 git diff)。对应 L589-594。"""
    raise NotImplementedError

def build_claude_post_fix_prompt(review_feedback: str) -> str:
    """构建 Claude 修复提示。对应 L597-599。"""
    raise NotImplementedError

def build_codex_post_review_followup_prompt(sess, fix_result: str) -> str:
    """构建 Codex 再评审提示。对应 L602-606。"""
    raise NotImplementedError
