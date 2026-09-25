"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .ledger import LedgerService
from .service import DomainService
from .storage import Database


def _view(value):
    """把数据对象递归转换为可 JSON 序列化的字典。"""

    if hasattr(value, "__dict__"):
        return {key: _view(item) for key, item in value.__dict__.items()}
    if isinstance(value, (list, tuple)):
        return [_view(item) for item in value]
    if isinstance(value, dict):
        return {key: _view(item) for key, item in value.items()}
    return value


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    ledger = service if isinstance(service, LedgerService) else None
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if ledger is not None and method == "POST" and parsed.path == "/containers":
            receipt = ledger.register_container(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/containers/reweigh":
            receipt = ledger.reweigh_container(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "GET" and parsed.path == "/containers":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            status = query.get("status", [None])[0]
            return 200, {"items": [item.__dict__ for item in ledger.list_containers(site_id, status)]}
        if ledger is not None and method == "POST" and parsed.path == "/remix-authorizations":
            receipt = ledger.authorize_remix(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/remixes":
            receipt = ledger.execute_remix(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/handovers":
            receipt = ledger.initiate_handover(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/handover-responses":
            receipt = ledger.respond_handover(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/statements":
            receipt = ledger.submit_statement(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "POST" and parsed.path == "/arbitrations":
            receipt = ledger.arbitrate_discrepancy(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.response
        if ledger is not None and method == "GET" and parsed.path.startswith("/handovers/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 3 and parts[2] == "statements":
                return 200, {"items": ledger.list_statements(parts[1])}
            handover_id = parts[-1]
            return 200, _view(ledger.get_handover(handover_id))
        if ledger is not None and method == "GET" and parsed.path == "/discrepancies":
            site_id = query.get("site_id", [None])[0]
            status = query.get("status", [None])[0]
            handover_id = query.get("handover_id", [None])[0]
            return 200, {"items": [_view(item) for item in
                                   ledger.list_discrepancies(site_id=site_id, status=status,
                                                             handover_id=handover_id)]}
        if ledger is not None and method == "GET" and parsed.path.startswith("/containers/") \
                and parsed.path.endswith("/trace"):
            parts = parsed.path.strip("/").split("/")
            return 200, _view(ledger.trace_container(parts[1]))
        if ledger is not None and method == "GET" and parsed.path.startswith("/containers/"):
            container_id = parsed.path.rsplit("/", 1)[-1]
            return 200, _view(ledger.get_container(container_id))
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动环保业务基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = LedgerService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
