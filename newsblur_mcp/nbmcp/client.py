"""NewsBlur client (stdlib only). NewsBlur has no API tokens, so it logs in with the account's username and
password and keeps the session cookie in memory. The password is never included in errors or logs."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "nbmcp/0.1 (personal reading tools)"
LOGIN_COOLDOWN = 300.0          # after a rejected login, do not try again for 5 minutes (avoid locking the account)


class NewsBlurError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _urllib_transport(method, url, headers, body, timeout=30.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = dict(r.headers)
            h["Set-Cookie"] = r.headers.get_all("Set-Cookie") or []
            return r.status, h, r.read()
    except urllib.error.HTTPError as e:
        h = dict(e.headers)
        h["Set-Cookie"] = e.headers.get_all("Set-Cookie") or []
        return e.code, h, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NewsBlurError(0, f"could not reach NewsBlur ({type(e).__name__})") from None


def _session_cookie(headers) -> str | None:
    cookies = headers.get("Set-Cookie") or []
    if isinstance(cookies, str):
        cookies = [cookies]
    for c in cookies:
        first = c.split(";", 1)[0].strip()
        if first.startswith("newsblur_sessionid=") and len(first) > len("newsblur_sessionid="):
            return first
    return None


class NewsBlurClient:
    def __init__(self, username: str, password: str, base: str = "https://newsblur.com", transport=None,
                 sleep=time.sleep, clock=time.monotonic, max_concurrent: int = 2):
        self._user, self._pw, self.base = username, password, base.rstrip("/")
        self.transport = transport or _urllib_transport
        self.sleep, self.clock = sleep, clock
        self._gate = threading.BoundedSemaphore(max_concurrent)
        self._login_lock = threading.Lock()
        self._cookie: str | None = None
        self._login_failed_at: float | None = None

    # ------------------------------------------------------------ session
    def _login(self):
        with self._login_lock:
            if self._cookie:
                return
            if self._login_failed_at is not None and self.clock() - self._login_failed_at < LOGIN_COOLDOWN:
                raise NewsBlurError(401, "NewsBlur rejected the login recently; check NEWSBLUR_USERNAME and NEWSBLUR_PASSWORD, then redeploy")
            body = urllib.parse.urlencode({"username": self._user, "password": self._pw}).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": UA}
            with self._gate:
                status, resp_headers, raw = self.transport("POST", self.base + "/api/login", headers, body)
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            cookie = _session_cookie(resp_headers)
            if status == 200 and data.get("authenticated") and cookie:
                self._cookie, self._login_failed_at = cookie, None
                return
            if status == 429 or status >= 500 or status == 0:
                raise NewsBlurError(status, "NewsBlur is not accepting logins right now; try again shortly")
            self._login_failed_at = self.clock()
            raise NewsBlurError(401, "NewsBlur rejected the username or password")

    def _request(self, method: str, path: str, params=None, form=None, _retried=False):
        self._login()
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        headers = {"Cookie": self._cookie, "Accept": "application/json", "User-Agent": UA}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        retries = 3 if method == "GET" else 0          # a write is never repeated automatically
        for attempt in range(retries + 1):
            with self._gate:
                status, resp_headers, raw = self.transport(method, url, headers, data)
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 1.0 * (2 ** attempt)
                try:
                    wait = min(10.0, float(resp_headers.get("Retry-After", wait)))
                except (TypeError, ValueError):
                    pass
                self.sleep(wait)
                continue
            break
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        expired = status in (401, 403) or (isinstance(parsed, dict) and parsed.get("authenticated") is False)
        if expired and not _retried:
            self._cookie = None                          # session expired: log in once more and repeat
            return self._request(method, path, params, form, _retried=True)
        if expired:
            raise NewsBlurError(401, "NewsBlur session was rejected after logging in again")
        if 200 <= status < 300:
            if parsed is None:
                if not raw:
                    return {}
                raise NewsBlurError(status, "NewsBlur returned something that was not JSON")
            return parsed
        message = {429: "NewsBlur is rate limiting requests; try again shortly"}.get(status) or f"NewsBlur returned HTTP {status}"
        raise NewsBlurError(status, message)

    # ------------------------------------------------------------ reads
    def feeds(self) -> dict:
        return self._request("GET", "/reader/feeds", {"flat": "true", "include_favicons": "false"})

    def river_stories(self, feed_ids, page=1, read_filter="unread", order="newest", since=None) -> dict:
        params = [("feeds", int(f)) for f in feed_ids] + [("page", int(page)), ("read_filter", read_filter), ("order", order)]
        if since:
            params.append(("date_filter_start", since))
        return self._request("GET", "/reader/river_stories", params)

    def original_text(self, story_hash: str) -> dict:
        return self._request("GET", "/rss_feeds/original_text", {"story_hash": story_hash})

    # ------------------------------------------------------------ writes (callers gate these)
    def mark_read(self, story_hashes) -> dict:
        return self._request("POST", "/reader/mark_story_hashes_as_read", form={"story_hash": list(story_hashes)})
