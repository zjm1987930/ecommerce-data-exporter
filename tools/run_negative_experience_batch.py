from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.automation.negative_experience import NegativeExperienceAutomator
from exporter.config import Shop, load_config
from exporter.dates import DateRange, parse_date, previous_month, split_range
from exporter.logging import ExportLogger, ExportLogRow
from exporter.paths import log_dir


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
    parser = argparse.ArgumentParser(description="批量抓取拼多多消费者负向体验扣款数据")
    parser.add_argument("--shop-id", action="append", default=[], help="指定店铺 ID；可重复传入。不传则跑全部店铺")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--manual-date", action="store_true", help="手动选择创建时间；默认自动点选日期")
    parser.add_argument("--stop-on-error", action="store_true", help="某个店铺失败后立即停止")
    args = parser.parse_args()

    date_range = resolve_date_range(args.start, args.end)
    shops = resolve_shops(config.shops, args.shop_id)
    data_type = next(item for item in config.data_types if item.id == "negative_experience")
    chunks = split_range(date_range, 7)

    print(f"本次负向体验任务店铺数：{len(shops)}")
    print(f"日期范围：{date_range.start.isoformat()} 至 {date_range.end.isoformat()}")
    print("页面限制单次最多 7 天，脚本会自动拆分为以下区间：")
    for chunk in chunks:
        print(f"- {chunk.start.isoformat()} 至 {chunk.end.isoformat()}")
    print("重要：批量抓取前建议先运行 login_shops_batch.ps1，用普通 Edge 逐个店铺完成登录。")

    logger = ExportLogger(log_dir(ROOT, date_range))
    automator = NegativeExperienceAutomator(ROOT, config.platform, config.platform_name)
    output_file = ""
    failed = False
    failures: dict[str, str] = {}

    try:
        result = automator.run_batch(shops, date_range, manual_date=args.manual_date, stop_on_error=args.stop_on_error)
        output_file = str(result.output_file or "")
        print(result.status)
        print(result.message)
        print(f"总记录数：{result.total_records}")
        if result.failures:
            failures = result.failures
            for shop_name, reason in result.failures.items():
                print(f"失败店铺：{shop_name}；原因：{reason}")
            failed = True
    except Exception as exc:
        failed = True
        print(f"负向体验批量任务失败：{exc}")
        traceback.print_exc()
        if args.stop_on_error:
            raise
    finally:
        for shop in shops:
            started_at = datetime.now().isoformat(timespec="seconds")
            shop_failed = shop.name in failures or (failed and not output_file)
            status = "失败" if shop_failed else "成功"
            logger.add(
                ExportLogRow(
                    month=date_range.month_key,
                    platform=config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=date_range.start.isoformat(),
                    end_date=date_range.end.isoformat(),
                    status=status,
                    file_path=output_file,
                    reason=failures.get(shop.name, "批量任务失败，详见终端或截图" if shop_failed else ""),
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
