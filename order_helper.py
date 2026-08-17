"""
Business logic for processing pending webshop orders.
Handles spreadsheet reads, phase updates, and batch uploads.
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Optional

from auth import init_connections
from config_loader import batch_max_rows, ensure_runtime_dirs, load_config
from csv_utils import prepare_batch_payload
from logging_setup import get_logger

from spreadsheet_processing import (
    OrderRow,
    extract_order_payload,
    find_pending_orders,
    set_manual_phase,
    set_robot_phase,
    set_timestamp_processed_at,
)

# Import the bot (adjust path if you haven't moved it to webshop/bot.py yet)
from webshop.profil_handler import ProfileHandler
from webshop.webshop_orchestration import webshop_orchestration


def _delete_order_batch_csvs(batch_files: list[Path], row_number: int) -> None:
    """Remove download batch CSVs that belong to a finished order row."""
    logger = get_logger()
    for path in batch_files:
        try:
            if path.is_file():
                path.unlink()
                logger.info("Deleted batch CSV for finished row %s: %s", row_number, path.name)
        except OSError as exc:
            logger.warning("Could not delete batch CSV %s for row %s: %s", path, row_number, exc)


def process_single_order(
    sheet,
    sheets_client,
    order: OrderRow,
    config,
    profile: Optional[ProfileHandler] = None,
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
        order.client_number, order.client_name, order.email_id, order.row_number,
    )

    def on_phase(phase: str, detail: str) -> None:
        order.row_number = set_robot_phase(
            sheet, phase, detail, config, email_id=order.email_id, row_number=order.row_number,
        )

    owns_profile = profile is None
    batch_files: list[Path] = []
    
    try:
        if not order.email_id:
            raise ValueError("email_id is empty on processing row; cannot safely edit phases.")

        _, order.row_number = set_timestamp_processed_at(
            sheet, config, email_id=order.email_id, row_number=order.row_number,
        )
        order.row_number = set_robot_phase(
            sheet, "PROCESSING", "Extracting row data", config, email_id=order.email_id, row_number=order.row_number,
        )

        if not order.client_number:
            raise ValueError("client_number is empty on processing row.")
        if not order.attachments_path:
            raise ValueError("ATTACHMENTS_PATH is empty on processing row.")

        _, source_csv = extract_order_payload(sheet, order, sheets_client, config)

        max_rows = batch_max_rows(config)
        order.row_number = set_robot_phase(
            sheet, "PROCESSING", f"Preparing A/B CSV batches (max {max_rows} rows)", config, 
            email_id=order.email_id, row_number=order.row_number,
        )
        
        payload = prepare_batch_payload(
            source_csv,
            config.get("paths", "batch_csv_dir"),
            stem=f"batch_{order.client_number}_{order.email_id or order.row_number}",
            batch_size=max_rows,
        )
        batch_files = list(payload.batch_files)
        logger.info("Order has %s item row(s) -> %s batch file(s).", payload.total_rows, payload.batch_count)

        if profile is None:
            profile = ProfileHandler(config)
            profile.start()
        
        webshop_orchestration(
            page=profile.page,
            context=profile.context,
            client_number=order.client_number,
            client_name=order.client_name,
            client_mail=order.client_mail,
            email_title=order.email_title,
            batch_csvs=payload.batch_files,
            config=config,
            on_phase=on_phase,
            require_existing_session=require_existing_session,
        )

        # Mark as finished
        order.row_number = set_robot_phase(
            sheet, "FINISHED", "FINISHED", config, email_id=order.email_id, row_number=order.row_number,
        )
        finished_manual = config.get("phases", "finished_manual", fallback="FINISHED").strip()
        order.row_number = set_manual_phase(
            sheet, finished_manual, config, email_id=order.email_id, row_number=order.row_number,
        )
        
        _delete_order_batch_csvs(batch_files, order.row_number)
        logger.info("Process completed successfully for email_id=%s row %s.", order.email_id, order.row_number)
        return True

    except Exception as exc:
        reason = str(exc).strip() or type(exc).__name__
        logger.error("Error processing email_id=%s row %s: %s", order.email_id, order.row_number, reason)
        logger.error(traceback.format_exc())
        
        try:
            order.row_number = set_robot_phase(
                sheet, "ERROR", reason, config, email_id=order.email_id, row_number=order.row_number,
            )
            error_manual = config.get("phases", "error_manual", fallback="ERROR").strip()
            order.row_number = set_manual_phase(
                sheet, error_manual, config, email_id=order.email_id, row_number=order.row_number,
            )
        except Exception as sheet_exc:
            logger.error("Failed to write ERROR phase: %s", sheet_exc)
        return False

    finally:
        if owns_profile and profile is not None:
            profile.stop()

def process_emails(
    max_orders: Optional[int] = None,
    *,
    force_headless: Optional[bool] = None,
    quiet_when_idle: bool = False,
    profile: Optional[ProfileHandler] = None,
    require_existing_session: bool = False,
    logger=None, # Allow injecting logger from main
) -> int:
    """
    Main orchestration (Sheets arrival, then order processing).
    Returns number of successfully finished orders.
    """
    config = load_config()
    ensure_runtime_dirs(config)
    logger = logger or get_logger()

    if force_headless is not None:
        config.set("webshop", "headless", "true" if force_headless else "false")

    if not quiet_when_idle:
        logger.info("Connecting to Google Sheets...")

    sheets_client, main_sheet = init_connections(config)
    
    if not quiet_when_idle:
        logger.info("Sheets connection ready.")

    pending = find_pending_orders(main_sheet, config)
    if not pending:
        msg = "No rows ready (MANUAL_PHASE=PROCESSING & ROBOT_PHASE empty & ACTIVE_PHASE=5_VALID)."
        if quiet_when_idle:
            logger.debug(msg)
        else:
            logger.info(msg)
        return 0

    if max_orders is not None:
        pending = pending[: max(0, max_orders)]

    success = 0
    owns_profile = profile is None
    try:
        if profile is None:
            profile = ProfileHandler(config)
            profile.start()

        for order in pending:
            ok = process_single_order(
                main_sheet,
                sheets_client,
                order,
                config,
                profile=profile,
                require_existing_session=require_existing_session,
            )
            if ok:
                success += 1
    finally:
        if owns_profile and profile is not None:
            profile.stop()

    logger.info("Finished run: %s/%s order(s) succeeded.", success, len(pending))
    return success