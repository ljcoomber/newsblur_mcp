from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    secret_path: str
    username: str
    password: str = ""
    allow_mark_read: bool = False             # the only write this server has; off unless ALLOW_MARK_READ=1
    allowed_folders: frozenset = frozenset()  # NewsBlur folder names; empty = every folder may be read
    max_marks_per_call: int = 25
    port: int = 8080
    max_text_chars: int = 12000
    allowed_origins: frozenset = frozenset({"https://claude.ai", "https://claude.com"})
    api_base: str = "https://newsblur.com"

    def __repr__(self):                       # the password must never reach a log or traceback
        return f"Config(username={self.username!r}, allow_mark_read={self.allow_mark_read}, allowed_folders={sorted(self.allowed_folders)})"


def _int(env, name, default, lo, hi):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


def load(env=None) -> Config:
    env = os.environ if env is None else env
    secret = env.get("SECRET_PATH", "").strip().strip("/")
    if len(secret) < 24 or "/" in secret:
        raise ConfigError("SECRET_PATH must be a random string of at least 24 characters (no slashes)")
    username = env.get("NEWSBLUR_USERNAME", "").strip()
    if not username:
        raise ConfigError("NEWSBLUR_USERNAME is not set")
    password = env.get("NEWSBLUR_PASSWORD", "")
    if not password:
        raise ConfigError("NEWSBLUR_PASSWORD is not set")
    folders = frozenset(x.strip() for x in env.get("ALLOWED_FOLDERS", "").split("|") if x.strip())
    origins = env.get("ALLOWED_ORIGINS", "").strip()
    return Config(
        secret_path=secret,
        username=username,
        password=password,
        allow_mark_read=env.get("ALLOW_MARK_READ", "").strip() == "1",
        allowed_folders=folders,
        max_marks_per_call=_int(env, "MAX_MARKS_PER_CALL", 25, 1, 100),
        port=_int(env, "PORT", 8080, 1, 65535),
        max_text_chars=_int(env, "MAX_TEXT_CHARS", 12000, 500, 60000),
        allowed_origins=frozenset(o.strip() for o in origins.split(",") if o.strip())
        if origins else Config.__dataclass_fields__["allowed_origins"].default,
    )
