from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import Download, Page, Response, sync_playwright

from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import load_config
from exporter.dates import DateRange, parse_date, previous_month
from exporter.paths import browser_profile_dir, task_shop_dir


def main() -> None:
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="监听拼多多退货查询/生成报表/下载接口")
    parser.add_argument("--shop-id", default=config.shops[0].id)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--manual-page", action="store_true", help="手动进入售后工作台后开始监听")
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

    output_dir = task_shop_dir(ROOT, date_range, "退货数据", shop)
    debug_dir = output_dir / "截图" / "退货接口监听"
    debug_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = debug_dir / f"退货接口监听_{timestamp}.json"
    screenshot_path = debug_dir / f"退货接口监听_{timestamp}.png"
    html_path = debug_dir / f"退货接口监听_{timestamp}.html"

    automator = ReturnsAutomator(ROOT, config.platform, config.platform_name)
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
                text = json.dumps(data, ensure_ascii=False)[:16000]
            elif _looks_like_file_response(response):
                text = "<binary/file response>"
            else:
                text = response.text()[:16000]
                stripped = text.lstrip()
                if stripped.startswith(("{", "[")):
                    data = json.loads(text)
        except Exception as exc:
            item["response_read_error"] = repr(exc)
        item["response_json"] = data
        item["response_text_preview"] = text[:5000]

        haystack = " ".join(
            [
                request.url,
                post_data,
                json.dumps(data, ensure_ascii=False) if data is not None else text,
                json.dumps(response.headers, ensure_ascii=False),
            ]
        )
        if not _looks_like_returns_api(haystack, item):
            return
        captures.append(item)
        print(f"捕获接口：{request.method} {request.url} status={response.status}")

    def on_download(download: Download) -> None:
        suggested = download.suggested_filename or f"退货接口监听_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
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
        context.on("page", on_page)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        page.on("download", on_download)
        page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
        automator._dismiss_page_popups(page)
        if args.manual_page:
            print("请手动进入：售后管理 -> 售后工作台。进入后回到这个窗口按 Enter。")
            input()
        else:
            automator._wait_for_possible_login(page)
            automator._open_after_sales_workbench(page)
        automator._dismiss_page_popups(page)

        print("")
        print(f"已打开退货监听环境：{shop.name}")
        print(f"目标日期：{date_range.start.isoformat()} 00:00:00 至 {date_range.end.isoformat()} 23:59:59")
        print("请手动完成：展开高级查询 -> 设置筛选条件 -> 查询 -> 批量导出 -> 确认生成 -> 查看已生成报表 -> 导出。")
        print("导出下载完成后，回到这个窗口按 Enter。")
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
        or "attachment" in disposition
        or ".xlsx" in disposition
        or ".xls" in disposition
    )


def _looks_like_returns_api(text: str, item: dict[str, Any]) -> bool:
    lowered = text.lower()
    markers = [
        "after",
        "refund",
        "return",
        "returns",
        "aftersales",
        "after_sales",
        "aftersale",
        "workbench",
        "export",
        "download",
        "report",
        "xlsx",
        "xls",
        "售后",
        "退货",
        "退款",
        "退款退货",
        "批量导出",
        "确认生成",
        "生成报表",
        "已生成报表",
        "导出",
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
