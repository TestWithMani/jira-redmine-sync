"""Redmine lookup uses exact subject matches inside the configured project."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from redmine_client import RedmineClient, RedmineError


class Transport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        self.headers.append(headers)
        parsed = urlparse(url)
        key = (method, parsed.path)
        handler = self.routes[key]
        status, payload = handler(parse_qs(parsed.query), body)
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        return status, raw


def client(routes) -> tuple[RedmineClient, Transport]:
    transport = Transport(routes)
    redmine = RedmineClient(
        base_url="https://redmine.example.com",
        api_key="secret",
        project_identifier="support",
        tracker_name="Support",
        transport=transport,
    )
    return redmine, transport


def project_route(query, body):
    return 200, {
        "project": {
            "id": 7,
            "identifier": "support",
            "trackers": [{"id": 3, "name": "Support"}],
        }
    }


class RedmineClientTests(unittest.TestCase):
    def test_ignores_search_hit_with_a_different_subject(self) -> None:
        def search(query, body):
            self.assertEqual(query["q"], ["SUP-1460"])
            self.assertEqual(query["titles_only"], ["1"])
            self.assertEqual(query["issues"], ["1"])
            return 200, {
                "results": [
                    {"id": 12, "title": "Notes about SUP-1460", "type": "issue"},
                ],
                "total_count": 1,
                "offset": 0,
                "limit": 25,
            }

        def issue(query, body):
            return 200, {
                "issue": {
                    "id": 12,
                    "subject": "Notes about SUP-1460",
                    "project": {"id": 7, "name": "Support"},
                }
            }

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("GET", "/projects/support/search.json"): search,
                ("GET", "/issues/12.json"): issue,
            }
        )
        self.assertIsNone(redmine.find_by_subject("SUP-1460"))

    def test_returns_exact_subject_in_project(self) -> None:
        def search(query, body):
            return 200, {
                "results": [{"id": 15, "title": "SUP-1460", "type": "issue"}],
                "total_count": 1,
            }

        def issue(query, body):
            return 200, {
                "issue": {
                    "id": 15,
                    "subject": "SUP-1460",
                    "project": {"id": 7, "name": "Support"},
                }
            }

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("GET", "/projects/support/search.json"): search,
                ("GET", "/issues/15.json"): issue,
            }
        )
        found = redmine.find_by_subject("SUP-1460")
        self.assertEqual(found["id"], 15)

    def test_create_posts_subject_tracker_and_parent(self) -> None:
        def create(query, body):
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["issue"]["subject"], "SUP-1461")
            self.assertEqual(payload["issue"]["project_id"], "support")
            self.assertEqual(payload["issue"]["tracker_id"], 3)
            self.assertEqual(payload["issue"]["parent_issue_id"], 15)
            return 201, {"issue": {"id": 16, "subject": "SUP-1461"}}

        redmine, transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("POST", "/issues.json"): create,
            }
        )
        created = redmine.create_issue(
            "SUP-1461",
            "desc",
            parent_issue_id=15,
            subtask=True,
        )
        self.assertEqual(created["id"], 16)
        method, url = transport.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(transport.headers[-1]["X-Redmine-API-Key"], "secret")
        self.assertTrue(url.endswith("/issues.json"))

    def test_redmine_error_status_raises(self) -> None:
        def create(query, body):
            return 422, {"errors": ["Subject can't be blank"]}

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("POST", "/issues.json"): create,
            }
        )
        with self.assertRaises(RedmineError) as caught:
            redmine.create_issue("SUP-1460", "desc")
        self.assertEqual(caught.exception.status, 422)

    def test_accepts_closed_issue_with_exact_subject(self) -> None:
        def search(query, body):
            return 200, {
                "results": [{"id": 18, "title": "Task #18 (Closed): SUP-1460", "type": "issue-closed"}],
                "total_count": 1,
            }

        def issue(query, body):
            return 200, {
                "issue": {"id": 18, "subject": "SUP-1460", "project": {"id": 7}},
            }

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("GET", "/projects/support/search.json"): search,
                ("GET", "/issues/18.json"): issue,
            }
        )
        self.assertEqual(redmine.find_by_subject("SUP-1460")["id"], 18)

    def test_ignores_same_subject_in_another_project(self) -> None:
        def search(query, body):
            return 200, {
                "results": [{"id": 19, "title": "SUP-1460", "type": "issue"}],
                "total_count": 1,
            }

        def issue(query, body):
            return 200, {
                "issue": {"id": 19, "subject": "SUP-1460", "project": {"id": 99}},
            }

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("GET", "/projects/support/search.json"): search,
                ("GET", "/issues/19.json"): issue,
            }
        )
        self.assertIsNone(redmine.find_by_subject("SUP-1460"))

    def test_tracker_name_match_is_case_insensitive(self) -> None:
        def create(query, body):
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["issue"]["tracker_id"], 3)
            return 201, {"issue": {"id": 20, "subject": "SUP-1460"}}

        redmine, _transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("POST", "/issues.json"): create,
            }
        )
        redmine.tracker_name = "support"
        self.assertEqual(redmine.create_issue("SUP-1460", "desc")["id"], 20)

    def test_missing_tracker_raises(self) -> None:
        redmine, _transport = client({("GET", "/projects/support.json"): project_route})
        redmine.tracker_name = "Bug"
        with self.assertRaises(RedmineError):
            redmine.create_issue("SUP-1460", "desc")

    def test_search_reads_the_next_page(self) -> None:
        def search(query, body):
            offset = query["offset"][0]
            if offset == "0":
                return 200, {
                    "results": [{"id": 21, "title": "Notes about SUP-1460", "type": "issue"}],
                    "total_count": 26,
                }
            self.assertEqual(offset, "25")
            return 200, {
                "results": [{"id": 22, "title": "SUP-1460", "type": "issue"}],
                "total_count": 26,
            }

        routes_issue_ids = {"count": 0}

        def issue_by_url(query, body):
            routes_issue_ids["count"] += 1
            if routes_issue_ids["count"] == 1:
                return 200, {
                    "issue": {"id": 21, "subject": "Notes about SUP-1460", "project": {"id": 7}},
                }
            return 200, {"issue": {"id": 22, "subject": "SUP-1460", "project": {"id": 7}}}

        redmine, transport = client(
            {
                ("GET", "/projects/support.json"): project_route,
                ("GET", "/projects/support/search.json"): search,
                ("GET", "/issues/21.json"): issue_by_url,
                ("GET", "/issues/22.json"): issue_by_url,
            }
        )
        self.assertEqual(redmine.find_by_subject("SUP-1460")["id"], 22)
        paths = [urlparse(url).path for _method, url in transport.calls]
        self.assertIn("/issues/21.json", paths)
        self.assertIn("/issues/22.json", paths)


if __name__ == "__main__":
    unittest.main()
