from __future__ import annotations

from datetime import datetime
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import load_config
from exporter.paths import browser_profile_dir, shop_dir
from exporter.dates import DateRange, parse_date


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="调试退货页面申请时间日期控件结构")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选: {available}")

    date_range = DateRange(parse_date(args.start), parse_date(args.end))
    output_dir = shop_dir(ROOT, date_range, config.platform_name, shop) / "截图"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = output_dir / f"日期控件调试_{timestamp}.png"
    html_path = output_dir / f"日期控件调试_{timestamp}.html"
    json_path = output_dir / f"日期控件调试_{timestamp}.json"

    automator = ReturnsAutomator(ROOT, config.platform, config.platform_name)
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
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
        automator._dismiss_page_popups(page)
        automator._wait_for_possible_login(page)
        automator._open_after_sales_workbench(page)
        automator._dismiss_page_popups(page)
        automator._expand_advanced_search(page)
        label = page.get_by_text("申请时间", exact=True).first
        date_input = label.locator("xpath=following::input[1]")
        date_input.click(timeout=5000)
        page.wait_for_timeout(1000)
        page.screenshot(path=str(screenshot_path), full_page=True)
        html = page.evaluate(
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
                const roots = Array.from(document.querySelectorAll("body *"))
                    .filter((el) => visible(el))
                    .filter((el) => {
                        const text = (el.innerText || "").trim();
                        const cls = String(el.className || "");
                        const id = String(el.id || "");
                        return /range|picker|calendar|date|RPR|beast|日期|时间/i.test(cls + " " + id)
                            || /202\\d年|[1-9]月|确认|00:00:00|23:59:59/.test(text);
                    })
                    .slice(0, 80);
                return roots.map((el) => {
                    const rect = el.getBoundingClientRect();
                    return `<!-- ${el.tagName} id="${el.id}" class="${el.className}" rect="${Math.round(rect.left)},${Math.round(rect.top)},${Math.round(rect.width)},${Math.round(rect.height)}" -->\\n${el.outerHTML}`;
                }).join("\\n\\n");
            }"""
        )
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
                return Array.from(document.querySelectorAll("button,input,select,td,span,div,[role='button']"))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName,
                            text: (el.innerText || el.textContent || "").trim().slice(0, 80),
                            value: el.value || "",
                            title: el.getAttribute("title") || "",
                            aria: el.getAttribute("aria-label") || "",
                            role: el.getAttribute("role") || "",
                            cls: String(el.className || "").slice(0, 160),
                            id: el.id || "",
                            rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)]
                        };
                    })
                    .filter((item) => /202\\d|年|月|确认|确定|00:00:00|23:59:59|^\\d{1,2}$|prev|next|left|right|range|picker|calendar|RPR|beast/i.test(
                        [item.text, item.value, item.title, item.aria, item.role, item.cls, item.id].join(" ")
                    ))
                    .slice(0, 400);
            }"""
        )
        html_path.write_text(html, encoding="utf-8")
        import json

        json_path.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存截图：{screenshot_path}")
        print(f"已保存 HTML：{html_path}")
        print(f"已保存 JSON：{json_path}")
        print("调试文件已生成。请关闭浏览器窗口。")
        page.wait_for_timeout(3000)
        context.close()


if __name__ == "__main__":
    main()
