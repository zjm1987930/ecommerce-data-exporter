from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import unquote, urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_HOME_URL, find_edge
from exporter.config import Shop
from exporter.dates import DateRange
from exporter.paths import browser_profile_dir, export_filename, task_screenshot_dir, task_shop_dir
from exporter.workbooks import (
    DELAYED_TAB_ORDER,
    merge_delayed_shipping_files,
)


DELAYED_TAB_LABELS = {
    "delayed_shipping": "延迟发货",
    "out_of_stock": "缺货",
    "fake_shipping": "虚假发货/虚假轨迹",
}

DELAYED_TAB_ALIASES = {
    "delayed": "delayed_shipping",
    "delay": "delayed_shipping",
    "delayed_shipping": "delayed_shipping",
    "延迟发货": "delayed_shipping",
    "out_of_stock": "out_of_stock",
    "stockout": "out_of_stock",
    "缺货": "out_of_stock",
    "fake": "fake_shipping",
    "fake_shipping": "fake_shipping",
    "虚假发货": "fake_shipping",
    "虚假发货/虚假轨迹": "fake_shipping",
}

DELAYED_TAB_API_CONFIG = {
    "延迟发货": {
        "query": "/genji/poppy/overtimeShipment/query/chargeList",
        "export": "/genji/poppy/overtimeShipment/export/chargeList",
        "export_list": "/genji/poppy/overtimeShipment/query/exportChargeList",
    },
    "缺货": {
        "query": "/genji/poppy/stockout/query/stockoutList",
        "export": "/genji/poppy/stockout/export/stockoutList",
        "export_list": "/genji/poppy/stockout/query/exportStockoutList",
    },
    "虚假发货/虚假轨迹": {
        "query": "/genji/poppy/fakeShipment/query/fakeShipmentList",
        "export": "/genji/poppy/fakeShipment/export/fakeShipmentList",
        "export_list": "/genji/poppy/fakeShipment/query/exportfakeShipmentList",
    },
}


@dataclass(frozen=True)
class DelayedShippingTabResult:
    tab: str
    total: int
    status: str
    downloaded_file: Path | None = None


@dataclass(frozen=True)
class DelayedShippingResult:
    status: str
    message: str
    output_file: Path | None = None
    tab_results: list[DelayedShippingTabResult] | None = None


class LoginRequiredError(RuntimeError):
    pass


class DelayedShippingAutomator:
    def __init__(self, project_root: Path, platform: str, platform_name: str) -> None:
        self.project_root = project_root
        self.platform = platform
        self.platform_name = platform_name
        self._common = ReturnsAutomator(project_root, platform, platform_name)
        self._user_action_callback: Callable[[str], None] | None = None

    def run_shop(
        self,
        shop: Shop,
        date_range: DateRange,
        *,
        tabs: list[str] | None = None,
        dry_run: bool = True,
        headed: bool = True,
        manual_page: bool = False,
        manual_date: bool = False,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> DelayedShippingResult:
        self._user_action_callback = user_action_callback
        selected_tabs = self._normalize_tabs(tabs)
        edge_path = find_edge()
        user_data_dir = browser_profile_dir(self.project_root, self.platform, shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        output_dir = task_shop_dir(self.project_root, date_range, "延迟发货数据", shop)
        raw_dir = output_dir / "延迟发货_平台原始文件"
        raw_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir = task_screenshot_dir(self.project_root, date_range, "延迟发货数据", shop)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = self._launch_shop_context(playwright, edge_path, user_data_dir, headed=headed)
            page = self._first_page(context)
            try:
                print(f"打开店铺浏览器环境：{shop.name}")
                page.goto(PDD_HOME_URL, wait_until="domcontentloaded")
                self._common._dismiss_page_popups(page)
                self._ensure_logged_in(page)
                if manual_page:
                    print("请在浏览器中手动进入：账户资金 -> 消费者补偿。进入后回到终端按 Enter。")
                    self._wait_for_user_action()
                else:
                    self._open_consumer_compensation(page)
                self._wait_for_compensation_page(page)
                self._common._dismiss_page_popups(page)

                tab_results: list[DelayedShippingTabResult] = []
                files_by_tab: dict[str, Path] = {}
                for tab in selected_tabs:
                    print("")
                    print(f"开始处理 TAB：{tab}")
                    self._open_tab(page, tab)
                    if self._supports_query_api(tab):
                        total = self._query_and_get_total_by_api(page, tab, date_range)
                    else:
                        self._set_compensation_time(page, date_range, manual_date=manual_date)
                        total = self._query_and_get_total(page)
                    print(f"{tab} 查询结果：{total} 条")
                    if total == 0:
                        tab_results.append(DelayedShippingTabResult(tab, total, "无数据"))
                        continue
                    if dry_run:
                        tab_results.append(DelayedShippingTabResult(tab, total, "试运行成功"))
                        continue
                    if self._supports_query_api(tab):
                        downloaded = self._download_generated_report_by_api(
                            page,
                            raw_dir,
                            tab,
                            date_range,
                            wait_seconds=3,
                            allow_missing=True,
                        )
                        if not downloaded:
                            self._generate_report_by_api(page, tab, date_range)
                            downloaded = self._download_generated_report_by_api(page, raw_dir, tab, date_range)
                    else:
                        self._generate_report(page, tab)
                        downloaded = self._download_generated_report(page, raw_dir, tab, date_range)
                    files_by_tab[tab] = downloaded
                    tab_results.append(DelayedShippingTabResult(tab, total, "成功", downloaded))

                output_file: Path | None = None
                if not dry_run:
                    output_file = self._output_path(output_dir, shop, date_range)
                    merge_delayed_shipping_files(files_by_tab, output_file)
                    print(f"已整合延迟发货工作簿：{output_file}")

                failed = [item for item in tab_results if item.status == "失败"]
                if failed:
                    status = "部分失败"
                elif dry_run:
                    status = "试运行成功"
                else:
                    status = "成功"
                message = self._summary_message(tab_results, output_file=output_file, dry_run=dry_run)
                return DelayedShippingResult(status, message, output_file, tab_results)
            except Exception:
                screenshot = screenshot_dir / f"延迟发货自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
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
                try:
                    context.close()
                except Exception:
                    pass

    def _wait_for_user_action(self) -> None:
        message = "请在打开的自动化窗口中完成人工操作，完成后回到 GUI 点击“我已完成人工操作，继续”。"
        callback = self._user_action_callback
        if callback:
            callback(message)
            return
        print(message)
        input()

    def _launch_shop_context(self, playwright, edge_path: str, user_data_dir: Path, *, headed: bool) -> BrowserContext:
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=edge_path,
                headless=not headed,
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
        except Exception as exc:
            if not self._is_edge_profile_startup_error(exc):
                raise
            raise RuntimeError(
                "Edge 浏览器环境启动失败。请关闭该店铺对应的普通 Edge 登录窗口后，单独重跑该店铺。"
            ) from exc

    def _is_edge_profile_startup_error(self, exc: Exception) -> bool:
        text = str(exc)
        return (
            "Target page, context or browser has been closed" in text
            or "ProcessSingleton" in text
            or "profile is already in use" in text
        )

    def _first_page(self, context: BrowserContext) -> Page:
        if context.pages:
            return context.pages[0]
        return context.new_page()

    def _normalize_tabs(self, tabs: list[str] | None) -> list[str]:
        if not tabs:
            return [DELAYED_TAB_ORDER[0]]
        normalized: list[str] = []
        for tab in tabs:
            key = DELAYED_TAB_ALIASES.get(tab, tab)
            label = DELAYED_TAB_LABELS.get(key, tab)
            if label not in DELAYED_TAB_ORDER:
                available = ", ".join(DELAYED_TAB_ORDER)
                raise ValueError(f"未知 TAB：{tab}。可选：{available}")
            if label not in normalized:
                normalized.append(label)
        return normalized

    def _supports_query_api(self, tab: str) -> bool:
        return tab in DELAYED_TAB_API_CONFIG

    def _ensure_logged_in(self, page: Page) -> None:
        for _ in range(20):
            if self._is_login_page(page):
                raise LoginRequiredError(
                    "当前店铺登录状态已失效。请关闭自动化窗口，用普通 Edge 补登录后重跑："
                    "powershell -ExecutionPolicy Bypass -File .\\login_shop.ps1 --shop-id 对应店铺ID"
                )
            try:
                body = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body = ""
            if "账户资金" in body or "售后管理" in body or "店铺管理" in body:
                return
            page.wait_for_timeout(500)
        if self._is_login_page(page):
            raise LoginRequiredError("当前店铺登录状态已失效，请先用 login_shop.ps1 普通 Edge 补登录。")

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

    def _open_consumer_compensation(self, page: Page) -> None:
        if self._is_compensation_page(page):
            return
        self._common._dismiss_page_popups(page)
        try:
            print("尝试点击菜单：消费者补偿")
            page.get_by_text("消费者补偿", exact=False).first.click(timeout=6000)
            page.wait_for_timeout(1000)
            if self._is_compensation_page(page):
                return
        except TimeoutError:
            print("未找到或无法点击：消费者补偿")

        try:
            print("消费者补偿入口未显示，尝试展开菜单大类：账户资金")
            page.get_by_text("账户资金", exact=False).first.click(timeout=4000)
            page.wait_for_timeout(800)
            page.get_by_text("消费者补偿", exact=False).first.click(timeout=6000)
            page.wait_for_timeout(1000)
            if self._is_compensation_page(page):
                return
        except TimeoutError:
            print("展开账户资金后仍未能点击消费者补偿")
        if not self._is_compensation_page(page):
            print("未能自动进入消费者补偿页面。请手动进入：账户资金 -> 消费者补偿，然后回到终端按 Enter。")
            self._wait_for_user_action()

    def _wait_for_compensation_page(self, page: Page) -> None:
        for _ in range(30):
            if self._is_login_page(page):
                raise LoginRequiredError("当前店铺登录状态已失效，请先用 login_shop.ps1 普通 Edge 补登录。")
            if self._is_compensation_page(page):
                return
            self._common._dismiss_page_popups(page)
            page.wait_for_timeout(500)
        raise TimeoutError("未能进入消费者补偿页面")

    def _is_compensation_page(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return "消费者补偿" in body and "补偿记录查询" in body and "补偿时间" in body

    def _open_tab(self, page: Page, tab: str) -> None:
        self._common._dismiss_page_popups(page)
        candidates = [tab]
        if tab == "虚假发货/虚假轨迹":
            candidates = ["虚假发货/虚假轨迹", "虚假发货", "虚假轨迹"]
        for text in candidates:
            for attempt in range(2):
                try:
                    self._common._dismiss_page_popups(page)
                    page.get_by_text(text, exact=True).first.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return
                except TimeoutError:
                    if attempt == 0:
                        self._common._dismiss_page_popups(page)
                        page.wait_for_timeout(500)
                        continue
                    break
        raise TimeoutError(f"未能打开 TAB：{tab}")

    def _set_compensation_time(self, page: Page, date_range: DateRange, *, manual_date: bool = False) -> None:
        self._common._dismiss_page_popups(page)
        expected = f"{date_range.start.isoformat()} ~ {date_range.end.isoformat()}"
        start_input, end_input = self._compensation_time_inputs(page)
        if manual_date:
            print(f"请手动选择补偿时间：{expected}。选完开始和结束日期后回到终端按 Enter。")
            self._wait_for_user_action()
        else:
            print(f"自动选择补偿时间：{expected}")
            try:
                start_input.click(timeout=5000)
                self._pick_date_by_visible_month(page, date_range.start)
                self._set_open_picker_time(page, "00:00:00")
                self._click_date_picker_confirm(page)
                self._wait_for_single_date_value(start_input, date_range.start)
                end_input.click(timeout=5000)
                self._pick_date_by_visible_month(page, date_range.end)
                self._set_open_picker_time(page, "23:59:59")
                self._click_date_picker_confirm(page)
                self._wait_for_single_date_value(end_input, date_range.end)
            except Exception as picker_exc:
                print(f"自动选择补偿时间失败，切换为手动选择：{picker_exc}")
                print(f"请手动选择补偿时间：{expected}。选完后回到终端按 Enter。")
                self._wait_for_user_action()
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        start_value = start_input.input_value(timeout=5000).strip()
        end_value = end_input.input_value(timeout=5000).strip()
        if date_range.start.isoformat() not in start_value or date_range.end.isoformat() not in end_value:
            raise RuntimeError(
                "补偿时间未正确设置，已停止查询，避免导出错误区间。"
                f" 期望：{expected}；当前页面显示：{start_value or '空'} ~ {end_value or '空'}"
            )
        if "00:00:00" not in start_value or "23:59:59" not in end_value:
            raise RuntimeError(
                "补偿时间的时分秒未正确设置，已停止查询，避免导出错误区间。"
                f" 期望：{date_range.start.isoformat()} 00:00:00 ~ {date_range.end.isoformat()} 23:59:59；"
                f"当前页面显示：{start_value or '空'} ~ {end_value or '空'}"
            )

    def _fill_datetime_input(self, locator, value: str) -> None:
        locator.click(timeout=5000)
        locator.evaluate(
            """(input, value) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                setter.call(input, value);
                for (const type of ["input", "change", "blur"]) {
                    input.dispatchEvent(new Event(type, { bubbles: true }));
                }
            }""",
            value,
        )
        locator.page.keyboard.press("Tab")
        locator.page.wait_for_timeout(300)
        current = locator.input_value(timeout=1000).strip()
        if value not in current:
            locator.click(timeout=5000)
            locator.press("Control+A")
            locator.type(value, delay=20)
            locator.page.keyboard.press("Tab")
            locator.page.wait_for_timeout(300)
            current = locator.input_value(timeout=1000).strip()
        if value not in current:
            raise RuntimeError(f"输入框未接受日期时间：期望 {value}，当前 {current or '空'}")

    def _nudge_date_input_to_target(self, locator, target, time_value: str) -> None:
        from datetime import datetime

        current = locator.input_value(timeout=1000).strip()
        current_dt = self._parse_datetime_value(current)
        if current_dt is None or current_dt.date() == target:
            return
        delta_days = (target - current_dt.date()).days
        if abs(delta_days) > 7:
            return
        print(f"日期点选偏移，尝试按天修正：当前 {current}，目标 {target.isoformat()}，偏移 {delta_days} 天")
        locator.click(timeout=5000)
        for _ in range(abs(delta_days)):
            key = "ArrowLeft" if delta_days < 0 else "ArrowRight"
            locator.page.keyboard.press(key)
            locator.page.wait_for_timeout(150)
        self._set_open_picker_time(locator.page, time_value)
        self._click_date_picker_confirm(locator.page)
        fixed = locator.input_value(timeout=1000).strip()
        fixed_dt = self._parse_datetime_value(fixed)
        if fixed_dt is None or fixed_dt.date() != target:
            raise RuntimeError(f"日期修正失败：期望 {target.isoformat()}，当前 {fixed or '空'}")

    def _parse_datetime_value(self, value: str):
        from datetime import datetime

        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _compensation_time_inputs(self, page: Page):
        handles = page.locator("input").element_handles()
        candidates: list[tuple[int, object]] = []
        for index, handle in enumerate(handles):
            try:
                info = handle.evaluate(
                    """(input) => {
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
                        if (!visible(input)) return null;
                        const value = input.value || "";
                        const placeholder = input.getAttribute("placeholder") || "";
                        const rect = input.getBoundingClientRect();
                        return { value, placeholder, x: rect.left, y: rect.top };
                    }"""
                )
            except Exception:
                continue
            if not info:
                continue
            value = str(info.get("value") or "")
            placeholder = str(info.get("placeholder") or "")
            if re.search(r"20\d{2}-\d{2}-\d{2}", value) or "开始" in placeholder or "结束" in placeholder:
                candidates.append((index, handle))
        if len(candidates) < 2:
            raise TimeoutError("未能识别补偿时间的开始/结束输入框")
        first = page.locator("input").nth(candidates[0][0])
        second = page.locator("input").nth(candidates[1][0])
        return first, second

    def _pick_date_by_visible_month(self, page: Page, target) -> None:
        for _ in range(18):
            if self._open_picker_month(page) == target.year * 12 + target.month:
                if self._click_open_picker_day(page, target.day):
                    page.wait_for_timeout(350)
                    return
                raise TimeoutError(f"当前日期面板已是目标月份，但未找到日期：{target.isoformat()}")
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

    def _open_picker_month(self, page: Page) -> int | None:
        value = page.evaluate(
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
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return null;
                const header = Array.from(root.querySelectorAll("[class*='RPR_panelHeader']")).filter(visible).pop();
                if (!header) return null;
                const yearText = Array.from(header.querySelectorAll("input")).map((input) => input.value || "").join(" ");
                const monthText = ((header.querySelector("[class*='RPR_dateText']") || {}).innerText || "");
                const year = Number((yearText.match(/20\\d{2}/) || [])[0]);
                const month = Number((monthText.match(/([1-9]|1[0-2])月/) || [])[1]);
                if (!year || !month) return null;
                return year * 12 + month;
            }"""
        )
        return int(value) if value is not None else None

    def _click_open_picker_day(self, page: Page, day: int) -> bool:
        if not self._click_open_picker_day_cell(page, day):
            return False
        for _ in range(12):
            page.wait_for_timeout(150)
            if self._open_picker_active_day(page) == str(day):
                return True
        return False

    def _click_open_picker_day_cell(self, page: Page, day: int) -> bool:
        return bool(page.evaluate(
            """(day) => {
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
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return null;
                const table = Array.from(root.querySelectorAll("[class*='RPR_tableWrapper']")).filter(visible).pop();
                if (!table) return null;
                const cells = Array.from(table.querySelectorAll("[role='date-cell']"))
                    .filter(visible)
                    .filter((cell) => {
                        const className = String(cell.className || "");
                        if (/disabled|not-allowed/i.test(className) || className.includes("RPR_outOfMonth")) return false;
                        const inner = cell.querySelector("[class*='RPR_cell']") || cell;
                        const text = (inner.innerText || inner.textContent || "").trim();
                        const title = inner.getAttribute("title") || "";
                        return text === String(day) && title === String(day);
                    });
                if (cells.length !== 1) {
                    return false;
                }
                const cell = cells[0];
                const inner = cell.querySelector("[class*='RPR_cell']") || cell;
                inner.scrollIntoView({ block: "center", inline: "center" });
                inner.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, view: window }));
                inner.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
                inner.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
                inner.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                return true;
            }""",
            day,
        ))

    def _open_picker_active_day(self, page: Page) -> str:
        value = page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width > 0
                        && rect.height > 0;
                };
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return "";
                const active = Array.from(root.querySelectorAll("[role='date-cell']"))
                    .filter(visible)
                    .find((cell) => String(cell.className || "").includes("RPR_active"));
                if (!active) return "";
                const inner = active.querySelector("[class*='RPR_cell']") || active;
                return (inner.innerText || inner.textContent || "").trim();
            }"""
        )
        return str(value or "")

    def _visible_calendar_months(self, page: Page) -> list[int]:
        return page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width > 0
                        && rect.height > 0;
                };
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return [];
                const months = new Set();
                for (const el of Array.from(root.querySelectorAll("[class*='RPR_panelHeader']"))) {
                    if (!visible(el)) continue;
                    const values = Array.from(el.querySelectorAll("input")).map((input) => input.value || "").join(" ");
                    const monthText = ((el.querySelector("[class*='RPR_dateText']") || {}).innerText || "");
                    const text = `${values} ${monthText}`;
                    const match = text.match(/(20\\d{2})年[\\s\\S]{0,40}?([1-9]|1[0-2])月/);
                    if (match) months.add(Number(match[1]) * 12 + Number(match[2]));
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
                            && rect.height > 0;
                    };
                    const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                        .filter(visible)
                        .pop();
                    if (!root) return false;
                    const wanted = direction === "prev" ? "beast-core-icon-left" : "beast-core-icon-right";
                    const direct = Array.from(root.querySelectorAll(`svg[data-testid='${wanted}']`)).filter(visible)[0];
                    if (direct) {
                        direct.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        return true;
                    }
                    const candidates = Array.from(root.querySelectorAll("button,span,i,div,[role='button'],svg"))
                        .filter((el) => visible(el))
                        .filter((el) => {
                            const text = (el.innerText || el.textContent || "").trim();
                            const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                            const className = String(el.className || "");
                            if (direction === "prev") return text === "<" || /prev|left|上一个月/i.test(label + " " + className);
                            return text === ">" || /next|right|下一个月/i.test(label + " " + className);
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (direction === "prev" ? ar.left - br.left : br.left - ar.left);
                        });
                    if (!candidates.length) return false;
                    candidates[0].click();
                    return true;
                }""",
                direction,
            )
        )

    def _click_date_picker_confirm(self, page: Page) -> None:
        for label in ["确定", "确认"]:
            try:
                page.get_by_text(label, exact=True).last.click(timeout=3000)
                page.wait_for_timeout(300)
                return
            except TimeoutError:
                continue
        raise TimeoutError("未找到日期控件确定按钮")

    def _set_open_picker_time(self, page: Page, value: str) -> None:
        ok = page.evaluate(
            """(value) => {
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
                const inputs = Array.from(document.querySelectorAll("input"))
                    .filter(visible)
                    .filter((input) => /^\\d{2}:\\d{2}:\\d{2}$/.test(input.value || ""));
                const input = inputs[inputs.length - 1];
                if (!input) return false;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                setter.call(input, value);
                for (const type of ["input", "change", "blur"]) {
                    input.dispatchEvent(new Event(type, { bubbles: true }));
                }
                return true;
            }""",
            value,
        )
        if not ok:
            raise TimeoutError(f"未能设置日期控件时间：{value}")
        page.wait_for_timeout(200)
        current = self._open_picker_time_value(page)
        if current == value:
            return
        raise RuntimeError(f"日期控件时间未正确设置：期望 {value}，当前 {current or '空'}")

    def _open_picker_time_value(self, page: Page) -> str:
        value = page.evaluate(
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
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return "";
                const input = Array.from(root.querySelectorAll("input"))
                    .filter(visible)
                    .find((item) => /^\\d{2}:\\d{2}:\\d{2}$/.test(item.value || ""));
                return input ? input.value : "";
            }"""
        )
        return str(value or "")

    def _select_open_picker_time(self, page: Page, value: str) -> None:
        hour, minute, second = value.split(":")
        opened = page.evaluate(
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
                const root = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot']"))
                    .filter(visible)
                    .pop();
                if (!root) return false;
                const input = Array.from(root.querySelectorAll("input"))
                    .filter(visible)
                    .find((item) => /^\\d{2}:\\d{2}:\\d{2}$/.test(item.value || ""));
                if (!input) return false;
                input.click();
                return true;
            }"""
        )
        if not opened:
            raise TimeoutError("未能打开时间选择控件")
        page.wait_for_timeout(500)
        if self._click_time_panel_values(page, [hour, minute, second]):
            page.wait_for_timeout(300)
            return
        raise TimeoutError(f"未能在时间选择控件中选择 {value}")

    def _click_time_panel_values(self, page: Page, values: list[str]) -> bool:
        return bool(
            page.evaluate(
                """(values) => {
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
                    const candidates = Array.from(document.querySelectorAll("li,div,span"))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || "").trim();
                            return { el, text, rect };
                        })
                        .filter((item) => /^\\d{2}$/.test(item.text));
                    if (!candidates.length) return false;
                    const columns = [];
                    for (const item of candidates) {
                        let column = columns.find((col) => Math.abs(col.x - item.rect.left) < 12);
                        if (!column) {
                            column = { x: item.rect.left, items: [] };
                            columns.push(column);
                        }
                        column.items.push(item);
                    }
                    columns.sort((a, b) => a.x - b.x);
                    const timeColumns = columns.filter((col) => col.items.length >= 10).slice(-3);
                    if (timeColumns.length < 3) return false;
                    for (let i = 0; i < 3; i += 1) {
                        const wanted = values[i];
                        const column = timeColumns[i];
                        const item = column.items
                            .filter((entry) => entry.text === wanted)
                            .sort((a, b) => a.rect.top - b.rect.top)[0];
                        if (!item) return false;
                        item.el.scrollIntoView({ block: "center" });
                        item.el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    }
                    return true;
                }""",
                values,
            )
        )

    def _wait_for_single_date_value(self, date_input, expected_date) -> None:
        expected = expected_date.isoformat()
        for _ in range(20):
            current_value = date_input.input_value(timeout=1000).strip()
            if expected in current_value:
                return
            date_input.page.wait_for_timeout(250)
        current_value = date_input.input_value(timeout=1000).strip()
        raise RuntimeError(
            "补偿时间选择后没有自动写入输入框。"
            f" 期望包含：{expected}；当前：{current_value or '空'}"
        )

    def _query_and_get_total(self, page: Page) -> int:
        self._common._dismiss_page_popups(page)
        page.get_by_role("button", name="查询").last.click(timeout=5000)
        page.wait_for_timeout(1800)
        body = page.locator("body").inner_text(timeout=5000)
        match = re.search(r"共查询到\s*(\d+)\s*条数据", body)
        if match:
            return int(match.group(1))
        match = re.search(r"共有\s*(\d+)\s*条", body)
        if match:
            return int(match.group(1))
        if "没有查询到相应的补偿记录" in body or "没有查询到相应的数据" in body:
            return 0
        raise TimeoutError("未能读取延迟发货查询结果总数")

    def _query_and_get_total_by_api(self, page: Page, tab: str, date_range: DateRange) -> int:
        config = DELAYED_TAB_API_CONFIG.get(tab)
        if not config:
            raise RuntimeError(f"{tab} 暂未接入接口查询")
        self._common._dismiss_page_popups(page)
        payload = {
            "endTime": self._datetime_ms(date_range.end, datetime_time(23, 59, 59)),
            "orderSn": "",
            "pageNum": 1,
            "pageSize": 10,
            "startTime": self._datetime_ms(date_range.start, datetime_time(0, 0, 0)),
        }
        print(
            f"通过接口查询{tab}：北京时间 "
            f"{self._datetime_local_text(date_range.start, datetime_time(0, 0, 0))} ~ "
            f"{self._datetime_local_text(date_range.end, datetime_time(23, 59, 59))}；"
            f"payload={payload}"
        )
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
                    data = JSON.parse(text);
                } catch (error) {
                    return { ok: false, status: response.status, text };
                }
                return { ok: response.ok, status: response.status, data };
            }""",
            {"url": config["query"], "payload": payload},
        )
        if not result.get("ok"):
            raise RuntimeError(f"{tab}接口查询失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if not data.get("success"):
            raise RuntimeError(f"{tab}接口返回失败：{data}")
        total = ((data.get("result") or {}).get("total"))
        if total is None:
            raise RuntimeError(f"{tab}接口响应缺少 total：{data}")
        return int(total)

    def _generate_report_by_api(self, page: Page, tab: str, date_range: DateRange) -> None:
        config = DELAYED_TAB_API_CONFIG.get(tab)
        if not config:
            raise RuntimeError(f"{tab} 暂未接入接口导出")
        self._common._dismiss_page_popups(page)
        payload = {
            "startTime": self._datetime_utc_iso(date_range.start, datetime_time(0, 0, 0)),
            "endTime": self._datetime_utc_iso(date_range.end, datetime_time(23, 59, 59)),
        }
        print(
            f"通过接口生成{tab}报表：北京时间 "
            f"{self._datetime_local_text(date_range.start, datetime_time(0, 0, 0))} ~ "
            f"{self._datetime_local_text(date_range.end, datetime_time(23, 59, 59))}；"
            f"接口UTC payload={payload}"
        )
        data = self._fetch_json(
            page,
            config["export"],
            payload,
            f"{tab}报表生成",
        )
        if data and not data.get("success", True):
            raise RuntimeError(f"{tab}报表生成接口返回失败：{data}")
        page.wait_for_timeout(1000)

    def _download_generated_report_by_api(
        self,
        page: Page,
        raw_dir: Path,
        tab: str,
        date_range: DateRange,
        *,
        wait_seconds: int = 120,
        allow_missing: bool = False,
    ) -> Path | None:
        config = DELAYED_TAB_API_CONFIG.get(tab)
        if not config:
            raise RuntimeError(f"{tab} 暂未接入接口下载")
        start_ms = self._datetime_ms(date_range.start, datetime_time(0, 0, 0))
        end_ms = self._datetime_ms(date_range.end, datetime_time(23, 59, 59))
        deadline = time.time() + wait_seconds
        last_status = ""
        while time.time() < deadline:
            self._common._dismiss_page_popups(page)
            data = self._fetch_json(
                page,
                config["export_list"],
                {"pageNum": 1, "pageSize": 10},
                f"{tab}导出列表",
            )
            if not data.get("success"):
                raise RuntimeError(f"{tab}导出列表接口返回失败：{data}")
            records = ((data.get("result") or {}).get("data") or [])
            matched = self._matching_export_record(records, start_ms, end_ms)
            if matched:
                status = matched.get("exportStatus")
                matched_start = matched.get("punishStartTime")
                matched_end = matched.get("punishEndTime")
                last_status = (
                    f"exportStatus={status}, "
                    f"补偿时间={self._ms_local_text(matched_start)} ~ {self._ms_local_text(matched_end)}, "
                    f"errorMsg={matched.get('errorMsg')}"
                )
                if status == 1 and matched.get("exportUrl"):
                    print(f"{tab} 命中导出报表：{last_status}")
                    return self._download_url_by_api(page, raw_dir, tab, matched["exportUrl"])
                if matched.get("errorMsg"):
                    raise RuntimeError(f"{tab}报表生成失败：{matched.get('errorMsg')}")
            page.wait_for_timeout(2000)
        if allow_missing:
            return None
        raise TimeoutError(f"等待 {tab} 导出列表生成超时：{last_status}")

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
        return result.get("data") or {}

    def _matching_export_record(self, records: list[dict], start_ms: int, end_ms: int) -> dict | None:
        for record in records:
            if record.get("punishStartTime") == start_ms and record.get("punishEndTime") == end_ms:
                return record
        return None

    def _download_url_by_api(self, page: Page, raw_dir: Path, tab: str, url: str) -> Path:
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
            url,
        )
        if not result.get("ok"):
            raise RuntimeError(f"{tab} 文件下载失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        filename = self._filename_from_download(url, result.get("contentDisposition") or "", tab)
        target = self._unique_download_path(raw_dir, filename)
        target.write_bytes(base64.b64decode(result["base64"]))
        print(f"{tab} 原始文件已下载：{target}")
        return target

    def _datetime_ms(self, day, value: datetime_time) -> int:
        china = timezone(timedelta(hours=8))
        dt = datetime.combine(day, value, tzinfo=china)
        return int(dt.timestamp() * 1000)

    def _datetime_local_text(self, day, value: datetime_time) -> str:
        china = timezone(timedelta(hours=8))
        dt = datetime.combine(day, value, tzinfo=china)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _ms_local_text(self, value) -> str:
        if not isinstance(value, int):
            return str(value)
        china = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(value / 1000, china).strftime("%Y-%m-%d %H:%M:%S")

    def _datetime_utc_iso(self, day, value: datetime_time) -> str:
        china = timezone(timedelta(hours=8))
        dt = datetime.combine(day, value, tzinfo=china).astimezone(timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _generate_report(self, page: Page, tab: str) -> None:
        self._common._dismiss_page_popups(page)
        page.get_by_text("批量导出", exact=True).last.click(timeout=5000)
        self._wait_for_export_dialog(page, tab)
        page.get_by_text("生成报表", exact=True).last.click(timeout=5000)
        page.wait_for_timeout(1500)

    def _wait_for_export_dialog(self, page: Page, tab: str) -> None:
        expected = {
            "延迟发货": "导出延迟发货补偿订单明细",
            "缺货": "导出缺货补偿订单明细",
            "虚假发货/虚假轨迹": "导出虚假发货补偿订单明细",
        }[tab]
        try:
            page.get_by_text(expected, exact=False).first.wait_for(timeout=5000)
        except TimeoutError:
            page.get_by_text("生成报表", exact=True).first.wait_for(timeout=5000)

    def _download_generated_report(self, page: Page, raw_dir: Path, tab: str, date_range: DateRange) -> Path:
        deadline = time.time() + 120
        while time.time() < deadline:
            self._common._dismiss_page_popups(page)
            try:
                row = self._matching_export_row(page, date_range)
                if row and row.is_visible(timeout=500):
                    download_button = row.get_by_text("下载补偿订单列表", exact=True).last
                    with page.expect_download(timeout=30000) as download_info:
                        download_button.click(timeout=5000)
                    download = download_info.value
                    suggested = download.suggested_filename or f"{tab}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
                    target = self._unique_download_path(raw_dir, suggested)
                    download.save_as(target)
                    print(f"{tab} 原始文件已下载：{target}")
                    return target
            except TimeoutError:
                pass
            self._click_refresh_if_available(page)
            page.wait_for_timeout(1500)
        raise TimeoutError(f"等待 {tab} 批量导出列表生成并下载超时")

    def _matching_export_row(self, page: Page, date_range: DateRange):
        start_text = date_range.start.isoformat()
        end_text = date_range.end.isoformat()
        rows = page.locator("tr").filter(has_text="下载补偿订单列表")
        preferred = rows.filter(has_text=start_text).filter(has_text=end_text).first
        if preferred.count() > 0:
            return preferred
        return rows.first if rows.count() > 0 else None

    def _click_refresh_if_available(self, page: Page) -> None:
        try:
            page.get_by_role("button", name="刷新").last.click(timeout=1000)
        except TimeoutError:
            return

    def _filename_from_download(self, url: str, content_disposition: str, tab: str) -> str:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', content_disposition, flags=re.IGNORECASE)
        if match:
            filename = unquote(match.group(1).strip())
        else:
            filename = unquote(Path(urlparse(url).path).name)
        if not filename:
            filename = f"{tab}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        if not filename.lower().endswith(".xlsx"):
            filename = f"{filename}.xlsx"
        return filename

    def _unique_download_path(self, raw_dir: Path, suggested: str) -> Path:
        target = raw_dir / suggested
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(1, 100):
            candidate = raw_dir / f"{stem}_{index}{suffix}"
            if not candidate.exists():
                return candidate
        return raw_dir / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}{suffix}"

    def _output_path(self, output_dir: Path, shop: Shop, date_range: DateRange) -> Path:
        return output_dir / export_filename(shop, date_range, "延迟发货", ".xlsx")

    def _summary_message(
        self,
        tab_results: list[DelayedShippingTabResult],
        *,
        output_file: Path | None,
        dry_run: bool,
    ) -> str:
        parts = [f"{item.tab}:{item.total}条/{item.status}" for item in tab_results]
        if dry_run:
            return "；".join(parts)
        return f"{'；'.join(parts)}；已生成：{output_file}"
