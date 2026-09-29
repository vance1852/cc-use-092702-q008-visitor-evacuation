"""确定性的承载容量计算、预约决策与疏散批次编组。

所有函数都是纯函数，不触碰数据库：service 层负责把容量版本、关闭窗口、
在园状态和既有占用组装成入参，再把这里的结论落库。每个结论都带
``capacity_sources``，说明决定使用的容量来源（资源修订版本、名义容量、
生效容量、叠加的关闭窗口和预警硬关闭标记）。

一个预约需求用四元组 ``(domain, resource_id, slot, units)`` 表示：分时入园、
步道方向、摆渡席位按人数占用，停车按 1 个泊位占用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


RETAIN = "retain"
RESCHEDULE = "reschedule"
DENY = "deny"
EVACUATE = "evacuate"

SLOT_MINUTES = 30

# 重点人群的疏散优先级：数字越小越先编入批次。
ASSISTANCE_RANK = {"medical": 0, "wheelchair": 1, "elderly": 2, "stroller": 3, "guide": 4}
NO_ASSISTANCE_RANK = 5

HANDOVER_CHECKLIST = (
    "身份与预约核对",
    "重点人群协助装备交接",
    "随身物品清点",
    "摆渡登车席位确认",
    "抵达集结点签字",
)

ASSEMBLY_POINTS = ("东门集结点", "南门集结点", "西门集结点")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def slot_bounds(slot: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(slot).replace(tzinfo=timezone.utc)
    return start, start + timedelta(minutes=SLOT_MINUTES)


def slot_overlaps(slot: str, starts_at: str, ends_at: str) -> bool:
    """判断关闭窗口是否与某个分时区间（左闭右开）相交。"""
    start, end = slot_bounds(slot)
    window_start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    return window_start < end and window_end > start


def effective_capacity(nominal: int, closure_percents: Iterable[int], *, hard_closed: bool = False) -> int:
    """名义容量依次乘以各重叠关闭窗口的百分比；预警硬关闭直接归零。"""
    if nominal < 0:
        raise ValueError("名义容量不能为负数")
    if hard_closed:
        return 0
    result = float(nominal)
    for percent in closure_percents:
        if not 0 <= percent <= 100:
            raise ValueError("关闭窗口百分比必须在 0 到 100 之间")
        result *= percent / 100
    return int(result // 1)  # 容量只能整人占用，余量不可用


def cap_key(domain: str, resource_id: str, slot: str) -> str:
    return f"{domain}|{resource_id}|{slot}"


def reservation_requirements(reservation: Mapping[str, Any]) -> list[tuple[str, str, str, int]]:
    """从预约字段推导资源需求：停车占 1 个泊位，其余按同行人数占用。"""
    slot = str(reservation["entry_slot"])
    people = int(reservation["party_size"])
    requirements: list[tuple[str, str, str, int]] = [
        ("entry_slot", str(reservation["entry_resource_id"]), slot, people)
    ]
    if reservation.get("trail_resource_id"):
        requirements.append(("trail", str(reservation["trail_resource_id"]), slot, people))
    if reservation.get("shuttle_resource_id"):
        requirements.append(("shuttle", str(reservation["shuttle_resource_id"]), slot, people))
    if reservation.get("parking_resource_id"):
        requirements.append(("parking", str(reservation["parking_resource_id"]), slot, 1))
    return requirements


def _view(capacity: Mapping[str, Mapping[str, Any]], key: str) -> Mapping[str, Any] | None:
    return capacity.get(key)


def _capacity_sources(
    requirements: Sequence[tuple[str, str, str, int]],
    slot: str,
    capacity: Mapping[str, Mapping[str, Any]],
    held_by_key: Mapping[str, int],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for domain, resource_id, _requirement_slot, units in requirements:
        key = cap_key(domain, resource_id, slot)
        view = _view(capacity, key)
        held = held_by_key.get(key, 0)
        if view is None:
            sources.append({
                "domain": domain,
                "resource_id": resource_id,
                "slot": slot,
                "status": "missing",
                "revision": None,
                "nominal_capacity": None,
                "effective_capacity": 0,
                "required_units": units,
                "held_units": held,
                "headroom": 0,
                "closures": [],
                "alert_hard_closed": False,
            })
            continue
        sources.append({
            "domain": domain,
            "resource_id": resource_id,
            "slot": slot,
            "status": "ok",
            "revision": int(view["revision"]),
            "nominal_capacity": int(view["nominal_capacity"]),
            "effective_capacity": int(view["effective_capacity"]),
            "required_units": units,
            "held_units": held,
            "headroom": max(0, int(view["effective_capacity"]) - held),
            "closures": list(view.get("closures", ())),
            "alert_hard_closed": bool(view.get("alert_hard_closed", False)),
        })
    return sources


def _fits(
    requirements: Sequence[tuple[str, str, str, int]],
    slot: str,
    capacity: Mapping[str, Mapping[str, Any]],
    held_by_key: Mapping[str, int],
) -> bool:
    for domain, resource_id, _requirement_slot, units in requirements:
        key = cap_key(domain, resource_id, slot)
        view = _view(capacity, key)
        if view is None:
            return False
        if int(view["effective_capacity"]) - held_by_key.get(key, 0) < units:
            return False
    return True


def _block_reason(
    requirements: Sequence[tuple[str, str, str, int]],
    slot: str,
    capacity: Mapping[str, Mapping[str, Any]],
    held_by_key: Mapping[str, int],
) -> str | None:
    """返回阻止在该时段成行的首个原因代码。"""
    for domain, resource_id, _requirement_slot, units in requirements:
        view = _view(capacity, cap_key(domain, resource_id, slot))
        if view is None:
            return "resource_not_in_snapshot"
        if view.get("alert_hard_closed"):
            return "weather_alert_closed"
        if int(view["effective_capacity"]) == 0:
            return "closure_window" if view.get("closures") else "capacity_zero"
        if int(view["effective_capacity"]) - held_by_key.get(cap_key(domain, resource_id, slot), 0) < units:
            return "slot_oversubscribed"
    return None


def _candidate_slots(
    capacity: Mapping[str, Mapping[str, Any]],
    entry_resource_id: str,
    current_slot: str,
    *,
    same_day_only: bool,
) -> list[str]:
    prefix = f"entry_slot|{entry_resource_id}|"
    slots = {key.split("|", 2)[2] for key in capacity if key.startswith(prefix)}
    ordered = sorted(slot for slot in slots if slot > current_slot)
    if same_day_only:
        current_date = current_slot[:10]
        ordered = [slot for slot in ordered if slot[:10] == current_date]
    return ordered


def booking_decisions(
    reservations: Sequence[Mapping[str, Any]],
    capacity: Mapping[str, Mapping[str, Any]],
    base_holds: Mapping[str, int],
) -> list[dict[str, Any]]:
    """为待确认预约生成保留/改签/拒绝方案（普通订座只在当日内改分时）。"""
    held_by_key = dict(base_holds)
    decisions: list[dict[str, Any]] = []
    ordered = sorted(reservations, key=lambda item: (str(item.get("created_at", "")), str(item["reservation_id"])))
    for reservation in ordered:
        slot = str(reservation["entry_slot"])
        requirements = reservation_requirements(reservation)
        reason = _block_reason(requirements, slot, capacity, held_by_key)
        action = RETAIN
        target_slot: str | None = None
        detail = "所有关联资源在当前快照下均有足够余量"
        if reason is not None:
            alternative = None
            for candidate in _candidate_slots(capacity, str(reservation["entry_resource_id"]), slot, same_day_only=True):
                if _fits(requirements, candidate, capacity, held_by_key):
                    alternative = candidate
                    break
            if alternative is not None:
                action = RESCHEDULE
                target_slot = alternative
                detail = f"原时段不可成行（{reason}），当日稍后时段仍可承载"
            else:
                action = DENY
                detail = f"原时段不可成行（{reason}），当日内无任何可承载全单的时段"
        sources = _capacity_sources(requirements, slot, capacity, held_by_key)
        target_sources: list[dict[str, Any]] = []
        if action == RETAIN:
            for domain, resource_id, _requirement_slot, units in requirements:
                key = cap_key(domain, resource_id, slot)
                held_by_key[key] = held_by_key.get(key, 0) + units
        elif action == RESCHEDULE:
            # 方案一旦确认就会占用目标时段，因此连续编排时把目标占用计入模拟。
            target_sources = _capacity_sources(requirements, target_slot, capacity, held_by_key)  # type: ignore[arg-type]
            for domain, resource_id, _requirement_slot, units in requirements:
                key = cap_key(domain, resource_id, target_slot)  # type: ignore[arg-type]
                held_by_key[key] = held_by_key.get(key, 0) + units
        decisions.append({
            "reservation_id": str(reservation["reservation_id"]),
            "action": action,
            "reason_code": "capacity_available" if action == RETAIN else (
                "later_slot_available" if action == RESCHEDULE else "no_capacity_rest_of_day"
            ),
            "reason_detail": detail,
            "blocked_by": reason,
            "target": {} if target_slot is None else {"entry_slot": target_slot},
            "capacity_sources": sources,
            "target_capacity_sources": target_sources,
            "assistance": list(reservation.get("assistance", ())),
        })
    return decisions


def weather_decisions(
    reservations: Sequence[Mapping[str, Any]],
    capacity: Mapping[str, Mapping[str, Any]],
    base_holds: Mapping[str, int],
    affected_resources: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """预警触发后的方案：在园者只疏散、不自动改签未来；未入园者可改签或退订。"""
    affected = set(affected_resources)
    decisions: list[dict[str, Any]] = []
    held_by_key = dict(base_holds)
    ordered = sorted(reservations, key=lambda item: (str(item.get("created_at", "")), str(item["reservation_id"])))
    for reservation in ordered:
        slot = str(reservation["entry_slot"])
        requirements = reservation_requirements(reservation)
        on_site = reservation.get("state") == "checked_in"
        touches_alert = any((domain, resource_id) in affected for domain, resource_id, _s, _u in requirements)
        reason = _block_reason(requirements, slot, capacity, held_by_key)
        # 已入园游客：只要预约资源受预警影响或当前不可用，必须疏散；
        # 任何情况下都不得把在园游客自动改签到未来时段。
        if on_site and (touches_alert or reason is not None):
            action = EVACUATE
            reason_code = "alert_visitor_on_site"
            detail = "游客已入园且处于预警缩小开放范围，必须疏散，不得自动改签到未来时段"
            target: dict[str, Any] = {}
            target_sources: list[dict[str, Any]] = []
        elif reason is None:
            action = RETAIN
            reason_code = "outside_affected_scope"
            detail = "预约资源不在预警影响范围且容量充足，予以保留"
            target = {}
            target_sources = []
        else:
            alternative = None
            for candidate in _candidate_slots(capacity, str(reservation["entry_resource_id"]), slot, same_day_only=False):
                if _fits(requirements, candidate, capacity, held_by_key):
                    alternative = candidate
                    break
            if alternative is not None:
                action = RESCHEDULE
                reason_code = "alert_future_slot_available"
                detail = f"原时段受预警影响（{reason}），游客尚未入园，可改签到稍后可承载时段"
                target = {"entry_slot": alternative}
            else:
                action = DENY
                reason_code = "alert_closed_without_alternative"
                detail = f"原时段受预警影响（{reason}）且无后续可承载时段，预约必须取消并释放名额"
                target = {}
        sources = _capacity_sources(requirements, slot, capacity, held_by_key)
        if action == RESCHEDULE:
            target_sources = _capacity_sources(requirements, target["entry_slot"], capacity, held_by_key)
            for domain, resource_id, _requirement_slot, units in requirements:
                key = cap_key(domain, resource_id, target["entry_slot"])
                held_by_key[key] = held_by_key.get(key, 0) + units
        decisions.append({
            "reservation_id": str(reservation["reservation_id"]),
            "action": action,
            "reason_code": reason_code,
            "reason_detail": detail,
            "blocked_by": reason,
            "target": target,
            "capacity_sources": sources,
            "target_capacity_sources": target_sources,
            "assistance": list(reservation.get("assistance", ())),
            "on_site": on_site,
        })
    return decisions


def _assistance_kinds(decision: Mapping[str, Any]) -> list[str]:
    return [
        str(item["kind"]) if isinstance(item, Mapping) else str(item)
        for item in decision.get("assistance", ())
    ]


def safety_actions_for(decision: Mapping[str, Any]) -> list[tuple[str, str]]:
    """根据决策推导必须闭环的安全动作 (code, detail)，供服务层写入台账。"""
    action = decision["action"]
    actions: list[tuple[str, str]] = []
    kinds = _assistance_kinds(decision)
    if action == RETAIN:
        for kind in kinds:
            actions.append((f"assistance:{kind}", f"为重点人群（{kind}）确认入园协助"))
    elif action == RESCHEDULE:
        actions.append(("notify:reschedule", "通知游客改签后的入园时段"))
        for kind in kinds:
            actions.append((f"assistance:{kind}", f"改签后重新确认 {kind} 协助安排"))
    elif action == DENY:
        actions.append(("notify:cancel", "通知游客预约取消与退票/退费安排"))
    elif action == EVACUATE:
        actions.append(("notify:evacuation", "向游客推送疏散路线与集结点"))
        for kind in kinds:
            actions.append((f"evac_assist:{kind}", f"疏散全程 {kind} 重点人群专人陪护"))
        actions.append(("vehicle:dispatch", "确认摆渡车接驳并登车清点"))
        actions.append(("headcount", "集结点完成人数核对"))
        actions.append(("handover", "完成与接收方的交接清单签字"))
    return actions


def _evacuee_rank(evacuee: Mapping[str, Any]) -> tuple[int, str]:
    kinds = [
        str(item["kind"]) if isinstance(item, Mapping) else str(item)
        for item in evacuee.get("assistance", ())
    ]
    rank = min((ASSISTANCE_RANK.get(kind, NO_ASSISTANCE_RANK) for kind in kinds), default=NO_ASSISTANCE_RANK)
    return rank, str(evacuee["reservation_id"])


def _member_dict(evacuee: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reservation_id": str(evacuee["reservation_id"]),
        "pax": int(evacuee["pax"]),
        "assistance": list(evacuee.get("assistance", ())),
        "checklist": list(HANDOVER_CHECKLIST),
    }


def _batch_dict(
    sequence_no: int,
    members: list[dict[str, Any]],
    shuttle: Mapping[str, Any] | None,
    seats: int,
    *,
    pending_vehicle: bool = False,
) -> dict[str, Any]:
    kinds = [
        str(item["kind"]) if isinstance(item, Mapping) else str(item)
        for member in members for item in member["assistance"]
    ]
    priority_kind = min(kinds, key=lambda kind: ASSISTANCE_RANK.get(kind, NO_ASSISTANCE_RANK)) if kinds else None
    return {
        "sequence_no": sequence_no + 1,
        "assembly_point": ASSEMBLY_POINTS[sequence_no % len(ASSEMBLY_POINTS)],
        "shuttle_resource_id": None if shuttle is None else str(shuttle["resource_id"]),
        "shuttle_slot": None if shuttle is None else str(shuttle["slot"]),
        "seats": seats,
        "occupied": sum(int(member["pax"]) for member in members),
        "priority_kind": priority_kind,
        "vehicle_pending": pending_vehicle,
        "members": members,
    }


def build_evacuation_batches(
    evacuees: Sequence[Mapping[str, Any]],
    shuttle_options: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """把在园受影响游客确定性地编入疏散批次并生成交接清单。

    重点人群优先；有可用摆渡时按席位装车，整单不拆分到不同车辆；没有可用
    摆渡时形成待车批次，由服务层把 vehicle:dispatch 保留为未完成动作。
    """
    ordered = sorted(evacuees, key=_evacuee_rank)
    shuttles = sorted(shuttle_options, key=lambda item: (str(item["slot"]), str(item["resource_id"])))
    batches: list[dict[str, Any]] = []
    queue = list(ordered)

    for shuttle in shuttles:
        if not queue:
            break
        seats = int(shuttle["seats"])
        members: list[dict[str, Any]] = []
        remaining = seats
        while queue:
            pax = int(queue[0]["pax"])
            if pax > remaining:
                break
            members.append(_member_dict(queue.pop(0)))
            remaining -= pax
        if members:
            batches.append(_batch_dict(len(batches), members, shuttle, seats))

    standby = [_member_dict(evacuee) for evacuee in queue]
    if standby:
        seats = sum(int(member["pax"]) for member in standby)
        batches.append(_batch_dict(len(batches), standby, None, seats, pending_vehicle=True))
    return {"batches": batches, "evacuees_total": len(ordered), "vehicles_used": len(shuttles)}
