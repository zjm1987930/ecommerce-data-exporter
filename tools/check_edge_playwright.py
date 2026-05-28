from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.browser import find_edge
from exporter.config import load_config
from exporter.paths import browser_profile_dir
from playwright.sync_api import sync_playwright


def main() -> None:
    config = load_config(ROOT)
    shop = config.shops[0]
    profile = browser_profile_dir(ROOT, config.platform, shop)
    profile.mkdir(parents=True, exist_ok=True)
    edge = find_edge()
    print(f"EDGE={edge}")
    print(f"PROFILE={profile}")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=edge,
            headless=False,
            viewport={"width": 1000, "height": 700},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("about:blank")
        page.wait_for_timeout(3000)
        context.close()
    print("EDGE_PLAYWRIGHT_OK")


if __name__ == "__main__":
    main()
