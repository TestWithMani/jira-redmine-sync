"""HTTP webhook that creates Redmine issues when Jira issues are created."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from redmine_client import RedmineClient, RedmineError
from sync import Config, JiraClient, SyncError, SyncService

LOG = logging.getLogger("jira_redmine_sync")
MAX_BODY_BYTES = 2_000_000
WEBHOOK_PATH = "/webhooks/jira"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def verify_webhook_secret(secret: str, body: bytes, headers: Any) -> bool:
    if not secret:
        return False
    provided = _header(headers, "X-Webhook-Secret")
    if provided and hmac.compare_digest(provided, secret):
        return True
    for name in ("X-Hub-Signature", "X-Hub-Signature-256"):
        signature = _header(headers, name)
        if signature and _signature_matches(secret, body, signature):
            return True
    return False


def handle_webhook_request(
    service: SyncService,
    secret: str,
    body: bytes,
    headers: Any,
) -> tuple[int, dict[str, Any]]:
    if not verify_webhook_secret(secret, body, headers):
        return 401, {"error": "unauthorized"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "invalid_json"}
    if not isinstance(payload, dict):
        return 400, {"error": "invalid_json"}
    try:
        return 200, service.handle(payload)
    except (RedmineError, SyncError):
        LOG.exception("Redmine sync failed")
        return 500, {"error": "sync_failed"}
    except Exception:
        LOG.exception("Redmine sync failed")
        return 500, {"error": "sync_failed"}


def build_service(config: Config) -> SyncService:
    redmine = RedmineClient(
        base_url=config.redmine_url,
        api_key=config.redmine_api_key,
        project_identifier=config.redmine_project,
        tracker_name=config.redmine_tracker_name,
        subtask_tracker_name=config.redmine_subtask_tracker_name,
    )
    jira = None
    if config.jira_email and config.jira_api_token:
        jira = JiraClient(config.jira_base_url, config.jira_email, config.jira_api_token)
    return SyncService(
        redmine=redmine,
        project_keys=config.jira_project_keys,
        jira_base_url=config.jira_base_url,
        jira=jira,
    )


def make_handler(service: SyncService, secret: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != WEBHOOK_PATH:
                self._send(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(400, {"error": "invalid_body"})
                return
            body = self.rfile.read(length)
            status, payload = handle_webhook_request(service, secret, body, self.headers)
            self._send(status, payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main() -> None:
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env(dict(os.environ))
    except SyncError as exc:
        LOG.error("%s", exc)
        raise SystemExit(1) from exc
    service = build_service(config)
    run_dir = root / ".run"
    run_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(run_dir / "service.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    if config.jira_email and config.jira_api_token:
        interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
        lookback = int(os.environ.get("POLL_LOOKBACK_DAYS", "2"))
        thread = threading.Thread(
            target=_poll_loop,
            args=(service, interval, lookback, run_dir / "seen-keys.txt"),
            name="jira-poll",
            daemon=True,
        )
        thread.start()
        LOG.info("Polling Jira every %s seconds", interval)
    else:
        LOG.info("Jira polling is off until JIRA_EMAIL and JIRA_API_TOKEN are set")
    server = ThreadingHTTPServer((host, port), make_handler(service, config.jira_webhook_secret))
    LOG.info("Listening on http://%s:%s%s", host, port, WEBHOOK_PATH)
    server.serve_forever()


def _poll_loop(service: SyncService, interval: int, lookback: int, seen_path: Path) -> None:
    jira = service.jira
    if jira is None:
        return
    while True:
        try:
            for result in service.poll_recent(jira, lookback, seen_path):
                LOG.info("poll %s %s", result.get("jiraKey"), result.get("action"))
        except Exception:
            LOG.exception("Jira poll failed")
        time.sleep(max(interval, 15))


def _header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    value = getter(name) if getter else None
    if value is None and isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                value = item
                break
    return str(value).strip() if value else ""


def _signature_matches(secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    presented = signature.strip()
    if presented.lower().startswith("sha256="):
        presented = presented.split("=", 1)[1].strip()
    if len(presented) != len(digest):
        return False
    return hmac.compare_digest(presented, digest)


if __name__ == "__main__":
    main()
