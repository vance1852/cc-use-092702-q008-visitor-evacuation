"""贯通统一容量快照、原子占用、气象预警疏散与幂等交接的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import VisitorOrchestrationService


DATE = "2026-10-01"


def _resource(domain, resource_id, slot, capacity, direction=None):
    return {
        "domain": domain,
        "resource_id": resource_id,
        "name": resource_id,
        "slot": f"{DATE}T{slot}",
        "capacity": capacity,
        **({"direction": direction} if direction else {}),
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = VisitorOrchestrationService(
        connection, FrozenClock(datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
    )
    for user_id, role in (
        ("plan", "planner"),
        ("agent", "agent"),
        ("risk", "risk"),
        ("rescue", "rescue"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 1) 四类容量各自登记分时版本：入园名额、步道方向、摆渡席位、停车泊位。
    for slot, capacity in (("08:00", 10), ("08:30", 50), ("09:00", 50), ("10:00", 10)):
        service.register_resource("plan", _resource("entry_slot", "gate-east", slot, capacity))
    for slot, capacity in (("08:00", 5), ("08:30", 50), ("09:00", 50)):
        service.register_resource("plan", _resource("trail", "canyon-trail", slot, capacity, "up"))
    for slot, capacity in (("08:00", 30), ("08:30", 30), ("09:00", 30)):
        service.register_resource("plan", _resource("shuttle", "shuttle-line-a", slot, capacity))
    for slot, capacity in (("08:00", 20), ("08:30", 20), ("09:00", 20)):
        service.register_resource("plan", _resource("parking", "lot-north", slot, capacity))

    # 2) 国庆预约：整单同时占用四类关联资源，并登记重点人群协助需求。
    base_resources = {
        "entry_resource_id": "gate-east",
        "trail_resource_id": "canyon-trail",
        "trail_direction": "up",
        "shuttle_resource_id": "shuttle-line-a",
        "parking_resource_id": "lot-north",
    }
    service.submit_reservation("agent", {
        "reservation_id": "r-001", "party_size": 4, "entry_slot": f"{DATE}T08:00",
        **base_resources,
        "assistance": [{"visitor_id": "v-1", "kind": "medical", "note": "需医护陪同"}],
        "idempotency_key": "key-r001",
    })
    service.submit_reservation("agent", {
        "reservation_id": "r-002", "party_size": 4, "entry_slot": f"{DATE}T08:00",
        **base_resources,
        "assistance": [{"visitor_id": "v-2", "kind": "elderly"}],
        "idempotency_key": "key-r002",
    })
    service.submit_reservation("agent", {
        "reservation_id": "r-003", "party_size": 60, "entry_slot": f"{DATE}T08:30",
        **base_resources,
        "assistance": [{"visitor_id": "v-3", "kind": "wheelchair"}],
        "idempotency_key": "key-r003",
    })
    service.submit_reservation("agent", {
        "reservation_id": "r-004", "party_size": 2, "entry_slot": f"{DATE}T08:00",
        "entry_resource_id": "gate-east",
        "shuttle_resource_id": "shuttle-line-a",
        "parking_resource_id": "lot-north",
        "idempotency_key": "key-r004",
    })
    service.submit_reservation("agent", {
        "reservation_id": "r-005", "party_size": 2, "entry_slot": f"{DATE}T09:00",
        "entry_resource_id": "gate-east",
        "parking_resource_id": "lot-north",
        "idempotency_key": "key-r005",
    })

    # 3) 统一版本快照下生成带原因的保留/改签/拒绝方案。
    booking_plan = service.create_booking_plan("agent", DATE)
    actions = {d["reservation_id"]: d["action"] for d in booking_plan["decisions"]}
    assert actions == {"r-001": "retain", "r-002": "reschedule", "r-003": "deny",
                       "r-004": "retain", "r-005": "retain"}, actions
    r002 = next(d for d in booking_plan["decisions"] if d["reservation_id"] == "r-002")
    assert r002["target"]["entry_slot"] == f"{DATE}T08:30"
    assert r002["blocked_by"] == "slot_oversubscribed"
    assert {s["domain"] for s in r002["capacity_sources"]} == {"entry_slot", "trail", "shuttle", "parking"}
    assert any(s["domain"] == "trail" and s["revision"] == 1 for s in r002["capacity_sources"])

    # 4) 确认时原子占用全部关联资源。
    confirmed = service.confirm_booking_plan("agent", booking_plan["plan_id"])
    assert confirmed["state"] == "confirmed"
    assert service.reservation_status("agent", "r-001")["state"] == "confirmed"
    held = connection.execute(
        "SELECT COALESCE(SUM(units),0) AS units FROM reservation_resources "
        "WHERE domain='trail' AND resource_id='canyon-trail' AND slot=? AND state='held'",
        (f"{DATE}T08:00",),
    ).fetchone()["units"]
    assert held == 4, held

    # 5) 任一容量版本变化，引用它的整单确认必须失败。
    service.submit_reservation("agent", {
        "reservation_id": "r-006", "party_size": 3, "entry_slot": f"{DATE}T10:00",
        "entry_resource_id": "gate-east",
        "idempotency_key": "key-r006",
    })
    stale_plan = service.create_booking_plan("agent", DATE)
    service.adjust_capacity("plan", {"domain": "entry_slot", "resource_id": "gate-east",
                                     "slot": f"{DATE}T10:00", "capacity": 8})
    try:
        service.confirm_booking_plan("agent", stale_plan["plan_id"])
        raise AssertionError("容量版本变化后整单应当失败")
    except InvalidState as exc:
        assert "容量版本已变化" in str(exc)
    failed = service.plan(stale_plan["plan_id"], actor_id="audit")
    assert failed["state"] == "failed" and failed["failure_reason"]

    # 6) 一部分游客入园核销。
    service.check_in("agent", "r-001")

    # 7) 预警前先出现短时摆渡关闭窗口；随后气象预警缩小峡谷步道开放范围。
    service.announce_closure("risk", {
        "window_id": "win-shuttle-0800",
        "domain": "shuttle",
        "resource_id": "shuttle-line-a",
        "starts_at": f"{DATE}T08:00:00Z",
        "ends_at": f"{DATE}T08:30:00Z",
        "capacity_percent": 0,
        "reason": "接驳短时管制",
    })
    alert = service.issue_alert("risk", {
        "alert_id": "alert-orange-canyon",
        "level": "orange",
        "title": "峡谷强对流橙色预警",
        "issued_at": f"{DATE}T08:05:00Z",
        "affected_resources": [{"domain": "trail", "resource_id": "canyon-trail"}],
    })

    # 8) 预警编排：已入园者疏散且绝不自动改签未来；未入园者改签或取消。
    weather_plan = service.create_weather_plan("risk", alert["alert_id"])
    weather_actions = {d["reservation_id"]: d["action"] for d in weather_plan["decisions"]}
    assert weather_actions == {"r-001": "evacuate", "r-002": "deny",
                               "r-004": "reschedule", "r-005": "retain"}, weather_actions
    evac_decision = next(d for d in weather_plan["decisions"] if d["reservation_id"] == "r-001")
    assert evac_decision["reason_code"] == "alert_visitor_on_site"
    assert evac_decision["target"] == {}
    assert any(s["alert_hard_closed"] and s["effective_capacity"] == 0
               for s in evac_decision["capacity_sources"] if s["domain"] == "trail")
    r004 = next(d for d in weather_plan["decisions"] if d["reservation_id"] == "r-004")
    assert r004["target"]["entry_slot"] == f"{DATE}T08:30"
    assert r004["blocked_by"] == "closure_window"

    confirmed_weather = service.confirm_weather_plan("risk", weather_plan["plan_id"])
    evacuation_id = confirmed_weather["evacuation"]["evacuation_id"]
    batches = confirmed_weather["evacuation"]["batches"]
    assert len(batches) == 1, batches
    batch = batches[0]
    assert batch["members"][0]["reservation_id"] == "r-001"
    assert batch["priority_kind"] == "medical"
    assert batch["shuttle_resource_id"] == "shuttle-line-a"
    assert batch["shuttle_slot"] == f"{DATE}T08:30"
    checklist = batch["members"][0]["checklist"]
    assert len(checklist) == 5 and "抵达集结点签字" in checklist
    assert service.reservation_status("agent", "r-001")["state"] == "evacuating"

    # 9) 可执行疏散批次：发车、抵达、交接，交接后才释放在园名额。
    service.dispatch_batch("rescue", batch["batch_id"], "crew-1")
    service.arrive_batch("rescue", batch["batch_id"])
    before = connection.execute(
        "SELECT COUNT(*) AS c FROM reservation_resources WHERE reservation_id='r-001' AND state='held'"
    ).fetchone()["c"]
    assert before >= 1
    handover = service.handover_batch("rescue", batch["batch_id"], "南门救助站", "handover-r001-01")
    assert handover["released_reservations"] == ["r-001"]
    replayed = service.handover_batch("rescue", batch["batch_id"], "南门救助站", "handover-r001-01")
    assert replayed == handover  # 重复回执回放同一结果，绝不重复释放名额
    after = connection.execute(
        "SELECT COUNT(*) AS c FROM reservation_resources WHERE reservation_id='r-001' AND state='held'"
    ).fetchone()["c"]
    assert after == 0
    assert service.reservation_status("agent", "r-001")["state"] == "evacuated"
    assert service.evacuation("rescue", evacuation_id)["state"] == "completed"

    # 10) 查询必须说明容量来源和仍未完成的安全动作；闭环剩余动作。
    status = service.reservation_status("agent", "r-004")
    assert status["state"] == "rescheduled"
    assert any(d["action"] == "reschedule" for d in status["decisions"])
    open_codes = {a["code"] for a in service.plan(weather_plan["plan_id"], actor_id="audit")["open_safety_actions"]}
    assert "notify:reschedule" in open_codes and "notify:cancel" in open_codes
    notify_cancel = next(
        a for a in service.plan(weather_plan["plan_id"], actor_id="audit")["open_safety_actions"]
        if a["code"] == "notify:cancel" and a["reservation_id"] == "r-002"
    )
    service.complete_safety_action("agent", notify_cancel["action_id"])
    remaining = service.plan(weather_plan["plan_id"], actor_id="audit")["open_safety_actions"]
    assert all(not (a["code"] == "notify:cancel" and a["reservation_id"] == "r-002") for a in remaining)

    result = {
        "status": "ok",
        "booking_plan_id": booking_plan["plan_id"],
        "booking_actions": actions,
        "stale_plan_state": failed["state"],
        "weather_plan_id": weather_plan["plan_id"],
        "weather_actions": weather_actions,
        "evacuation_id": evacuation_id,
        "batch_id": batch["batch_id"],
        "handover_replayed": replayed == handover,
        "open_safety_actions": len(remaining),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行游客承载编排服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
