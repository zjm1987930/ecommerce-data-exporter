from __future__ import annotations

import argparse
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.customer_service_performance import CustomerServicePerformanceAutomator
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="单店铺拼多多客服绩效数据接口下载验证")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--dry-run", action="store_true", help="只请求下载地址，不保存文件")
    parser.add_argument("--debug", action="store_true", help="失败时打印完整 traceback")
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选：{available}")

    date_range = previous_month()
    if bool(args.start) != bool(args.end):
        raise SystemExit("--start 和 --end 必须同时提供")
    if args.start and args.end:
        date_range = DateRange(parse_date(args.start), parse_date(args.end))

    automator = CustomerServicePerformanceAutomator(ROOT, config.platform, config.platform_name)
    try:
        result = automator.run(shop, date_range, dry_run=args.dry_run)
    except Exception as exc:
        print(f"客服绩效单店铺验证失败：{exc}")
        if args.debug:
            traceback.print_exc()
        raise SystemExit(1) from exc
    print(result.status)
    print(result.message)
    if result.downloaded_file:
        print(result.downloaded_file)


if __name__ == "__main__":
    main()
