from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
import time
from typing import Callable

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

from exporter.browser import find_edge, PDD_HOME_URL
from exporter.config import Shop
from exporter.dates import DateRange
from exporter.paths import browser_profile_dir, task_shop_dir


@dataclass(frozen=True)
class ReturnsAutomationResult:
    status: str
    message: str
    downloaded_file: Path | None = None


class ReturnsAutomator:
    def __init__(self, project_root: Path, platform: str, platform_name: str) -> None:
        self.project_root = project_root
        self.platform = platform
        self.platform_name = platform_name
        self._user_action_callback: Callable[[str], None] | None = None

    def run(
        self,
        shop: Shop,
        date_range: DateRange,
        *,
        dry_run: bool = True,
        headed: bool = True,
        manual_workbench: bool = False,
        manual_date: bool = False,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> ReturnsAutomationResult:
        self._user_action_callback = user_action_callback
        edge_path = find_edge()
        user_data_dir = browser_profile_dir(self.project_root, self.platform, shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        output_dir = task_shop_dir(self.project_root, date_range, "退货数据", shop)
        output_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=edge_path,
                headless=not headed,
                chromium_sandbox=True,
                accept_downloads=True,
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = self._first_page(context)
                print(f"打开店铺浏览器环境：{shop.name}")
                page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
                print("已打开拼多多后台首页。")
                self._dismiss_page_popups(page)
                self._ensure_logged_in_for_api(page)
                print("开始通过接口查询退货数据。")
                total = self._query_total_by_api(page, date_range)
                print(f"查询结果数量：{total}")

                if total == 0:
                    return ReturnsAutomationResult("无数据", "退货数据查询结果为 0")
                if dry_run:
                    return ReturnsAutomationResult("试运行成功", f"查询到 {total} 条，未点击导出")

                print("开始通过接口生成并下载退货报表。")
                downloaded = self._download_generated_report_by_api(page, output_dir, shop, date_range, total)
                return ReturnsAutomationResult("成功", f"已下载退货数据：{downloaded}", downloaded)
            except Exception:
                screenshot_dir = output_dir / "截图"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot = screenshot_dir / f"退货自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
                try:
                    page.screenshot(path=str(screenshot), full_page=True)
                    print(f"已保存失败截图：{screenshot}")
                except Exception:
                    pass
                print("脚本遇到错误，浏览器将保持打开 60 秒方便查看当前页面。")
                page.wait_for_timeout(60000)
                raise
            finally:
                self._user_action_callback = None
                context.close()

    def _wait_for_user_action(self) -> None:
        message = "请在打开的自动化窗口中完成人工操作，完成后回到 GUI 点击“我已完成人工操作，继续”。"
        callback = self._user_action_callback
        if callback:
            callback(message)
            return
        print(message)
        self._wait_for_user_action()

    def _first_page(self, context: BrowserContext) -> Page:
        if context.pages:
            return context.pages[0]
        return context.new_page()

    def _wait_for_possible_login(self, page: Page) -> None:
        try:
            page.get_by_text("售后管理").first.wait_for(timeout=8000)
            return
        except TimeoutError:
            pass
        print("请在打开的 Edge 窗口中完成拼多多登录。登录完成并进入后台首页后，回到终端按 Enter。")
        input()

    def _ensure_logged_in_for_api(self, page: Page) -> None:
        for _ in range(20):
            if self._is_login_page(page):
                raise RuntimeError(
                    "当前店铺登录状态已失效。请关闭自动化窗口，用 GUI 的“补登录选中店铺”补登录后重跑。"
                )
            try:
                body = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body = ""
            if "售后管理" in body or "店铺管理" in body or "拼多多 商家后台" in body:
                return
            page.wait_for_timeout(500)
        if self._is_login_page(page):
            raise RuntimeError("当前店铺登录状态已失效，请先补登录后重跑。")

    def _is_login_page(self, page: Page) -> bool:
        try:
            url = page.url
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        if "/login" in url:
            return True
        login_hints = ["扫码登录", "账号登录", "请向右滑块完成拼图", "拼多多商家版App扫码登录"]
        return any(item in body for item in login_hints)

    def _returns_api_payload(self, date_range: DateRange) -> dict:
        return {
            "pageSize": 10,
            "pageNumber": 1,
            "mallRemarkStatus": None,
            "mallRemarkTag": None,
            "afterSalesType": 2,
            "afterSalesStatusList": ["5"],
            "sellerAfterSalesShipping": 1,
            "startCreatedTime": self._datetime_seconds(date_range.start, datetime_time(0, 0, 0)),
            "endCreatedTime": self._datetime_seconds(date_range.end, datetime_time(23, 59, 59)),
        }

    def _query_total_by_api(self, page: Page, date_range: DateRange) -> int:
        payload = {
            **self._returns_api_payload(date_range),
            "orderByCreatedAtDesc": True,
        }
        print(
            "通过接口查询退货："
            f"{date_range.start.isoformat()} 00:00:00 ~ {date_range.end.isoformat()} 23:59:59；"
            f"payload={payload}"
        )
        data = self._fetch_json(page, "/mercury/mms/afterSales/queryList", payload, "退货查询")
        result = data.get("result") or {}
        if result.get("total") is None:
            raise RuntimeError(f"退货查询接口缺少 total：{data}")
        return int(result["total"])

    def _download_generated_report_by_api(
        self,
        page: Page,
        output_dir: Path,
        shop: Shop,
        date_range: DateRange,
        total: int,
    ) -> Path:
        target = self._unique_download_path(output_dir, self._returns_filename(shop, date_range))
        export_record = self._find_generated_report_by_api(page, date_range, total, wait_seconds=3)
        if not export_record:
            payload = self._returns_api_payload(date_range)
            print(f"通过接口生成退货报表：payload={payload}")
            self._fetch_json(page, "/mercury/mms/afterSales/applyExcelReport", payload, "退货报表生成")
            export_record = self._find_generated_report_by_api(page, date_range, total, wait_seconds=120)
        if not export_record:
            raise TimeoutError("等待退货接口报表生成超时")
        report_id = export_record.get("id")
        if not report_id:
            raise RuntimeError(f"退货导出列表记录缺少 id：{export_record}")
        url_data = self._fetch_json(
            page,
            "/mercury/mms/afterSales/queryExcelReportUrl",
            {"id": report_id},
            "退货报表下载地址",
        )
        signed_url = url_data.get("result")
        if not signed_url:
            raise RuntimeError(f"退货报表下载地址接口缺少 result：{url_data}")
        print(f"退货接口已返回下载地址：{str(signed_url).split('?')[0]}")
        self._download_signed_url_by_fetch(page, str(signed_url), target)
        return target

    def _find_generated_report_by_api(
        self,
        page: Page,
        date_range: DateRange,
        total: int,
        *,
        wait_seconds: int,
    ) -> dict | None:
        start_seconds = self._datetime_seconds(date_range.start, datetime_time(0, 0, 0))
        end_seconds = self._datetime_seconds(date_range.end, datetime_time(23, 59, 59))
        deadline = time.time() + wait_seconds
        last_status = ""
        while time.time() < deadline:
            data = self._fetch_json(page, "/mercury/mms/afterSales/queryExcelExportList", {}, "退货导出列表")
            records = data.get("result") or []
            for record in records:
                request_obj = record.get("requestObj") or {}
                if (
                    request_obj.get("afterSalesType") == 2
                    and int(request_obj.get("startCreatedTime") or 0) == start_seconds
                    and int(request_obj.get("endCreatedTime") or 0) == end_seconds
                    and int(record.get("exportListCount") or 0) == int(total)
                ):
                    status = record.get("status")
                    last_status = f"id={record.get('id')} status={status} count={record.get('exportListCount')}"
                    if status == 2:
                        print(f"退货命中已生成报表：{last_status}")
                        return record
            page.wait_for_timeout(2000)
        if last_status:
            print(f"退货导出列表最后状态：{last_status}")
        return None

    def _fetch_json(self, page: Page, url: str, payload: dict, label: str) -> dict:
        result = page.evaluate(
            """async ({ url, payload }) => {
                const response = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "content-type": "application/json"
                    },
                    body: JSON.stringify(payload)
                });
                const text = await response.text();
                let data = null;
                try {
                    data = text ? JSON.parse(text) : {};
                } catch (error) {
                    return { ok: false, status: response.status, text };
                }
                return { ok: response.ok, status: response.status, data, text };
            }""",
            {"url": url, "payload": payload},
        )
        if not result.get("ok"):
            raise RuntimeError(f"{label}接口请求失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if not data.get("success", False):
            raise RuntimeError(f"{label}接口返回失败：{data}")
        return data

    def _download_signed_url_by_fetch(self, page: Page, signed_url: str, target: Path) -> None:
        result = page.evaluate(
            """async (url) => {
                const response = await fetch(url, { credentials: "include" });
                if (!response.ok) {
                    return { ok: false, status: response.status, text: await response.text() };
                }
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = "";
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                }
                return {
                    ok: true,
                    status: response.status,
                    contentType: response.headers.get("content-type") || "",
                    contentDisposition: response.headers.get("content-disposition") || "",
                    base64: btoa(binary)
                };
            }""",
            signed_url,
        )
        if not result.get("ok"):
            raise RuntimeError(f"退货文件下载失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        target.write_bytes(base64.b64decode(result["base64"]))

    def _datetime_seconds(self, day: date, value: datetime_time) -> int:
        china = timezone(timedelta(hours=8))
        dt = datetime.combine(day, value, tzinfo=china)
        return int(dt.timestamp())

    def _open_after_sales_workbench(self, page: Page) -> None:
        self._dismiss_page_popups(page)
        # Prefer menu navigation; if the user already navigated there, this is harmless.
        if self._has_text(page, "售后工作台", timeout=2000) and self._has_text(page, "批量导出", timeout=2000):
            return

        for text in ["售后工作台", "售后管理"]:
            try:
                print(f"尝试点击菜单：{text}")
                self._dismiss_page_popups(page)
                page.get_by_text(text, exact=False).first.click(timeout=5000)
                page.wait_for_timeout(800)
                if self._has_text(page, "售后工作台", timeout=2000) and self._has_text(page, "批量导出", timeout=2000):
                    return
            except TimeoutError:
                print(f"未找到或无法点击：{text}")
                continue

        if not self._has_text(page, "售后工作台", timeout=5000) and not self._has_text(page, "批量导出", timeout=5000):
            print("未能自动进入售后工作台。请在浏览器中手动进入：售后管理 -> 售后工作台，然后回到终端按 Enter。")
            self._wait_for_user_action()

    def _set_filters(self, page: Page, date_range: DateRange, *, manual_date: bool = False) -> None:
        self._dismiss_page_popups(page)
        self._expand_advanced_search(page)
        self._dismiss_page_popups(page)
        self._select_by_label(page, "售后类型", "退款退货")
        self._select_by_label(page, "售后状态", "退款成功")
        self._select_by_label(page, "发货状态", "已发货")
        self._fill_date_range(page, date_range, manual_date=manual_date)

    def _expand_advanced_search(self, page: Page) -> None:
        for text in ["展开高级查询", "收起高级查询"]:
            try:
                locator = page.get_by_text(text, exact=False).first
                if locator.is_visible(timeout=2000):
                    if "展开" in text:
                        locator.click()
                    return
            except TimeoutError:
                continue

    def _select_by_label(self, page: Page, label_text: str, option_text: str) -> None:
        self._dismiss_page_popups(page)
        label = page.get_by_text(label_text, exact=True).first
        try:
            box = label.locator("xpath=following::*[contains(@class, 'select') or self::input][1]")
            box.click(timeout=4000)
        except TimeoutError:
            page.get_by_text(label_text, exact=False).locator("xpath=..").click(timeout=4000)
        page.get_by_text(option_text, exact=True).last.click(timeout=5000)

    def _fill_date_range(self, page: Page, date_range: DateRange, *, manual_date: bool = False) -> None:
        self._dismiss_page_popups(page)
        value = f"{date_range.start.isoformat()} 00:00:00 ~ {date_range.end.isoformat()} 23:59:59"
        label = page.get_by_text("申请时间", exact=True).first
        date_input = label.locator("xpath=following::input[1]")
        date_input.wait_for(state="visible", timeout=5000)
        date_input.click(timeout=5000)
        if manual_date:
            print(f"请在浏览器日期控件中手动选择申请时间：{value}")
            print("选好日期并点击日期控件里的“确认”后，回到终端按 Enter。脚本会校验日期，不正确不会继续查询。")
            self._wait_for_user_action()
        else:
            print(f"自动选择申请时间：{value}")
            try:
                self._pick_date_by_visible_month(page, date_range.start, prefer_panel=0)
                self._pick_date_by_visible_month(page, date_range.end, prefer_panel=1)
                self._click_date_picker_confirm(page)
            except Exception as exc:
                print(f"自动选择日期失败，切换为手动选择：{exc}")
                print(f"请在浏览器日期控件中手动选择申请时间：{value}")
                print("选好日期并点击日期控件里的“确认”后，回到终端按 Enter。脚本会校验日期，不正确不会继续查询。")
                self._wait_for_user_action()
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        current_value = date_input.input_value(timeout=5000).strip()
        if date_range.start.isoformat() not in current_value or date_range.end.isoformat() not in current_value:
            raise RuntimeError(
                "申请时间未正确设置，已停止查询，避免导出全部数据。"
                f" 期望：{value}；当前页面显示：{current_value or '空'}"
            )

    def _pick_date_by_visible_month(self, page: Page, target: date, *, prefer_panel: int) -> None:
        target_month = target.year * 12 + target.month
        panel_months = [self._calendar_panel_month(page, index) for index in range(2)]
        exact_panels = [index for index, month in enumerate(panel_months) if month == target_month]
        if exact_panels:
            panel_index = prefer_panel if prefer_panel in exact_panels else exact_panels[0]
            self._pick_date_in_panel(page, panel_index, target, allow_out_of_month=False)
            return

        near_panels = [
            index
            for index, month in enumerate(panel_months)
            if month is not None and abs(month - target_month) == 1
        ]
        if near_panels:
            panel_index = prefer_panel if prefer_panel in near_panels else near_panels[0]
            self._pick_date_in_panel(page, panel_index, target, allow_out_of_month=True)
            return

        panel_index = prefer_panel if panel_months[prefer_panel] is not None else 0
        self._set_calendar_panel_month(page, panel_index, target)
        self._pick_date_in_panel(page, panel_index, target, allow_out_of_month=False)

    def _pick_date_in_panel(
        self,
        page: Page,
        panel_index: int,
        target: date,
        *,
        allow_out_of_month: bool | None = None,
    ) -> None:
        current_month = self._calendar_panel_month(page, panel_index)
        target_month = target.year * 12 + target.month
        if allow_out_of_month is None:
            allow_out_of_month = current_month is not None and abs(current_month - target_month) == 1
        if current_month != target_month and not allow_out_of_month:
            self._set_calendar_panel_month(page, panel_index, target)
            allow_out_of_month = False

        click_point = page.evaluate(
            """({ panelIndex, day, allowOutOfMonth }) => {
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
                const wrappers = Array.from(document.querySelectorAll("[class*='RPR_tableWrapper']"))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    });
                const wrapper = wrappers[panelIndex];
                if (!wrapper) return false;
                const cells = Array.from(wrapper.querySelectorAll("[role='date-cell']"))
                    .filter(visible)
                    .filter((cell) => {
                        if (allowOutOfMonth) return true;
                        return !String(cell.className || "").includes("RPR_outOfMonth");
                    });
                const cell = cells.find((item) => {
                    const inner = item.querySelector("[class*='RPR_cell']");
                    const text = ((inner && (inner.innerText || inner.textContent)) || item.innerText || item.textContent || "").trim();
                    const title = inner ? (inner.getAttribute("title") || "") : "";
                    return text === String(day) || title === String(day);
                });
                if (!cell) return false;
                const target = cell.querySelector("[class*='RPR_cell']") || cell;
                const rect = target.getBoundingClientRect();
                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
            }""",
            {"panelIndex": panel_index, "day": target.day, "allowOutOfMonth": allow_out_of_month},
        )
        if not click_point:
            self._set_calendar_panel_month(page, panel_index, target)
            click_point = page.evaluate(
                """({ panelIndex, day }) => {
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
                    const wrappers = Array.from(document.querySelectorAll("[class*='RPR_tableWrapper']"))
                        .filter(visible)
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    const wrapper = wrappers[panelIndex];
                    if (!wrapper) return false;
                    const cells = Array.from(wrapper.querySelectorAll("[role='date-cell']"))
                        .filter(visible)
                        .filter((cell) => !String(cell.className || "").includes("RPR_outOfMonth"));
                    const cell = cells.find((item) => {
                        const inner = item.querySelector("[class*='RPR_cell']");
                        const text = ((inner && (inner.innerText || inner.textContent)) || item.innerText || item.textContent || "").trim();
                        const title = inner ? (inner.getAttribute("title") || "") : "";
                        return text === String(day) || title === String(day);
                    });
                    if (!cell) return false;
                    const target = cell.querySelector("[class*='RPR_cell']") || cell;
                    const rect = target.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                {"panelIndex": panel_index, "day": target.day},
            )
        if not click_point:
            raise TimeoutError(f"未能在第 {panel_index + 1} 个日期面板点击 {target.isoformat()}")
        page.mouse.click(click_point["x"], click_point["y"])
        page.wait_for_timeout(300)

    def _calendar_panel_month(self, page: Page, panel_index: int) -> int | None:
        return page.evaluate(
            """(panelIndex) => {
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
                const panels = Array.from(document.querySelectorAll("[class*='RPR_panelHeader']"))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    });
                const panel = panels[panelIndex];
                if (!panel) return null;
                const values = Array.from(panel.querySelectorAll("input"))
                    .map((item) => item.value || "")
                    .filter(Boolean);
                const yearText = values.find((value) => /20\\d{2}/.test(value)) || "";
                const monthText = (
                    values.find((value) => /月/.test(value))
                    || ((panel.querySelector("[class*='RPR_dateText']") || {}).innerText || "")
                    || (panel.innerText || "")
                );
                const year = Number((yearText.match(/20\\d{2}/) || [])[0]);
                const month = Number((monthText.match(/\\d{1,2}/) || [])[0]);
                if (!year || !month) return null;
                return year * 12 + month;
            }""",
            panel_index,
        )

    def _set_calendar_panel_month(self, page: Page, panel_index: int, target: date) -> None:
        target_month = target.year * 12 + target.month
        for _ in range(18):
            current_month = page.evaluate(
                """(panelIndex) => {
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
                    const panels = Array.from(document.querySelectorAll("[class*='RPR_panelHeader']"))
                        .filter(visible)
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    const panel = panels[panelIndex];
                    if (!panel) return null;
                    const values = Array.from(panel.querySelectorAll("input"))
                        .map((item) => item.value || "")
                        .filter(Boolean);
                    const yearText = values.find((value) => /20\\d{2}/.test(value)) || "";
                    const monthText = (
                        values.find((value) => /月/.test(value))
                        || ((panel.querySelector("[class*='RPR_dateText']") || {}).innerText || "")
                        || (panel.innerText || "")
                    );
                    const year = Number((yearText.match(/20\\d{2}/) || [])[0]);
                    const month = Number((monthText.match(/\\d{1,2}/) || [])[0]);
                    if (!year || !month) return null;
                    return year * 12 + month;
                }""",
                panel_index,
            )
            if current_month == target_month:
                return
            if current_month is None:
                break
            direction = "next" if current_month < target_month else "prev"
            click_point = page.evaluate(
                """({ panelIndex, direction }) => {
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
                    const panels = Array.from(document.querySelectorAll("[class*='RPR_panelHeader']"))
                        .filter(visible)
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    const panel = panels[panelIndex];
                    if (!panel) return false;
                    const icons = Array.from(panel.querySelectorAll("svg,[role='button'],button"))
                        .filter(visible);
                    const icon = icons.find((item) => {
                        const testid = item.getAttribute("data-testid") || "";
                        const className = String(item.className || "");
                        if (direction === "prev") {
                            return testid.includes("left") || /prev|left/i.test(className);
                        }
                        return testid.includes("right") || /next|right/i.test(className);
                    });
                    if (!icon) {
                        const rect = panel.getBoundingClientRect();
                        return {
                            x: direction === "prev" ? rect.left + 12 : rect.right - 12,
                            y: rect.top + rect.height / 2
                        };
                    }
                    const rect = icon.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                {"panelIndex": panel_index, "direction": direction},
            )
            if not click_point:
                break
            page.mouse.click(click_point["x"], click_point["y"])
            page.wait_for_timeout(600)
        raise TimeoutError(f"未能将第 {panel_index + 1} 个日期面板切换到 {target.year}-{target.month:02d}")

    def _pick_date_in_open_picker(self, page: Page, target: date) -> None:
        for _ in range(8):
            if self._click_calendar_day(page, target):
                page.wait_for_timeout(300)
                return
            visible_months = self._visible_calendar_months(page)
            target_month = target.year * 12 + target.month
            if visible_months and target_month < min(visible_months):
                if not self._click_calendar_nav(page, "prev"):
                    break
            else:
                if not self._click_calendar_nav(page, "next"):
                    break
            page.wait_for_timeout(300)
        raise TimeoutError(f"未能在日期控件中自动选择日期：{target.isoformat()}")

    def _click_calendar_day(self, page: Page, target: date) -> bool:
        return bool(
            page.evaluate(
                """({ year, month, day }) => {
                    const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
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
                    const isDisabled = (el) => {
                        let node = el;
                        while (node && node !== document.body) {
                            const className = String(node.className || "");
                            const ariaDisabled = node.getAttribute && node.getAttribute("aria-disabled");
                            if (/disabled|not-allowed/i.test(className) || ariaDisabled === "true") return true;
                            node = node.parentElement;
                        }
                        return false;
                    };

                    const attrMatch = Array.from(document.querySelectorAll("[title],[aria-label],[data-date],[data-value]"))
                        .find((el) => {
                            if (!visible(el) || isDisabled(el)) return false;
                            const attrs = [
                                el.getAttribute("title"),
                                el.getAttribute("aria-label"),
                                el.getAttribute("data-date"),
                                el.getAttribute("data-value")
                            ].filter(Boolean).join(" ");
                            return attrs.includes(iso);
                        });
                    if (attrMatch) {
                        attrMatch.click();
                        return true;
                    }

                    const panelInfo = (el) => {
                        let node = el.parentElement;
                        for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
                            const text = node.innerText || "";
                            if (text.includes(`${year}年`) && text.includes(`${month}月`)) {
                                return { ok: true, area: node.getBoundingClientRect() };
                            }
                        }
                        return { ok: false, area: null };
                    };
                    const elements = Array.from(document.querySelectorAll("td,button,span,div"));
                    let candidates = elements.filter((el) => {
                        const text = (el.innerText || el.textContent || "").trim();
                        return text === String(day) && visible(el) && !isDisabled(el) && panelInfo(el).ok;
                    });
                    if (!candidates.length) {
                        const headers = Array.from(document.querySelectorAll("div,span,button"))
                            .filter((el) => visible(el))
                            .map((el) => {
                                const rect = el.getBoundingClientRect();
                                const text = (el.innerText || el.textContent || "").trim();
                                return { el, rect, text };
                            });
                        const yearHeaders = headers.filter((item) => item.text === `${year}年`);
                        const monthHeaders = headers.filter((item) => item.text === `${month}月`);
                        const pairs = [];
                        for (const y of yearHeaders) {
                            for (const m of monthHeaders) {
                                if (Math.abs(y.rect.top - m.rect.top) < 80) {
                                    pairs.push({
                                        left: Math.min(y.rect.left, m.rect.left) - 160,
                                        right: Math.max(y.rect.right, m.rect.right) + 180,
                                        top: Math.max(y.rect.bottom, m.rect.bottom),
                                        bottom: Math.max(y.rect.bottom, m.rect.bottom) + 320
                                    });
                                }
                            }
                        }
                        candidates = elements.filter((el) => {
                            const text = (el.innerText || el.textContent || "").trim();
                            if (text !== String(day) || !visible(el) || isDisabled(el)) return false;
                            const rect = el.getBoundingClientRect();
                            const cx = rect.left + rect.width / 2;
                            const cy = rect.top + rect.height / 2;
                            return pairs.some((area) => cx >= area.left && cx <= area.right && cy >= area.top && cy <= area.bottom);
                        });
                    }
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    });
                    const target = candidates[0];
                    target.click();
                    return true;
                }""",
                {"year": target.year, "month": target.month, "day": target.day},
            )
        )

    def _visible_calendar_months(self, page: Page) -> list[int]:
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
                        && rect.bottom >= 0
                        && rect.left <= window.innerWidth
                        && rect.top <= window.innerHeight;
                };
                const months = new Set();
                for (const el of Array.from(document.querySelectorAll("div,span,button"))) {
                    if (!visible(el)) continue;
                    const text = (el.innerText || el.textContent || "").trim();
                    const match = text.match(/(20\\d{2})年\\s*([1-9]|1[0-2])月/);
                    if (match) {
                        months.add(Number(match[1]) * 12 + Number(match[2]));
                    }
                    const looseMatch = text.match(/(20\\d{2})年[\\s\\S]{0,120}?([1-9]|1[0-2])月/);
                    if (looseMatch) {
                        months.add(Number(looseMatch[1]) * 12 + Number(looseMatch[2]));
                    }
                }
                return Array.from(months);
            }"""
        )

    def _click_calendar_nav(self, page: Page, direction: str) -> bool:
        return bool(
            page.evaluate(
                """(direction) => {
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
                    const wanted = direction === "prev" ? ["‹", "<", "上一月"] : ["›", ">", "下一月"];
                    const candidates = Array.from(document.querySelectorAll("button,span,i,div,[role='button']"))
                        .filter(el => visible(el))
                        .filter(el => {
                            const text = (el.innerText || el.textContent || "").trim();
                            const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                            const className = String(el.className || "");
                            if (wanted.includes(text) || wanted.includes(label)) return true;
                            if (direction === "prev" && /prev|left/i.test(className)) return true;
                            if (direction === "next" && /next|right/i.test(className)) return true;
                            return false;
                        });
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (direction === "prev" ? ar.left - br.left : br.left - ar.left);
                    });
                    candidates[0].click();
                    return true;
                }""",
                direction,
            )
        )

    def _click_date_picker_confirm(self, page: Page) -> None:
        for label in ["确认", "确定"]:
            try:
                page.get_by_text(label, exact=True).last.click(timeout=3000)
                return
            except TimeoutError:
                continue
        raise TimeoutError("未找到日期控件确认按钮")

    def _query_and_get_total(self, page: Page) -> int:
        self._dismiss_page_popups(page)
        page.get_by_role("button", name="查询").last.click(timeout=5000)
        page.wait_for_timeout(1500)
        if self._has_text(page, "暂无售后单", timeout=2000):
            return 0
        total_text = self._first_matching_text(page, r"全部\((\d+)\)")
        if total_text is not None:
            return int(total_text)
        total_footer = self._first_matching_text(page, r"共有\s*(\d+)\s*条")
        if total_footer is not None:
            return int(total_footer)
        return -1

    def _generate_report(self, page: Page) -> None:
        self._dismiss_page_popups(page)
        page.get_by_text("批量导出", exact=True).last.click(timeout=5000)
        page.get_by_text("确认生成", exact=True).click(timeout=5000)

    def _download_generated_report(
        self,
        page: Page,
        output_dir: Path,
        shop: Shop,
        date_range: DateRange,
        total: int,
    ) -> Path:
        page.get_by_text("查看已生成报表", exact=True).click(timeout=5000)
        deadline = time.time() + 90
        start_text = date_range.start.isoformat()
        end_text = date_range.end.isoformat()
        total_text = str(total)
        while time.time() < deadline:
            try:
                row = (
                    page.locator("tr")
                    .filter(has_text="生成成功")
                    .filter(has_text=start_text)
                    .filter(has_text=end_text)
                    .filter(has_text=total_text)
                    .first
                )
                if row.count() > 0 and row.is_visible(timeout=500):
                    export_link = row.get_by_text("导出", exact=True).last
                    print("检测到本次退货报表已生成成功，开始下载。")
                    with page.expect_download(timeout=30000) as download_info:
                        export_link.click(timeout=5000)
                    download = download_info.value
                    target = self._unique_download_path(output_dir, self._returns_filename(shop, date_range))
                    download.save_as(target)
                    return target
            except TimeoutError:
                pass
            page.wait_for_timeout(500)
        raise TimeoutError("等待退货报表生成超时")

    def _returns_filename(self, shop: Shop, date_range: DateRange) -> str:
        return f"{date_range.range_key}{shop.sheet_name}退货.xlsx"

    def _unique_download_path(self, output_dir: Path, filename: str) -> Path:
        target = output_dir / filename
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(1, 100):
            candidate = output_dir / f"{stem}_{index}{suffix}"
            if not candidate.exists():
                return candidate
        return output_dir / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}{suffix}"

    def _dismiss_page_popups(self, page: Page) -> None:
        for _ in range(4):
            target = page.evaluate(
                """() => {
                    const protectedTexts = [
                        "批量导出报表",
                        "近30个已生成报表",
                        "确认生成",
                        "生成成功",
                        "导出"
                    ];
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
                    const hasProtectedAncestor = (el) => {
                        let node = el;
                        while (node && node !== document.body) {
                            const text = (node.innerText || "").slice(0, 500);
                            if (protectedTexts.some(item => text.includes(item))) {
                                return true;
                            }
                            node = node.parentElement;
                        }
                        return false;
                    };
                    const scoreElement = (el) => {
                        if (!visible(el) || hasProtectedAncestor(el)) return -1;
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || "").trim();
                        const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                        const className = String(el.className || "");
                        let score = 0;
                        if (/^(×|x|X|关闭|關閉)$/.test(text)) score += 8;
                        if (/关闭|close/i.test(label)) score += 8;
                        if (/close|modal-close|dialog-close/i.test(className)) score += 6;
                        if (["BUTTON", "A", "I", "SPAN", "DIV"].includes(el.tagName)) score += 1;
                        if (rect.width <= 80 && rect.height <= 80) score += 3;
                        const style = window.getComputedStyle(el);
                        const zIndex = Number.parseInt(style.zIndex || "0", 10);
                        if (Number.isFinite(zIndex) && zIndex >= 10) score += 2;
                        const cx = rect.left + rect.width / 2;
                        const cy = rect.top + rect.height / 2;
                        if (cx > window.innerWidth * 0.35 && cx < window.innerWidth * 0.8 && cy < window.innerHeight * 0.8) score += 1;
                        return score;
                    };
                    const elements = Array.from(document.querySelectorAll("button,a,i,span,div,[role='button']"));
                    const candidates = elements
                        .map(el => ({ el, score: scoreElement(el) }))
                        .filter(item => item.score >= 8)
                        .sort((a, b) => b.score - a.score);
                    if (!candidates.length) return null;
                    const rect = candidates[0].el.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }"""
            )
            if not target:
                return
            page.mouse.click(target["x"], target["y"])
            page.wait_for_timeout(500)

    def _has_text(self, page: Page, text: str, timeout: int = 1000) -> bool:
        try:
            page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _first_matching_text(self, page: Page, pattern: str) -> str | None:
        import re

        body = page.locator("body").inner_text(timeout=5000)
        match = re.search(pattern, body)
        return match.group(1) if match else None
