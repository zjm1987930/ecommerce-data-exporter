from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from exporter.automation.delayed_shipping import DelayedShippingAutomator
from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import load_config
from exporter.dates import DateRange, parse_date
from exporter.paths import browser_profile_dir, shop_dir


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="调试延迟发货页面补偿时间日期控件结构")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--manual-page", action="store_true", help="手动进入消费者补偿页面后再读控件")
    parser.add_argument("--which", choices=["start", "end"], default="start", help="打开开始时间或结束时间控件")
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选：{available}")

    date_range = DateRange(parse_date(args.start), parse_date(args.end))
    output_dir = shop_dir(ROOT, date_range, config.platform_name, shop) / "截图"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"延迟发货日期控件调试_{args.which}_{timestamp}"
    screenshot_path = output_dir / f"{prefix}.png"
    html_path = output_dir / f"{prefix}.html"
    json_path = output_dir / f"{prefix}.json"

    common = ReturnsAutomator(ROOT, config.platform, config.platform_name)
    automator = DelayedShippingAutomator(ROOT, config.platform, config.platform_name)
    edge_path = find_edge()
    profile_dir = browser_profile_dir(ROOT, config.platform, shop)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=edge_path,
            headless=False,
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
        page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
        common._dismiss_page_popups(page)
        if args.manual_page:
            print("请手动进入：账户资金 -> 消费者补偿。进入后回到终端按 Enter。")
            input()
        else:
            automator._ensure_logged_in(page)
            automator._open_consumer_compensation(page)
        automator._wait_for_compensation_page(page)
        start_input, end_input = automator._compensation_time_inputs(page)
        target_input = start_input if args.which == "start" else end_input
        target_input.click(timeout=5000)
        page.wait_for_timeout(1000)

        details = page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width > 0
                        && rect.height > 0
                        && rect.right >= 0
                        && rect.bottom >= 0
                        && rect.left <= window.innerWidth
                        && rect.top <= window.innerHeight;
                };
                const inputSummary = Array.from(document.querySelectorAll("input"))
                    .filter(visible)
                    .map((input) => {
                        const rect = input.getBoundingClientRect();
                        return {
                            value: input.value || "",
                            placeholder: input.getAttribute("placeholder") || "",
                            readonly: input.readOnly,
                            disabled: input.disabled,
                            cls: String(input.className || "").slice(0, 180),
                            rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)]
                        };
                    });
                const candidates = Array.from(document.querySelectorAll("button,input,td,span,div,[role='button'],[role='date-cell']"))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName,
                            text: (el.innerText || el.textContent || "").trim().slice(0, 120),
                            value: el.value || "",
                            title: el.getAttribute("title") || "",
                            aria: el.getAttribute("aria-label") || "",
                            role: el.getAttribute("role") || "",
                            cls: String(el.className || "").slice(0, 180),
                            rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)]
                        };
                    })
                    .filter((item) => /202\\d|年|月|确定|确认|00:00|23:59|^\\d{1,2}$|prev|next|left|right|picker|calendar|RPR|beast/i.test(
                        [item.text, item.value, item.title, item.aria, item.role, item.cls].join(" ")
                    ))
                    .slice(0, 700);
                return { inputSummary, candidates };
            }"""
        )
        page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(page.locator("body").evaluate("body => body.outerHTML"), encoding="utf-8")
        json_path.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存截图：{screenshot_path}")
        print(f"已保存 HTML：{html_path}")
        print(f"已保存 JSON：{json_path}")
        page.wait_for_timeout(3000)
        context.close()


if __name__ == "__main__":
    main()
