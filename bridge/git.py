"""
Bridge Git 工具
===============
纯 git 操作函数，执行基础设施层。

函数清单:
  - _is_git_repo(cwd): 检测目录是否在 git 仓库中
  - capture_baseline_ref(cwd): 捕获执行前基线 ref (stash create / HEAD)
  - capture_baseline_untracked(cwd): 捕获执行前 untracked 文件集
  - capture_execution_diff(cwd, baseline_ref, baseline_untracked): 生成执行 diff

依赖: 仅 subprocess，无内部循环依赖。
"""

import subprocess


def _is_git_repo(cwd):
    """检测目录是否在 git 仓库内。"""
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                     capture_output=True, text=True, cwd=cwd, timeout=5)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def capture_baseline_ref(cwd):
    """捕获 git baseline ref — stash create 优先，回退到 HEAD。"""
    try:
        r = subprocess.run(["git", "stash", "create"], capture_output=True, text=True, cwd=cwd, timeout=10)
        ref = r.stdout.strip()
        if ref:
            return ref
        r2 = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=cwd, timeout=5)
        return r2.stdout.strip() if r2.returncode == 0 else None
    except Exception:
        return None


def capture_baseline_untracked(cwd):
    """捕获当前 untracked 文件集合。"""
    try:
        r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                     capture_output=True, text=True, cwd=cwd, timeout=5)
        return set(r.stdout.strip().splitlines()) if r.returncode == 0 else set()
    except Exception:
        return set()


def capture_execution_diff(cwd, baseline_ref, baseline_untracked=None):
    """生成执行 diff — tracked 变更 + 新增 untracked 文件，最大 15KB。"""
    if not baseline_ref:
        return None
    try:
        r = subprocess.run(["git", "diff", baseline_ref],
                     capture_output=True, text=True, cwd=cwd, timeout=15)
        if r.returncode != 0:
            return None
        diff = r.stdout.strip()

        r2 = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                      capture_output=True, text=True, cwd=cwd, timeout=5)
        current_untracked = set(r2.stdout.strip().splitlines()) if r2.returncode == 0 else set()
        new_untracked = current_untracked - (baseline_untracked or set())

        parts = []
        if diff:
            parts.append(diff)
        if new_untracked:
            parts.append("\n### 本次执行新增的文件 (untracked)\n" + "\n".join(sorted(new_untracked)))

        result = "\n".join(parts) if parts else "(无变更)"
        if len(result) > 15000:
            result = result[:15000] + "\n...(diff 过大，已截断)"
        return result
    except Exception:
        return None
