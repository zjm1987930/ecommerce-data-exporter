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

from exporter.automation.customer_service_performance import CustomerServicePerformanceAutomator
from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_CUSTOMER_SERVICE_PERFORMANCE_URL, find_edge
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month
from exporter.paths import browser_profile_dir, task_shop_dir


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="监听拼多多客服绩效查询/下载接口")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--manual-date", action="store_true", help="由用户手动设置日期后再点击下载")
    args = parser.parse_args()

    shop = next((item for item in config.shops if item.id == args.shop_id), None)
    if not shop:
        available = ", ".join(item.id for item in config.shops)
        raise SystemExit(f"未知 shop-id: {args.shop_id}. 可选：{available}")

    date_range = previous_month()
    if bool(args.start) != bool(args.end):
        raise SystemExit("--start 和 --end 必须同时提供")
    if args.start and args.end:
        date_range = DateRange(parse_date(args.start), parse_date(args.end))

    output_dir = task_shop_dir(ROOT, date_range, "客服绩效数据", shop)
    screenshot_dir = output_dir / "截图"
    debug_dir = screenshot_dir / "客服绩效接口监听"
    debug_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = debug_dir / f"客服绩效接口监听_{timestamp}.json"
    screenshot_path = debug_dir / f"客服绩效接口监听_{timestamp}.png"
    html_path = debug_dir / f"客服绩效接口监听_{timestamp}.html"

    common = ReturnsAutomator(ROOT, config.platform, config.platform_name)
    automator = CustomerServicePerformanceAutomator(ROOT, config.platform, config.platform_name)
    edge_path = find_edge()
    profile_dir = browser_profile_dir(ROOT, config.platform, shop)
    profile_dir.mkdir(parents=True, exist_ok=True)

    captures: list[dict[str, Any]] = []
    downloads: list[dict[str, str]] = []
    page_events: list[dict[str, str]] = []

    def on_response(response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch", "document"}:
            return
        post_data = ""
        try:
            post_data = request.post_data or ""
        except Exception as exc:
            post_data = f"<post_data_read_error: {exc!r}>"
        item: dict[str, Any] = {
            "url": request.url,
            "method": request.method,
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": request.headers,
            "post_data": post_data,
            "response_headers": response.headers,
        }
        text = ""
        data: Any = None
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                data = response.json()
                text = json.dumps(data, ensure_ascii=False)[:12000]
            elif _looks_like_file_response(response):
                text = "<binary/file response>"
            else:
                text = response.text()[:12000]
                stripped = text.lstrip()
                if stripped.startswith(("{", "[")):
                    data = json.loads(text)
        except Exception as exc:
            item["response_read_error"] = repr(exc)
        item["response_json"] = data
        item["response_text_preview"] = text[:4000]

        haystack = " ".join(
            [
                request.url,
                post_data,
                json.dumps(data, ensure_ascii=False) if data is not None else text,
                json.dumps(response.headers, ensure_ascii=False),
            ]
        )
        if not _looks_like_customer_service_performance(haystack, item):
            return
        captures.append(item)
        print(f"捕获接口：{request.method} {request.url} status={response.status}")

    def on_download(download: Download) -> None:
        suggested = download.suggested_filename or f"客服绩效接口监听_{datetime.now():%Y%m%d_%H%M%S}.xls"
        target = _unique_path(debug_dir / suggested)
        try:
            download.save_as(target)
            downloads.append({"suggested_filename": suggested, "saved_path": str(target), "url": download.url})
            print(f"捕获下载文件：{target}")
        except Exception as exc:
            downloads.append({"suggested_filename": suggested, "error": repr(exc), "url": download.url})
            print(f"保存下载文件失败：{exc}")

    def on_page(new_page: Page) -> None:
        page_events.append({"event": "page", "url": new_page.url})
        print(f"检测到新页面：{new_page.url}")

        def on_frame_navigated(frame) -> None:
            if frame == new_page.main_frame:
                page_events.append({"event": "navigated", "url": new_page.url})
                print(f"新页面跳转：{new_page.url}")

        new_page.on("framenavigated", on_frame_navigated)

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
        context.on("page", on_page)
        page.on("response", on_response)
        page.on("download", on_download)
        page.goto(PDD_CUSTOMER_SERVICE_PERFORMANCE_URL, wait_until="domcontentloaded")
        common._dismiss_page_popups(page)
        automator._wait_for_performance_page(page)
        automator._open_performance_tab(page)
        automator._wait_for_performance_detail(page)

        print("")
        print(f"已打开客服绩效页面：{shop.name}")
        print(f"目标日期：{date_range.start.isoformat()} 至 {date_range.end.isoformat()}")
        if args.manual_date:
            print("请在页面手动设置客服绩效详情日期范围，然后点击“下载表单”。")
        else:
            print("脚本将先自动设置日期，然后请你点击“下载表单”。")
            automator._set_date_range(page, date_range)
        print("点击“下载表单”后，等浏览器右上角出现下载记录，再回到这个窗口按 Enter。")
        input()
        page.wait_for_timeout(2500)
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        output = {
            "shop_id": shop.id,
            "shop_name": shop.name,
            "date_range": {
                "start": date_range.start.isoformat(),
                "end": date_range.end.isoformat(),
            },
            "captures": captures,
            "downloads": downloads,
            "page_events": page_events,
            "open_pages": [{"url": item.url, "title": _safe_title(item)} for item in context.pages],
        }
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存接口调试 JSON：{output_path}")
        print(f"已保存截图：{screenshot_path}")
        print(f"已保存 HTML：{html_path}")
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
        or "application/download" in content_type
        or "attachment" in disposition
        or ".xls" in disposition
    )


def _looks_like_customer_service_performance(text: str, item: dict[str, Any]) -> bool:
    lowered = text.lower()
    markers = [
        "mms-chat",
        "merchant",
        "service",
        "customer",
        "performance",
        "kefu",
        "export",
        "download",
        "xls",
        "客服",
        "绩效",
        "下载表单",
        "客服账号",
        "询单",
        "成团",
    ]
    if any(marker.lower() in lowered for marker in markers):
        return True
    headers = item.get("response_headers") or {}
    content_disposition = headers.get("content-disposition", "")
    return bool(content_disposition and ("xls" in content_disposition.lower() or "attachment" in content_disposition.lower()))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 100):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")


def _safe_title(page: Page) -> str:
    try:
        return page.title(timeout=1000)
    except Exception:
        return ""


if __name__ == "__main__":
    main()
