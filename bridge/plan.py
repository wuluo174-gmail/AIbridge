"""
Bridge Plan 文件检测
===================
Plan 文件快照差集 + 关键词关联校验。
从 bridge.py L133-172 提取的真实实现。

函数清单:
  - snapshot_plan_files(): 快照 ~/.claude/plans/ 下所有 .md 文件
  - find_new_plan_file(before_snapshot): 对比快照，找到新增或修改的 plan 文件
  - validate_plan_relevance(plan_content, task): 检查 plan 与任务的关键词关联
"""

import re
from pathlib import Path


def snapshot_plan_files():
    """快照 ~/.claude/plans/ 下所有 .md 文件，返回 {path: mtime}。"""
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return {}
    return {p: p.stat().st_mtime for p in plans_dir.glob("*.md")}


def find_new_plan_file(before_snapshot):
    """对比快照，找到新增或修改的 plan 文件内容。"""
    after = snapshot_plan_files()
    new_files = []
    for path, mtime in after.items():
        if path not in before_snapshot or mtime > before_snapshot[path]:
            new_files.append(path)
    if not new_files:
        return ""
    newest = max(new_files, key=lambda p: p.stat().st_mtime)
    try:
        return newest.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def validate_plan_relevance(plan_content, task):
    """检查 plan 内容是否与当前任务存在关键词关联。
    防御外部 Claude 进程在持锁期间写入无关 plan 文件的边缘情况。
    保护边界：关键词匹配是启发式的，不是完美隔离。
    """
    if not plan_content or not task:
        return True  # 无内容或无任务时不拦截
    # 取任务中的关键词（长度 >= 2 的中文/英文 token）
    tokens = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z_]\w{2,}', task)
    if not tokens:
        return True  # 无法提取关键词时不拦截
    # 至少有一个关键词出现在 plan 内容中
    return any(t in plan_content for t in tokens)
