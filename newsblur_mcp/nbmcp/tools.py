"""The tools Claude can call. Reads are always on; mark_stories_read is listed only when ALLOW_MARK_READ=1.

There is deliberately no subscribe, unsubscribe, delete, share, star or unread tool.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from .client import NewsBlurClient, NewsBlurError
from .config import Config

HASH_RE = re.compile(r"^([0-9]{1,12}):([0-9a-f]{4,40})$")
WORDS_PER_MINUTE = 230
MAX_STORIES = 50
MAX_PAGES = 10
MAX_FEEDS_PER_QUERY = 200
MARK_CHUNK = 5            # NewsBlur asks clients to batch marks in small groups, not hammer the server
MARK_PAUSE = 1.5
FEEDS_TTL = 60.0


class ToolError(Exception):
    pass


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre", "section", "article", "ul", "ol"}
    SKIP = {"script", "style", "noscript", "svg", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    p.close()
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", "".join(p.out))
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _int_arg(args, name, default, lo, hi):
    v = args.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not lo <= v <= hi:
        raise ToolError(f"{name} must be a whole number between {lo} and {hi}")
    return int(v)


def _str_arg(args, name, default, choices):
    v = args.get(name, default)
    if v not in choices:
        raise ToolError(f"{name} must be one of {', '.join(choices)}")
    return v


class Tools:
    def __init__(self, cfg: Config, client: NewsBlurClient, clock=time.time):
        self.cfg, self.client, self.clock = cfg, client, clock
        self._feeds, self._feeds_at = None, 0.0

    # ------------------------------------------------------------ catalogue
    def specs(self) -> list[dict]:
        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}
        specs = [
            {"name": "list_folders",
             "description": "List the NewsBlur folders you may read, each with its feeds (id, title, unread counts: positive, neutral, negative). Use it to find folder names and feed ids.",
             "inputSchema": obj({})},
            {"name": "list_stories",
             "description": ("List stories, newest first, from one folder (folder) or specific feeds (feed_ids); with neither, from every readable folder. "
                             "Each story has story_hash, title, url, feed, authors, date, a 300-character snippet and an estimated reading time. "
                             f"Whole pages are returned, so you may get a few more than limit (max {MAX_STORIES}); next_page is set when there are more. since_days limits to recent stories."),
             "inputSchema": obj({
                 "folder": {"type": "string"},
                 "feed_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": MAX_FEEDS_PER_QUERY},
                 "read_filter": {"type": "string", "enum": ["unread", "all"], "default": "unread"},
                 "order": {"type": "string", "enum": ["newest", "oldest"], "default": "newest"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": MAX_STORIES, "default": 20},
                 "page": {"type": "integer", "minimum": 1, "default": 1},
                 "since_days": {"type": "integer", "minimum": 1, "maximum": 365}})},
            {"name": "get_story_text",
             "description": ("Fetch the readable full text of up to 5 stories by story_hash (NewsBlur's original-text view), for summarising. "
                             "Returns word count, estimated reading minutes and the text trimmed to max_chars. One failing story does not stop the others."),
             "inputSchema": obj({
                 "story_hashes": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                 "max_chars": {"type": "integer", "minimum": 500, "maximum": 60000, "description": f"default {self.cfg.max_text_chars}"}},
                ["story_hashes"])},
        ]
        if self.cfg.allow_mark_read:
            specs.append(
                {"name": "mark_stories_read",
                 "description": (f"Mark up to {self.cfg.max_marks_per_call} stories as read by story_hash. This is the only change this server can make. "
                                 "Use dry_run=true to preview. Marks are sent in small batches and are never retried automatically."),
                 "inputSchema": obj({
                     "story_hashes": {"type": "array", "minItems": 1, "maxItems": self.cfg.max_marks_per_call, "items": {"type": "string"}},
                     "dry_run": {"type": "boolean", "default": False}}, ["story_hashes"])})
        return specs

    def call(self, name: str, args: dict):
        args = args or {}
        handlers = {"list_folders": self.list_folders, "list_stories": self.list_stories, "get_story_text": self.get_story_text}
        if self.cfg.allow_mark_read:
            handlers["mark_stories_read"] = self.mark_stories_read
        if name not in handlers:
            raise ToolError(f"unknown tool {name!r}")
        try:
            return handlers[name](args)
        except NewsBlurError as e:
            raise ToolError(str(e)) from None

    # ------------------------------------------------------------ helpers
    def _catalogue(self):
        """(folders: name -> [feed ids], feeds: id -> info), limited to the allowed folders."""
        now = self.clock()
        if self._feeds is None or now - self._feeds_at > FEEDS_TTL:
            self._feeds, self._feeds_at = self.client.feeds(), now
        raw = self._feeds
        feeds = {int(k): v for k, v in (raw.get("feeds") or {}).items()}
        folders = {}
        for name, ids in (raw.get("flat_folders") or {}).items():
            clean = (name or "").strip()
            folders[clean or "(top level)"] = [int(i) for i in ids if int(i) in feeds]
        if self.cfg.allowed_folders:
            def ok(n):
                return any(n == a or n.startswith(a + " - ") for a in self.cfg.allowed_folders)
            folders = {n: ids for n, ids in folders.items() if ok(n)}
        allowed = {i for ids in folders.values() for i in ids}
        return folders, {i: f for i, f in feeds.items() if i in allowed}

    # ------------------------------------------------------------ reads
    def list_folders(self, a):
        folders, feeds = self._catalogue()
        out = []
        for name, ids in sorted(folders.items()):
            out.append({"name": name, "feeds": [
                {"id": i, "title": feeds[i].get("feed_title"), "unread": {"positive": feeds[i].get("ps", 0), "neutral": feeds[i].get("nt", 0),
                                                                          "negative": feeds[i].get("ng", 0)}} for i in ids if i in feeds]})
        return {"folders": out}

    def list_stories(self, a):
        folders, feeds = self._catalogue()
        limit = _int_arg(a, "limit", 20, 1, MAX_STORIES)
        page = _int_arg(a, "page", 1, 1, 1000)
        read_filter = _str_arg(a, "read_filter", "unread", ("unread", "all"))
        order = _str_arg(a, "order", "newest", ("newest", "oldest"))
        since = None
        if "since_days" in a:
            days = _int_arg(a, "since_days", 7, 1, 365)
            since = (datetime.fromtimestamp(self.clock(), timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        if a.get("folder") is not None and a.get("feed_ids") is not None:
            raise ToolError("give folder or feed_ids, not both")
        if a.get("folder") is not None:
            name = a["folder"]
            ids = sorted({i for n, fids in folders.items() if n == name or n.startswith(name + " - ") for i in fids})
            if not ids:
                raise ToolError(f"no readable folder named {name!r}; call list_folders")
        elif a.get("feed_ids") is not None:
            ids = [int(i) for i in a["feed_ids"]]
            if not ids or any(isinstance(i, bool) for i in a["feed_ids"]):
                raise ToolError("feed_ids must be a non-empty list of whole numbers")
            bad = [i for i in ids if i not in feeds]
            if bad:
                raise ToolError(f"feeds not readable or unknown: {bad[:5]}")
        else:
            ids = sorted(feeds)
        if not ids:
            return {"stories": [], "next_page": None}
        if len(ids) > MAX_FEEDS_PER_QUERY:
            raise ToolError(f"too many feeds ({len(ids)}); pick a folder")
        stories, seen, pages, p, more = [], set(), 0, page, False
        while len(stories) < limit and pages < MAX_PAGES:
            data = self.client.river_stories(ids, p, read_filter, order, since)
            rows = data.get("stories") or []
            hidden = data.get("hidden_stories_count") or 0
            pages += 1
            p += 1
            if not rows and not hidden:
                more = False
                break
            for s in rows:
                h = s.get("story_hash")
                if h and h not in seen:
                    seen.add(h)
                    stories.append(self._slim(s, feeds))
            more = bool(rows or hidden)
        return {"stories": stories, "count": len(stories), "next_page": p if more else None}

    def _slim(self, s, feeds):
        fid = s.get("story_feed_id")
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            pass
        text = html_to_text(s.get("story_content") or "")
        words = len(text.split())
        return {
            "story_hash": s.get("story_hash"),
            "title": s.get("story_title"),
            "url": s.get("story_permalink"),
            "feed_id": fid,
            "feed": (feeds.get(fid) or {}).get("feed_title"),
            "authors": s.get("story_authors") or None,
            "date": s.get("story_date"),
            "snippet": text[:300] or None,
            "feed_words": words,
            "read": bool(s.get("read_status")),
            "tags": s.get("story_tags") or [],
        }

    def get_story_text(self, a):
        hashes = a.get("story_hashes")
        if not isinstance(hashes, list) or not 1 <= len(hashes) <= 5:
            raise ToolError("story_hashes must be a list of 1 to 5 story hashes")
        max_chars = _int_arg(a, "max_chars", self.cfg.max_text_chars, 500, 60000)
        _, feeds = self._catalogue()
        out = []
        for h in hashes:
            m = HASH_RE.match(h) if isinstance(h, str) else None
            if not m:
                out.append({"story_hash": h if isinstance(h, str) else None, "error": "not a story hash (expected feedid:hash)"})
                continue
            if int(m.group(1)) not in feeds:
                out.append({"story_hash": h, "error": "story is not in a readable folder"})
                continue
            try:
                d = self.client.original_text(h)
            except NewsBlurError as e:
                out.append({"story_hash": h, "error": str(e)})
                continue
            text = html_to_text(d.get("original_text") or "")
            if d.get("failed") or not text:
                out.append({"story_hash": h, "error": "NewsBlur could not extract the original text", "failed": True})
                continue
            words = len(text.split())
            out.append({"story_hash": h, "words": words, "reading_minutes": max(1, round(words / WORDS_PER_MINUTE)),
                        "truncated": len(text) > max_chars, "text": text[:max_chars]})
        return {"stories": out}

    # ------------------------------------------------------------ writes
    def mark_stories_read(self, a):
        hashes = a.get("story_hashes")
        if not isinstance(hashes, list) or not 1 <= len(hashes) <= self.cfg.max_marks_per_call:
            raise ToolError(f"story_hashes must be a list of 1 to {self.cfg.max_marks_per_call} story hashes")
        dry = bool(a.get("dry_run", False))
        _, feeds = self._catalogue()
        results, todo = [], []
        for h in hashes:
            m = HASH_RE.match(h) if isinstance(h, str) else None
            if not m:
                results.append({"story_hash": h if isinstance(h, str) else None, "ok": False, "error": "not a story hash (expected feedid:hash)"})
            elif int(m.group(1)) not in feeds:
                results.append({"story_hash": h, "ok": False, "error": "story is not in a readable folder"})
            elif h in todo:
                results.append({"story_hash": h, "ok": False, "error": "duplicate in this call"})
            else:
                todo.append(h)
        if dry:
            results += [{"story_hash": h, "ok": True, "dry_run": True} for h in todo]
        else:
            for i in range(0, len(todo), MARK_CHUNK):
                chunk = todo[i:i + MARK_CHUNK]
                if i:
                    self.client.sleep(MARK_PAUSE)
                try:
                    d = self.client.mark_read(chunk)
                    ok = d.get("code", 1) != -1 and d.get("result", "ok") != "error"
                    err = None if ok else "NewsBlur refused the request"
                except NewsBlurError as e:
                    ok, err = False, str(e)
                results += [({"story_hash": h, "ok": True} if ok else {"story_hash": h, "ok": False, "error": err}) for h in chunk]
        good = sum(1 for r in results if r["ok"])
        return {"dry_run": dry, "ok": good, "failed": len(results) - good, "results": results}
