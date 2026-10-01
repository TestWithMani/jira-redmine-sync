"""Webhook auth and HTTP status mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import handle_webhook_request, sync_once, verify_webhook_secret
from redmine_client import RedmineError
from sync import SyncError


SECRET = "test-secret"


class StubService:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or {"action": "created", "jiraKey": "SUP-1460"}
        self.error = error
        self.payloads = []

    def handle(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.result


def signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature": f"sha256={digest}"}


class AppTests(unittest.TestCase):
    def test_shared_header_and_hmac_are_accepted(self) -> None:
        body = b'{"webhookEvent":"jira:issue_created"}'
        self.assertTrue(verify_webhook_secret(SECRET, body, {"X-Webhook-Secret": SECRET}))
        self.assertTrue(verify_webhook_secret(SECRET, body, signed(body)))
        self.assertFalse(verify_webhook_secret(SECRET, body, {"X-Webhook-Secret": "nope"}))
        self.assertFalse(verify_webhook_secret("", body, {"X-Webhook-Secret": SECRET}))

    def test_request_statuses(self) -> None:
        body = json.dumps({"webhookEvent": "jira:issue_created"}).encode("utf-8")
        service = StubService()
        status, payload = handle_webhook_request(service, SECRET, body, {"X-Webhook-Secret": SECRET})
        self.assertEqual(status, 200)
        self.assertEqual(payload["action"], "created")

        status, payload = handle_webhook_request(service, SECRET, body, {})
        self.assertEqual(status, 401)

        status, payload = handle_webhook_request(
            service, SECRET, b"not-json", {"X-Webhook-Secret": SECRET}
        )
        self.assertEqual(status, 400)

        failing = StubService(error=RedmineError("down", status=503))
        status, payload = handle_webhook_request(
            failing, SECRET, body, {"X-Webhook-Secret": SECRET}
        )
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "sync_failed"})

        failing = StubService(error=SyncError("missing parent"))
        status, _payload = handle_webhook_request(
            failing, SECRET, body, signed(body)
        )
        self.assertEqual(status, 500)

        status, payload = handle_webhook_request(
            service, SECRET, b"[]", {"X-Webhook-Secret": SECRET}
        )
        self.assertEqual(status, 400)

        failing = StubService(error=RuntimeError("unexpected"))
        status, payload = handle_webhook_request(
            failing, SECRET, body, {"X-Webhook-Secret": SECRET}
        )
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "sync_failed"})

    def test_sync_once_fetches_the_issue_and_handles_create(self) -> None:
        class FakeJira:
            def get_issue(self, key):
                self.key = key
                return {"key": key, "fields": {"summary": "One"}}

        service = StubService()
        service.jira = FakeJira()
        result = sync_once(service, "sup-1472")
        self.assertEqual(service.jira.key, "SUP-1472")
        self.assertEqual(service.payloads[0]["webhookEvent"], "jira:issue_created")
        self.assertEqual(service.payloads[0]["issue"]["key"], "SUP-1472")
        self.assertEqual(result["jiraKey"], "SUP-1460")

    def test_sync_once_requires_a_jira_client_and_a_real_key(self) -> None:
        service = StubService()
        service.jira = None
        with self.assertRaises(SyncError):
            sync_once(service, "SUP-1472")
        service.jira = object()
        with self.assertRaises(SyncError):
            sync_once(service, "not-a-key")

    def test_signature_256_header_is_accepted(self) -> None:
        body = b'{"webhookEvent":"jira:issue_created"}'
        digest = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        self.assertTrue(
            verify_webhook_secret(SECRET, body, {"X-Hub-Signature-256": f"sha256={digest}"})
        )
        self.assertFalse(verify_webhook_secret(SECRET, body, {"X-Hub-Signature": "sha256=abc"}))


if __name__ == "__main__":
    unittest.main()
