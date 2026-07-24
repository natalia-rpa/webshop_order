"""Google Sheets/Drive auth via service account (oauth-keys.json)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from config_loader import google_scopes, load_config
from logging_setup import get_logger

logger = get_logger()


def _service_account_keys_path(config) -> str:
    """Prefer [google] keys_path; fall back to gmail oauth_keys / legacy sheets_keys."""
    for section, key in (
        ("google", "keys_path"),
        ("google", "sheets_keys"),
        ("gmail", "oauth_keys"),
    ):
        if config.has_option(section, key):
            path = config.get(section, key).strip()
            if path:
                return path
    return "static/secrets/oauth-keys.json"


def authenticate_sheets(config=None) -> gspread.Client:
    """Authorize gspread with a service-account JSON key file."""
    config = config or load_config()
    keys_path = _service_account_keys_path(config)
    scopes = google_scopes(config)

    if not Path(keys_path).exists():
        raise FileNotFoundError(
            f"Google service-account keys not found: {keys_path}. "
            "Place the JSON at static/secrets/oauth-keys.json"
        )

    creds = Credentials.from_service_account_file(keys_path, scopes=scopes)
    client = gspread.authorize(creds)
    logger.info("Google Sheets authenticated (%s).", keys_path)
    return client


def open_main_sheet(client: gspread.Client, config=None) -> gspread.Worksheet:
    config = config or load_config()
    url = config.get("spreadsheets", "spreadsheet_url")
    sheet_name = config.get("spreadsheets", "main_sheet_name")
    spreadsheet = client.open_by_url(url)
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        gid = int(config.get("spreadsheets", "main_sheet_gid"))
        worksheet = spreadsheet.get_worksheet_by_id(gid)
        logger.warning(
            "Sheet %r not found by name; opened by gid=%s (%s).",
            sheet_name,
            gid,
            worksheet.title,
        )
    logger.info("Opened spreadsheet sheet: %s", worksheet.title)
    return worksheet


def init_connections(
    config=None,
) -> Tuple[Optional[Any], gspread.Client, gspread.Worksheet]:
    """
    Sheets arrival & initialization.
    Gmail is not used yet (returns None).
    """
    config = config or load_config()
    sheets = authenticate_sheets(config)
    main = open_main_sheet(sheets, config)
    return None, sheets, main
