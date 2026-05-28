from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.browser import open_shop_browser
from exporter.config import Shop, load_config


def resolve_shops(all_shops: list[Shop], selected_ids: list[str]) -> list[Shop]:
    if not selected_ids:
        return all_shops

    shops_by_id = {shop.id: shop for shop in all_shops}
    unknown = [shop_id for shop_id in selected_ids if shop_id not in shops_by_id]
    if unknown:
        available = ", ".join(shop.id for shop in all_shops)
        raise SystemExit(f"未知 shop-id: {', '.join(unknown)}. 可选: {available}")
    return [shops_by_id[shop_id] for shop_id in selected_ids]


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="顺序打开普通 Edge 店铺登录环境")
    parser.add_argument("--shop-id", action="append", default=[], help="指定店铺 ID；可重复传入。不传则处理全部店铺")
    args = parser.parse_args()

    shops = resolve_shops(config.shops, args.shop_id)
    print(f"本次需要检查登录状态的店铺数：{len(shops)}")
    print("每次只会打开一个普通 Edge 登录环境。登录完成后请关闭该 Edge 窗口，再回到终端按 Enter 继续下一个。")

    for index, shop in enumerate(shops, start=1):
        print("")
        print(f"===== [{index}/{len(shops)}] {shop.name} =====")
        profile = open_shop_browser(ROOT, config.platform, shop)
        print(f"已打开普通 Edge 登录环境：{shop.name}")
        print(f"登录环境目录：{profile}")
        input("请确认已登录并关闭该 Edge 窗口后，按 Enter 继续...")

    print("")
    print("全部店铺登录环境检查完成。现在可以运行批量退货导出脚本。")


if __name__ == "__main__":
    main()
