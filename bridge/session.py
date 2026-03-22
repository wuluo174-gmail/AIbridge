"""
Bridge session model
====================
统一会话、角色 lane、artifact 与 intervention 的运行时真相源。
"""

from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bridge.projections import projection_payload_for_event
from bridge.protocol import INTERVENTION_STATUSES, STREAM_EVENT_TYPES
from bridge.workflow import WorkflowConfig, WorkflowRole, workflow_config_to_dict


LOG_DIR = Path("/tmp/bridge-logs")

sessions: dict[str, "SessionState"] = {}
sessions_lock = threading.Lock()
_persist_hook = None


@dataclass
class RoleLane:
    lane_id: str
    role_key: str
    tool_id: str
    enabled: bool = True
    sort_order: int = 0
    lane_status: str = "idle"
    transport_kind: str = "bridge-terminal"
    viewport: dict = field(default_factory=dict)
    last_seq: int = -1
    resume_state: dict = field(default_factory=dict)

    @classmethod
    def from_workflow_role(cls, role: WorkflowRole) -> "RoleLane":
        return cls(
            lane_id=f"lane_{role.role_key}_{uuid.uuid4().hex[:8]}",
            role_key=role.role_key,
            tool_id=role.tool_id,
            enabled=role.enabled,
            sort_order=role.sort_order,
        )

    def to_wire(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "role_key": self.role_key,
            "tool_id": self.tool_id,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "lane_status": self.lane_status,
            "transport_kind": self.transport_kind,
            "viewport": deepcopy(self.viewport),
            "last_seq": self.last_seq,
        }


class SessionState:
    def __init__(self, session_id: str, task: str, project_path: str, workflow: WorkflowConfig):
        self.session_id = session_id
        self.task = task
        self.project_path = project_path
        self.workflow_template = workflow.workflow_template
        self.view_mode = workflow.view_mode
        self.max_rounds = workflow.max_rounds
        self.max_review_rounds = workflow.max_review_rounds
        self.status = "running"
        self.active_stage = "planning"
        self.current_round = 0
        self.current_review_round = 0
        self.consensus_round = 0
        self.error = None
        self.interrupt_reason = None

        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at
        self.finished_at = None

        self.workflow_config = workflow_config_to_dict(workflow)
        self.roles: dict[str, RoleLane] = {
            role.role_key: RoleLane.from_workflow_role(role)
            for role in workflow.roles
        }
        self.stream_events: list[dict] = []
        self.artifacts: list[dict] = []
        self.interventions: list[dict] = []

        self.event_cond = threading.Condition()
        self.status_lock = threading.Lock()
        self.proc_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.active_proc = None
        self.active_pgid = None
        self.log_dir = LOG_DIR / session_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.is_git_repo = False
        self.exec_baseline_ref = None
        self.exec_baseline_untracked = set()

    @property
    def resume_available(self) -> bool:
        return self.status in {"paused", "interrupted"}

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()
        if self.status in {"aborted", "done", "error"}:
            self.finished_at = self.finished_at or self.updated_at
        else:
            self.finished_at = None

    def to_public_state(self) -> dict:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "project_path": self.project_path,
            "workflow_template": self.workflow_template,
            "view_mode": self.view_mode,
            "status": self.status,
            "active_stage": self.active_stage,
            "current_round": self.current_round,
            "current_review_round": self.current_review_round,
            "consensus_round": self.consensus_round,
            "max_rounds": self.max_rounds,
            "max_review_rounds": self.max_review_rounds,
            "error": self.error,
            "interrupt_reason": self.interrupt_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "resume_available": self.resume_available,
        }

    @classmethod
    def from_persisted(
        cls,
        row: dict,
        workflow: dict,
        role_rows: list[dict],
        lane_rows: list[dict],
        event_rows: list[dict],
        artifact_rows: list[dict],
        intervention_rows: list[dict],
    ) -> "SessionState":
        workflow_obj = WorkflowConfig(
            view_mode=workflow.get("view_mode", "scene"),
            workflow_template=workflow.get("workflow_template", "standard"),
            max_rounds=workflow.get("max_rounds", row.get("max_rounds", 5)),
            max_review_rounds=workflow.get("max_review_rounds", row.get("max_review_rounds", 3)),
            roles=tuple(
                WorkflowRole(
                    role_key=role["role_key"],
                    tool_id=role["tool_id"],
                    enabled=bool(role.get("enabled", True)),
                    sort_order=int(role.get("sort_order", 0)),
                )
                for role in role_rows
            ),
        )
        sess = cls(row["id"], row["task"], row["project_path"], workflow_obj)
        sess.workflow_config = workflow
        sess.workflow_template = row.get("workflow_template") or workflow_obj.workflow_template
        sess.view_mode = row.get("view_mode") or workflow_obj.view_mode
        sess.status = row.get("status") or "interrupted"
        sess.active_stage = row.get("active_stage") or "planning"
        sess.current_round = row.get("current_round", 0)
        sess.current_review_round = row.get("current_review_round", 0)
        sess.consensus_round = row.get("consensus_round", 0)
        sess.max_rounds = row.get("max_rounds", workflow_obj.max_rounds)
        sess.max_review_rounds = row.get("max_review_rounds", workflow_obj.max_review_rounds)
        sess.error = row.get("error")
        sess.interrupt_reason = row.get("interrupt_reason")
        sess.created_at = row.get("created_at") or sess.created_at
        sess.updated_at = row.get("updated_at") or sess.updated_at
        sess.finished_at = row.get("finished_at")
        sess.roles = {}
        lane_by_role = {lane["role_key"]: lane for lane in lane_rows}
        for role in role_rows:
            lane_row = lane_by_role.get(role["role_key"], {})
            sess.roles[role["role_key"]] = RoleLane(
                lane_id=lane_row.get("id", f"lane_{role['role_key']}"),
                role_key=role["role_key"],
                tool_id=role["tool_id"],
                enabled=bool(role.get("enabled", True)),
                sort_order=int(role.get("sort_order", 0)),
                lane_status=lane_row.get("lane_status", "idle"),
                transport_kind=lane_row.get("transport_kind", "bridge-terminal"),
                viewport=json.loads(lane_row.get("viewport_json") or "{}"),
                last_seq=int(lane_row.get("last_seq", -1)),
                resume_state=json.loads(role.get("resume_state_json") or "{}"),
            )
        sess.stream_events = list(event_rows)
        sess.artifacts = list(artifact_rows)
        sess.interventions = list(intervention_rows)
        return sess


def set_persist_hook(fn):
    global _persist_hook
    _persist_hook = fn


def session_summary(sess: SessionState) -> dict:
    return {
        "session_id": sess.session_id,
        "task": sess.task,
        "project_path": sess.project_path,
        "view_mode": sess.view_mode,
        "status": sess.status,
        "active_stage": sess.active_stage,
        "current_round": sess.current_round,
        "current_review_round": sess.current_review_round,
        "max_rounds": sess.max_rounds,
        "max_review_rounds": sess.max_review_rounds,
        "updated_at": sess.updated_at,
        "created_at": sess.created_at,
        "finished_at": sess.finished_at,
        "interrupt_reason": sess.interrupt_reason,
    }


def lane_snapshot(sess: SessionState, role_key: str) -> dict:
    lane = sess.roles[role_key]
    return deepcopy(lane.to_wire())


def artifact_snapshot(artifact: dict) -> dict:
    return deepcopy(artifact)


def intervention_snapshot(intervention: dict) -> dict:
    return deepcopy(intervention)


def session_event_payload(sess: SessionState, **extra) -> dict:
    payload = {
        "session": sess.to_public_state(),
        "summary": session_summary(sess),
    }
    payload.update(extra)
    return payload


def lane_event_payload(sess: SessionState, role_key: str, **extra) -> dict:
    payload = {
        "lane": lane_snapshot(sess, role_key),
    }
    payload.update(extra)
    return payload


def event_snapshot(event: dict, *, include_projection: bool = False) -> dict:
    snapshot = deepcopy(event)
    if not isinstance(snapshot.get("data"), dict):
        return snapshot
    if include_projection:
        snapshot["data"]["projection"] = projection_payload_for_event(snapshot)
    else:
        snapshot["data"].pop("projection", None)
    return snapshot


def set_lane_viewport(
    sess: SessionState,
    role_key: str,
    *,
    width_px: int,
    height_px: int,
    cols: int,
    rows: int,
    source: str = "terminal",
) -> tuple[dict, bool]:
    lane = sess.roles[role_key]
    normalized = {
        "width_px": max(0, int(width_px)),
        "height_px": max(0, int(height_px)),
        "cols": max(1, int(cols)),
        "rows": max(1, int(rows)),
        "updated_at": datetime.now().isoformat(),
    }
    previous = lane.viewport or {}
    unchanged = (
        previous.get("width_px") == normalized["width_px"]
        and previous.get("height_px") == normalized["height_px"]
        and previous.get("cols") == normalized["cols"]
        and previous.get("rows") == normalized["rows"]
    )
    if unchanged:
        return lane_snapshot(sess, role_key), False

    lane.viewport = normalized
    add_event(
        sess,
        "lane.viewport_changed",
        lane_event_payload(
            sess,
            role_key,
            message=f"{normalized['cols']}x{normalized['rows']} ({normalized['width_px']}x{normalized['height_px']})",
        ),
        role_key=role_key,
        source=source,
    )
    return lane_snapshot(sess, role_key), True


def _persist_session(sess: SessionState) -> None:
    if _persist_hook is None:
        return
    try:
        if hasattr(_persist_hook, "save_session_state"):
            _persist_hook.save_session_state(sess)
        else:
            _persist_hook(sess)
    except Exception:
        pass


def _persist_lane(sess: SessionState, role_key: str) -> None:
    if _persist_hook is None:
        return
    try:
        if hasattr(_persist_hook, "upsert_lane"):
            _persist_hook.upsert_lane(sess, role_key)
        else:
            _persist_session(sess)
    except Exception:
        pass


def _persist_event(
    sess: SessionState,
    event: dict,
    *,
    artifact: dict | None = None,
    intervention: dict | None = None,
) -> None:
    if _persist_hook is None:
        return
    try:
        if hasattr(_persist_hook, "append_event"):
            _persist_hook.append_event(sess, event, artifact=artifact, intervention=intervention)
        else:
            _persist_session(sess)
    except Exception:
        pass


def register_session(sess: SessionState) -> None:
    with sessions_lock:
        sessions[sess.session_id] = sess


def get_session(session_id: str | None) -> SessionState | None:
    if not session_id:
        return None
    with sessions_lock:
        return sessions.get(session_id)


def remove_session(session_id: str) -> None:
    with sessions_lock:
        sessions.pop(session_id, None)


def add_event(
    sess: SessionState,
    event_type: str,
    data: dict,
    *,
    role_key: str | None = None,
    source: str | None = None,
    artifact: dict | None = None,
    intervention: dict | None = None,
) -> dict:
    if event_type not in STREAM_EVENT_TYPES:
        raise ValueError(f"undeclared event type: {event_type}")
    payload = deepcopy(data)
    evt = {
        "id": len(sess.stream_events),
        "type": event_type,
        "role_key": role_key,
        "source": source or (role_key or "system"),
        "data": payload,
        "ts": datetime.now().isoformat(),
    }
    with sess.event_cond:
        sess.touch()
        sess.stream_events.append(evt)
        if role_key and role_key in sess.roles:
            sess.roles[role_key].last_seq = evt["id"]
        sess.event_cond.notify_all()
    _persist_event(sess, evt, artifact=artifact, intervention=intervention)
    return evt


def publish_artifact(
    sess: SessionState,
    *,
    role_key: str,
    round_no: int,
    phase: str,
    artifact_kind: str,
    content: str,
    source_event_seq: int | None = None,
) -> dict:
    artifact = {
        "id": uuid.uuid4().hex,
        "session_id": sess.session_id,
        "lane_id": sess.roles[role_key].lane_id,
        "role_key": role_key,
        "round": round_no,
        "phase": phase,
        "artifact_kind": artifact_kind,
        "content": content,
        "source_event_seq": source_event_seq,
        "created_at": datetime.now().isoformat(),
    }
    with sess.event_cond:
        sess.touch()
        sess.artifacts.append(artifact)
        sess.event_cond.notify_all()
    add_event(
        sess,
        "artifact.published",
        {
            "artifact": artifact_snapshot(artifact),
            "artifact_id": artifact["id"],
            "artifact_kind": artifact_kind,
            "phase": phase,
            "round": round_no,
            "content": content,
        },
        role_key=role_key,
        source="artifact",
        artifact=artifact,
    )
    return artifact


def add_intervention(
    sess: SessionState,
    *,
    origin_view: str,
    origin_role_key: str | None,
    target_roles: tuple[str, ...],
    target_scope: str,
    text: str = "",
    command: str | None = None,
    status: str = "queued",
) -> dict:
    if status not in INTERVENTION_STATUSES:
        raise ValueError(f"invalid intervention status: {status}")
    intervention = {
        "id": uuid.uuid4().hex,
        "session_id": sess.session_id,
        "origin_view": origin_view,
        "origin_role_key": origin_role_key,
        "target_roles": list(target_roles),
        "target_scope": target_scope,
        "text": text,
        "command": command,
        "status": status,
        "consumed_by_roles": {},
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    with sess.event_cond:
        sess.touch()
        sess.interventions.append(intervention)
        sess.event_cond.notify_all()
    add_event(
        sess,
        "intervention.received",
        {
            "intervention": intervention_snapshot(intervention),
            "intervention_id": intervention["id"],
            "origin_view": origin_view,
            "target_roles": intervention["target_roles"],
            "target_scope": target_scope,
            "text": text,
            "command": command,
            "status": intervention["status"],
        },
        role_key=origin_role_key,
        source="intervention",
        intervention=intervention,
    )
    return intervention


def consume_interventions(sess: SessionState, role_key: str, round_no: int) -> list[str]:
    consumed = []
    for intervention in sess.interventions:
        if intervention["status"] not in INTERVENTION_STATUSES or intervention["status"] in {"cancelled", "rejected"}:
            continue
        if role_key not in intervention.get("target_roles", []):
            continue
        if role_key in intervention["consumed_by_roles"]:
            continue
        text = (intervention.get("text") or "").strip()
        if not text:
            continue
        intervention["status"] = "acknowledged"
        intervention["consumed_by_roles"][role_key] = {"round": round_no, "ts": datetime.now().isoformat()}
        intervention["updated_at"] = datetime.now().isoformat()
        if all(r in intervention["consumed_by_roles"] for r in intervention.get("target_roles", [])):
            intervention["status"] = "consumed"
        consumed.append(text)
        add_event(
            sess,
            "intervention.consumed",
            {
                "intervention": intervention_snapshot(intervention),
                "intervention_id": intervention["id"],
                "status": intervention["status"],
                "round": round_no,
            },
            role_key=role_key,
            source="intervention",
            intervention=intervention,
        )
    return consumed


def latest_artifact(sess: SessionState, artifact_kind: str, role_key: str | None = None) -> dict | None:
    for artifact in reversed(sess.artifacts):
        if artifact["artifact_kind"] != artifact_kind:
            continue
        if role_key and artifact["role_key"] != role_key:
            continue
        return artifact
    return None


def last_complete_round(sess: SessionState) -> int:
    plan_rounds = {artifact["round"] for artifact in sess.artifacts if artifact["artifact_kind"] == "plan"}
    review_rounds = {artifact["round"] for artifact in sess.artifacts if artifact["artifact_kind"] == "review"}
    complete = plan_rounds & review_rounds
    return max(complete) if complete else 0


def touch_status(sess: SessionState, *, status: str | None = None, active_stage: str | None = None, error: str | None = None, interrupt_reason: str | None = None) -> None:
    with sess.status_lock:
        if status is not None:
            sess.status = status
        if active_stage is not None:
            sess.active_stage = active_stage
        if error is not None:
            sess.error = error
        sess.interrupt_reason = interrupt_reason
    sess.touch()


def add_history_event(sess: SessionState, history_list, entry, event_type):
    # Legacy compatibility shim for modules not yet migrated.
    return add_event(sess, event_type, entry, role_key=entry.get("role"))
