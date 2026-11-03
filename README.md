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
| `JIRA_WEBHOOK_SECRET` | Shared secret for `POST /webhooks/jira` |
| `JIRA_PROJECT_KEYS` | Comma-separated keys. Default `SUP,MC,DSC,DPS` |
| `JIRA_BASE_URL` | Used in the Redmine description link |
| `JIRA_EMAIL` | Atlassian account that can read the project |
| `JIRA_API_TOKEN` | API token for that account. Turns polling and `--reconcile` / `--once` on |
| `POLL_INTERVAL_SECONDS` | How often to ask Jira. Default `60` |
| `POLL_LOOKBACK_DAYS` | How far back a poll looks. Default `2` |
| `RECONCILE_LOOKBACK_DAYS` | How far back `--reconcile` looks. Default `7` |
| `HOST` / `PORT` | Bind address. Default `127.0.0.1:8765` |

Redmine needs the REST API enabled (Administration, Settings, API).

```powershell
python app.py
```

`GET /health` returns `{"status":"ok"}`.

With `JIRA_EMAIL` and `JIRA_API_TOKEN` set, one issue can be synced and the process then exits:

```powershell
python app.py --once SUP-1472
```

To find Jira issues from the last `RECONCILE_LOOKBACK_DAYS` (default 7) that are still missing in Redmine and create them:

```powershell
python app.py --reconcile
```

Reconcile checks Redmine by exact subject (the Jira key). It does not use `.run/seen-keys.txt`. It prints a JSON summary with `scanned`, `created`, `skipped`, and `failed` counts, and exits non-zero if any create failed.

The same Jira settings also turn on a 60-second poll when `app.py` is started with no arguments. A key is remembered after a successful poll, so the next poll skips it. Tickets older than the lookback window are left alone. Prefer `--reconcile` (or the Jenkins cron) to recover missed webhook creates.

## Jenkins

This folder is the whole app. On the Jenkins server, create a **Secret file** credential with ID `jira-redmine-env`. The file is a `.env` like `.env.example`. Keep `JIRA_EMAIL` and `JIRA_API_TOKEN` in that secret so `--once` and `--reconcile` can call Jira.

Create a Pipeline job from this repo and use the `Jenkinsfile`.

- Non-empty `ISSUE_KEY` → `python app.py --once` (webhook / manual one-shot)
- Empty `ISSUE_KEY` → `python app.py --reconcile` (catch-up for missing Redmine subjects)
- Cron `H */4 * * *` runs about every four hours with an empty `ISSUE_KEY`, so missed creates are backfilled
- `disableConcurrentBuilds` keeps webhook creates and reconcile from overlapping

## Jira webhook

Jira Cloud only accepts an `https://` webhook URL. A Cloudflare Worker at `worker/index.js` checks the webhook secret and starts the Jenkins build for that issue key.

1. Open `https://drafttemp.atlassian.net`.
2. Settings (gear), System, WebHooks.
3. URL: `https://<worker>.workers.dev`
4. Scope: Issue, created. JQL: `project in (SUP, MC, DSC, DPS)`
5. Set the secret to `JIRA_WEBHOOK_SECRET`. Jira signs the body in `X-Hub-Signature` (`sha256=<hmac>`). A caller can also send header `X-Webhook-Secret`.

Requests with a missing or wrong secret get `401`. Redmine errors get `500` so a delivery can be retried. A retry is safe: the subject lookup skips issues that were already created.

## What gets created

Redmine subject: the Jira key only.

Redmine description:

```text
Jira: https://drafttemp.atlassian.net/browse/SUP-1460
Summary: <Jira summary>

<plain-text description>
```

Subtasks set `parent_issue_id` to the Redmine issue whose subject is the parent key. If that parent is missing, it is created first. When `JIRA_EMAIL` and `JIRA_API_TOKEN` are set, the parent is loaded from Jira. Otherwise the parent is created from the key, summary, and link on the payload.

Duplicate checks compare the subject exactly, inside `REDMINE_PROJECT`. A search hit in another issue's description is ignored. Two deliveries of the same create are serialized with an in-process lock and a second lookup immediately before create.

## Tests

```powershell
python -m unittest discover -s tests -v
```
