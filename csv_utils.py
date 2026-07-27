"""Prepare batch-order CSVs: columns A/B only, split into <=100-row files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Union

import pandas as pd

from logging_setup import get_logger

logger = get_logger()

DEFAULT_BATCH_MAX_ROWS = 100


@dataclass
class BatchPayload:
    """Item/qty dataframe plus on-disk CSV chunks ready for Batch Order upload."""

    items: pd.DataFrame
    batch_files: List[Path]
    total_rows: int
    batch_size: int

    @property
    def batch_count(self) -> int:
        return len(self.batch_files)


def _read_csv_flexible(source: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(source, header=None, dtype=str, keep_default_na=False)
    except Exception:
        return pd.read_csv(
            source,
            header=None,
            dtype=str,
            keep_default_na=False,
            sep=None,
            engine="python",
        )


# Headers required by webshop Batch Order upload (row 1).
ITEM_HEADER = "Item number"
QTY_HEADER = "Order amount"
_HEADER_LIKE = {
    "item",
    "item number",
    "itemnumber",
    "part",
    "part number",
    "order amount",
    "qty",
    "quantity",
}


def load_items_dataframe(source_csv: Union[str, Path]) -> pd.DataFrame:
    """
    Load attachment CSV into a dataframe with columns:
      item (col A), qty (col B)

    Row 1 may be headers; header-like / empty A+B rows are skipped.
    """
    source = Path(source_csv)
    if not source.exists():
        raise FileNotFoundError(f"Source CSV not found: {source}")

    df = _read_csv_flexible(source)
    if df.shape[1] < 2:
        raise ValueError(
            f"CSV {source.name} has {df.shape[1]} column(s); need at least A and B."
        )

    items = df.iloc[:, :2].copy()
    items.columns = ["item", "qty"]
    items["item"] = items["item"].astype(str).str.strip()
    items["qty"] = items["qty"].astype(str).str.strip()
    items = items[~((items["item"] == "") & (items["qty"] == ""))]
    items = items[~items["item"].str.lower().isin(_HEADER_LIKE)].reset_index(drop=True)

    logger.info(
        "Loaded shopping list from %s: %s item row(s).",
        source.name,
        len(items),
    )
    return items


def split_into_batches(
    items: pd.DataFrame,
    output_dir: Union[str, Path],
    stem: str,
    batch_size: int = DEFAULT_BATCH_MAX_ROWS,
) -> List[Path]:
    """
    Write item/qty CSVs with headers in row 1, data from row 2, in chunks of
    `batch_size` (webshop Batch Order accepts max 100 data rows per upload).
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if items.empty:
        raise ValueError("No item rows to batch.")

    paths: List[Path] = []
    total = len(items)
    num_batches = (total + batch_size - 1) // batch_size

    for i in range(num_batches):
        chunk = items.iloc[i * batch_size : (i + 1) * batch_size].copy()
        chunk.columns = [ITEM_HEADER, QTY_HEADER]
        name = f"{stem}_batch_{i + 1:02d}_of_{num_batches:02d}.csv"
        target = out_dir / name
        chunk.to_csv(target, index=False, header=True)
        paths.append(target)
        logger.info(
            "Wrote batch file %s (%s rows, batch %s/%s).",
            target.name,
            len(chunk),
            i + 1,
            num_batches,
        )

    return paths


def prepare_batch_payload(
    source_csv: Union[str, Path],
    output_dir: Union[str, Path],
    stem: str,
    batch_size: int = DEFAULT_BATCH_MAX_ROWS,
) -> BatchPayload:
    """
    Extract columns A/B, build item/qty dataframe, split into <=batch_size CSVs.
    """
    items = load_items_dataframe(source_csv)
    batch_files = split_into_batches(items, output_dir, stem=stem, batch_size=batch_size)
    logger.info(
        "Prepared %s batch file(s) from %s rows (max %s per file).",
        len(batch_files),
        len(items),
        batch_size,
    )
    return BatchPayload(
        items=items,
        batch_files=batch_files,
        total_rows=len(items),
        batch_size=batch_size,
    )


# Backwards-compatible single-file helper (no split).
def extract_columns_ab(
    source_csv: Union[str, Path],
    output_dir: Union[str, Path],
    output_name: str | None = None,
) -> Path:
    source = Path(source_csv)
    items = load_items_dataframe(source)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = output_name or f"{source.stem}_ab.csv"
    target = out_dir / name
    out = items.rename(columns={"item": ITEM_HEADER, "qty": QTY_HEADER})
    out.to_csv(target, index=False, header=True)
    logger.info("Prepared batch CSV with columns A/B: %s (%s rows).", target, len(items))
    return target
