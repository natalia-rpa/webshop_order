"""Spreadsheet row discovery, phase updates, attachment download."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import gspread
import requests
from gspread.exceptions import APIError

from config_loader import load_config
from logging_setup import get_logger

logger = get_logger()


@dataclass
class OrderRow:
    row_number: int  # 1-based sheet row
    client_number: str
    client_name: str
    email_id: str
    attachments_path: str
    manual_phase: str
    robot_phase: str


def _is_empty(value: Optional[str]) -> bool:
    return value is None or str(value).strip() == ""


def _norm_header(name: str) -> str:
    return re.sub(r"[\s_]+", "", str(name).strip().lower())


def build_header_map(headers: List[str]) -> Dict[str, int]:
    """Map normalized header -> 0-based column index."""
    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        key = _norm_header(header)
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def resolve_column(header_map: Dict[str, int], configured_name: str) -> int:
    key = _norm_header(configured_name)
    if key not in header_map:
        raise KeyError(
            f"Column {configured_name!r} not found in sheet headers. "
            f"Available: {sorted(header_map)}"
        )
    return header_map[key]


def safe_update_cell(
    sheet: gspread.Worksheet,
    row: int,
    col: int,
    value: str,
    retries: int = 6,
    delay: float = 3.0,
) -> None:
    for attempt in range(1, retries + 1):
        try:
            sheet.update_cell(row, col, value)
            return
        except APIError as exc:
            msg = str(exc).lower()
            if attempt < retries and ("429" in msg or "quota" in msg or "rate" in msg):
                wait = delay * (2 ** (attempt - 1))
                logger.warning(
                    "Sheets quota hit updating r%s c%s; retry in %.0fs (%s/%s).",
                    row,
                    col,
                    wait,
                    attempt,
                    retries,
                )
                time.sleep(wait)
                continue
            raise


def set_robot_phase(
    sheet: gspread.Worksheet,
    row_number: int,
    phase: str,
    detail: str = "",
    config=None,
) -> None:
    """
    Write ROBOT_PHASE as 'PHASE' or 'PHASE - detail'.
    Examples: PROCESSING - Logging in | ERROR - Missing CSV | FINISHED
    """
    config = config or load_config()
    headers = sheet.row_values(1)
    header_map = build_header_map(headers)
    col_name = config.get("columns", "robot_phase")
    col_idx = resolve_column(header_map, col_name) + 1  # 1-based for update_cell

    text = phase.strip()
    if detail and phase.upper() != "FINISHED":
        text = f"{phase} - {detail}"
    elif detail and phase.upper() == "FINISHED":
        text = detail if detail.upper() == "FINISHED" else f"FINISHED - {detail}"
    elif phase.upper() == "FINISHED":
        text = "FINISHED"

    safe_update_cell(sheet, row_number, col_idx, text)
    logger.info("ROBOT_PHASE row %s -> %s", row_number, text)


def find_pending_orders(
    sheet: gspread.Worksheet,
    config=None,
) -> List[OrderRow]:
    """
    Start condition: MANUAL_PHASE == VALID and ROBOT_PHASE empty.
    """
    config = config or load_config()
    start_manual = config.get("phases", "start_manual").strip().upper()
    cols = config["columns"]

    values = sheet.get_all_values()
    if not values:
        logger.warning("MAIN sheet is empty.")
        return []

    headers = values[0]
    header_map = build_header_map(headers)

    idx_client_number = resolve_column(header_map, cols.get("client_number"))
    idx_client_name = resolve_column(header_map, cols.get("client_name"))
    idx_email_id = resolve_column(header_map, cols.get("email_id"))
    idx_attachments = resolve_column(header_map, cols.get("attachments_path"))
    idx_manual = resolve_column(header_map, cols.get("manual_phase"))
    idx_robot = resolve_column(header_map, cols.get("robot_phase"))

    pending: List[OrderRow] = []
    for row_offset, row in enumerate(values[1:], start=2):
        def cell(i: int) -> str:
            return row[i].strip() if i < len(row) else ""

        manual = cell(idx_manual)
        robot = cell(idx_robot)
        if manual.upper() != start_manual or not _is_empty(robot):
            continue

        order = OrderRow(
            row_number=row_offset,
            client_number=cell(idx_client_number),
            client_name=cell(idx_client_name),
            email_id=cell(idx_email_id),
            attachments_path=cell(idx_attachments),
            manual_phase=manual,
            robot_phase=robot,
        )
        pending.append(order)

    logger.info(
        "Found %s pending order row(s) (MANUAL_PHASE=%s, ROBOT_PHASE empty).",
        len(pending),
        start_manual,
    )
    return pending


def _extract_drive_file_id(path_or_url: str) -> Optional[str]:
    text = path_or_url.strip()
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]{25,})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    parsed = urlparse(text)
    if "drive.google.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        if "id" in qs:
            return qs["id"][0]
    return None


def _sheet_name_candidates(source: str) -> List[str]:
    """Build possible worksheet titles from values like 'Sheet1.csv'."""
    name = Path(source).name.strip()
    candidates = [name]
    if name.lower().endswith(".csv"):
        candidates.append(name[:-4])
    # Deduplicate while preserving order
    seen = set()
    unique: List[str] = []
    for item in candidates:
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def export_worksheet_to_csv(
    worksheet: gspread.Worksheet,
    destination_dir: str | Path,
    output_name: Optional[str] = None,
) -> Path:
    """Download all values from a worksheet and save as CSV."""
    dest_dir = Path(destination_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    values = worksheet.get_all_values()
    name = output_name or f"{worksheet.title}.csv"
    if not name.lower().endswith(".csv"):
        name = f"{name}.csv"
    target = dest_dir / name

    import csv

    with open(target, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(values)

    logger.info(
        "Exported worksheet %r -> %s (%s rows).",
        worksheet.title,
        target,
        len(values),
    )
    return target


def _export_named_sheet_as_csv(
    spreadsheet: gspread.Spreadsheet,
    sheet_ref: str,
    destination_dir: str | Path,
) -> Path:
    """Find worksheet by name (Sheet1 / Sheet1.csv) and export to CSV."""
    titles = {ws.title: ws for ws in spreadsheet.worksheets()}
    title_by_lower = {t.casefold(): ws for t, ws in titles.items()}

    for candidate in _sheet_name_candidates(sheet_ref):
        ws = titles.get(candidate) or title_by_lower.get(candidate.casefold())
        if ws is not None:
            out_name = sheet_ref if sheet_ref.lower().endswith(".csv") else f"{ws.title}.csv"
            return export_worksheet_to_csv(ws, destination_dir, output_name=out_name)

    available = ", ".join(sorted(titles)) or "(none)"
    raise FileNotFoundError(
        f"No worksheet matching ATTACHMENTS_PATH={sheet_ref!r}. "
        f"Available sheets: {available}"
    )


def _client_credentials(sheets_client: gspread.Client):
    """gspread 6 stores credentials on http_client.auth (not client.auth)."""
    http_client = getattr(sheets_client, "http_client", None)
    creds = getattr(http_client, "auth", None) if http_client is not None else None
    if creds is None:
        creds = getattr(sheets_client, "auth", None)
    if creds is None:
        raise RuntimeError("Could not read credentials from gspread Client.")
    return creds


def _find_drive_file_by_name(
    sheets_client: gspread.Client,
    file_name: str,
    dest_dir: Path,
) -> Optional[Path]:
    """Search Drive for a file with this exact name and download it."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return None

    creds = _client_credentials(sheets_client)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    safe_name = file_name.replace("'", "\\'")
    query = f"name = '{safe_name}' and trashed = false"
    result = (
        drive.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=5)
        .execute()
    )
    files = result.get("files") or []
    if not files:
        return None
    return _download_drive_file(sheets_client, files[0]["id"], dest_dir)


def download_attachment(
    attachments_path: str,
    destination_dir: str | Path,
    sheets_client: Optional[gspread.Client] = None,
    spreadsheet: Optional[gspread.Spreadsheet] = None,
) -> Path:
    """
    Resolve ATTACHMENTS_PATH:
    - local filesystem path
    - http(s) URL
    - Google Drive file link / id
    - worksheet name in the same spreadsheet (e.g. Sheet1.csv / Sheet1)
    - Drive file name search (e.g. Sheet1.csv)
    """
    dest_dir = Path(destination_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    source = attachments_path.strip().strip('"')

    if not source:
        raise ValueError("ATTACHMENTS_PATH is empty.")

    local = Path(source)
    if local.exists() and local.is_file():
        target = dest_dir / local.name
        if local.resolve() != target.resolve():
            target.write_bytes(local.read_bytes())
        logger.info("Copied local attachment: %s", target)
        return target

    drive_id = _extract_drive_file_id(source)
    if drive_id and sheets_client is not None:
        return _download_drive_file(sheets_client, drive_id, dest_dir)

    if source.lower().startswith(("http://", "https://")):
        response = requests.get(source, timeout=120)
        response.raise_for_status()
        name = Path(urlparse(source).path).name or f"attachment_{int(time.time())}.csv"
        if not name.lower().endswith(".csv"):
            name = f"{name}.csv"
        target = dest_dir / name
        target.write_bytes(response.content)
        logger.info("Downloaded attachment from URL: %s", target)
        return target

    # Bare name like Sheet1.csv → export matching worksheet from this spreadsheet
    if spreadsheet is not None:
        try:
            return _export_named_sheet_as_csv(spreadsheet, source, dest_dir)
        except FileNotFoundError:
            logger.info(
                "No worksheet for %r; trying Drive file name search.",
                source,
            )

    if sheets_client is not None:
        found = _find_drive_file_by_name(sheets_client, Path(source).name, dest_dir)
        if found is not None:
            return found
        # Also try without .csv suffix as filename
        for candidate in _sheet_name_candidates(source):
            if candidate == Path(source).name:
                continue
            found = _find_drive_file_by_name(
                sheets_client, f"{candidate}.csv", dest_dir
            )
            if found is not None:
                return found

    raise FileNotFoundError(
        f"Cannot resolve ATTACHMENTS_PATH: {attachments_path!r}. "
        "Expected local path, http(s) URL, Drive link/id, worksheet name "
        "(e.g. Sheet1.csv), or Drive file name."
    )


def _download_drive_file(
    sheets_client: gspread.Client,
    file_id: str,
    dest_dir: Path,
) -> Path:
    """Download a Drive file using the same credentials as gspread."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        import io
    except ImportError as exc:
        raise RuntimeError("google-api-python-client required for Drive downloads") from exc

    creds = _client_credentials(sheets_client)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    name = meta.get("name") or f"{file_id}.csv"
    target = dest_dir / name

    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    target.write_bytes(buffer.getvalue())
    logger.info("Downloaded Drive file %s -> %s", file_id, target)
    return target


def extract_order_payload(
    sheet: gspread.Worksheet,
    order: OrderRow,
    sheets_client: gspread.Client,
    config=None,
) -> Tuple[OrderRow, Path]:
    """Download / export attachment CSV for the order row and return path."""
    config = config or load_config()
    downloads = config.get("webshop", "downloads_dir")
    set_robot_phase(
        sheet, order.row_number, "PROCESSING", "Downloading attachment CSV", config
    )
    path = download_attachment(
        order.attachments_path,
        downloads,
        sheets_client=sheets_client,
        spreadsheet=sheet.spreadsheet,
    )
    return order, path
