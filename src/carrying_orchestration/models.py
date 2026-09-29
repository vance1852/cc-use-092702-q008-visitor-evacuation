"""承载编排领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RESOURCE_KINDS = {"entry-slot", "trail-direction", "shuttle", "parking"}
ASSISTANCE_KINDS = {"wheelchair", "stroller", "medication", "hearing", "visual", "elderly", "other"}
WEATHER_LEVELS = {"blue", "yellow", "orange", "red"}
WEATHER_ORDER = {"blue": 1, "yellow": 2, "orange": 3, "red": 4}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field_name} 必须是非负整数")
    return value


def positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field_name} 必须是正整数")
    return value


def iso_text(value: object, field_name: str) -> str:
    text = required_text(value, field_name, 40)
    try:
        from .clock import utc_text
        return utc_text(parse_utc(text, field_name))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def assistance_kind(value: object) -> str:
    result = required_text(value, "assistance_kind", 24).lower()
    if result not in ASSISTANCE_KINDS:
        raise ValidationFailed("assistance_kind 不是受支持的重点人群协助类型")
    return result


@dataclass(frozen=True, slots=True)
class CapacityVersionInput:
    """单条容量来源：分时入园名额 / 步道方向 / 摆渡车 / 停车区。"""

    resource_kind: str
    resource_id: str
    scope_key: str
    capacity: int
    source_revision: str
    effective_from: str
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapacityVersionInput":
        kind = required_text(raw.get("resource_kind"), "resource_kind", 24).lower()
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("resource_kind 必须是 entry-slot、trail-direction、shuttle 或 parking")
        return cls(
            resource_kind=kind,
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            scope_key=required_text(raw.get("scope_key"), "scope_key", 96),
            capacity=non_negative_int(raw.get("capacity"), "capacity"),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            effective_from=iso_text(raw.get("effective_from"), "effective_from"),
            note=required_text(raw.get("note", ""), "note", 256) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class ClosureWindowInput:
    """临时关闭窗口：在 [starts_at, ends_at) 内把某容量来源压到 capacity_percent%。"""

    window_id: str
    resource_kind: str
    resource_id: str
    scope_key: str
    starts_at: str
    ends_at: str
    capacity_percent: int
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ClosureWindowInput":
        kind = required_text(raw.get("resource_kind"), "resource_kind", 24).lower()
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("resource_kind 必须是 entry-slot、trail-direction、shuttle 或 parking")
        starts_at = iso_text(raw.get("starts_at"), "starts_at")
        ends_at = iso_text(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percent = non_negative_int(raw.get("capacity_percent"), "capacity_percent")
        if percent > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            resource_kind=kind,
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            scope_key=required_text(raw.get("scope_key"), "scope_key", 96),
            starts_at=starts_at,
            ends_at=ends_at,
            capacity_percent=percent,
            reason=required_text(raw.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class AssistanceNeed:
    assistance_kind: str
    headcount: int
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssistanceNeed":
        return cls(
            assistance_kind=assistance_kind(raw.get("assistance_kind")),
            headcount=positive_int(raw.get("headcount"), "headcount"),
            note=required_text(raw.get("note", ""), "note", 256) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class ReservationResourceRequirement:
    resource_kind: str
    resource_id: str
    scope_key: str
    quantity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationResourceRequirement":
        kind = required_text(raw.get("resource_kind"), "resource_kind", 24).lower()
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("resource_kind 必须是 entry-slot、trail-direction、shuttle 或 parking")
        return cls(
            resource_kind=kind,
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            scope_key=required_text(raw.get("scope_key"), "scope_key", 96),
            quantity=positive_int(raw.get("quantity"), "quantity"),
        )


@dataclass(frozen=True, slots=True)
class ReservationInput:
    reservation_id: str
    visitor_name: str
    party_size: int
    enters_at: str
    requirements: tuple[ReservationResourceRequirement, ...]
    assistance_needs: tuple[AssistanceNeed, ...]
    idempotency_key: str
    contact: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationInput":
        requirements_raw = raw.get("requirements", [])
        assistance_raw = raw.get("assistance_needs", [])
        if not isinstance(requirements_raw, list) or not requirements_raw:
            raise ValidationFailed("requirements 至少包含一项资源占用")
        if not isinstance(assistance_raw, list):
            raise ValidationFailed("assistance_needs 必须是数组")
        requirements = tuple(ReservationResourceRequirement.from_dict(item) for item in requirements_raw)
        kinds = {item.resource_kind for item in requirements}
        if "entry-slot" not in kinds:
            raise ValidationFailed("预约必须占用一个分时入园名额 (entry-slot)")
        keys = [(item.resource_kind, item.resource_id, item.scope_key) for item in requirements]
        if len(keys) != len(set(keys)):
            raise ValidationFailed("同一资源不能在一条预约中重复占用")
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            visitor_name=required_text(raw.get("visitor_name"), "visitor_name"),
            party_size=positive_int(raw.get("party_size"), "party_size"),
            enters_at=iso_text(raw.get("enters_at"), "enters_at"),
            requirements=requirements,
            assistance_needs=tuple(AssistanceNeed.from_dict(item) for item in assistance_raw),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            contact=required_text(raw.get("contact", ""), "contact", 64) if raw.get("contact") else "",
        )


@dataclass(frozen=True, slots=True)
class WeatherAlertInput:
    alert_id: str
    level: str
    title: str
    starts_at: str
    ends_at: str
    closed_resources: tuple[str, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WeatherAlertInput":
        level = required_text(raw.get("level"), "level", 16).lower()
        if level not in WEATHER_LEVELS:
            raise ValidationFailed("level 必须是 blue、yellow、orange 或 red")
        starts_at = iso_text(raw.get("starts_at"), "starts_at")
        ends_at = iso_text(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        closed_raw = raw.get("closed_resources", [])
        if not isinstance(closed_raw, list) or not closed_raw:
            raise ValidationFailed("closed_resources 至少包含一个受影响容量引用")
        closed = tuple(required_text(item, "closed_resources 项", 160) for item in closed_raw)
        if len(closed) != len(set(closed)):
            raise ValidationFailed("closed_resources 不能重复")
        return cls(
            alert_id=identifier(raw.get("alert_id"), "alert_id"),
            level=level,
            title=required_text(raw.get("title"), "title"),
            starts_at=starts_at,
            ends_at=ends_at,
            closed_resources=closed,
            reason=required_text(raw.get("reason"), "reason"),
        )
