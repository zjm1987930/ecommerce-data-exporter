from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.negative_experience import NegativeExperienceAutomator
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month, split_range
from exporter.workbooks import write_negative_experience_workbook


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="单店铺抓取拼多多消费者负向体验扣款数据")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--manual-date", action="store_true", help="手动选择创建时间；默认自动点选日期")
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选：{available}")

    date_range = previous_month()
    if args.start and args.end:
        date_range = DateRange(parse_date(args.start), parse_date(args.end))

    automator = NegativeExperienceAutomator(ROOT, config.platform, config.platform_name)
    records = automator.run_shop(shop, split_range(date_range, 7), manual_date=args.manual_date)
    output = automator._output_path(date_range)
    write_negative_experience_workbook({shop.sheet_name: records}, output)
    print(f"{shop.name} 抓取完成：{len(records)} 条")
    print(f"已生成：{output}")


if __name__ == "__main__":
    main()
