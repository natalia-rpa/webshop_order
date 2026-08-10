"""Prepare batch-order CSV"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Union
import pandas as pd
from logging_setup import get_logger

logger = get_logger()

DEFAULT_BATCH_MAX_ROWS = 100


@dataclass
class BatchPayload:
    """Item/quantity dataframe plus on-disk CSV chunks ready for Batch Order upload."""

    items: pd.DataFrame
    batch_files: List[Path]
    total_rows: int
    batch_size: int

    @property
    def batch_count(self) -> int:
        return len(self.batch_files)


def prepare_batch_payload(source_csv, output_dir, stem, batch_size: int = DEFAULT_BATCH_MAX_ROWS) -> BatchPayload:
    """
    Extract columns A/B, build item/quantity dataframe, split into batch_size csvs (max 100 rows).
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(source_csv, usecols=[0, 1], dtype=str).dropna(how="all")
    df.columns = ["Item number", "Order amount"]
    
    batch_files = []

    # cut dataframe into chunks of batch_size
    for i in range(0, len(df), batch_size):
        chunk = df.iloc[i : i + batch_size]
        target = out_dir / f"{stem}_batch_{i//batch_size + 1}.csv"
        chunk.to_csv(target, index=False)
        batch_files.append(target)
        logger.info("Wrote batch file %s (%s rows).", target.name, len(chunk))

    return BatchPayload(items=df, batch_files=batch_files, total_rows=len(df), batch_size=batch_size)
