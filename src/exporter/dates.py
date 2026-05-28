from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def month_key(self) -> str:
        return self.start.strftime("%Y-%m")

    @property
    def range_key(self) -> str:
        return f"{self.start:%Y%m%d}-{self.end:%Y%m%d}"

    @property
    def display(self) -> str:
        return f"{self.start.isoformat()} 至 {self.end.isoformat()}"


def previous_month(today: date | None = None) -> DateRange:
    today = today or date.today()
    first_this_month = today.replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    first_prev_month = last_prev_month.replace(day=1)
    return DateRange(first_prev_month, last_prev_month)


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def split_range(date_range: DateRange, max_days: int) -> list[DateRange]:
    ranges: list[DateRange] = []
    current = date_range.start
    while current <= date_range.end:
        chunk_end = min(current + timedelta(days=max_days - 1), date_range.end)
        ranges.append(DateRange(current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return ranges
