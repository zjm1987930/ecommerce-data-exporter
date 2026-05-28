from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.browser import open_shop_browser
from exporter.config import load_config


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="打开普通 Edge 店铺登录环境")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选: {available}")

    profile = open_shop_browser(ROOT, config.platform, shop)
    print(f"已打开普通 Edge 登录环境：{shop.name}")
    print(f"登录环境目录：{profile}")
    print("请在浏览器中完成登录。登录成功后请关闭这个 Edge 窗口，再运行自动化脚本。")


if __name__ == "__main__":
    main()
