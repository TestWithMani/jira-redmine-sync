"""Create Redmine issues for new Jira issues."""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from redmine_client import RedmineError

logger = logging.getLogger(__name__)

CREATED_EVENTS = {"jira:issue_created", "issue_created"}
MAX_PARENT_DEPTH = 10


class SyncError(Exception):
    """The webhook could not be synced."""


@dataclass
class Config:
    redmine_url: str
    redmine_api_key: str
    redmine_project: str
    redmine_tracker_name: str
    jira_webhook_secret: str
    jira_project_keys: set[str]
    jira_base_url: str
    redmine_subtask_tracker_name: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        project_keys = {
            key.strip().upper()
            for key in env.get("JIRA_PROJECT_KEYS", "SUP").split(",")
            if key.strip()
        }
        missing = [
            name
            for name in (
                "REDMINE_URL",
                "REDMINE_API_KEY",
                "REDMINE_PROJECT",
                "REDMINE_TRACKER_NAME",
                "JIRA_WEBHOOK_SECRET",
            )
            if not env.get(name, "").strip()
        ]
        if missing:
            raise SyncError("Missing required settings: " + ", ".join(missing))
        if not project_keys:
            raise SyncError("JIRA_PROJECT_KEYS must list at least one project key")
        return cls(
            redmine_url=env["REDMINE_URL"].strip(),
            redmine_api_key=env["REDMINE_API_KEY"].strip(),
            redmine_project=env["REDMINE_PROJECT"].strip(),
            redmine_tracker_name=env["REDMINE_TRACKER_NAME"].strip(),
            redmine_subtask_tracker_name=(env.get("REDMINE_SUBTASK_TRACKER_NAME") or "").strip()
            or None,
            jira_webhook_secret=env["JIRA_WEBHOOK_SECRET"].strip(),
            jira_project_keys=project_keys,
            jira_base_url=(env.get("JIRA_BASE_URL") or "https://drafttemp.atlassian.net").strip(),
            jira_email=(env.get("JIRA_EMAIL") or "").strip() or None,
            jira_api_token=(env.get("JIRA_API_TOKEN") or "").strip() or None,
        )


class KeyLocks:
    """In-process lock per Jira issue key."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def hold(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
        return lock


class JiraClient:
    """Optional Jira REST client used when a parent issue is not in the webhook."""

    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)

    def get_issue(self, key: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"fields": "summary,description,issuetype,project,parent"}
        )
        payload = self._get(f"/rest/api/3/issue/{urllib.parse.quote(key)}?{query}")
        if "key" not in payload:
            raise SyncError(f"Jira lookup for {key} returned an unexpected payload")
        return payload

    def search_created(self, project_keys: list[str], lookback_days: int) -> list[dict[str, Any]]:
        projects = ", ".join(project_keys)
        jql = f"project in ({projects}) AND created >= -{int(lookback_days)}d ORDER BY created ASC"
        issues: list[dict[str, Any]] = []
        next_token = None
        while len(issues) < 200:
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": 50,
                "fields": ["summary", "description", "issuetype", "project", "parent", "created"],
            }
            if next_token:
                body["nextPageToken"] = next_token
            payload = self._post("/rest/api/3/search/jql", body)
            batch = payload.get("issues") or []
            issues.extend(item for item in batch if isinstance(item, dict))
            next_token = payload.get("nextPageToken")
            if not next_token or not batch:
                break
        return issues

    def _headers(self) -> dict[str, str]:
        token = f"{self.email}:{self.api_token}".encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(token).decode("ascii"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise SyncError(f"Jira {method} {path} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise SyncError(f"Jira {method} {path} failed: {exc.reason}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SyncError(f"Jira {method} {path} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SyncError(f"Jira {method} {path} returned an unexpected payload")
        return payload


class SyncService:
    def __init__(
        self,
        redmine: Any,
        project_keys: set[str],
        jira_base_url: str,
        jira: JiraClient | None = None,
        locks: KeyLocks | None = None,
    ) -> None:
        self.redmine = redmine
        self.project_keys = {key.upper() for key in project_keys}
        self.jira_base_url = jira_base_url.rstrip("/")
        self.jira = jira if jira and jira.configured else None
        self.locks = locks or KeyLocks()

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("webhookEvent") or payload.get("issue_event_type_name")
        if event not in CREATED_EVENTS:
            return _result(action="ignored", reason="event")
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not issue.get("key"):
            raise SyncError("Webhook did not include a Jira issue key")
        project_key = ((issue.get("fields") or {}).get("project") or {}).get("key")
        if not project_key or str(project_key).upper() not in self.project_keys:
            return _result(action="ignored", reason="project", jira_key=issue.get("key"))
        record, created, parent = self._ensure(issue, depth=0)
        parent_id = parent[0]["id"] if parent else None
        parent_created = parent[1] if parent else False
        logger.info(
            "jira %s -> redmine %s (%s)",
            issue["key"],
            record["id"],
            "created" if created else "skipped",
        )
        return _result(
            action="created" if created else "skipped",
            reason=None if created else "duplicate",
            jira_key=issue["key"],
            redmine_id=record["id"],
            parent_redmine_id=parent_id,
            parent_created=parent_created,
        )

    def poll_recent(self, jira: JiraClient, lookback_days: int, seen_path: Any) -> list[dict[str, Any]]:
        """Create Redmine issues for Jira issues opened during the lookback window."""
        seen = _read_seen(seen_path)
        issues = jira.search_created(sorted(self.project_keys), lookback_days)
        issues.sort(key=lambda item: str((item.get("fields") or {}).get("created") or ""))
        results: list[dict[str, Any]] = []
        for issue in issues:
            key = str(issue.get("key") or "")
            if not key or key in seen:
                continue
            result = self.handle({"webhookEvent": "jira:issue_created", "issue": issue})
            seen.add(key)
            _write_seen(seen_path, seen)
            results.append(result)
        return results

    def _ensure(
        self,
        issue: dict[str, Any],
        depth: int,
    ) -> tuple[dict[str, Any], bool, tuple[dict[str, Any], bool] | None]:
        if depth > MAX_PARENT_DEPTH:
            raise SyncError(f"Parent chain for {issue.get('key')} is too deep")
        parent_record: tuple[dict[str, Any], bool] | None = None
        if _is_subtask(issue):
            parent_key = _parent_key(issue)
            if parent_key == issue.get("key"):
                raise SyncError(f"{issue.get('key')} lists itself as its parent")
            with self.locks.hold(parent_key):
                existing_parent = self.redmine.find_by_subject(parent_key)
            if existing_parent:
                parent_record = (existing_parent, False)
            else:
                parent_issue = self._resolve_parent(issue)
                parent_synced, parent_created, _grandparent = self._ensure(parent_issue, depth + 1)
                parent_record = (parent_synced, parent_created)
        key = str(issue["key"])
        with self.locks.hold(key):
            record, created = self._create_if_missing(
                issue,
                parent_id=parent_record[0]["id"] if parent_record else None,
            )
        return record, created, parent_record

    def _resolve_parent(self, issue: dict[str, Any]) -> dict[str, Any]:
        parent = (issue.get("fields") or {}).get("parent") or {}
        parent_key = _parent_key(issue)
        if self.jira is not None:
            return self.jira.get_issue(str(parent_key))
        parent_fields = parent.get("fields") or {}
        project_key = ((issue.get("fields") or {}).get("project") or {}).get("key")
        return {
            "key": parent_key,
            "self": parent.get("self"),
            "fields": {
                "summary": parent_fields.get("summary") or parent_key,
                "description": parent_fields.get("description"),
                "project": {"key": project_key},
                "issuetype": parent_fields.get("issuetype") or {"subtask": False, "name": "Task"},
                "parent": parent_fields.get("parent"),
            },
        }

    def _create_if_missing(
        self,
        issue: dict[str, Any],
        parent_id: int | None,
    ) -> tuple[dict[str, Any], bool]:
        key = str(issue["key"])
        existing = self.redmine.find_by_subject(key)
        if existing:
            return existing, False
        existing = self.redmine.find_by_subject(key)
        if existing:
            return existing, False
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or key
        description = build_description(
            summary=str(summary),
            body=adf_to_text(fields.get("description")),
            browse_url=browse_url(issue, self.jira_base_url),
        )
        created = self.redmine.create_issue(
            subject=key,
            description=description,
            parent_issue_id=parent_id,
            subtask=_is_subtask(issue),
        )
        return created, True


def build_description(summary: str, body: str, browse_url: str) -> str:
    lines = [f"Jira: {browse_url}", f"Summary: {summary}"]
    text = body.strip()
    if text:
        lines.extend(["", text])
    return "\n".join(lines)


def browse_url(issue: dict[str, Any], jira_base_url: str) -> str:
    key = issue["key"]
    if jira_base_url:
        return f"{jira_base_url.rstrip('/')}/browse/{key}"
    self_url = str(issue.get("self") or "")
    if "/rest/" in self_url:
        root = self_url.split("/rest/", 1)[0]
        return f"{root}/browse/{key}"
    return key


def adf_to_text(node: Any) -> str:
    """Flatten Jira Atlassian Document Format, or return a plain string."""
    text = _adf_node(node).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _adf_node(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_node(item) for item in node)
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    if node_type in {"inlineCard", "mention"}:
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("url") or "")
    inner = "".join(_adf_node(child) for child in node.get("content") or [])
    if node_type == "listItem":
        return "- " + inner.strip() + "\n"
    if node_type in {"paragraph", "heading", "codeBlock", "blockquote"}:
        return inner.rstrip() + "\n"
    return inner


def _read_seen(path: Any) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _write_seen(path: Any, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(sorted(seen))
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _parent_key(issue: dict[str, Any]) -> str:
    parent = (issue.get("fields") or {}).get("parent") or {}
    parent_key = parent.get("key")
    if not parent_key:
        raise SyncError(f"Subtask {issue.get('key')} has no parent key")
    return str(parent_key)


def _is_subtask(issue: dict[str, Any]) -> bool:
    issue_type = (issue.get("fields") or {}).get("issuetype") or {}
    if issue_type.get("subtask") is True:
        return True
    name = str(issue_type.get("name") or "").strip().lower().replace("-", "").replace(" ", "")
    return name == "subtask"


def _result(
    action: str,
    reason: str | None,
    jira_key: str | None = None,
    redmine_id: int | None = None,
    parent_redmine_id: int | None = None,
    parent_created: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "jiraKey": jira_key,
        "redmineId": redmine_id,
        "parentRedmineId": parent_redmine_id,
        "parentCreated": parent_created,
    }
