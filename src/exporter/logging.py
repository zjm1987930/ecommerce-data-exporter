from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import csv
import json
from pathlib import Path


@dataclass
class ExportLogRow:
    month: str
    platform: str
    shop: str
    data_type: str
    start_date: str
    end_date: str
    status: str
    file_path: str = ""
    reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    screenshot_path: str = ""


class ExportLogger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.rows: list[ExportLogRow] = []
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def add(self, row: ExportLogRow) -> None:
        self.rows.append(row)

    def write(self) -> tuple[Path, Path]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.log_dir / f"导出日志_{timestamp}.csv"
        json_path = self.log_dir / f"export-log_{timestamp}.json"

        fields = list(ExportLogRow.__dataclass_fields__.keys())
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in self.rows:
                writer.writerow(asdict(row))

        json_path.write_text(
            json.dumps([asdict(row) for row in self.rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return csv_path, json_path
