from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import Download, Response, sync_playwright

from exporter.automation.delayed_shipping import DelayedShippingAutomator
from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import load_config
from exporter.dates import DateRange, parse_date
from exporter.paths import browser_profile_dir, shop_dir


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="抓取延迟发货批量导出/生成报表/下载接口")
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
    output_dir = shop_dir(ROOT, date_range, config.platform_name, shop)
    screenshot_dir = output_dir / "截图"
    raw_dir = output_dir / "延迟发货_平台原始文件"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"延迟发货导出接口调试_{timestamp}"
    output_path = screenshot_dir / f"{prefix}.json"
    screenshot_path = screenshot_dir / f"{prefix}.png"

    common = ReturnsAutomator(ROOT, config.platform, config.platform_name)
    automator = DelayedShippingAutomator(ROOT, config.platform, config.platform_name)
    edge_path = find_edge()
    profile_dir = browser_profile_dir(ROOT, config.platform, shop)
    profile_dir.mkdir(parents=True, exist_ok=True)

    captures: list[dict[str, Any]] = []
    downloads: list[dict[str, str]] = []

    def on_response(response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch", "document"}:
            return
        item: dict[str, Any] = {
            "url": request.url,
            "method": request.method,
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": request.headers,
            "post_data": request.post_data,
            "response_headers": response.headers,
        }
        text = ""
        data: Any = None
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                data = response.json()
                text = json.dumps(data, ensure_ascii=False)[:8000]
            elif _looks_like_file_response(response):
                text = "<binary/file response>"
            else:
                text = response.text()[:8000]
                stripped = text.lstrip()
                if stripped.startswith(("{", "[")):
                    data = json.loads(text)
        except Exception as exc:
            item["response_read_error"] = repr(exc)
        item["response_json"] = data
        item["response_text_preview"] = text[:3000]

        haystack = " ".join(
            [
                request.url,
                request.post_data or "",
                json.dumps(data, ensure_ascii=False) if data is not None else text,
            ]
        )
        if not _looks_like_delayed_shipping_export(haystack, item):
            return
        captures.append(item)
        print(f"捕获接口：{request.method} {request.url} status={response.status}")

    def on_download(download: Download) -> None:
        suggested = download.suggested_filename or f"延迟发货导出_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        target = _unique_path(raw_dir / suggested)
        try:
            download.save_as(target)
            downloads.append({"suggested_filename": suggested, "saved_path": str(target), "url": download.url})
            print(f"捕获下载文件：{target}")
        except Exception as exc:
            downloads.append({"suggested_filename": suggested, "error": repr(exc), "url": download.url})
            print(f"保存下载文件失败：{exc}")

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
        page.on("download", on_download)
        page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
        common._dismiss_page_popups(page)
        if args.manual_page:
            print("请手动进入：账户资金 -> 消费者补偿，并保持在“延迟发货”TAB。进入后回到终端按 Enter。")
            input()
        else:
            automator._ensure_logged_in(page)
            automator._open_consumer_compensation(page)
        automator._wait_for_compensation_page(page)
        automator._open_tab(page, "延迟发货")

        print("")
        print(f"请在页面手动设置补偿时间：{date_range.start.isoformat()} 00:00:00 至 {date_range.end.isoformat()} 23:59:59")
        print("然后依次点击：查询 -> 批量导出 -> 生成报表。")
        print("如果跳到批量导出列表，请等报表生成后点击“下载补偿订单列表”。完成后回到终端按 Enter。")
        input()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(screenshot_path), full_page=True)
        output = {
            "shop_id": shop.id,
            "shop_name": shop.name,
            "date_range": {
                "start": date_range.start.isoformat(),
                "end": date_range.end.isoformat(),
            },
            "captures": captures,
            "downloads": downloads,
        }
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存接口调试 JSON：{output_path}")
        print(f"已保存截图：{screenshot_path}")
        print(f"捕获接口数量：{len(captures)}")
        print(f"捕获下载数量：{len(downloads)}")
        page.wait_for_timeout(1000)
        context.close()


def _looks_like_file_response(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    disposition = response.headers.get("content-disposition", "").lower()
    return (
        "application/vnd" in content_type
        or "application/octet-stream" in content_type
        or "attachment" in disposition
        or ".xlsx" in disposition
    )


def _looks_like_delayed_shipping_export(text: str, item: dict[str, Any]) -> bool:
    lowered = text.lower()
    markers = [
        "overtimeshipment",
        "charge",
        "compens",
        "export",
        "download",
        "report",
        "xlsx",
        "补偿",
        "延迟发货",
        "发货",
        "批量导出",
        "生成报表",
        "下载补偿订单列表",
    ]
    if any(marker.lower() in lowered for marker in markers):
        return True
    headers = item.get("response_headers") or {}
    content_disposition = headers.get("content-disposition", "")
    return bool(content_disposition and ("xlsx" in content_disposition.lower() or "attachment" in content_disposition.lower()))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 100):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")


if __name__ == "__main__":
    main()
