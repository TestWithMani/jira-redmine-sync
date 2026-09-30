"""Sync decisions without calling Jira or Redmine."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from redmine_client import RedmineError
from sync import SyncError, SyncService, adf_to_text


class FakeRedmine:
    def __init__(self) -> None:
        self.subjects: dict[str, dict] = {}
        self.created: list[dict] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def find_by_subject(self, subject: str):
        with self._lock:
            return self.subjects.get(subject)

    def create_issue(self, subject, description, parent_issue_id=None, subtask=False):
        with self._lock:
            if subject in self.subjects:
                raise AssertionError(f"duplicate create for {subject}")
            issue = {
                "id": self._next_id,
                "subject": subject,
                "description": description,
                "parent_issue_id": parent_issue_id,
                "subtask": subtask,
            }
            self._next_id += 1
            self.subjects[subject] = issue
            self.created.append(issue)
            return issue


class FakeSearch:
    def search_created(self, project_keys, lookback_days):
        return [
            {
                "key": "SUP-2000",
                "fields": {
                    "summary": "Polled",
                    "description": "From poll",
                    "created": "2026-09-30T12:00:00.000-0400",
                    "project": {"key": "SUP"},
                    "issuetype": {"name": "Task", "subtask": False},
                },
            }
        ]


class FakeJira:
    configured = True

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def get_issue(self, key: str):
        self.fetched.append(key)
        return {
            "key": key,
            "fields": {
                "summary": "Fetched parent",
                "description": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "From Jira"}],
                        }
                    ],
                },
                "project": {"key": "SUP"},
                "issuetype": {"name": "Task", "subtask": False},
            },
        }


def service(redmine: FakeRedmine, jira=None) -> SyncService:
    return SyncService(
        redmine=redmine,
        project_keys={"SUP"},
        jira_base_url="https://drafttemp.atlassian.net",
        jira=jira,
    )


def created_payload(key="SUP-1460", project="SUP", description="Body text", summary="Add filter"):
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "project": {"key": project},
                "issuetype": {"name": "Task", "subtask": False},
            },
        },
    }


def subtask_payload(key="SUP-1461", parent_key="SUP-1460", parent_summary="Parent summary"):
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": key,
            "fields": {
                "summary": "Child work",
                "description": "Child body",
                "project": {"key": "SUP"},
                "issuetype": {"name": "Sub-task", "subtask": True},
                "parent": {
                    "key": parent_key,
                    "fields": {"summary": parent_summary},
                },
            },
        },
    }


class SyncTests(unittest.TestCase):
    def test_creates_issue_named_with_jira_key(self) -> None:
        redmine = FakeRedmine()
        result = service(redmine).handle(created_payload())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["jiraKey"], "SUP-1460")
        self.assertEqual(redmine.created[0]["subject"], "SUP-1460")
        description = redmine.created[0]["description"]
        self.assertIn("Jira: https://drafttemp.atlassian.net/browse/SUP-1460", description)
        self.assertIn("Summary: Add filter", description)
        self.assertIn("Body text", description)
        self.assertIsNone(redmine.created[0]["parent_issue_id"])

    def test_skips_duplicate_subject(self) -> None:
        redmine = FakeRedmine()
        redmine.subjects["SUP-1460"] = {"id": 5, "subject": "SUP-1460"}
        result = service(redmine).handle(created_payload())
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["reason"], "duplicate")
        self.assertEqual(result["redmineId"], 5)
        self.assertEqual(redmine.created, [])

    def test_second_lookup_skips_create(self) -> None:
        class AppearsOnSecondLookup(FakeRedmine):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def find_by_subject(self, subject: str):
                self.calls += 1
                if self.calls == 1:
                    return None
                return {"id": 4, "subject": subject}

        redmine = AppearsOnSecondLookup()
        result = service(redmine).handle(created_payload())
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["redmineId"], 4)
        self.assertEqual(redmine.created, [])
        self.assertGreaterEqual(redmine.calls, 2)

    def test_creates_child_under_existing_parent(self) -> None:
        redmine = FakeRedmine()
        redmine.subjects["SUP-1460"] = {"id": 9, "subject": "SUP-1460"}
        result = service(redmine).handle(subtask_payload())
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["parentRedmineId"], 9)
        self.assertFalse(result["parentCreated"])
        self.assertEqual(len(redmine.created), 1)
        child = redmine.created[0]
        self.assertEqual(child["subject"], "SUP-1461")
        self.assertEqual(child["parent_issue_id"], 9)
        self.assertTrue(child["subtask"])

    def test_creates_parent_when_missing_then_child(self) -> None:
        redmine = FakeRedmine()
        result = service(redmine).handle(subtask_payload())
        self.assertEqual(result["action"], "created")
        self.assertTrue(result["parentCreated"])
        self.assertEqual([issue["subject"] for issue in redmine.created], ["SUP-1460", "SUP-1461"])
        parent, child = redmine.created
        self.assertIsNone(parent["parent_issue_id"])
        self.assertIn("Summary: Parent summary", parent["description"])
        self.assertIn("https://drafttemp.atlassian.net/browse/SUP-1460", parent["description"])
        self.assertEqual(child["parent_issue_id"], parent["id"])
        self.assertEqual(result["parentRedmineId"], parent["id"])

    def test_parent_missing_uses_jira_when_configured(self) -> None:
        redmine = FakeRedmine()
        jira = FakeJira()
        service(redmine, jira).handle(subtask_payload())
        self.assertEqual(jira.fetched, ["SUP-1460"])
        self.assertIn("Summary: Fetched parent", redmine.created[0]["description"])
        self.assertIn("From Jira", redmine.created[0]["description"])

    def test_does_not_fetch_parent_when_it_already_exists(self) -> None:
        redmine = FakeRedmine()
        redmine.subjects["SUP-1460"] = {"id": 9, "subject": "SUP-1460"}
        jira = FakeJira()
        service(redmine, jira).handle(subtask_payload())
        self.assertEqual(jira.fetched, [])

    def test_does_not_create_child_when_parent_create_fails(self) -> None:
        class ParentFails(FakeRedmine):
            def create_issue(self, subject, description, parent_issue_id=None, subtask=False):
                if parent_issue_id is None:
                    raise RedmineError("parent failed", status=422)
                return super().create_issue(subject, description, parent_issue_id, subtask)

        redmine = ParentFails()
        with self.assertRaises(RedmineError):
            service(redmine).handle(subtask_payload())
        self.assertEqual(redmine.created, [])

    def test_child_failure_leaves_parent_for_retry(self) -> None:
        class ChildFails(FakeRedmine):
            def create_issue(self, subject, description, parent_issue_id=None, subtask=False):
                if parent_issue_id is not None:
                    raise RedmineError("child failed", status=422)
                return super().create_issue(subject, description, parent_issue_id, subtask)

        redmine = ChildFails()
        with self.assertRaises(RedmineError):
            service(redmine).handle(subtask_payload())
        self.assertEqual([issue["subject"] for issue in redmine.created], ["SUP-1460"])

    def test_ignores_updates_and_other_projects(self) -> None:
        redmine = FakeRedmine()
        updated = created_payload()
        updated["webhookEvent"] = "jira:issue_updated"
        self.assertEqual(service(redmine).handle(updated)["reason"], "event")
        self.assertEqual(service(redmine).handle(created_payload(project="OTHER"))["reason"], "project")
        self.assertEqual(redmine.created, [])

    def test_subtask_without_parent_key_fails(self) -> None:
        payload = subtask_payload()
        del payload["issue"]["fields"]["parent"]
        with self.assertRaises(SyncError):
            service(FakeRedmine()).handle(payload)

    def test_concurrent_creates_only_one_issue(self) -> None:
        redmine = FakeRedmine()
        original_create = redmine.create_issue

        def slow_create(subject, description, parent_issue_id=None, subtask=False):
            time.sleep(0.2)
            return original_create(subject, description, parent_issue_id, subtask)

        redmine.create_issue = slow_create  # type: ignore[method-assign]
        sync = service(redmine)
        gate = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                gate.wait(timeout=2)
                results.append(sync.handle(created_payload()))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(redmine.created), 1)
        actions = sorted(result["action"] for result in results)
        self.assertEqual(actions, ["created", "skipped"])

    def test_poll_creates_unseen_issue_and_remembers_it(self) -> None:
        redmine = FakeRedmine()
        sync = service(redmine)
        jira = FakeSearch()
        seen = Path(__file__).resolve().parent / "_seen_keys.txt"
        if seen.exists():
            seen.unlink()
        self.addCleanup(lambda: seen.unlink(missing_ok=True))
        first = sync.poll_recent(jira, 2, seen)
        second = sync.poll_recent(jira, 2, seen)
        self.assertEqual([item["action"] for item in first], ["created"])
        self.assertEqual(redmine.created[0]["subject"], "SUP-2000")
        self.assertEqual(second, [])
        self.assertEqual(len(redmine.created), 1)

    def test_adf_description_flattens_to_text(self) -> None:
        text = adf_to_text(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "One"}],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
        self.assertIn("Hello", text)
        self.assertIn("- One", text)


if __name__ == "__main__":
    unittest.main()
