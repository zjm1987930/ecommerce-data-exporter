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

from exporter.automation.kuaishou_deposit import KUAISHOU_HOME_URL, KUAISHOU_PROFILE_ID
from exporter.browser import find_edge
from exporter.config import Shop
from exporter.paths import browser_profile_dir


PLATFORM = "kuaishou"
DEBUG_DIR = ROOT / "原始数据" / "导出日志" / "快手切店接口监听"


def main() -> None:
    parser = argparse.ArgumentParser(description="监听快手右上角切换店铺接口")
    parser.add_argument("--profile-id", default=KUAISHOU_PROFILE_ID)
    parser.add_argument("--url", default=KUAISHOU_HOME_URL)
    args = parser.parse_args()

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = DEBUG_DIR / f"快手切店接口监听_{timestamp}.json"
    screenshot_path = DEBUG_DIR / f"快手切店接口监听_{timestamp}.png"
    html_path = DEBUG_DIR / f"快手切店接口监听_{timestamp}.html"

    profile_shop = Shop(
        id=args.profile_id,
        name="快手默认账号",
        sheet_name="快手默认账号",
        platform=PLATFORM,
        platform_name="快手",
    )
    profile_dir = browser_profile_dir(ROOT, PLATFORM, profile_shop)
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
        if not _looks_like_shop_switch_api(haystack, item):
            return
        captures.append(item)
        print(f"捕获接口：{request.method} {request.url} status={response.status}")

    def on_download(download: Download) -> None:
        downloads.append({"suggested_filename": download.suggested_filename or "", "url": download.url})

    def on_page(new_page: Page) -> None:
        page_events.append({"event": "page", "url": new_page.url})
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
            executable_path=find_edge(),
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
        for page_item in context.pages:
            page_item.on("response", on_response)
            page_item.on("download", on_download)
        page = _pick_start_page(context.pages) or context.new_page()
        page.bring_to_front()
        page.on("response", on_response)
        page.on("download", on_download)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        print("")
        print(f"已打开快手切店监听环境：{profile_dir}")
        print("请手动操作：右上角店铺入口 -> 切换店铺/身份 -> 选择另一家店铺。")
        print("建议本次切换：逸观图书旗舰店 <-> 大雁图书。")
        print("确认页面切换到新店铺后，回到这个窗口按 Enter。")
        input()
        page.wait_for_timeout(3000)

        active_page = _active_page(context.pages)
        dom_snapshot = _capture_dom_snapshot(active_page)
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
            "profile_id": args.profile_id,
            "captures": captures,
            "downloads": downloads,
            "page_events": page_events,
            "open_pages": [{"url": item.url, "title": _safe_title(item)} for item in context.pages],
            "dom_snapshot": dom_snapshot,
        }
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存接口调试 JSON：{output_path}")
        print(f"已保存截图：{screenshot_path}")
        print(f"已保存 HTML：{html_path}")
        print(f"捕获接口数量：{len(captures)}")
        context.close()


def _looks_like_file_response(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    disposition = response.headers.get("content-disposition", "").lower()
    return "application/octet-stream" in content_type or "attachment" in disposition


def _looks_like_shop_switch_api(text: str, item: dict[str, Any]) -> bool:
    lowered = text.lower()
    markers = [
        "switch",
        "shop",
        "seller",
        "merchant",
        "account",
        "identity",
        "role",
        "tenant",
        "submch",
        "sub_mch",
        "mchid",
        "mch_id",
        "userrole",
        "shopid",
        "shop_id",
        "shopname",
        "shop_name",
        "kwai",
        "kuaixiaodian",
        "切换店铺",
        "选择店铺",
        "店铺名称",
        "店铺id",
        "大雁图书",
        "逸观图书",
        "金喆图书",
    ]
    if any(marker in lowered for marker in markers):
        return True
    headers = item.get("response_headers") or {}
    return bool(headers.get("location") and "shop" in headers.get("location", "").lower())


def _capture_dom_snapshot(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
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
                const nodes = Array.from(document.querySelectorAll("div,span,button,a,[role='button']"))
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
                    .filter((item) => /图书|店铺|切换|身份|账号|大雁|逸观|金喆|资金|账单/.test(item.text))
                    .slice(0, 300);
                const storage = {};
                for (const source of [window.localStorage, window.sessionStorage]) {
                    for (let i = 0; i < source.length; i += 1) {
                        const key = source.key(i);
                        const value = source.getItem(key) || "";
                        if (/shop|seller|merchant|account|identity|role|mch|店铺|图书|大雁|逸观|金喆/i.test(key + value)) {
                            storage[key] = value;
                        }
                    }
                }
                return { url: location.href, title: document.title, bodyText: document.body.innerText, nodes, storage };
            }"""
        )
    except Exception as exc:
        return {"error": repr(exc)}


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
