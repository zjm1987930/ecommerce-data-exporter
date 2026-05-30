from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from exporter.automation.douyin_deposit import DOUYIN_HOME_URL, DOUYIN_PROFILE_ID, DouyinDepositAutomator
from exporter.browser import find_edge
from exporter.config import Shop
from exporter.paths import browser_profile_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取抖音切店弹窗 DOM 和本地存储中的店铺主体缓存")
    parser.add_argument("--profile-id", default=DOUYIN_PROFILE_ID)
    args = parser.parse_args()
    profile_shop = Shop(
        id=args.profile_id,
        name="抖音默认账号",
        sheet_name="抖音默认账号",
        platform="douyin",
        platform_name="抖音",
    )
    profile_dir = browser_profile_dir(ROOT, "douyin", profile_shop)
    automator = DouyinDepositAutomator(ROOT)
    with sync_playwright() as playwright:
        context = automator._launch_context(playwright, find_edge(), profile_dir, headed=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        automator._dismiss_page_popups(page)
        automator._open_account_menu(page)
        automator._click_switch_shop_entry(page)
        page.wait_for_timeout(1500)
        data = page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width > 0
                        && rect.height > 0
                        && rect.right >= 0
                        && rect.bottom >= 0;
                };
                const text = document.body.innerText;
                const cards = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const attrs = {};
                        for (const attr of el.attributes || []) attrs[attr.name] = attr.value;
                        return {
                            text: (el.innerText || el.textContent || "").trim(),
                            tag: el.tagName,
                            className: String(el.className || ""),
                            attrs,
                            rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
                        };
                    })
                    .filter((item) => /图书|文化|书海|伟业|金喆|悦趣|逸观/.test(item.text))
                    .slice(0, 200);
                const storage = {};
                for (const source of [window.localStorage, window.sessionStorage]) {
                    for (let i = 0; i < source.length; i += 1) {
                        const key = source.key(i);
                        const value = source.getItem(key) || "";
                        if (/subject|shop|account|bind|identity|login|user|店铺|图书|文化/i.test(key + value)) {
                            storage[key] = value;
                        }
                    }
                }
                return { url: location.href, text, cards, storage };
            }"""
        )
        out = ROOT / "原始数据" / "导出日志" / "抖音切店接口监听" / "抖音切店DOM.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(out)
        preview = json.dumps(data["cards"][:80], ensure_ascii=False, indent=2)[:12000]
        print(preview.encode("gbk", errors="ignore").decode("gbk", errors="ignore"))
        context.close()


if __name__ == "__main__":
    main()
