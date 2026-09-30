# Jira to Redmine create sync

When a Jira issue is created in a watched project, this service creates a Redmine issue whose subject is the Jira key. `SUP-1460` becomes a Redmine issue named `SUP-1460`. A Jira subtask becomes a Redmine child issue named with the subtask key.

Later edits in Jira are ignored. If the Redmine subject already exists, the service does not create another issue.

## Setup

Requires Python 3.9+.

```powershell
cd jira-redmine-sync
copy .env.example .env
```

Edit `.env` locally. Do not commit it.

| Setting | Purpose |
| --- | --- |
| `REDMINE_URL` | Redmine site, no trailing path |
| `REDMINE_API_KEY` | API key of a user who can create issues |
| `REDMINE_PROJECT` | Project identifier |
| `REDMINE_TRACKER_NAME` | Tracker enabled on that project |
| `REDMINE_SUBTASK_TRACKER_NAME` | Optional. Defaults to `REDMINE_TRACKER_NAME` |
| `JIRA_WEBHOOK_SECRET` | Shared secret Jira sends on each request |
| `JIRA_PROJECT_KEYS` | Comma-separated keys. Default `SUP` |
| `JIRA_BASE_URL` | Used in the Redmine description link |
| `JIRA_EMAIL` / `JIRA_API_TOKEN` | Optional. Fetches the parent when a subtask arrives first |
| `HOST` / `PORT` | Bind address. Default `127.0.0.1:8765` |

Redmine needs the REST API enabled (Administration, Settings, API).

```powershell
python app.py
```

`GET /health` returns `{"status":"ok"}`. Jira calls `POST /webhooks/jira`.

Jira Cloud cannot reach localhost. Point a tunnel or host at `http://127.0.0.1:8765` and use that public URL in Jira.

## Jira webhook

A Jira admin registers this. The API user used for issue reads cannot create system webhooks.

1. Open `https://drafttemp.atlassian.net`.
2. Settings (gear), System, WebHooks, Create a WebHook.
3. URL: `https://<public-host>/webhooks/jira`
4. Scope: Issue, created. JQL: `project = SUP`
5. If the form has a secret, set the same value as `JIRA_WEBHOOK_SECRET`. Jira signs the body in `X-Hub-Signature` (`sha256=<hmac>`).

If you use a Jira Automation rule instead of a system webhook, send the issue-created payload to the same URL and add header `X-Webhook-Secret` with the same secret.

Requests with a missing or wrong secret get `401`. Redmine errors get `500` so Jira can retry. A retry is safe: the subject lookup skips issues that were already created.

## What gets created

Redmine subject: the Jira key only.

Redmine description:

```text
Jira: https://drafttemp.atlassian.net/browse/SUP-1460
Summary: <Jira summary>

<plain-text description>
```

Subtasks set `parent_issue_id` to the Redmine issue whose subject is the parent key. If that parent is missing, it is created first. When `JIRA_EMAIL` and `JIRA_API_TOKEN` are set, the parent is loaded from Jira. Otherwise the parent is created from the key, summary, and link on the webhook.

Duplicate checks compare the subject exactly, inside `REDMINE_PROJECT`. A search hit in another issue's description is ignored. Two deliveries of the same create are serialized with an in-process lock and a second lookup immediately before create.

## Jenkins

This folder is the whole app. On the Jenkins server, create a **Secret file** credential with ID `jira-redmine-env`. The file is a `.env` like `.env.example`, with `HOST=0.0.0.0` so Jira can reach the port.

Create a Pipeline job from this repo and use the `Jenkinsfile`. The job stays running on `python app.py`. Point the Jira webhook at `http://<jenkins-host>:8765/webhooks/jira`. Port 8765 must be open on that machine. Jenkins itself stays on port 8080.

## Always on

`supervise.ps1` starts the service when port 8765 is free. A Windows sign-in task named `JiraRedmineSync` runs that script at login and every 5 minutes, so a stopped process comes back without a manual start.

A free tunnel address expires. With `JIRA_EMAIL` and `JIRA_API_TOKEN` set, the service also asks Jira every 60 seconds for Support issues created in the last 2 days and creates any Redmine issue that is missing. That path does not use the tunnel.

## Tests

```powershell
python -m unittest discover -s tests -v
```
