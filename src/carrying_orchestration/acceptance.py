"""贯通容量版本快照、带原因方案、原子确认、气象预警疏散与幂等回执的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CarryingOrchestrationService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))
    service = CarryingOrchestrationService(connection, clock)
    for user_id, role in (
        ("plan", "planner"),
        ("dispatch", "dispatcher"),
        ("risk", "risk"),
        ("ranger", "ranger"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    catalog = (
        ("entry-slot", "gate-main", "2026-10-01T01:00:00Z", 120, "entry-0900-v1"),
        ("entry-slot", "gate-main", "2026-10-01T03:00:00Z", 120, "entry-1100-v1"),
        ("trail-direction", "canyon-eastbound", "2026-10-01", 60, "trail-east-v1"),
        ("trail-direction", "canyon-westbound", "2026-10-01", 60, "trail-west-v1"),
        ("shuttle", "shuttle-line-a", "2026-10-01", 80, "shuttle-a-v1"),
        ("parking", "lot-north", "2026-10-01", 90, "parking-n-v1"),
    )
    for kind, rid, scope, capacity, revision in catalog:
        service.register_capacity_version("plan", {
            "resource_kind": kind, "resource_id": rid, "scope_key": scope,
            "capacity": capacity, "source_revision": revision,
            "effective_from": "2026-09-25T00:00:00Z",
        })

    def reserve(number: int, *, slot: str = "2026-10-01T01:00:00Z", party: int = 4,
                trail: str = "canyon-eastbound", assistance: str | None = None) -> None:
        service.submit_reservation("dispatch", {
            "reservation_id": f"res-{number:03d}",
            "visitor_name": f"家庭{number}",
            "contact": "13800000000",
            "party_size": party,
            "enters_at": slot,
            "requirements": [
                {"resource_kind": "entry-slot", "resource_id": "gate-main", "scope_key": slot, "quantity": party},
                {"resource_kind": "trail-direction", "resource_id": trail, "scope_key": "2026-10-01", "quantity": party},
                {"resource_kind": "shuttle", "resource_id": "shuttle-line-a", "scope_key": "2026-10-01", "quantity": party},
                {"resource_kind": "parking", "resource_id": "lot-north", "scope_key": "2026-10-01", "quantity": party},
            ],
            "assistance_needs": (
                [{"assistance_kind": assistance, "headcount": 1, "note": "国庆预约登记"}]
                if assistance else []
            ),
            "idempotency_key": f"idem-{number:03d}",
        })

    # 18 个家庭共 72 人：入园名额充足，但东向步道 60 人将成为瓶颈
    for number in range(18):
        reserve(number, assistance="wheelchair" if number == 0 else None)
    service.arrange_assistance("dispatch", "res-000", "wheelchair")

    holiday_plan = service.generate_adjustment_plan("dispatch")
    confirmed = service.confirm_plan("dispatch", holiday_plan["plan_id"], "confirm-national-day")
    retained = sum(1 for decision in holiday_plan["decisions"] if decision["action"] == "retain")
    released = sum(1 for decision in holiday_plan["decisions"] if decision["action"] == "release_quota")

    # 前三个家庭已经检票入园
    for number in range(3):
        service.check_in("ranger", f"res-{number:03d}")

    # 气象橙色预警：东向峡谷步道强制关闭
    service.trigger_weather_alert("risk", {
        "alert_id": "alert-orange-national-day",
        "level": "orange",
        "title": "国庆峡谷强降雨橙色预警",
        "starts_at": "2026-09-30T03:30:00Z",
        "ends_at": "2026-10-01T00:00:00Z",
        "closed_resources": ["trail-direction|canyon-eastbound"],
        "reason": "气象部门发布强降雨预警，东向峡谷步道须立即封闭",
    })
    clock.advance(hours=2)
    evacuation_plan = service.generate_adjustment_plan("risk")
    service.confirm_plan("dispatch", evacuation_plan["plan_id"], "confirm-evacuation")

    batch_id = evacuation_plan["evacuation_batches"][0]["batch_id"]
    steps = ["notify_visitors", "headcount_assembly", "assistance_ready", "transport_dispatch"]
    first_receipt = service.acknowledge_evacuation("ranger", batch_id, steps, "receipt-stage-1")
    replayed_receipt = service.acknowledge_evacuation("ranger", batch_id, steps, "receipt-stage-1")
    final_receipt = service.acknowledge_evacuation("ranger", batch_id, ["handover_received"], "receipt-handover")
    batch = service.evacuation_batch("audit", batch_id)
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "snapshot_revision": holiday_plan["snapshot_revision"],
        "national_day_plan": {
            "plan_id": holiday_plan["plan_id"],
            "retained_reservations": retained,
            "released_reservations": released,
            "confirmed_actions": len(confirmed["actions"]),
        },
        "weather_alert": "alert-orange-national-day",
        "evacuation": {
            "plan_id": evacuation_plan["plan_id"],
            "batch_id": batch_id,
            "sector_key": batch["sector_key"],
            "headcount": batch["headcount"],
            "assistance_headcount": batch["assistance_headcount"],
            "members": [member["reservation_id"] for member in batch["members"]],
            "first_receipt_status": first_receipt["status"],
            "duplicate_replay_identical": replayed_receipt == first_receipt,
            "final_status": final_receipt["status"],
            "released_once_at_handover": len(set(final_receipt["released_reservation_ids"]))
            == len(final_receipt["released_reservation_ids"]),
        },
        "audit": service.audit_chain("audit"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行统一承载编排服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
