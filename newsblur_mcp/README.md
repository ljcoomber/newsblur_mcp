# newsblur-mcp

Small MCP server (stdlib only) that lets Claude read stories from one NewsBlur account for weekly reading triage.
It logs in with the account's username and password (NewsBlur has no API tokens) and keeps the session cookie in memory.

Tools (always on): `list_folders`, `list_stories`, `get_story_text`.
Tool (only with `ALLOW_MARK_READ=1`): `mark_stories_read`, the only change this server can make, sent in batches of 5.
There is no subscribe, unsubscribe, delete, share or star tool.

Environment variables (Railway):
- `NEWSBLUR_USERNAME`, `NEWSBLUR_PASSWORD`: the account login. Type them into the Railway dashboard yourself; never paste them anywhere else.
- `SECRET_PATH`: random string, at least 24 characters, no slashes. The connector URL is `https://<domain>/<SECRET_PATH>/mcp`.
- `ALLOWED_FOLDERS`: optional, folder names separated by `|` (a folder name may contain commas). Empty means every folder is readable.
- `ALLOW_MARK_READ`: `1` to list `mark_stories_read`. Default off.
- Optional: `PORT` (8080), `MAX_TEXT_CHARS` (12000), `MAX_MARKS_PER_CALL` (25), `ALLOWED_ORIGINS`.

A rejected login is not retried for 5 minutes, to avoid locking the account. Fix the variables and redeploy.

Tests: `python3 -W error::ResourceWarning -I tests/test_newsblur.py` (31 tests, fake NewsBlur API; the real API shapes still need one live check).
