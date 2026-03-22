"""
Bridge workflow model
=====================
四角色工作流配置、角色常量与辅助函数。
"""

from __future__ import annotations

import dataclasses


ROLE_KEYS = ("planner", "reviewer", "executor", "validator")
VIEW_MODES = ("terminal", "scene")
WORKFLOW_TEMPLATE = "standard"


@dataclasses.dataclass(frozen=True)
class WorkflowRole:
    role_key: str
    tool_id: str
    enabled: bool = True
    sort_order: int = 0


@dataclasses.dataclass(frozen=True)
class WorkflowConfig:
    view_mode: str = "scene"
    workflow_template: str = WORKFLOW_TEMPLATE
    max_rounds: int = 5
    max_review_rounds: int = 3
    roles: tuple[WorkflowRole, ...] = (
        WorkflowRole("planner", "claude-code", True, 0),
        WorkflowRole("reviewer", "codex", True, 1),
        WorkflowRole("executor", "claude-code", True, 2),
        WorkflowRole("validator", "codex", True, 3),
    )


def default_workflow_config() -> WorkflowConfig:
    return WorkflowConfig()


def _default_role_map() -> dict[str, WorkflowRole]:
    return {role.role_key: role for role in default_workflow_config().roles}


def normalize_workflow_config(data: dict | None) -> WorkflowConfig:
    if not data:
        return default_workflow_config()

    default_cfg = default_workflow_config()
    default_roles = _default_role_map()
    raw_roles = data.get("roles") or [dataclasses.asdict(role) for role in default_cfg.roles]
    roles: list[WorkflowRole] = []
    seen = set()
    for index, item in enumerate(raw_roles):
        role_key = str(item.get("role_key", "")).strip()
        if role_key not in ROLE_KEYS or role_key in seen:
            continue
        seen.add(role_key)
        default_role = default_roles[role_key]
        roles.append(
            WorkflowRole(
                role_key=role_key,
                tool_id=str(item.get("tool_id") or default_role.tool_id).strip() or default_role.tool_id,
                enabled=bool(item.get("enabled", default_role.enabled)),
                sort_order=int(item.get("sort_order", index)),
            )
        )

    for role_key, default_role in default_roles.items():
        if role_key not in seen:
            roles.append(default_role)

    roles.sort(key=lambda role: role.sort_order)

    view_mode = str(data.get("view_mode") or default_cfg.view_mode).strip()
    if view_mode not in VIEW_MODES:
        view_mode = default_cfg.view_mode

    workflow_template = str(data.get("workflow_template") or default_cfg.workflow_template).strip() or WORKFLOW_TEMPLATE
    max_rounds = max(1, min(int(data.get("max_rounds", default_cfg.max_rounds)), 20))
    max_review_rounds = max(1, min(int(data.get("max_review_rounds", default_cfg.max_review_rounds)), 20))

    return WorkflowConfig(
        view_mode=view_mode,
        workflow_template=workflow_template,
        max_rounds=max_rounds,
        max_review_rounds=max_review_rounds,
        roles=tuple(roles),
    )


def workflow_config_to_dict(config: WorkflowConfig) -> dict:
    return {
        "view_mode": config.view_mode,
        "workflow_template": config.workflow_template,
        "max_rounds": config.max_rounds,
        "max_review_rounds": config.max_review_rounds,
        "roles": [dataclasses.asdict(role) for role in config.roles],
    }


def role_tool_map(config: WorkflowConfig) -> dict[str, str]:
    return {role.role_key: role.tool_id for role in config.roles if role.enabled}


def target_roles_for_stage(stage: str) -> tuple[str, ...]:
    if stage in {"planning", "reviewing", "awaiting_execution"}:
        return ("planner", "reviewer")
    if stage in {"executing", "validating", "repairing"}:
        return ("executor", "validator")
    return ROLE_KEYS
