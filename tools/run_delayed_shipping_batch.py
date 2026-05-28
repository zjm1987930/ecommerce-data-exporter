from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.delayed_shipping import DelayedShippingAutomator
from exporter.config import Shop, load_config
from exporter.dates import DateRange, parse_date, previous_month
from exporter.logging import ExportLogger, ExportLogRow
from exporter.paths import log_dir


DEFAULT_TABS = ["延迟发货", "缺货", "虚假发货/虚假轨迹"]


def resolve_date_range(start: str | None, end: str | None) -> DateRange:
    if bool(start) != bool(end):
        raise SystemExit("--start 和 --end 必须同时提供")
    if start and end:
        date_range = DateRange(parse_date(start), parse_date(end))
    else:
        date_range = previous_month()
    if date_range.start > date_range.end:
        raise SystemExit("--start 不能晚于 --end")
    return date_range


def resolve_shops(all_shops: list[Shop], selected_ids: list[str]) -> list[Shop]:
    if not selected_ids:
        return all_shops

    shops_by_id = {shop.id: shop for shop in all_shops}
    unknown = [shop_id for shop_id in selected_ids if shop_id not in shops_by_id]
    if unknown:
        available = ", ".join(shop.id for shop in all_shops)
        raise SystemExit(f"未知 shop-id: {', '.join(unknown)}. 可选：{available}")
    return [shops_by_id[shop_id] for shop_id in selected_ids]


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="批量执行拼多多延迟发货数据导出")
    parser.add_argument("--shop-id", action="append", default=[], help="指定店铺 ID；可重复传入。不传则跑全部店铺")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--run", action="store_true", help="实际生成并下载；默认只查询不导出")
    parser.add_argument("--tab", action="append", default=[], help="指定 TAB；可重复传入。默认跑前三个 TAB")
    parser.add_argument("--first-tab-only", action="store_true", help="只跑延迟发货 TAB")
    parser.add_argument("--manual-page", action="store_true", help="手动进入消费者补偿页面；默认自动进入")
    parser.add_argument("--stop-on-error", action="store_true", help="某个店铺失败后立刻停止")
    parser.add_argument("--debug", action="store_true", help="失败时打印完整 traceback")
    args = parser.parse_args()

    date_range = resolve_date_range(args.start, args.end)
    shops = resolve_shops(config.shops, args.shop_id)
    data_type = next(item for item in config.data_types if item.id == "delayed_shipping")
    tabs = ["延迟发货"] if args.first_tab_only else (args.tab or DEFAULT_TABS)

    logger = ExportLogger(log_dir(ROOT, date_range))
    automator = DelayedShippingAutomator(ROOT, config.platform, config.platform_name)

    print(f"本次延迟发货任务店铺数：{len(shops)}")
    print(f"日期范围：{date_range.start.isoformat()} 至 {date_range.end.isoformat()}")
    print(f"TAB：{'、'.join(tabs)}")
    print("重要：批量导出前建议先运行 login_shops_batch.ps1，用普通 Edge 逐个店铺完成登录。")
    print("登录失效时脚本会标记该店铺失败；请用 login_shop.ps1 补登录后单独补跑失败店铺。")
    if args.run:
        print("当前模式：实际导出并下载")
    else:
        print("当前模式：只查询，不导出。需要导出请加 --run")

    for index, shop in enumerate(shops, start=1):
        print("")
        print(f"===== [{index}/{len(shops)}] {shop.name} =====")
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        try:
            result = automator.run_shop(
                shop,
                date_range,
                tabs=tabs,
                dry_run=not args.run,
                manual_page=args.manual_page,
            )
            status = result.status
            reason = result.message
            if result.output_file:
                file_path = str(result.output_file)
            print(result.status)
            print(result.message)
        except Exception as exc:
            reason = str(exc)
            print(f"店铺失败：{shop.name}")
            print(reason)
            if args.debug:
                traceback.print_exc()
            if args.stop_on_error:
                logger.add(
                    ExportLogRow(
                        month=date_range.month_key,
                        platform=config.platform_name,
                        shop=shop.name,
                        data_type=data_type.name,
                        start_date=date_range.start.isoformat(),
                        end_date=date_range.end.isoformat(),
                        status=status,
                        file_path=file_path,
                        reason=reason,
                        started_at=started_at,
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                    )
                )
                break
        else:
            logger.add(
                ExportLogRow(
                    month=date_range.month_key,
                    platform=config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=date_range.start.isoformat(),
                    end_date=date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
            continue

        if not args.stop_on_error:
            logger.add(
                ExportLogRow(
                    month=date_range.month_key,
                    platform=config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=date_range.start.isoformat(),
                    end_date=date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )

    csv_path, json_path = logger.write()
    print("")
    print(f"导出日志：{csv_path}")
    print(f"机器日志：{json_path}")


if __name__ == "__main__":
    main()
