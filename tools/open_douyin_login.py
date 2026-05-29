from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.browser import open_shop_browser
from exporter.config import Shop


DOUYIN_HOME_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"
DOUYIN_LOGIN_URL = "https://fxg.jinritemai.com/login"
PLATFORM = "douyin"


def main() -> None:
    parser = argparse.ArgumentParser(description="打开普通 Edge 抖音登录环境")
    parser.add_argument("--profile-id", default="douyin_default", help="抖音登录状态目录 ID，建议多个店铺共用 douyin_default")
    parser.add_argument("--profile-name", default="抖音默认账号", help="仅用于控制台提示")
    parser.add_argument("--url", default=DOUYIN_HOME_URL, help="启动后打开的 URL")
    parser.add_argument("--login", action="store_true", help="直接打开抖音登录页")
    args = parser.parse_args()

    profile = Shop(id=args.profile_id, name=args.profile_name, sheet_name=args.profile_name)
    url = DOUYIN_LOGIN_URL if args.login else args.url
    profile_dir = open_shop_browser(ROOT, PLATFORM, profile, url)
    print(f"已打开普通 Edge 抖音登录环境：{args.profile_name}")
    print(f"登录环境目录：{profile_dir}")
    print("请在浏览器中完成登录并确认能进入抖音商家后台。")
    print("登录成功后关闭这个普通 Edge 窗口，再运行抖音保证金监听脚本。")


if __name__ == "__main__":
    main()
