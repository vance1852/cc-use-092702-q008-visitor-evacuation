"""游客承载编排领域输入契约。

容量域分为四类，各自独立维护版本，但在编排确认时被同一版本快照统一引用：

- entry_slot  分时入园名额（闸机/园区总体分时配额）；
- trail       峡谷步道方向容量（单向放行，按方向计）；
- shuttle     交通接驳（摆渡车班次席位）；
- parking    停车区泊位。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DOMAIN_KINDS = {"entry_slot", "trail", "shuttle", "parking"}
ALERT_LEVELS = {"blue", "yellow", "orange", "red"}
ASSISTANCE_KINDS = {"wheelchair", "stroller", "elderly", "medical", "guide"}
# 形如 2026-10-01T09:30 的整点/半点分时标签。
SLOT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):(00|30)$")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field} 必须是非负整数")
    return value


def positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def slot_label(value: object, field: str = "slot") -> str:
    result = required_text(value, field, 16)
    if not SLOT_PATTERN.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DDThh:00 或 YYYY-MM-DDThh:30 形式的分时标签")
    return result


def timestamp(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


@dataclass(frozen=True, slots=True)
class CapacityResource:
    """一条可编排容量资源（分时名额行、步道方向、摆渡班次、停车分区）。"""

    domain: str
    resource_id: str
    name: str
    slot: str
    capacity: int
    direction: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapacityResource":
        domain = required_text(raw.get("domain"), "domain", 16)
        if domain not in DOMAIN_KINDS:
            raise ValidationFailed("domain 必须是 entry_slot、trail、shuttle 或 parking")
        capacity = nonnegative_int(raw.get("capacity"), "capacity")
        direction = None
        if domain == "trail":
            direction = required_text(raw.get("direction"), "direction", 16).lower()
            if direction not in {"up", "down"}:
                raise ValidationFailed("步道方向必须是 up 或 down")
        return cls(
            domain=domain,
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            name=required_text(raw.get("name"), "name"),
            slot=slot_label(raw.get("slot")),
            capacity=capacity,
            direction=direction,
        )


@dataclass(frozen=True, slots=True)
class ClosureWindow:
    """临时关闭窗口：在 [starts_at, ends_at) 内将某资源容量压到 capacity_percent。"""

    window_id: str
    domain: str
    resource_id: str
    starts_at: str
    ends_at: str
    capacity_percent: int
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ClosureWindow":
        domain = required_text(raw.get("domain"), "domain", 16)
        if domain not in DOMAIN_KINDS:
            raise ValidationFailed("domain 必须是 entry_slot、trail、shuttle 或 parking")
        starts_at = timestamp(raw.get("starts_at"), "starts_at")
        ends_at = timestamp(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percent = nonnegative_int(raw.get("capacity_percent"), "capacity_percent")
        if percent > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            domain=domain,
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            starts_at=starts_at,
            ends_at=ends_at,
            capacity_percent=percent,
            reason=required_text(raw.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class AssistanceNeed:
    """重点人群协助需求（轮椅、童车、老人陪同、医疗照护、向导）。"""

    visitor_id: str
    kind: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssistanceNeed":
        kind = required_text(raw.get("kind"), "kind", 16).lower()
        if kind not in ASSISTANCE_KINDS:
            raise ValidationFailed("kind 必须是 wheelchair、stroller、elderly、medical 或 guide")
        note = str(raw.get("note", "")).strip()
        return cls(
            visitor_id=identifier(raw.get("visitor_id"), "visitor_id"),
            kind=kind,
            note=note[:256],
        )


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    """一单预约：整单同时占用入园名额、步道方向、摆渡、停车等关联资源。"""

    reservation_id: str
    party_size: int
    entry_resource_id: str
    entry_slot: str
    trail_resource_id: str | None
    trail_direction: str | None
    shuttle_resource_id: str | None
    parking_resource_id: str | None
    assistance: tuple[AssistanceNeed, ...]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationRequest":
        assistance_raw = raw.get("assistance", ())
        if not isinstance(assistance_raw, (list, tuple)):
            raise ValidationFailed("assistance 必须是数组")
        assistance = tuple(AssistanceNeed.from_dict(item) for item in assistance_raw)
        visitors = {item.visitor_id for item in assistance}
        if len(visitors) != len(assistance):
            raise ValidationFailed("同一预约内协助需求的 visitor_id 不能重复")
        trail_direction = raw.get("trail_direction")
        if trail_direction is not None:
            trail_direction = required_text(trail_direction, "trail_direction", 16).lower()
            if trail_direction not in {"up", "down"}:
                raise ValidationFailed("trail_direction 必须是 up 或 down")
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            party_size=positive_int(raw.get("party_size"), "party_size"),
            entry_resource_id=identifier(raw.get("entry_resource_id"), "entry_resource_id"),
            entry_slot=slot_label(raw.get("entry_slot")),
            trail_resource_id=None if raw.get("trail_resource_id") is None else identifier(raw.get("trail_resource_id"), "trail_resource_id"),
            trail_direction=trail_direction,
            shuttle_resource_id=None if raw.get("shuttle_resource_id") is None else identifier(raw.get("shuttle_resource_id"), "shuttle_resource_id"),
            parking_resource_id=None if raw.get("parking_resource_id") is None else identifier(raw.get("parking_resource_id"), "parking_resource_id"),
            assistance=assistance,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class WeatherAlert:
    """气象预警：缩小开放范围，并触发在园游客的疏散编排。"""

    alert_id: str
    level: str
    title: str
    issued_at: str
    affected_resources: tuple[tuple[str, str], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WeatherAlert":
        level = required_text(raw.get("level"), "level", 16).lower()
        if level not in ALERT_LEVELS:
            raise ValidationFailed("level 必须是 blue、yellow、orange 或 red")
        affected_raw = raw.get("affected_resources", ())
        if not isinstance(affected_raw, (list, tuple)) or not affected_raw:
            raise ValidationFailed("affected_resources 必须是非空数组")
        affected: list[tuple[str, str]] = []
        for item in affected_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("affected_resources 项必须是对象")
            domain = required_text(item.get("domain"), "affected_resources.domain", 16)
            if domain not in DOMAIN_KINDS:
                raise ValidationFailed("affected_resources.domain 非法")
            affected.append((domain, identifier(item.get("resource_id"), "affected_resources.resource_id")))
        return cls(
            alert_id=identifier(raw.get("alert_id"), "alert_id"),
            level=level,
            title=required_text(raw.get("title"), "title"),
            issued_at=timestamp(raw.get("issued_at"), "issued_at"),
            affected_resources=tuple(affected),
        )
