"""无第三方依赖的统一承载编排 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .errors import CarryingError, ValidationFailed
from .service import CarryingOrchestrationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: CarryingOrchestrationService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = target.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/capacity_versions":
                return Response(201, self.service.register_capacity_version(actor, payload))
            if method == "POST" and path == "/closure_windows":
                return Response(201, self.service.register_closure_window(actor, payload))
            if method == "POST" and path == "/weather_alerts":
                return Response(201, self.service.trigger_weather_alert(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "weather_alerts" and parts[2] == "lift":
                return Response(200, self.service.lift_weather_alert(actor, parts[1]))
            if method == "POST" and path == "/reservations":
                return Response(201, self.service.submit_reservation(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "check_in":
                return Response(200, self.service.check_in(actor, parts[1]))
            if method == "POST" and len(parts) == 4 and parts[0] == "reservations" and parts[2] == "assistance":
                return Response(200, self.service.arrange_assistance(actor, parts[1], parts[3]))
            if method == "POST" and path == "/adjustment_plans":
                return Response(201, self.service.generate_adjustment_plan(actor))
            if method == "GET" and len(parts) == 3 and parts[0] == "adjustment_plans":
                return Response(200, self.service.plan(actor, int(parts[1])))
            if method == "POST" and len(parts) == 3 and parts[0] == "adjustment_plans" and parts[2] == "confirm":
                return Response(200, self.service.confirm_plan(actor, int(parts[1]), payload["idempotency_key"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "snapshots":
                return Response(200, self.service.snapshot(actor, int(parts[1])))
            if method == "GET" and len(parts) == 3 and parts[0] == "evacuation_batches":
                return Response(200, self.service.evacuation_batch(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "evacuation_batches" and parts[2] == "receipts":
                return Response(201, self.service.acknowledge_evacuation(
                    actor, parts[1], payload["steps"], payload["idempotency_key"]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except CarryingError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CarryingOrchestration/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动统一承载编排与应急疏散服务")
    parser.add_argument("--database", type=Path, default=Path("carrying_orchestration.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(CarryingOrchestrationService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
