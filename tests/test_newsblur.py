"""Run:  python3 -W error::ResourceWarning -I tests/test_newsblur.py"""
import http.client, json, os, sys, unittest, urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newsblur_mcp"))
from nbmcp import client as nbclient, config, protocol, server, tools

SECRET = "s3cr3t" + "0123456789abcdef" + "ghijklmn"
USER, PASSWORD = "lee", "pw-DO-NOT-LEAK-98765"
COOKIE = "newsblur_sessionid=abc123"


class FakeNewsBlur:
    """In-memory stand-in for the NewsBlur API (response shapes from the public client behaviour)."""

    def __init__(self, n_per_feed=8):
        self.requests, self.logins, self.marked = [], 0, []
        self.session_valid = True
        self.feeds = {
            "1": {"id": 1, "feed_title": "Newsletters A", "ps": 0, "nt": n_per_feed, "ng": 0},
            "2": {"id": 2, "feed_title": "Newsletters B", "ps": 1, "nt": n_per_feed, "ng": 0},
            "3": {"id": 3, "feed_title": "Private blog", "ps": 0, "nt": 3, "ng": 0},
        }
        self.flat = {"Linky Newsletters": [1, 2], "Private": [3], " ": []}
        self.stories = {}
        for fid in (1, 2, 3):
            self.stories[fid] = [{"story_hash": f"{fid}:{i:06x}", "story_title": f"Story {fid}-{i}", "story_permalink": f"https://example.com/{fid}/{i}",
                                  "story_authors": "Ann", "story_date": f"2026-10-1{i % 9} 07:00:00", "story_feed_id": fid,
                                  "story_content": f"<p>Hello <b>world</b> {fid}-{i}</p><script>evil()</script>", "read_status": 0, "story_tags": ["t"]}
                                 for i in range(1, n_per_feed + 1)]
        self.fail = {}
        self.page_size = 6
        self.password = PASSWORD
        self.original = {"1:000001": "<h1>Full</h1><p>Full text of the story.</p>"}

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        form = urllib.parse.parse_qs(body.decode()) if body else {}
        self.requests.append((method, path, q, form, headers))
        queue = self.fail.get((method, path))
        if queue:
            return queue.pop(0)
        if path == "/api/login":
            self.logins += 1
            if form.get("username") == [USER] and form.get("password") == [self.password]:
                self.session_valid = True
                return 200, {"Set-Cookie": [COOKIE + "; expires=Thu, 01-Jan-2099 00:00:00 GMT; Path=/; HttpOnly"]}, b'{"authenticated": true, "code": 1}'
            return 200, {"Set-Cookie": []}, b'{"authenticated": false, "code": -1, "errors": {"__all__": ["bad"]}}'
        if headers.get("Cookie") != COOKIE or not self.session_valid:
            return 403, {}, b'{"authenticated": false, "code": -1}'
        if method == "GET" and path == "/reader/feeds":
            return 200, {}, json.dumps({"authenticated": True, "feeds": self.feeds, "flat_folders": self.flat}).encode()
        if method == "GET" and path == "/reader/river_stories":
            ids = [int(x) for x in q.get("feeds", [])]
            page = int(q.get("page", ["1"])[0])
            rows = [s for fid in ids for s in self.stories.get(fid, [])]
            rows = [s for s in rows if not s["read_status"]] if q.get("read_filter", ["unread"])[0] == "unread" else rows
            rows.sort(key=lambda s: s["story_hash"], reverse=q.get("order", ["newest"])[0] == "newest")
            chunk = rows[(page - 1) * self.page_size: page * self.page_size]
            return 200, {}, json.dumps({"stories": chunk, "hidden_stories_count": 0}).encode()
        if method == "GET" and path == "/rss_feeds/original_text":
            h = q["story_hash"][0]
            if h in self.original:
                return 200, {}, json.dumps({"original_text": self.original[h], "failed": False}).encode()
            return 200, {}, b'{"original_text": "", "failed": true}'
        if method == "POST" and path == "/reader/mark_story_hashes_as_read":
            self.marked.append(form.get("story_hash", []))
            return 200, {}, b'{"code": 1, "result": "ok"}'
        return 404, {}, b"{}"


def make(mark=False, folders=(), **env):
    fake = FakeNewsBlur()
    e = {"SECRET_PATH": SECRET, "NEWSBLUR_USERNAME": USER, "NEWSBLUR_PASSWORD": PASSWORD, "ALLOW_MARK_READ": "1" if mark else "",
         "ALLOWED_FOLDERS": "|".join(folders)}
    e.update(env)
    cfg = config.load(e)
    sleeps = []
    cl = nbclient.NewsBlurClient(USER, PASSWORD, cfg.api_base, transport=fake, sleep=sleeps.append)
    return fake, cfg, cl, tools.Tools(cfg, cl), sleeps


class ConfigTests(unittest.TestCase):
    def base(self, **kw):
        e = {"SECRET_PATH": SECRET, "NEWSBLUR_USERNAME": USER, "NEWSBLUR_PASSWORD": PASSWORD}
        e.update(kw)
        return e

    def test_requires_secret_user_and_password(self):
        for missing in ("SECRET_PATH", "NEWSBLUR_USERNAME", "NEWSBLUR_PASSWORD"):
            e = self.base()
            del e[missing]
            with self.assertRaises(config.ConfigError):
                config.load(e)
        with self.assertRaises(config.ConfigError):
            config.load(self.base(SECRET_PATH="short"))

    def test_defaults_are_read_only(self):
        cfg = config.load(self.base())
        self.assertFalse(cfg.allow_mark_read)
        self.assertEqual(cfg.allowed_folders, frozenset())

    def test_mark_read_needs_exactly_1(self):
        self.assertFalse(config.load(self.base(ALLOW_MARK_READ="true")).allow_mark_read)
        self.assertTrue(config.load(self.base(ALLOW_MARK_READ="1")).allow_mark_read)

    def test_password_not_in_repr(self):
        cfg = config.load(self.base())
        self.assertNotIn(PASSWORD, repr(cfg))
        self.assertNotIn(PASSWORD, str(cfg))

    def test_folder_list_uses_pipe(self):
        cfg = config.load(self.base(ALLOWED_FOLDERS="Linky Newsletters| Other , x "))
        self.assertEqual(cfg.allowed_folders, frozenset({"Linky Newsletters", "Other , x"}))


class ClientTests(unittest.TestCase):
    def test_login_once_and_cookie_sent(self):
        fake, _, cl, _, _ = make()
        cl.feeds()
        cl.feeds()
        self.assertEqual(fake.logins, 1)
        self.assertTrue(all(r[4].get("Cookie") == COOKIE for r in fake.requests if r[1] != "/api/login"))

    def test_bad_password_gives_friendly_error_and_cooldown(self):
        fake, _, cl, _, _ = make()
        fake.password = "changed"
        with self.assertRaises(nbclient.NewsBlurError) as c:
            cl.feeds()
        self.assertNotIn(PASSWORD, str(c.exception))
        with self.assertRaises(nbclient.NewsBlurError):
            cl.feeds()
        self.assertEqual(fake.logins, 1)            # no second attempt inside the cooldown

    def test_expired_session_logs_in_again_once(self):
        fake, _, cl, _, _ = make()
        cl.feeds()
        fake.session_valid = False
        fake.stories  # session invalid until the next login
        orig = fake.__call__

        def relogin_ok(method, url, headers, body):
            return orig(method, url, headers, body)
        cl.transport = relogin_ok
        data = cl.feeds()
        self.assertIn("feeds", data)
        self.assertEqual(fake.logins, 2)

    def test_still_rejected_after_relogin_raises(self):
        fake, _, cl, _, _ = make()
        fake.fail[("GET", "/reader/feeds")] = [(403, {}, b"{}"), (403, {}, b"{}")]
        with self.assertRaises(nbclient.NewsBlurError):
            cl.feeds()

    def test_get_retries_on_429_but_post_does_not(self):
        fake, _, cl, _, sleeps = make()
        fake.fail[("GET", "/reader/feeds")] = [(429, {"Retry-After": "2"}, b"{}")]
        self.assertIn("feeds", cl.feeds())
        self.assertEqual(sleeps, [2.0])
        fake.fail[("POST", "/reader/mark_story_hashes_as_read")] = [(429, {}, b"{}")]
        with self.assertRaises(nbclient.NewsBlurError):
            cl.mark_read(["1:000001"])
        self.assertEqual(len([r for r in fake.requests if r[1] == "/reader/mark_story_hashes_as_read"]), 1)

    def test_network_failure_is_friendly(self):
        _, _, cl, _, _ = make()

        def boom(*a):
            raise nbclient.NewsBlurError(0, "could not reach NewsBlur (OSError)")
        cl.transport = boom
        with self.assertRaises(nbclient.NewsBlurError):
            cl.feeds()


class ReadTools(unittest.TestCase):
    def test_list_folders(self):
        _, _, _, t, _ = make()
        out = t.call("list_folders", {})
        names = [f["name"] for f in out["folders"]]
        self.assertIn("Linky Newsletters", names)
        self.assertIn("(top level)", names)
        f = next(f for f in out["folders"] if f["name"] == "Linky Newsletters")
        self.assertEqual([x["id"] for x in f["feeds"]], [1, 2])
        self.assertEqual(f["feeds"][1]["unread"]["positive"], 1)

    def test_allowed_folders_hide_the_rest(self):
        _, _, _, t, _ = make(folders=("Linky Newsletters",))
        out = t.call("list_folders", {})
        self.assertEqual([f["name"] for f in out["folders"]], ["Linky Newsletters"])
        with self.assertRaises(tools.ToolError):
            t.call("list_stories", {"folder": "Private"})
        with self.assertRaises(tools.ToolError):
            t.call("list_stories", {"feed_ids": [3]})
        out = t.call("list_stories", {"limit": 50})
        self.assertTrue(all(s["feed_id"] in (1, 2) for s in out["stories"]))

    def test_list_stories_paging_and_limit(self):
        fake, _, _, t, _ = make()
        out = t.call("list_stories", {"folder": "Linky Newsletters", "limit": 10})
        self.assertEqual(out["count"], 12)                      # whole pages of 6, so never a gap between pages
        self.assertEqual(len({s["story_hash"] for s in out["stories"]}), 12)
        self.assertIsNotNone(out["next_page"])
        out2 = t.call("list_stories", {"folder": "Linky Newsletters", "limit": 10, "page": out["next_page"]})
        self.assertGreater(out2["count"], 0)

    def test_story_shape_is_clean_text_and_has_no_html_or_script(self):
        _, _, _, t, _ = make()
        s = t.call("list_stories", {"feed_ids": [1], "limit": 1})["stories"][0]
        self.assertEqual(set(s), {"story_hash", "title", "url", "feed_id", "feed", "authors", "date", "snippet", "feed_words", "read", "tags"})
        self.assertNotIn("<", s["snippet"])
        self.assertNotIn("evil", s["snippet"])
        self.assertEqual(s["feed"], "Newsletters A")

    def test_argument_validation(self):
        _, _, _, t, _ = make()
        for args in ({"limit": 0}, {"limit": 51}, {"limit": True}, {"read_filter": "x"}, {"order": "x"}, {"page": 0},
                     {"folder": "A", "feed_ids": [1]}, {"feed_ids": []}, {"since_days": 0}):
            with self.assertRaises(tools.ToolError, msg=str(args)):
                t.call("list_stories", args)
        with self.assertRaises(tools.ToolError):
            t.call("list_stories", {"folder": "No such folder"})
        with self.assertRaises(tools.ToolError):
            t.call("nope", {})

    def test_since_days_sends_a_date(self):
        fake, _, _, t, _ = make()
        t.call("list_stories", {"feed_ids": [1], "since_days": 3, "limit": 1})
        req = next(r for r in fake.requests if r[1] == "/reader/river_stories")
        self.assertRegex(req[2]["date_filter_start"][0], r"^\d{4}-\d{2}-\d{2}$")

    def test_get_story_text(self):
        _, _, _, t, _ = make()
        out = t.call("get_story_text", {"story_hashes": ["1:000001", "1:000002", "9:abcdef", "bad"]})["stories"]
        self.assertIn("Full text of the story.", out[0]["text"])
        self.assertEqual(out[0]["reading_minutes"], 1)
        self.assertTrue(out[1]["failed"])
        self.assertIn("readable folder", out[2]["error"])
        self.assertIn("not a story hash", out[3]["error"])

    def test_get_story_text_truncates(self):
        fake, _, _, t, _ = make()
        fake.original["1:000003"] = "<p>" + "word " * 5000 + "</p>"
        r = t.call("get_story_text", {"story_hashes": ["1:000003"], "max_chars": 500})["stories"][0]
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["text"]), 500)
        self.assertGreater(r["reading_minutes"], 15)

    def test_login_failure_surfaces_as_tool_error_without_password(self):
        fake, _, _, t, _ = make()
        fake.password = "changed"
        with self.assertRaises(tools.ToolError) as c:
            t.call("list_folders", {})
        self.assertNotIn(PASSWORD, str(c.exception))


class MarkReadTools(unittest.TestCase):
    def test_not_listed_or_callable_without_flag(self):
        _, _, _, t, _ = make(mark=False)
        self.assertEqual([s["name"] for s in t.specs()], ["list_folders", "list_stories", "get_story_text"])
        with self.assertRaises(tools.ToolError):
            t.call("mark_stories_read", {"story_hashes": ["1:000001"]})

    def test_listed_with_flag(self):
        _, _, _, t, _ = make(mark=True)
        self.assertIn("mark_stories_read", [s["name"] for s in t.specs()])

    def test_dry_run_changes_nothing(self):
        fake, _, _, t, _ = make(mark=True)
        out = t.call("mark_stories_read", {"story_hashes": ["1:000001", "2:000002"], "dry_run": True})
        self.assertEqual((out["ok"], out["failed"]), (2, 0))
        self.assertEqual(fake.marked, [])

    def test_batches_of_five_with_pauses(self):
        fake, _, _, t, sleeps = make(mark=True)
        hashes = [f"1:{i:06x}" for i in range(1, 8)]
        out = t.call("mark_stories_read", {"story_hashes": hashes})
        self.assertEqual((out["ok"], out["failed"]), (7, 0))
        self.assertEqual([len(m) for m in fake.marked], [5, 2])
        self.assertEqual(sleeps, [tools.MARK_PAUSE])

    def test_rejects_bad_unreadable_and_duplicate(self):
        fake, _, _, t, _ = make(mark=True, folders=("Linky Newsletters",))
        out = t.call("mark_stories_read", {"story_hashes": ["1:000001", "1:000001", "3:000001", "junk", 5]})
        self.assertEqual((out["ok"], out["failed"]), (1, 4))
        self.assertEqual(fake.marked, [["1:000001"]])

    def test_too_many(self):
        _, _, _, t, _ = make(mark=True, MAX_MARKS_PER_CALL="2")
        with self.assertRaises(tools.ToolError):
            t.call("mark_stories_read", {"story_hashes": ["1:000001", "1:000002", "1:000003"]})

    def test_a_failed_batch_is_reported_not_retried(self):
        fake, _, _, t, _ = make(mark=True)
        fake.fail[("POST", "/reader/mark_story_hashes_as_read")] = [(500, {}, b"{}")]
        out = t.call("mark_stories_read", {"story_hashes": ["1:000001", "1:000002"]})
        self.assertEqual((out["ok"], out["failed"]), (0, 2))
        self.assertEqual(len([r for r in fake.requests if r[1] == "/reader/mark_story_hashes_as_read"]), 1)


class HttpEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake, cfg, cl, cls.tools, _ = make(mark=True)
        cls.cfg = cfg
        cls.srv = server.make_server(cfg, cls.tools, host="127.0.0.1", port=0)
        cls.port = cls.srv.server_address[1]
        server.serve_in_thread(cls.srv)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, path, payload, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = origin
        c.request("POST", path, json.dumps(payload), h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def rpc(self, method, params=None, id_=1):
        return self.post(f"/{SECRET}/mcp", {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})

    def test_initialize_and_tools(self):
        st, body = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "newsblur-reader")
        st, body = self.rpc("tools/list")
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["list_folders", "list_stories", "get_story_text", "mark_stories_read"])

    def test_call_and_password_never_in_responses(self):
        st, body = self.rpc("tools/call", {"name": "list_stories", "arguments": {"folder": "Linky Newsletters", "limit": 3}})
        res = json.loads(body)["result"]
        self.assertFalse(res["isError"])
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertEqual(json.loads(res["content"][0]["text"])["count"], 6)

    def test_wrong_path_and_origin(self):
        self.assertEqual(self.post("/nope/mcp", {})[0], 404)
        self.assertEqual(self.post(f"/{SECRET}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(f"/{SECRET}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="https://claude.ai")[0], 200)

    def test_healthz(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b"ok"))
        c.close()


if __name__ == "__main__":
    unittest.main()
