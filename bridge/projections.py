"""
Bridge projections
==================
统一账本到终端/场景视图的纯函数投影。
"""

from __future__ import annotations

from bridge.workflow import ROLE_KEYS


ROLE_TITLES = {
    "planner": "规划者",
    "reviewer": "审查者",
    "executor": "执行者",
    "validator": "校验者",
}


def _copy_scene_item(item):
    return dict(item) if item else None


def _event_message(event):
    data = event.get("data", {})
    event_type = event.get("type")
    if event_type == "session.status_changed":
        return f"[状态] {data.get('message') or data.get('status') or ''}".strip()
    if event_type == "session.stage_changed":
        return f"[阶段] {data.get('active_stage') or ''} {data.get('message') or ''}".strip()
    if event_type == "session.view_mode_changed":
        return f"[模式] {data.get('message') or data.get('view_mode') or ''}".strip()
    if event_type == "lane.status_changed":
        return f"[通道] {data.get('message') or data.get('status') or ''}".strip()
    if event_type == "lane.viewport_changed":
        return ""
    if event_type == "lane.thinking_started":
        return f"[思考] {data.get('message') or ''}".strip()
    if event_type == "lane.cli_started":
        command = data.get("command") or []
        command_text = " ".join(command) if isinstance(command, list) else str(command)
        return f"[CLI] {command_text}".strip()
    if event_type == "lane.stdout_chunk":
        return str(data.get("text") or "")
    if event_type == "lane.stderr_chunk":
        return f"[stderr] {data.get('text') or ''}".strip()
    if event_type == "lane.command_started":
        return f"$ {data.get('command') or ''}".strip()
    if event_type == "lane.command_output":
        return str(data.get("text") or "")
    if event_type == "lane.result_emitted":
        return f"[结果] {data.get('text') or ''}".strip()
    if event_type == "artifact.published":
        return f"[产物] {data.get('artifact_kind') or ''} R{data.get('round') or ''}".strip()
    if event_type == "intervention.received":
        return f"[用户输入] {data.get('text') or data.get('command') or ''}".strip()
    if event_type == "intervention.consumed":
        return f"[已消费] #{data.get('intervention_id') or ''} R{data.get('round') or ''}".strip()
    if event_type == "warning.raised":
        return f"[警告] {data.get('message') or ''}".strip()
    if event_type == "error.raised":
        return f"[错误] {data.get('message') or ''}".strip()
    return f"[{event_type}] {data}".strip()


def scene_item_for_artifact(artifact):
    return {
        "id": f"artifact-{artifact['id']}",
        "created_at": artifact["created_at"],
        "type": "artifact",
        "role_key": artifact["role_key"],
        "title": f"{ROLE_TITLES.get(artifact['role_key'], artifact['role_key'])} · {artifact['artifact_kind']} · R{artifact['round']}",
        "content": artifact["content"],
        "meta": f"{artifact['phase']} · {artifact['created_at']}",
    }


def scene_item_for_intervention(entry):
    return {
        "id": f"intervention-{entry['id']}",
        "created_at": entry["created_at"],
        "type": "intervention",
        "role_key": entry.get("origin_role_key"),
        "title": "用户干预",
        "content": entry.get("text") or entry.get("command") or "",
        "meta": f"{entry['target_scope']} · {entry['status']}",
    }


def scene_item_for_event(event):
    event_type = event.get("type")
    data = event.get("data", {})
    role_key = event.get("role_key")
    role_title = ROLE_TITLES.get(role_key, "系统")
    base = {
        "id": f"event-{event.get('id')}",
        "created_at": event.get("ts"),
        "type": "event",
        "role_key": role_key,
        "meta": event.get("ts"),
    }
    if event_type == "session.status_changed":
        return {
            **base,
            "role_key": None,
            "title": "会话状态",
            "content": str(data.get("message") or data.get("status") or ""),
        }
    if event_type == "session.stage_changed":
        return {
            **base,
            "role_key": None,
            "title": "工作流阶段",
            "content": f"{data.get('active_stage') or ''} {data.get('message') or ''}".strip(),
        }
    if event_type == "session.view_mode_changed":
        return {
            **base,
            "role_key": None,
            "title": "视图切换",
            "content": str(data.get("message") or data.get("view_mode") or ""),
        }
    if event_type == "lane.command_started":
        return {
            **base,
            "title": f"{role_title} 执行命令",
            "content": str(data.get("command") or ""),
        }
    if event_type == "lane.status_changed":
        return {
            **base,
            "title": f"{role_title} 通道状态",
            "content": str(data.get("message") or data.get("status") or ""),
        }
    if event_type == "lane.result_emitted":
        return {
            **base,
            "title": f"{role_title} 产出结果",
            "content": str(data.get("text") or ""),
        }
    if event_type == "warning.raised":
        return {**base, "title": "警告", "content": str(data.get("message") or "")}
    if event_type == "error.raised":
        return {**base, "title": "错误", "content": str(data.get("message") or "")}
    return None


def terminal_delta_for_event(event):
    data = event.get("data", {})
    if isinstance(data.get("projection"), dict):
        projection = data["projection"]
        terminal = projection.get("terminal")
        if isinstance(terminal, dict):
            return {
                role_key: str(text)
                for role_key, text in terminal.items()
                if role_key in ROLE_KEYS and str(text).strip()
            }
    message = _event_message(event)
    if not message:
        return {}
    role_key = event.get("role_key")
    if role_key in ROLE_KEYS:
        return {role_key: message}
    return {role_key: message for role_key in ROLE_KEYS}


def scene_delta_for_event(event):
    data = event.get("data", {})
    if isinstance(data.get("projection"), dict) and data["projection"].get("scene"):
        return _copy_scene_item(data["projection"]["scene"])
    if event.get("type") == "artifact.published" and isinstance(data.get("artifact"), dict):
        return scene_item_for_artifact(data["artifact"])
    if event.get("type") in {"intervention.received", "intervention.consumed"} and isinstance(data.get("intervention"), dict):
        return scene_item_for_intervention(data["intervention"])
    return scene_item_for_event(event)


def projection_payload_for_event(event):
    return {
        "terminal": terminal_delta_for_event(event),
        "scene": scene_delta_for_event(event),
    }


def project_terminal(events):
    result = {role_key: [] for role_key in ROLE_KEYS}
    for event in events:
        for role_key, line in terminal_delta_for_event(event).items():
            result[role_key].append(line)
    return {role_key: "\n".join(lines) for role_key, lines in result.items()}


def project_scene(events, artifacts, interventions):
    artifact_items = [scene_item_for_artifact(artifact) for artifact in artifacts]
    intervention_items = [scene_item_for_intervention(entry) for entry in interventions]
    event_items = [item for item in (scene_item_for_event(event) for event in events) if item]
    return sorted(
        [*artifact_items, *intervention_items, *event_items],
        key=lambda item: (item["created_at"], item["id"]),
    )
