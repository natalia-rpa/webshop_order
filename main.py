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
    args = parser.parse_args(argv)

    if args.check:
        return 0 if check_required_files() else 1

    if not check_required_files():
        return 1

    try:
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
