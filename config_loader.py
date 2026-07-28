"""Load webshop_config.ini and expose typed helpers."""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import List

CONFIG_PATH = Path("static/config/webshop_config.ini")


def load_config(path: str | Path = CONFIG_PATH) -> configparser.ConfigParser:
    # interpolation=None so passwords with '%' are kept literally
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path):
        raise FileNotFoundError(f"Config not found: {path}")
    return config


def _parse_scopes(raw: str) -> List[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def gmail_scopes(config: configparser.ConfigParser) -> List[str]:
    return _parse_scopes(config.get("gmail", "scopes", fallback=""))


def google_scopes(config: configparser.ConfigParser) -> List[str]:
    return _parse_scopes(config.get("google", "scopes", fallback=""))


def oauth_scopes(config: configparser.ConfigParser) -> List[str]:
    """Combined Gmail + Sheets/Drive scopes for a single OAuth consent."""
    seen = set()
    combined: List[str] = []
    for scope in gmail_scopes(config) + google_scopes(config):
        if scope not in seen:
            seen.add(scope)
            combined.append(scope)
    return combined


def webshop_password(config: configparser.ConfigParser) -> str:
    """Always use [webshop] password from webshop_config.ini."""
    return config.get("webshop", "password", fallback="").strip()


def batch_max_rows(config: configparser.ConfigParser) -> int:
    return config.getint("webshop", "batch_max_rows", fallback=100)


def unattended_poll_interval_sec(config: configparser.ConfigParser) -> int:
    """Seconds between unattended sheet polls (default 60)."""
    return config.getint("unattended", "poll_interval_sec", fallback=60)


def unattended_headless(config: configparser.ConfigParser) -> bool:
    """Whether unattended mode forces headless Chrome."""
    return config.getboolean("unattended", "headless", fallback=False)


def webshop_cdp_port(config: configparser.ConfigParser) -> int:
    """Fixed Chrome remote-debugging port for --login / unattended session share."""
    return config.getint("webshop", "cdp_port", fallback=9222)


def alert_notify_emails(config: configparser.ConfigParser) -> List[str]:
    """Recipients for session-restore alerts ([alerts] notify_emails)."""
    raw = config.get("alerts", "notify_emails", fallback="")
    emails: List[str] = []
    for part in raw.replace(";", ",").split(","):
        addr = part.strip()
        if addr and "@" in addr:
            emails.append(addr)
    return emails


def ensure_runtime_dirs(config: configparser.ConfigParser) -> None:
    Path(config.get("webshop", "downloads_dir")).mkdir(parents=True, exist_ok=True)
    Path(
        config.get("webshop", "user_data_dir", fallback="static/browser_profile")
    ).mkdir(parents=True, exist_ok=True)
    Path(config.get("paths", "batch_csv_dir")).mkdir(parents=True, exist_ok=True)
    Path(config.get("logging", "log_file")).parent.mkdir(parents=True, exist_ok=True)
