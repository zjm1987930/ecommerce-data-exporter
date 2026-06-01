from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.browser import KUAISHOU_HOME_URL, open_shop_browser
from exporter.config import Shop


KUAISHOU_LOGIN_URL = "https://s.kwaixiaodian.com/"
PLATFORM = "kuaishou"


def main() -> None:
    parser = argparse.ArgumentParser(description="打开普通 Edge 快手登录环境")
    parser.add_argument("--profile-id", default="kuaishou_default", help="快手登录状态目录 ID")
    parser.add_argument("--profile-name", default="快手默认账号", help="仅用于控制台提示")
    parser.add_argument("--url", default=KUAISHOU_HOME_URL, help="启动后打开的 URL")
    parser.add_argument("--login", action="store_true", help="直接打开快手登录入口")
    args = parser.parse_args()

    profile = Shop(
        id=args.profile_id,
        name=args.profile_name,
        sheet_name=args.profile_name,
        platform=PLATFORM,
        platform_name="快手",
    )
    url = KUAISHOU_LOGIN_URL if args.login else args.url
    profile_dir = open_shop_browser(ROOT, PLATFORM, profile, url)
    print(f"已打开普通 Edge 快手登录环境：{args.profile_name}")
    print(f"登录环境目录：{profile_dir}")
    print("请在浏览器中完成登录，并确认能进入快手小店/快手电商商家后台。")
    print("登录成功后关闭这个普通 Edge 窗口，再运行快手保证金监听脚本。")


if __name__ == "__main__":
    main()
