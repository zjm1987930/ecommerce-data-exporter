from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from exporter.automation.douyin_deposit import DOUYIN_HOME_URL, DOUYIN_PROFILE_ID
from exporter.browser import find_edge
from exporter.config import Shop
from exporter.paths import browser_profile_dir


def main() -> None:
    profile_shop = Shop(
        id=DOUYIN_PROFILE_ID,
        name="抖音默认账号",
        sheet_name="抖音默认账号",
        platform="douyin",
        platform_name="抖音",
    )
    profile_dir = browser_profile_dir(ROOT, "douyin", profile_shop)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=find_edge(),
            headless=True,
            chromium_sandbox=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-background-mode",
                "--edge-skip-compat-layer-relaunch",
                "--no-first-run",
                "--profile-directory=Default",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        for path in [
            "/byteshop/accountbind/getbindlist",
            "/ecomauth/loginv1/get_login_user_detail_info",
            "/byteshop/shopuser/getAdminV2",
        ]:
            result = page.evaluate(
                """async (path) => {
                    const response = await fetch(path + "?appid=1", { credentials: "include" });
                    const text = await response.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (error) {}
                    return { status: response.status, url: response.url, data, text };
                }""",
                path,
            )
            print("###", path, result["status"], result["url"])
            print(json.dumps(result["data"], ensure_ascii=False, indent=2)[:20000] if result["data"] else result["text"][:20000])
        context.close()


if __name__ == "__main__":
    main()
