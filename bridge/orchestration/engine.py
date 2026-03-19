"""
Bridge 编排引擎
===============
协商/执行/审查循环的核心逻辑。

当前阶段：函数签名骨架。
Step 4 时从 bridge.py L609-912 迁入完整实现。

对应 bridge.py (commit cdc4613):
  - last_complete_round: L612-623
  - is_approved: L626-629
  - run_negotiation: L632-757
  - run_execution: L759-800
  - run_first_review: L803-841
  - run_review_fix_cycle: L844-912
"""


def last_complete_round(history: list) -> int:
    """返回 history 中最后一个完整轮次号（同时有 claude 和 codex 记录）。

    无则返回 0。用于续接失败时的回退基准。
    对应 bridge.py L612-623。
    """
    # Step 4 时迁入实现
    raise NotImplementedError("骨架声明，Step 4 迁入实现")


def run_negotiation(sess, start_round: int = 1):
    """主协商循环。

    交替调用 Planner 和 Reviewer adapter:
    1. Planner 出方案 / 修订
    2. Reviewer 审查
    3. 检测共识 (APPROVED)
    4. 达到 max_rounds 或共识后停止

    续接失败时回退到 last_complete_round。

    对应 bridge.py L632-757。
    """
    # Step 4 时迁入实现
    raise NotImplementedError("骨架声明，Step 4 迁入实现")


def run_execution(sess):
    """执行已审批的方案。

    1. 捕获 Git baseline (stash create + untracked)
    2. 调用 Planner adapter 执行 (--dangerously-skip-permissions)
    3. 自动触发 run_first_review

    对应 bridge.py L759-800。
    """
    # Step 4 时迁入实现
    raise NotImplementedError("骨架声明，Step 4 迁入实现")


def run_first_review(sess, approved_plan: str):
    """执行后自动发起首轮 Reviewer 审查。

    1. 构建带 git diff 的审查提示
    2. 调用 Reviewer adapter
    3. 检测"任务收口成功" → done
    4. 检测问题 → review_fix, 等待用户确认

    对应 bridge.py L803-841。
    """
    # Step 4 时迁入实现
    raise NotImplementedError("骨架声明，Step 4 迁入实现")


def run_review_fix_cycle(sess):
    """用户确认后的修复循环。

    1. Planner 修复 (带 --dangerously-skip-permissions)
    2. Reviewer 再评审
    3. 检测"任务收口成功" → done
    4. 仍有问题 → review_fix, 等待下一轮
    5. 最多 max_review_rounds (默认 3) 轮

    对应 bridge.py L844-912。
    """
    # Step 4 时迁入实现
    raise NotImplementedError("骨架声明，Step 4 迁入实现")
