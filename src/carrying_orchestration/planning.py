"""确定性的承载快照、保留/调整方案与疏散批次计算（无副作用纯函数）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


KIND_PRIORITY = {"trail-direction": 0, "shuttle": 1, "entry-slot": 2, "parking": 3}
CHECKLIST_TEMPLATE: tuple[tuple[str, str], ...] = (
    ("notify_visitors", "通知本批次游客停止前往受影响区域"),
    ("headcount_assembly", "在集结点完成人数清点"),
    ("assistance_ready", "重点人群协助人员与装备到位"),
    ("transport_dispatch", "摆渡车/接驳车辆就位待发"),
    ("handover_received", "接收点完成人员交接签收"),
)
EVACUATE_STEP = "handover_received"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def resource_ref(resource_kind: str, resource_id: str, scope_key: str) -> str:
    return f"{resource_kind}|{resource_id}|{scope_key}"


def ref_matches(pattern: str, resource_kind: str, resource_id: str, scope_key: str) -> bool:
    """closed_resources 支持 kind|rid|scope、kind|rid 与 kind 三种粒度。"""
    exact = resource_ref(resource_kind, resource_id, scope_key)
    if pattern == exact or pattern == resource_kind:
        return True
    return pattern == f"{resource_kind}|{resource_id}"


def _window_active(window: Mapping[str, Any], at: str) -> bool:
    return window["starts_at"] <= at < window["ends_at"]


def _alert_active(alert: Mapping[str, Any], at: str) -> bool:
    return alert["starts_at"] <= at < alert["ends_at"]


def effective_capacity(
    base: int,
    at: str,
    *,
    windows: Sequence[Mapping[str, Any]] = (),
    alerts: Sequence[Mapping[str, Any]] = (),
    resource_kind: str = "",
    resource_id: str = "",
    scope_key: str = "",
) -> dict[str, Any]:
    """返回某时刻的有效容量及压减来源（临时关闭窗口与气象预警）。"""
    value = max(0, base)
    active_window_ids: list[str] = []
    active_alert_ids: list[str] = []
    closure_percent = 100
    for window in windows:
        if _window_active(window, at):
            active_window_ids.append(window["window_id"])
            closure_percent = min(closure_percent, int(window["capacity_percent"]))
    forced_zero = False
    for alert in alerts:
        if not _alert_active(alert, at):
            continue
        if any(ref_matches(pattern, resource_kind, resource_id, scope_key) for pattern in alert["refs"]):
            active_alert_ids.append(alert["alert_id"])
            forced_zero = True
    if active_window_ids:
        value = value * closure_percent // 100
    if forced_zero:
        value = 0
    return {
        "capacity": value,
        "closure_window_ids": sorted(active_window_ids),
        "alert_ids": sorted(active_alert_ids),
        "forced_zero": forced_zero,
    }


def build_manifest(
    versions: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    alerts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """构造容量版本快照的规范化清单；任何输入变化都会改变其 sha256。"""
    version_rows = sorted(
        (
            {
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "scope_key": row["scope_key"],
                "version_id": int(row["version_id"]),
                "capacity": int(row["capacity"]),
                "source_revision": row["source_revision"],
            }
            for row in versions
        ),
        key=lambda item: (item["resource_kind"], item["resource_id"], item["scope_key"]),
    )
    window_rows = sorted(
        (
            {
                "window_id": row["window_id"],
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "scope_key": row["scope_key"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "capacity_percent": int(row["capacity_percent"]),
                "state": row.get("state", "announced"),
            }
            for row in windows
        ),
        key=lambda item: item["window_id"],
    )
    alert_rows = sorted(
        (
            {
                "alert_id": row["alert_id"],
                "level": row["level"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "state": row.get("state", "active"),
                "refs": sorted(row["refs"]),
            }
            for row in alerts
        ),
        key=lambda item: item["alert_id"],
    )
    return {"versions": version_rows, "windows": window_rows, "alerts": alert_rows}


def _resource_index(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        resource_ref(row["resource_kind"], row["resource_id"], row["scope_key"]): row
        for row in snapshot["resources"]
    }


def _capacity_source(
    index: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    requirement: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    ref = resource_ref(requirement["kind"], requirement["rid"], requirement["scope"])
    version = index.get(ref)
    windows = [
        window
        for window in snapshot["windows"]
        if window["resource_kind"] == requirement["kind"]
        and window["resource_id"] == requirement["rid"]
        and window["scope_key"] == requirement["scope"]
    ]
    if version is None:
        return {
            "resource_kind": requirement["kind"],
            "resource_id": requirement["rid"],
            "scope_key": requirement["scope"],
            "needed": int(requirement["quantity"]),
            "base_capacity": 0,
            "effective_capacity": 0,
            "version_id": None,
            "source_revision": None,
            "closure_window_ids": [],
            "alert_ids": [],
            "forced_zero": True,
            "basis": "容量来源缺失：快照中没有该容量版本",
        }
    effect = effective_capacity(
        int(version["capacity"]),
        at,
        windows=windows,
        alerts=snapshot["alerts"],
        resource_kind=requirement["kind"],
        resource_id=requirement["rid"],
        scope_key=requirement["scope"],
    )
    basis = f"容量版本 v{version['version_id']}（来源修订 {version['source_revision']}）基准 {version['capacity']}"
    if effect["closure_window_ids"]:
        basis += f"；临时关闭窗口 {','.join(effect['closure_window_ids'])} 压减至 {effect['capacity']}"
    if effect["alert_ids"]:
        basis += f"；气象预警 {','.join(effect['alert_ids'])} 强制关闭"
    return {
        "resource_kind": requirement["kind"],
        "resource_id": requirement["rid"],
        "scope_key": requirement["scope"],
        "needed": int(requirement["quantity"]),
        "base_capacity": int(version["capacity"]),
        "effective_capacity": effect["capacity"],
        "version_id": version["version_id"],
        "source_revision": version["source_revision"],
        "closure_window_ids": effect["closure_window_ids"],
        "alert_ids": effect["alert_ids"],
        "forced_zero": effect["forced_zero"],
        "basis": basis,
    }


def _find_alternative(
    index: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    reservation: Mapping[str, Any],
    holds: dict[str, dict[str, int]],
    now: str,
) -> str | None:
    """为分时入园名额寻找可整体迁入的未来时段；关联资源必须同时存在且有余量。"""
    entry = next(item for item in reservation["requirements"] if item["kind"] == "entry-slot")
    candidate_scopes = sorted(
        {
            row["scope_key"]
            for row in snapshot["resources"]
            if row["resource_kind"] == "entry-slot"
            and row["resource_id"] == entry["rid"]
            and row["scope_key"] != entry["scope"]
            and row["scope_key"] > now
        }
    )
    for candidate in candidate_scopes:
        fits = True
        for requirement in reservation["requirements"]:
            target_scope = candidate if requirement["kind"] == "entry-slot" else requirement["scope"]
            moved = dict(requirement, scope=target_scope)
            source = _capacity_source(index, snapshot, moved, candidate)
            used = sum(
                quantity
                for rid, quantity in holds.get(
                    resource_ref(requirement["kind"], requirement["rid"], target_scope), {}
                ).items()
                if rid != reservation["reservation_id"]
            )
            if source["effective_capacity"] - used < requirement["quantity"]:
                fits = False
                break
        if fits:
            return candidate
    return None


def generate_plan(snapshot: Mapping[str, Any], reservations: Sequence[Mapping[str, Any]], now: str) -> dict[str, Any]:
    """基于同一版本快照生成带原因的保留/调整方案。

    已入园游客只会得到保留或疏散决定，永不会被自动改签到未来时段。
    """
    index = _resource_index(snapshot)
    # 每条资源当前被哪些预约占用（评估某预约时必须扣除它自身的占用，避免误判超额）
    holds: dict[str, dict[str, int]] = {}
    for reservation in reservations:
        if reservation["state"] in {"confirmed", "rescheduled", "checked_in", "evacuating"}:
            for item in reservation["requirements"]:
                key = resource_ref(item["kind"], item["rid"], item["scope"])
                per = holds.setdefault(key, {})
                per[reservation["reservation_id"]] = per.get(reservation["reservation_id"], 0) + int(item["quantity"])

    def used_by_others(ref: str, reservation_id: str) -> int:
        return sum(quantity for rid, quantity in holds.get(ref, {}).items() if rid != reservation_id)

    decisions: list[dict[str, Any]] = []
    evacuations: list[dict[str, Any]] = []
    ordered = sorted(reservations, key=lambda item: (item["created_at"], item["reservation_id"]))
    for reservation in ordered:
        requirements = reservation["requirements"]
        inside = reservation["state"] in {"checked_in", "evacuating"}
        eval_at = now if inside else reservation["enters_at"]
        sources = [_capacity_source(index, snapshot, item, eval_at) for item in requirements]
        reasons: list[str] = []
        unresolved: list[dict[str, Any]] = []
        for need in reservation["assistance"]:
            if not need.get("arranged"):
                unresolved.append({
                    "code": "assistance_unarranged",
                    "assistance_kind": need["assistance_kind"],
                    "headcount": int(need["headcount"]),
                    "message": f"重点人群协助 {need['assistance_kind']} 尚未安排落实",
                })

        blocked = [s for s in sources if s["forced_zero"] or s["effective_capacity"] == 0]
        proposed_scope: str | None = None
        if inside:
            if blocked:
                action = "evacuate"
                for source in blocked:
                    if source["alert_ids"]:
                        reasons.append(
                            f"weather_alert:{','.join(source['alert_ids'])} 关闭 {resource_ref(source['resource_kind'], source['resource_id'], source['scope_key'])}"
                        )
                    if source["closure_window_ids"]:
                        reasons.append(
                            f"closure_active:{','.join(source['closure_window_ids'])} 关闭 {resource_ref(source['resource_kind'], source['resource_id'], source['scope_key'])}"
                        )
                reasons.append("already_inside:已入园游客不自动改签，转入疏散批次")
            else:
                action = "retain_in_park"
                reasons.append("capacity_sufficient:在园游客当前占用容量仍有效")
        else:
            overflow: list[dict[str, Any]] = []
            for item, source in zip(requirements, sources):
                key = resource_ref(item["kind"], item["rid"], item["scope"])
                used = used_by_others(key, reservation["reservation_id"])
                if source["effective_capacity"] - used < item["quantity"]:
                    overflow.append(source)
            expired = reservation["enters_at"] <= now and reservation["state"] in {"reserved", "confirmed"}
            if not blocked and not overflow and not expired:
                action = "retain"
                for item in requirements:
                    key = resource_ref(item["kind"], item["rid"], item["scope"])
                    per = holds.setdefault(key, {})
                    per[reservation["reservation_id"]] = per.get(reservation["reservation_id"], 0) + int(item["quantity"])
                reasons.append("capacity_sufficient:各关联容量来源均满足整单占用")
            elif expired:
                action = "release_quota"
                reasons.append("slot_expired:预约时段已过且未入园，名额应予释放")
            else:
                for source in blocked:
                    if source["alert_ids"]:
                        reasons.append(
                            f"weather_alert:{','.join(source['alert_ids'])} 关闭 {resource_ref(source['resource_kind'], source['resource_id'], source['scope_key'])}"
                        )
                    if source["closure_window_ids"]:
                        reasons.append(
                            f"closure_active:{','.join(source['closure_window_ids'])} 关闭 {resource_ref(source['resource_kind'], source['resource_id'], source['scope_key'])}"
                        )
                for source in overflow:
                    reasons.append(
                        f"capacity_insufficient:{resource_ref(source['resource_kind'], source['resource_id'], source['scope_key'])} "
                        f"有效 {source['effective_capacity']} 需 {source['needed']}（版本 v{source['version_id']}）"
                    )
                proposed_scope = _find_alternative(index, snapshot, reservation, holds, now)
                if proposed_scope is not None:
                    action = "reschedule"
                    reasons.append(f"reschedule_alternative:可整体改至 {proposed_scope}")
                else:
                    action = "release_quota"
                    reasons.append("no_alternative:无满足全部关联资源的未来时段，释放名额并联系游客改约")

        moves = None
        if action == "reschedule" and proposed_scope is not None:
            # 改签决定展示的容量来源统一按目标时段重新计算（含保留原 scope 的关联资源）
            sources = [
                _capacity_source(
                    index,
                    snapshot,
                    dict(item, scope=proposed_scope if item["kind"] == "entry-slot" else item["scope"]),
                    proposed_scope,
                )
                for item in requirements
            ]
            moves = [
                {
                    "resource_kind": item["kind"],
                    "resource_id": item["rid"],
                    "from_scope": item["scope"],
                    "to_scope": proposed_scope,
                }
                for item in requirements
                if item["kind"] == "entry-slot"
            ]
        decision = {
            "reservation_id": reservation["reservation_id"],
            "visitor_name": reservation["visitor_name"],
            "party_size": int(reservation["party_size"]),
            "state": reservation["state"],
            "enters_at": reservation["enters_at"],
            "action": action,
            "reasons": reasons,
            "capacity_sources": sources,
            "proposed_enters_at": proposed_scope,
            "moves": moves,
            "assistance_needs": [
                {"assistance_kind": item["assistance_kind"], "headcount": int(item["headcount"]), "arranged": bool(item.get("arranged"))}
                for item in reservation["assistance"]
            ],
            "unresolved_safety_actions": unresolved,
        }
        decisions.append(decision)
        if action == "evacuate":
            evacuations.append({"reservation": reservation, "decision": decision, "blocked": blocked})

    batches = build_evacuation_batches(snapshot, evacuations, now)
    by_reservation = {
        member["reservation_id"]: member
        for batch in batches
        for member in batch["reservation_ids"]
    }
    for decision in decisions:
        if decision["action"] == "evacuate":
            member = by_reservation[decision["reservation_id"]]
            decision["evacuation_batch_id"] = member["batch_id"]
            decision["unresolved_safety_actions"].extend(member["pending_actions"])
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_revision": snapshot["revision"],
        "generated_at": now,
        "decisions": decisions,
        "evacuation_batches": [
            {key: value for key, value in batch.items() if key != "pending_actions"}
            for batch in batches
        ],
    }


def renumber_batches(result: Mapping[str, Any], plan_id: int) -> dict[str, Any]:
    """方案落库拿到自增 plan_id 后，把批次编号改为 EB-{plan_id}-{seq} 并同步决策引用。"""
    renamed = {
        batch["batch_id"]: f"EB-{plan_id}-{batch['sequence_no']:02d}"
        for batch in result["evacuation_batches"]
    }
    for batch in result["evacuation_batches"]:
        new_batch = renamed[batch["batch_id"]]
        batch["batch_id"] = new_batch
        for step in batch["checklist"]:
            step["ack_key"] = f"{new_batch}:{step['step_code']}"
        for member in batch["reservation_ids"]:
            member["batch_id"] = new_batch
            for action in member["pending_actions"]:
                action["ack_key"] = f"{new_batch}:{action['ack_key'].split(':')[-1]}"
    for decision in result["decisions"]:
        if decision.get("evacuation_batch_id") in renamed:
            new_batch = renamed[decision["evacuation_batch_id"]]
            decision["evacuation_batch_id"] = new_batch
            for action in decision["unresolved_safety_actions"]:
                if action.get("ack_key"):
                    action["ack_key"] = f"{new_batch}:{action['ack_key'].split(':')[-1]}"
    return result


def build_evacuation_batches(
    snapshot: Mapping[str, Any],
    evacuations: Sequence[Mapping[str, Any]],
    now: str,
) -> list[dict[str, Any]]:
    """把必须疏散的在园预约按受影响区域编成可执行批次与交接清单。"""
    sectors: dict[str, list[Mapping[str, Any]]] = {}
    for item in evacuations:
        blocked = item["blocked"]
        sector_source = min(
            blocked,
            key=lambda source: (
                KIND_PRIORITY.get(source["resource_kind"], 9),
                source["resource_id"],
                source["scope_key"],
            ),
        )
        sector = resource_ref(sector_source["resource_kind"], sector_source["resource_id"], sector_source["scope_key"])
        sectors.setdefault(sector, []).append(item)

    ordered_sectors = sorted(
        sectors,
        key=lambda key: -sum(
            int(need["headcount"])
            for item in sectors[key]
            for need in item["reservation"]["assistance"]
        ),
    )
    batches: list[dict[str, Any]] = []
    for sequence, sector in enumerate(ordered_sectors, start=1):
        members = sorted(
            sectors[sector],
            key=lambda item: (
                -sum(int(need["headcount"]) for need in item["reservation"]["assistance"]),
                item["reservation"]["reservation_id"],
            ),
        )
        headcount = sum(int(item["reservation"]["party_size"]) for item in members)
        assistance_headcount = sum(
            int(need["headcount"])
            for item in members
            for need in item["reservation"]["assistance"]
        )
        alert_ids = sorted({aid for item in members for source in item["blocked"] for aid in source["alert_ids"]})
        batch_id = f"EB-{snapshot['revision']}-{sequence:02d}"
        checklist = [
            {
                "step_code": step,
                "content": content,
                "ack_key": f"{batch_id}:{step}",
                "status": "pending",
            }
            for step, content in CHECKLIST_TEMPLATE
        ]
        pending_actions = [
            {
                "code": f"evacuation_checklist:{step['step_code']}",
                "ack_key": step["ack_key"],
                "message": step["content"],
            }
            for step in checklist
        ]
        batches.append({
            "batch_id": batch_id,
            "sequence_no": sequence,
            "sector_key": sector,
            "assembly_point": f"集结点-{sector.split('|')[1]}",
            "alert_ids": alert_ids,
            "headcount": headcount,
            "assistance_headcount": assistance_headcount,
            "reservation_ids": [
                {
                    "reservation_id": item["reservation"]["reservation_id"],
                    "batch_id": batch_id,
                    "pending_actions": pending_actions,
                }
                for item in members
            ],
            "members": [
                {
                    "reservation_id": item["reservation"]["reservation_id"],
                    "visitor_name": item["reservation"]["visitor_name"],
                    "party_size": int(item["reservation"]["party_size"]),
                    "assistance_needs": [
                        {"assistance_kind": need["assistance_kind"], "headcount": int(need["headcount"])}
                        for need in item["reservation"]["assistance"]
                    ],
                }
                for item in members
            ],
            "checklist": checklist,
            "status": "planned",
            "pending_actions": pending_actions,
        })
    return batches
