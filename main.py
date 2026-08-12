"""
Webshop Order Robot — entry point.

process_emails():
  1. Google Sheets init
  2. Find MAIN rows: MANUAL_PHASE=PROCESSING & ROBOT_PHASE empty & ACTIVE_PHASE (COL G)=5_VALID
  3. Extract client data + prepare A/B CSV batches (max 100 rows each)
  4. Playwright: login, impersonate, batch upload loop, add to cart
  5. Update ROBOT_PHASE (PROCESSING / ERROR / FINISHED);
     on FINISHED set MANUAL_PHASE=FINISHED; on ERROR set MANUAL_PHASE=ERROR

run_unattended():
  Background loop — Chrome runs hidden (logs only), polls MAIN every N
  seconds (default 60), processes when MANUAL_PHASE=PROCESSING,
  ROBOT_PHASE empty, and ACTIVE_PHASE=5_VALID. Reuses the signed-in bot
  profile. If session is dead, run: python main.py --login
"""

from __future__ import annotations
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from config_loader import (
    ensure_runtime_dirs,
    load_config,
    unattended_headless,
    unattended_poll_interval_sec,
)
from email_notify import notify_session_inactive
from logging_setup import get_logger, setup_logging
from order_helper import process_emails

from webshop.webshop_client import WebshopClient

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


def run_unattended(max_orders: Optional[int] = None) -> int:
    """
    Background unattended loop with a long-lived signed-in Chrome session.

    Runs Chrome hidden (headless) — only logs are visible. Reuses the signed-in
    bot profile from --login (no password/MFA re-login). Polls MAIN for rows
    with MANUAL_PHASE=PROCESSING, ROBOT_PHASE empty, and ACTIVE_PHASE=5_VALID.
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
        "Polling every %ss for MANUAL_PHASE=PROCESSING & ROBOT_PHASE empty "
        "& ACTIVE_PHASE=5_VALID. Only logs are shown.",
        cdp_port,
        force_headless,
        poll_sec,
    )

    bot: Optional[WebshopClient] = None
    try:
        bot = WebshopClient(config=config)
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

            logger.info("Next poll in %ss (session stays active)...", poll_sec)
            time.sleep(poll_sec)
    finally:
        if bot is not None:
            # Leave Chrome signed-in for the next unattended / --login attach.
            bot.stop(keep_browser=True)
 


def check_required_files() -> bool:
    required = [
        "static/secrets/webshop_config.ini",
        "static/secrets/oauth-keys.json",
    ]
    missing = [p for p in required if not Path(p).exists()]
    if missing:
        print("Missing required files:")
        for path in missing:
            print(f"  - {path}")
        return False
    return True

# wait for 5 minutes for an action
def bootstrap_login(timeout_min: int = 5) -> int:
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

    bot = WebshopClient(config=config)
    try:
        bot.start()

        try:
            bot.ensure_signed_in_session(wait_ms=10_000)
        except Exception as auth_exc:
            logger.warning("Login failed, wait for MFA: %s", auth_exc)
            return 1
        
        bot.wait_until_impersonator_ready(timeout_ms=timeout_min*60000)
        logger.info(
            "Active session ready. You can close this Chrome window, then run:\n"
            "  python main.py --unattended\n"
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


def main() -> int:

    unattended = False
    max_orders = None
    login = False

    if '--unattended' in sys.argv:
        unattended = True

    if '--max' in sys.argv:
        idx = sys.argv.index('--max')
        max_orders = int(sys.argv[idx + 1])

    if '--login' in sys.argv:
        login = True

    if not check_required_files():
     return 1    

    print(f"Unattended: {unattended}, Max: {max_orders}")



    try:
        if login:
            return bootstrap_login()
        if unattended:
            return run_unattended(max_orders=max_orders)

        process_emails(max_orders=max_orders)
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
