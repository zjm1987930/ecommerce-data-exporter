from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.returns import ReturnsAutomator
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="单店铺退货数据自动化验证")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--run", action="store_true", help="实际点击生成并下载；默认只查询不导出")
    parser.add_argument(
        "--manual-workbench",
        action="store_true",
        help="登录后手动进入售后工作台，再由脚本接管筛选和查询",
    )
    parser.add_argument(
        "--manual-date",
        action="store_true",
        help="手动选择申请时间；默认由脚本自动点选日期",
    )
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选: {available}")

    date_range = previous_month()
    if args.start and args.end:
        date_range = DateRange(parse_date(args.start), parse_date(args.end))

    automator = ReturnsAutomator(ROOT, config.platform, config.platform_name)
    result = automator.run(
        shop,
        date_range,
        dry_run=not args.run,
        manual_workbench=args.manual_workbench,
        manual_date=args.manual_date,
    )
    print(result.status)
    print(result.message)
    if result.downloaded_file:
        print(result.downloaded_file)


if __name__ == "__main__":
    main()
