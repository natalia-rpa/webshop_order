"""
Webshop Order Robot — entry point.

process_emails():
  1. Gmail OAuth + Google Sheets init
  2. Find MAIN rows: MANUAL_PHASE=VALID & ROBOT_PHASE empty
  3. Extract client data + prepare A/B CSV batches (max 100 rows each)
  4. Playwright: login, impersonate, batch upload loop, add to cart
  5. Update ROBOT_PHASE (PROCESSING / ERROR / FINISHED)

run_unattended():
  Background loop — Chrome runs hidden (logs only), polls MAIN every N
  seconds (default 60), processes when MANUAL_PHASE=VALID and ROBOT_PHASE
  empty. Reuses the signed-in bot profile. If session is dead, run:
  python main.py --login
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from auth import init_connections
from config_loader import (
    batch_max_rows,
    ensure_runtime_dirs,
    load_config,
    unattended_headless,
    unattended_poll_interval_sec,
)
from csv_utils import prepare_batch_payload
from email_notify import notify_session_inactive
from logging_setup import get_logger, setup_logging
from spreadsheet_processing import (
    OrderRow,
    extract_order_payload,
    find_pending_orders,
    set_robot_phase,
    set_timestamp_processed_at,
)
from webshop_bot import WebshopBot

VERSION = "1.0.0"


def _is_session_inactive_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "session is not active" in text
        or "python main.py --login" in text
        or "impersonator not visible" in text
        or "impersonator (find/add user) control not available" in text
    )


def _alert_session_inactive(config, exc: BaseException) -> None:
    try:
        notify_session_inactive(config, detail=str(exc))
    except Exception as mail_exc:
        get_logger().error("Session-inactive alert email failed: %s", mail_exc)


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
    *,
    require_existing_session: bool = False,
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
        set_timestamp_processed_at(sheet, order.row_number, config)
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
            require_existing_session=require_existing_session,
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


def process_emails(
    max_orders: Optional[int] = None,
    *,
    force_headless: Optional[bool] = None,
    quiet_when_idle: bool = False,
    bot: Optional[WebshopBot] = None,
    require_existing_session: bool = False,
) -> int:
    """
    Main orchestration (Gmail + Sheets arrival, then order processing).

    Returns number of successfully finished orders.
    """
    config = load_config()
    ensure_runtime_dirs(config)
    logger = _bootstrap_logging(config)

    if force_headless is not None:
        config.set("webshop", "headless", "true" if force_headless else "false")

    if not quiet_when_idle:
        logger.info("======= Webshop Order Robot v%s =======", VERSION)
    logger.info("Connecting to Google Sheets...")

    gmail_service, sheets_client, main_sheet = init_connections(config)
    _ = gmail_service
    logger.info("Sheets connection ready.")

    pending = find_pending_orders(main_sheet, config)
    if not pending:
        if quiet_when_idle:
            logger.debug("No rows ready (MANUAL_PHASE=VALID & ROBOT_PHASE empty).")
        else:
            logger.info("No rows ready (MANUAL_PHASE=VALID & ROBOT_PHASE empty).")
        return 0

    if max_orders is not None:
        pending = pending[: max(0, max_orders)]

    success = 0
    owns_bot = bot is None
    try:
        if bot is None:
            bot = WebshopBot(config=config)
            bot.start()

        for order in pending:
            ok = process_single_order(
                main_sheet,
                sheets_client,
                order,
                config,
                bot=bot,
                require_existing_session=require_existing_session,
            )
            if ok:
                success += 1
    finally:
        if owns_bot and bot is not None:
            bot.stop()

    logger.info("Finished run: %s/%s order(s) succeeded.", success, len(pending))
    return success


def run_unattended(max_orders: Optional[int] = None) -> int:
    """
    Background unattended loop with a long-lived signed-in Chrome session.

    Runs Chrome hidden (headless) — only logs are visible. Reuses the signed-in
    bot profile from --login (no password/MFA re-login). Polls MAIN for rows
    with MANUAL_PHASE=VALID and ROBOT_PHASE empty.
    If the session is not active, stop and run: python main.py --login
    """
    config = load_config()
    ensure_runtime_dirs(config)
    logger = _bootstrap_logging(config)

    poll_sec = unattended_poll_interval_sec(config)
    force_headless = unattended_headless(config)
    config.set("webshop", "headless", "true" if force_headless else "false")

    cdp_port = config.getint("webshop", "cdp_port", fallback=9222)
    logger.info("======= Webshop Order Robot v%s — UNATTENDED =======", VERSION)
    logger.info(
        "Hidden Chrome session (CDP %s, headless=%s). "
        "Polling every %ss for MANUAL_PHASE=VALID & ROBOT_PHASE empty. "
        "Only logs are shown.",
        cdp_port,
        force_headless,
        poll_sec,
    )

    bot: Optional[WebshopBot] = None
    try:
        bot = WebshopBot(config=config)
        bot.start()
        try:
            bot.ensure_signed_in_session()
        except RuntimeError as exc:
            logger.error("%s", exc)
            if _is_session_inactive_error(exc):
                _alert_session_inactive(config, exc)
            bot.stop(keep_browser=True)
            return 1

        logger.info(
            "Active session locked in (Chrome hidden). "
            "Orders will run without signing in again."
        )

        cycle = 0
        while True:
            cycle += 1
            try:
                logger.info("--- Unattended poll cycle %s ---", cycle)
                process_emails(
                    max_orders=max_orders,
                    force_headless=force_headless,
                    quiet_when_idle=True,
                    bot=bot,
                    require_existing_session=True,
                )
            except KeyboardInterrupt:
                raise
            except RuntimeError as exc:
                logger.error("Unattended cycle %s: %s", cycle, exc)
                if _is_session_inactive_error(exc):
                    logger.error(
                        "Stopping unattended until session is restored. "
                        "Run: python main.py --login"
                    )
                    _alert_session_inactive(config, exc)
                    return 1
                logger.error(traceback.format_exc())
            except Exception as exc:
                logger.error("Unattended cycle %s failed: %s", cycle, exc)
                logger.error(traceback.format_exc())
                if _is_session_inactive_error(exc):
                    _alert_session_inactive(config, exc)
                    return 1

            bot.keep_session_warm()
            logger.info("Next poll in %ss (session stays active)...", poll_sec)
            time.sleep(poll_sec)
    finally:
        if bot is not None:
            # Leave Chrome signed-in for the next unattended / --login attach.
            bot.stop(keep_browser=True)
    return 0


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
    Open / attach the bot Chrome profile and wait until the webshop session
    is active (impersonator visible). Leaves Chrome running on the fixed CDP
    port so unattended mode can reuse the signed-in session.
    """
    config = load_config()
    ensure_runtime_dirs(config)
    logger = _bootstrap_logging(config)

    profile = Path(
        config.get("webshop", "user_data_dir", fallback="static/browser_profile")
    ).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    _label_bot_chrome_profile(profile, display_name="Hiab Bot")

    # Visible browser required for MFA / passkey.
    config.set("webshop", "headless", "false")
    cdp_port = config.getint("webshop", "cdp_port", fallback=9222)

    logger.info("======= Webshop login bootstrap v%s =======", VERSION)
    logger.info("Bot profile: %s", profile)
    logger.info("CDP port: %s (leave this Chrome open after login)", cdp_port)
    logger.info(
        "Sign in manually if prompted (passkey / MFA). "
        "Waiting until the active webshop session is ready..."
    )

    bot = WebshopBot(config=config)
    try:
        bot.start()
        bot.wait_until_impersonator_ready(timeout_ms=max(1, timeout_min) * 60_000)
        logger.info(
            "Active session ready. You can close this Chrome window, then run:\n"
            "  python main.py --unattended\n"
            "or:\n"
            "  python unattended_main.py\n"
            "(Unattended starts Chrome hidden — only logs are visible.)"
        )
        # Disconnect Playwright only — Chrome stays signed in.
        bot.stop(keep_browser=True)
        return 0
    except Exception as exc:
        logger.error("Login bootstrap failed: %s", exc)
        logger.error(traceback.format_exc())
        try:
            bot.stop(keep_browser=True)
        except Exception:
            pass
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


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Hiab Webshop Order Robot")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Process at most N pending rows this run (or per unattended cycle).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify required secret/config files exist.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Open/attach bot Chrome and wait until the webshop session is active. "
            "Then run --unattended (Chrome will be hidden; logs only)."
        ),
    )
    parser.add_argument(
        "--login-timeout-min",
        type=int,
        default=10,
        help="Minutes to wait for impersonator during --login (default: 10).",
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "Hidden background mode: run Chrome headless (logs only), keep the "
            "signed-in session alive, poll MAIN every poll_interval_sec "
            "(default 60s), process VALID rows without re-login."
        ),
    )
    args = parser.parse_args(argv)

    if args.check:
        return 0 if check_required_files() else 1

    if not check_required_files():
        return 1

    try:
        if args.login:
            return bootstrap_login(timeout_min=args.login_timeout_min)
        if args.unattended:
            return run_unattended(max_orders=args.max)
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
