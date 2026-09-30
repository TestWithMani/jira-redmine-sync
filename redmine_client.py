"""Redmine REST client for issue lookup and create."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

SEARCH_PAGE_SIZE = 25
MAX_SEARCH_OFFSET = 100


class RedmineError(Exception):
    """Redmine returned an error or an unexpected payload."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int = 30,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RedmineError(f"Redmine request failed: {exc.reason}") from exc


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_identifier: str,
        tracker_name: str,
        subtask_tracker_name: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project_identifier = project_identifier
        self.tracker_name = tracker_name
        self.subtask_tracker_name = subtask_tracker_name or tracker_name
        self._transport = transport or urllib_transport
        self._project_id: int | None = None
        self._tracker_ids: dict[str, int] = {}

    def find_by_subject(self, subject: str) -> dict[str, Any] | None:
        """Return the issue in this project whose subject equals ``subject``."""
        project_id = self._load_project_id()
        offset = 0
        while offset <= MAX_SEARCH_OFFSET:
            results, total = self._search(subject, offset)
            for result in results:
                if result.get("type") not in (None, "issue", "issue-closed"):
                    continue
                title = result.get("title") or ""
                if subject not in title:
                    continue
                issue = self._get_issue(int(result["id"]))
                issue_project = (issue.get("project") or {}).get("id")
                if issue.get("subject") == subject and issue_project == project_id:
                    return issue
            offset += SEARCH_PAGE_SIZE
            if offset >= total or not results:
                break
        return None

    def create_issue(
        self,
        subject: str,
        description: str,
        parent_issue_id: int | None = None,
        subtask: bool = False,
    ) -> dict[str, Any]:
        tracker_name = self.subtask_tracker_name if subtask else self.tracker_name
        payload: dict[str, Any] = {
            "issue": {
                "project_id": self.project_identifier,
                "tracker_id": self._tracker_id(tracker_name),
                "subject": subject,
                "description": description,
            }
        }
        if parent_issue_id is not None:
            payload["issue"]["parent_issue_id"] = parent_issue_id
        data = self._request("POST", "/issues.json", payload)
        issue = data.get("issue")
        if not issue or "id" not in issue:
            raise RedmineError("Redmine create response did not include an issue id")
        return issue

    def _search(self, subject: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        query = urllib.parse.urlencode(
            {
                "q": subject,
                "issues": "1",
                "titles_only": "1",
                "limit": str(SEARCH_PAGE_SIZE),
                "offset": str(offset),
            }
        )
        path = f"/projects/{urllib.parse.quote(self.project_identifier)}/search.json?{query}"
        data = self._request("GET", path)
        results = data.get("results") or []
        total = int(data.get("total_count") or 0)
        return results, total

    def _get_issue(self, issue_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/issues/{issue_id}.json")
        issue = data.get("issue")
        if not issue:
            raise RedmineError(f"Redmine issue {issue_id} response was empty")
        return issue

    def _load_project_id(self) -> int:
        if self._project_id is not None:
            return self._project_id
        project = self._project()
        self._project_id = int(project["id"])
        return self._project_id

    def _tracker_id(self, name: str) -> int:
        cached = self._tracker_ids.get(name)
        if cached is not None:
            return cached
        wanted = name.strip().lower()
        for tracker in self._project().get("trackers") or []:
            if str(tracker.get("name", "")).strip().lower() == wanted:
                tracker_id = int(tracker["id"])
                self._tracker_ids[name] = tracker_id
                return tracker_id
        raise RedmineError(
            f"Tracker {name!r} is not enabled on Redmine project {self.project_identifier}"
        )

    def _project(self) -> dict[str, Any]:
        path = f"/projects/{urllib.parse.quote(self.project_identifier)}.json?include=trackers"
        data = self._request("GET", path)
        project = data.get("project")
        if not project or "id" not in project:
            raise RedmineError(f"Redmine project {self.project_identifier} was not found")
        return project

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "X-Redmine-API-Key": self.api_key,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, self.base_url + path, headers, body)
        if status < 200 or status >= 300:
            detail = raw.decode("utf-8", errors="replace")[:500]
            raise RedmineError(f"Redmine {method} {path} failed ({status}): {detail}", status)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RedmineError(f"Redmine {method} {path} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RedmineError(f"Redmine {method} {path} returned an unexpected payload")
        return parsed
