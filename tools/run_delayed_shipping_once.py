from __future__ import annotations

import argparse
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.delayed_shipping import DelayedShippingAutomator
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="单店铺拼多多延迟发货数据自动化验证")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--tab", action="append", default=[], help="指定 TAB；可重复传入。默认只跑延迟发货")
    parser.add_argument("--all-tabs", action="store_true", help="跑前三个 TAB：延迟发货、缺货、虚假发货/虚假轨迹")
    parser.add_argument("--run", action="store_true", help="实际生成报表并下载；默认只查询不导出")
    parser.add_argument("--manual-page", action="store_true", help="手动进入消费者补偿页面；默认自动从菜单进入")
    parser.add_argument("--manual-date", action="store_true", help="手动选择补偿时间；默认自动点选日期")
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

    tabs = ["延迟发货", "缺货", "虚假发货/虚假轨迹"] if args.all_tabs else args.tab
    automator = DelayedShippingAutomator(ROOT, config.platform, config.platform_name)
    try:
        result = automator.run_shop(
            shop,
            date_range,
            tabs=tabs,
            dry_run=not args.run,
            manual_page=args.manual_page,
            manual_date=args.manual_date,
        )
    except Exception as exc:
        print(f"延迟发货单店铺验证失败：{exc}")
        if args.debug:
            traceback.print_exc()
        raise SystemExit(1) from exc
    print(result.status)
    print(result.message)
    if result.output_file:
        print(result.output_file)


if __name__ == "__main__":
    main()
