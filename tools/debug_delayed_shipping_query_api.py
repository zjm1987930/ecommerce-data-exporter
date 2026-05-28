from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import Request, Response, sync_playwright

from exporter.automation.delayed_shipping import DelayedShippingAutomator
from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import load_config
from exporter.dates import DateRange, parse_date
from exporter.paths import browser_profile_dir, shop_dir


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="抓取延迟发货页面查询接口请求与响应")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--manual-page", action="store_true", help="手动进入消费者补偿页面后开始监听")
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选：{available}")

    date_range = DateRange(parse_date(args.start), parse_date(args.end))
    output_dir = shop_dir(ROOT, date_range, config.platform_name, shop) / "截图"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"延迟发货查询接口调试_{timestamp}.json"
    screenshot_path = output_dir / f"延迟发货查询接口调试_{timestamp}.png"

    common = ReturnsAutomator(ROOT, config.platform, config.platform_name)
    automator = DelayedShippingAutomator(ROOT, config.platform, config.platform_name)
    edge_path = find_edge()
    profile_dir = browser_profile_dir(ROOT, config.platform, shop)
    profile_dir.mkdir(parents=True, exist_ok=True)

    captures: list[dict[str, Any]] = []

    def on_response(response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch"}:
            return
        text = ""
        data: Any = None
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                data = response.json()
                text = json.dumps(data, ensure_ascii=False)[:5000]
            else:
                text = response.text()[:5000]
                if text.lstrip().startswith(("{", "[")):
                    data = json.loads(text)
        except Exception:
            return
        haystack = " ".join(
            [
                request.url,
                request.post_data or "",
                text,
            ]
        )
        if not _looks_like_delayed_shipping(haystack, data):
            return
        captures.append(
            {
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "status": response.status,
                "request_headers": request.headers,
                "post_data": request.post_data,
                "response_headers": response.headers,
                "response_json": data,
                "response_text_preview": text[:2000],
                "detected_total": _find_total(data),
                "detected_records_preview": _find_records(data)[:3],
            }
        )
        print(f"捕获疑似接口：{request.method} {request.url}；total={_find_total(data)}")

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
        page.on("response", on_response)
        page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
        common._dismiss_page_popups(page)
        if args.manual_page:
            print("请手动进入：账户资金 -> 消费者补偿，并保持在延迟发货 TAB。进入后回到终端按 Enter。")
            input()
        else:
            automator._ensure_logged_in(page)
            automator._open_consumer_compensation(page)
        automator._wait_for_compensation_page(page)
        print("")
        print(f"请在页面手动选择补偿时间：{date_range.start.isoformat()} 00:00:00 至 {date_range.end.isoformat()} 23:59:59")
        print("然后点击页面上的“查询”。看到结果后回到终端按 Enter，脚本会保存接口请求与响应。")
        input()
        page.wait_for_timeout(2000)
        page.screenshot(path=str(screenshot_path), full_page=True)
        output_path.write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存接口调试 JSON：{output_path}")
        print(f"已保存截图：{screenshot_path}")
        print(f"捕获数量：{len(captures)}")
        page.wait_for_timeout(1000)
        context.close()


def _looks_like_delayed_shipping(text: str, data: Any) -> bool:
    markers = [
        "补偿",
        "compens",
        "delivery",
        "delay",
        "ship",
        "发货",
        "承诺发货",
        "补偿时间",
        "补偿金额",
    ]
    if any(marker.lower() in text.lower() for marker in markers):
        if _find_total(data) is not None or _find_records(data):
            return True
    return False


def _find_total(data: Any) -> int | None:
    if isinstance(data, dict):
        for key in ["total", "totalCount", "total_count", "count", "recordCount"]:
            value = data.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        for value in data.values():
            found = _find_total(value)
            if found is not None:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_total(item)
            if found is not None:
                return found
    return None


def _find_records(data: Any) -> list[Any]:
    if isinstance(data, dict):
        for key in ["list", "records", "items", "result", "data"]:
            value = data.get(key)
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                if _record_list_score(value) >= 2:
                    return value
        for value in data.values():
            found = _find_records(value)
            if found:
                return found
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        if _record_list_score(data) >= 2:
            return data
    return []


def _record_list_score(records: list[Any]) -> int:
    sample = json.dumps(records[:3], ensure_ascii=False)
    markers = ["order", "订单", "goods", "商品", "compens", "补偿", "deliver", "发货", "amount", "金额"]
    return sum(1 for marker in markers if marker.lower() in sample.lower())


if __name__ == "__main__":
    main()
