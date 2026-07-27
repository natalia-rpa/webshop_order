"""
Webshop Order Robot — entry point.

process_emails():
  1. Gmail OAuth + Google Sheets init
  2. Find MAIN rows: MANUAL_PHASE=VALID & ROBOT_PHASE empty
  3. Extract client data + prepare A/B CSV batches (max 100 rows each)
  4. Playwright: login, impersonate, batch upload loop, add to cart
  5. Update ROBOT_PHASE (PROCESSING / ERROR / FINISHED)
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

from auth import init_connections
from config_loader import batch_max_rows, ensure_runtime_dirs, load_config
from csv_utils import prepare_batch_payload
from logging_setup import get_logger, setup_logging
from spreadsheet_processing import (
    OrderRow,
    extract_order_payload,
    find_pending_orders,
    set_robot_phase,
)
from webshop_bot import WebshopBot

VERSION = "1.0.0"


def _bootstrap_logging(config) -> Any:
    return setup_logging(
        log_file=config.get("logging", "log_file"),
        log_level=config.get("logging", "log_level", fallback="INFO"),
        max_bytes=config.getint("logging", "max_bytes", fallback=10_485_760),
        backup_count=config.getint("logging", "backup_count", fallback=5),
    )


def process_single_order(
    sheet,
    sheets_client,
    order: OrderRow,
    config,
    bot: Optional[WebshopBot] = None,
) -> bool:
    """
    Process one MAIN row end-to-end.
    Returns True on success.
    """
    logger = get_logger()
    logger.info(
        "Process started for client_number=%s client_name=%s emailID=%s row=%s",
        order.client_number,
        order.client_name,
        order.email_id,
        order.row_number,
    )

    def on_phase(phase: str, detail: str) -> None:
        set_robot_phase(sheet, order.row_number, phase, detail, config)

    owns_bot = bot is None
    try:
        set_robot_phase(sheet, order.row_number, "PROCESSING", "Extracting row data", config)

        if not order.client_number:
            raise ValueError("client_number is empty on processing row.")
        if not order.attachments_path:
            raise ValueError("ATTACHMENTS_PATH is empty on processing row.")

        _, source_csv = extract_order_payload(sheet, order, sheets_client, config)

        max_rows = batch_max_rows(config)
        set_robot_phase(
            sheet,
            order.row_number,
            "PROCESSING",
            f"Preparing A/B CSV batches (max {max_rows} rows)",
            config,
        )
        payload = prepare_batch_payload(
            source_csv,
            config.get("paths", "batch_csv_dir"),
            stem=f"batch_{order.client_number}_{order.row_number}",
            batch_size=max_rows,
        )
        logger.info(
            "Order has %s item row(s) -> %s batch file(s).",
            payload.total_rows,
            payload.batch_count,
        )

        if bot is None:
            bot = WebshopBot(config=config, on_phase=on_phase)
            bot.start()
        else:
            bot.on_phase = on_phase

        bot.run_batch_order(
            client_number=order.client_number,
            client_name=order.client_name,
            batch_csvs=payload.batch_files,
        )

        set_robot_phase(sheet, order.row_number, "FINISHED", "FINISHED", config)
        logger.info("Process completed successfully for row %s.", order.row_number)
        return True

    except Exception as exc:
        reason = str(exc).strip() or type(exc).__name__
        logger.error("Error processing row %s: %s", order.row_number, reason)
        logger.error(traceback.format_exc())
        try:
            set_robot_phase(sheet, order.row_number, "ERROR", reason, config)
        except Exception as sheet_exc:
            logger.error("Failed to write ERROR phase: %s", sheet_exc)
        return False

    finally:
        if owns_bot and bot is not None:
            bot.stop()


def process_emails(max_orders: Optional[int] = None) -> int:
    """
    Main orchestration (Gmail + Sheets arrival, then order processing).

    Returns number of successfully finished orders.
    """
    config = load_config()
    ensure_runtime_dirs(config)
    logger = _bootstrap_logging(config)

    logger.info("======= Webshop Order Robot v%s =======", VERSION)
    logger.info("Connecting to Google Sheets...")

    gmail_service, sheets_client, main_sheet = init_connections(config)
    _ = gmail_service
    logger.info("Sheets connection ready.")

    pending = find_pending_orders(main_sheet, config)
    if not pending:
        logger.info("No rows ready (MANUAL_PHASE=VALID & ROBOT_PHASE empty).")
        return 0

    if max_orders is not None:
        pending = pending[: max(0, max_orders)]

    success = 0
    bot: Optional[WebshopBot] = None
    try:
        bot = WebshopBot(config=config)
        bot.start()

        for order in pending:
            ok = process_single_order(
                main_sheet, sheets_client, order, config, bot=bot
            )
            if ok:
                success += 1
    finally:
        if bot is not None:
            bot.stop()

    logger.info("Finished run: %s/%s order(s) succeeded.", success, len(pending))
    return success


def check_required_files() -> bool:
    required = [
        "static/config/webshop_config.ini",
        "static/secrets/oauth-keys.json",
    ]
    missing = [p for p in required if not Path(p).exists()]
    if missing:
        print("Missing required files:")
        for path in missing:
            print(f"  - {path}")
        return False
    return True


def bootstrap_login(timeout_min: int = 10) -> int:
    """
    Open REAL Chrome (not Playwright) with the bot profile only
    (static/browser_profile) — never the user's default Profile 1.
    """
    import subprocess
    import time

    config = load_config()
    ensure_runtime_dirs(config)
    logger = _bootstrap_logging(config)

    profile = Path(
        config.get("webshop", "user_data_dir", fallback="static/browser_profile")
    ).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Default").mkdir(parents=True, exist_ok=True)

    # Make the window obviously the bot profile (not Chrome "Profile 1").
    _label_bot_chrome_profile(profile, display_name="Hiab Bot")

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        for path in (profile / name, profile / "Default" / name):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    base_url = config.get("webshop", "base_url")
    chrome_candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    ]
    chrome_exe = next((p for p in chrome_candidates if p.is_file()), None)
    if chrome_exe is None:
        logger.error("chrome.exe not found. Install Google Chrome and retry.")
        return 1

    # Marker page so you can see this is NOT your personal Profile 1.
    marker = profile / "hiab_bot_login.html"
    marker.write_text(
        "<!doctype html><meta charset=utf-8>"
        "<title>Hiab Bot profile</title>"
        "<body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Hiab Bot Chrome profile</h1>"
        "<p>This is <b>not</b> your personal Profile 1.</p>"
        f"<p>Profile path: <code>{profile}</code></p>"
        f"<p><a href='{base_url}'>Open Hiab webshop</a></p>"
        "</body>",
        encoding="utf-8",
    )
    marker_url = marker.resolve().as_uri()

    logger.info("======= Webshop login bootstrap v%s (real Chrome) =======", VERSION)
    logger.info("Bot profile ONLY: %s", profile)
    logger.info(
        "Opening a separate Chrome instance labeled 'Hiab Bot'. "
        "If you still see Profile 1, close personal Chrome and retry."
    )
    logger.info("Start page: %s", marker_url)

    # Separate user-data-dir forces a second Chrome process (not Profile 1).
    # Keep args before the URL; quote-safe absolute path.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    subprocess.Popen(
        [
            str(chrome_exe),
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            marker_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    # Launcher chrome.exe often exits immediately after spawning the real
    # browser process — wait until no Chrome still uses our user-data-dir.
    time.sleep(2.0)
    if not _chrome_using_user_data_dir(profile):
        logger.error(
            "Chrome did not start with bot profile %s. "
            "Close all Chrome windows and run: python main.py --login",
            profile,
        )
        return 1

    deadline = time.time() + max(1, timeout_min) * 60
    logger.info(
        "Waiting until you CLOSE the Hiab Bot Chrome window (up to %s min)...",
        timeout_min,
    )
    while time.time() < deadline:
        if not _chrome_using_user_data_dir(profile):
            logger.info(
                "Hiab Bot Chrome closed. Session is in %s. "
                "Run the robot normally next (without --login).",
                profile,
            )
            return 0
        time.sleep(2.0)

    logger.warning(
        "Timeout waiting for Hiab Bot Chrome to close. "
        "Close it manually when login is done."
    )
    return 1


def _label_bot_chrome_profile(profile: Path, display_name: str = "Hiab Bot") -> None:
    """Set Chrome UI profile name so the avatar menu shows Hiab Bot."""
    import json

    local_state_path = profile / "Local State"
    data: dict = {}
    if local_state_path.is_file():
        try:
            data = json.loads(local_state_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    profile_block = data.setdefault("profile", {})
    info = profile_block.setdefault("info_cache", {})
    default = info.setdefault("Default", {})
    default["name"] = display_name
    default["shortcut_name"] = display_name
    profile_block["last_used"] = "Default"
    local_state_path.write_text(json.dumps(data), encoding="utf-8")

    prefs_path = profile / "Default" / "Preferences"
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            prefs.setdefault("profile", {})["name"] = display_name
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
        except Exception:
            pass


def _chrome_using_user_data_dir(user_data_dir: Path) -> bool:
    """True if any chrome.exe process was started with this --user-data-dir."""
    import subprocess

    needle = str(user_data_dir).lower().replace("/", "\\")
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" "
                "| Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        # Fallback: SingletonLock means a Chrome instance holds the profile.
        return (user_data_dir / "SingletonLock").exists()

    for line in (result.stdout or "").splitlines():
        normalized = line.lower().replace("/", "\\")
        if needle in normalized and "--user-data-dir" in normalized:
            return True
    return (user_data_dir / "SingletonLock").exists()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Hiab Webshop Order Robot")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Process at most N pending rows this run.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify required secret/config files exist.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open real Chrome (not Playwright) for one-time manual login into the bot profile.",
    )
    parser.add_argument(
        "--login-timeout-min",
        type=int,
        default=10,
        help="Minutes to wait for impersonator during --login (default: 10).",
    )
    args = parser.parse_args(argv)

    if args.check:
        return 0 if check_required_files() else 1

    if not check_required_files():
        return 1

    try:
        if args.login:
            return bootstrap_login(timeout_min=args.login_timeout_min)
        process_emails(max_orders=args.max)
        return 0
    except KeyboardInterrupt:
        get_logger().warning("Interrupted by user.")
        return 130
    except Exception as exc:
        logger = get_logger()
        logger.error("Fatal error: %s", exc)
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
