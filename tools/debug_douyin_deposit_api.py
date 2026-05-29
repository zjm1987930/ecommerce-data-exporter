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

from exporter.browser import find_edge
from exporter.config import Shop
from exporter.dates import DateRange, parse_date, previous_month
from exporter.paths import browser_profile_dir, task_shop_dir


DOUYIN_HOME_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"
DOUYIN_LOGIN_URL = "https://fxg.jinritemai.com/login"
PLATFORM = "douyin"
PLATFORM_NAME = "抖音"
DATA_TYPE_NAME = "保证金数据"


def main() -> None:
    parser = argparse.ArgumentParser(description="监听抖音体验保证金交易流水查询/导出接口")
    parser.add_argument("--shop-id", default="douyin_debug", help="抖音店铺 ID，用于区分浏览器登录状态目录")
    parser.add_argument("--shop-name", default="抖音调试店铺", help="抖音店铺名称，用于保存调试文件目录")
    parser.add_argument("--sheet-name", default="", help="店铺短名；不填则使用 shop-name")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--url", default=DOUYIN_HOME_URL, help="启动后打开的页面 URL")
    parser.add_argument("--login-url", default=DOUYIN_LOGIN_URL, help="首页无法显示时兜底打开的登录页 URL")
    args = parser.parse_args()

    date_range = previous_month()
    if bool(args.start) != bool(args.end):
        raise SystemExit("--start 和 --end 必须同时提供")
    if args.start and args.end:
        date_range = DateRange(parse_date(args.start), parse_date(args.end))

    shop = Shop(
        id=args.shop_id,
        name=args.shop_name,
        sheet_name=args.sheet_name.strip() or args.shop_name,
    )
    output_dir = task_shop_dir(ROOT, date_range, DATA_TYPE_NAME, shop)
    debug_dir = output_dir / "截图" / "抖音保证金接口监听"
    debug_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = debug_dir / f"抖音保证金接口监听_{timestamp}.json"
    screenshot_path = debug_dir / f"抖音保证金接口监听_{timestamp}.png"
    html_path = debug_dir / f"抖音保证金接口监听_{timestamp}.html"

    edge_path = find_edge()
    profile_dir = browser_profile_dir(ROOT, PLATFORM, shop)
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
                text = json.dumps(data, ensure_ascii=False)[:20000]
            elif _looks_like_file_response(response):
                text = "<binary/file response>"
            else:
                text = response.text()[:20000]
                stripped = text.lstrip()
                if stripped.startswith(("{", "[")):
                    data = json.loads(text)
        except Exception as exc:
            item["response_read_error"] = repr(exc)

        item["response_json"] = data
        item["response_text_preview"] = text[:6000]

        haystack = " ".join(
            [
                request.url,
                post_data,
                json.dumps(data, ensure_ascii=False) if data is not None else text,
                json.dumps(response.headers, ensure_ascii=False),
            ]
        )
        if not _looks_like_deposit_api(haystack, item):
            return
        captures.append(item)
        print(f"捕获接口：{request.method} {request.url} status={response.status}")

    def on_download(download: Download) -> None:
        suggested = download.suggested_filename or f"抖音保证金接口监听_{datetime.now():%Y%m%d_%H%M%S}"
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
        new_page.on("response", on_response)
        new_page.on("download", on_download)

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
        for existing_page in context.pages:
            existing_page.on("response", on_response)
            existing_page.on("download", on_download)
        page = _pick_start_page(context.pages) or context.new_page()
        page.bring_to_front()
        page.on("response", on_response)
        page.on("download", on_download)
        response = page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        print(f"打开页面：{args.url} status={response.status if response else 'unknown'}")
        page.wait_for_timeout(2500)
        if page.url == "about:blank":
            print(f"当前标签页仍是 about:blank，改为打开登录页：{args.login_url}")
            response = page.goto(args.login_url, wait_until="domcontentloaded", timeout=60000)
            print(f"打开登录页：{args.login_url} status={response.status if response else 'unknown'}")
            page.wait_for_timeout(2500)
        page.bring_to_front()

        print("")
        print(f"已打开抖音保证金监听环境：{shop.name}")
        print(f"浏览器登录状态目录：{profile_dir}")
        print(f"目标日期：{date_range.start.isoformat()} 至 {date_range.end.isoformat()}")
        print("请手动进入：首页 -> 资金 -> 保证金账户 -> 体验保证金 TAB。")
        print("然后手动设置交易时间、点击“查询”、点击“导出明细”。")
        print("如果页面出现生成报表、下载列表或弹窗，请继续完成直到文件下载或页面提示完成。")
        print("完成后回到这个窗口按 Enter，脚本会保存 JSON、截图和 HTML。")
        input()

        page.wait_for_timeout(3000)
        active_page = _active_page(context.pages)
        try:
            active_page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        try:
            html_path.write_text(active_page.content(), encoding="utf-8")
        except Exception:
            pass
        output = {
            "platform": PLATFORM,
            "platform_name": PLATFORM_NAME,
            "shop_id": shop.id,
            "shop_name": shop.name,
            "date_range": {
                "start": date_range.start.isoformat(),
                "end": date_range.end.isoformat(),
            },
            "manual_steps": [
                "首页 -> 资金 -> 保证金账户 -> 体验保证金 TAB",
                "设置交易时间",
                "点击查询",
                "点击导出明细",
            ],
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
    url = response.url.lower()
    return (
        "application/vnd" in content_type
        or "application/octet-stream" in content_type
        or "application/download" in content_type
        or "attachment" in disposition
        or any(suffix in disposition or suffix in url for suffix in [".xlsx", ".xls", ".csv", ".zip"])
    )


def _looks_like_deposit_api(text: str, item: dict[str, Any]) -> bool:
    lowered = text.lower()
    markers = [
        "fxg",
        "jinritemai",
        "deposit",
        "margin",
        "guarantee",
        "bail",
        "fund",
        "asset",
        "finance",
        "account",
        "flow",
        "bill",
        "transaction",
        "trans",
        "export",
        "download",
        "xlsx",
        "xls",
        "csv",
        "保证金",
        "体验保证金",
        "基础保证金",
        "交易流水",
        "交易时间",
        "交易类型",
        "交易单号",
        "导出明细",
    ]
    if any(marker in lowered for marker in markers):
        return True
    headers = item.get("response_headers") or {}
    content_disposition = headers.get("content-disposition", "")
    return bool(
        content_disposition
        and any(marker in content_disposition.lower() for marker in ["xls", "xlsx", "csv", "zip", "attachment"])
    )


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


def _active_page(pages: list[Page]) -> Page:
    for page in reversed(pages):
        try:
            if not page.is_closed():
                return page
        except Exception:
            continue
    return pages[0]


def _pick_start_page(pages: list[Page]) -> Page | None:
    for page in pages:
        try:
            if not page.is_closed() and page.url == "about:blank":
                return page
        except Exception:
            continue
    for page in pages:
        try:
            if not page.is_closed():
                return page
        except Exception:
            continue
    return None


if __name__ == "__main__":
    main()
