from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
import base64
from typing import Callable

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

from exporter.automation.returns import ReturnsAutomator
from exporter.browser import PDD_CUSTOMER_SERVICE_PERFORMANCE_URL, find_edge
from exporter.config import Shop
from exporter.dates import DateRange
from exporter.paths import browser_profile_dir, export_filename, task_screenshot_dir, task_shop_dir


@dataclass(frozen=True)
class CustomerServicePerformanceResult:
    status: str
    message: str
    downloaded_file: Path | None = None


class LoginRequiredError(RuntimeError):
    pass


class CustomerServicePerformanceAutomator:
    def __init__(self, project_root: Path, platform: str, platform_name: str) -> None:
        self.project_root = project_root
        self.platform = platform
        self.platform_name = platform_name
        self._common = ReturnsAutomator(project_root, platform, platform_name)
        self._user_action_callback: Callable[[str], None] | None = None

    def run(
        self,
        shop: Shop,
        date_range: DateRange,
        *,
        dry_run: bool = False,
        headed: bool = True,
        manual_date: bool = False,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> CustomerServicePerformanceResult:
        self._user_action_callback = user_action_callback
        self._validate_date_range(date_range)
        edge_path = find_edge()
        user_data_dir = browser_profile_dir(self.project_root, self.platform, shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        output_dir = task_shop_dir(self.project_root, date_range, "客服绩效数据", shop)
        output_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir = task_screenshot_dir(self.project_root, date_range, "客服绩效数据", shop)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = self._launch_shop_context(playwright, edge_path, user_data_dir, headed=headed)
            page = self._first_page(context)
            try:
                print(f"打开店铺浏览器环境：{shop.name}")
                page.goto(PDD_CUSTOMER_SERVICE_PERFORMANCE_URL, wait_until="domcontentloaded")
                self._common._dismiss_page_popups(page)
                self._wait_for_performance_page(page)
                self._common._dismiss_page_popups(page)
                self._open_performance_tab(page)
                self._wait_for_performance_detail(page)
                if dry_run:
                    total = self._query_download_url_by_api(page, date_range)
                    return CustomerServicePerformanceResult("试运行成功", f"客服绩效接口可用：{total[:120]}")

                print("开始通过接口下载客服绩效表单。")
                downloaded = self._download_form_by_api(page, output_dir, shop, date_range)
                return CustomerServicePerformanceResult("成功", f"已下载客服绩效数据：{downloaded}", downloaded)
            except Exception:
                screenshot = screenshot_dir / f"客服绩效自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
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

    def _validate_date_range(self, date_range: DateRange) -> None:
        today = date.today()
        earliest = today - timedelta(days=60)
        if date_range.start < earliest:
            raise RuntimeError(
                "客服绩效数据最多只能选择近60天。"
                f" 当前开始日期 {date_range.start.isoformat()} 早于可选最早日期 {earliest.isoformat()}，"
                "请缩短日期范围后重跑。"
            )
        if date_range.end > today:
            raise RuntimeError(
                "客服绩效数据不能选择未来日期。"
                f" 当前结束日期 {date_range.end.isoformat()} 晚于今天 {today.isoformat()}。"
            )

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

    def _wait_for_performance_page(self, page: Page) -> None:
        for _ in range(30):
            if self._is_login_page(page):
                raise LoginRequiredError(
                    "当前店铺登录状态已失效。请关闭自动化窗口，用 GUI 的“补登录选中店铺”补登录后重跑。"
                )
            if self._is_performance_page(page):
                return
            self._common._dismiss_page_popups(page)
            page.wait_for_timeout(500)
        raise TimeoutError("未能进入客服数据页面")

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

    def _is_performance_page(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return "客服数据" in body and ("客服绩效数据" in body or "服务数据" in body)

    def _open_performance_tab(self, page: Page) -> None:
        try:
            page.get_by_text("客服绩效数据", exact=True).first.click(timeout=5000)
            page.wait_for_timeout(1000)
        except TimeoutError:
            if not self._has_text(page, "客服绩效详情", timeout=2000):
                raise

    def _wait_for_performance_detail(self, page: Page) -> None:
        for _ in range(30):
            if self._is_login_page(page):
                raise LoginRequiredError("当前店铺登录状态已失效，请先补登录后重跑。")
            if self._has_text(page, "客服绩效详情", timeout=500):
                return
            page.wait_for_timeout(500)
        raise TimeoutError("未能进入客服绩效数据 TAB")

    def _set_date_range(self, page: Page, date_range: DateRange, *, manual_date: bool = False) -> None:
        self._common._dismiss_page_popups(page)
        expected = f"{date_range.start.isoformat()} ~ {date_range.end.isoformat()}"
        date_input = self._performance_date_input(page)
        date_input.click(timeout=5000)
        if manual_date:
            print(f"请手动选择客服绩效详情时间范围：{expected}，点“确认”后回到 GUI 点击继续。")
            self._wait_for_user_action()
        else:
            try:
                self._pick_date_by_visible_month(page, date_range.start)
                self._pick_date_by_visible_month(page, date_range.end)
                self._click_date_picker_confirm(page)
            except Exception as exc:
                print(f"自动选择客服绩效日期失败，切换为手动选择：{exc}")
                print(f"请手动选择客服绩效详情时间范围：{expected}，点“确认”后回到 GUI 点击继续。")
                self._wait_for_user_action()
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        current = date_input.input_value(timeout=5000).strip()
        if date_range.start.isoformat() not in current or date_range.end.isoformat() not in current:
            raise RuntimeError(
                "客服绩效详情时间范围未正确设置，已停止下载，避免导出错误区间。"
                f" 期望：{expected}；当前页面显示：{current or '空'}"
            )

    def _performance_date_input(self, page: Page):
        index = page.evaluate(
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
                const inputs = Array.from(document.querySelectorAll("input")).filter(visible);
                const dated = inputs
                    .map((input, index) => ({ input, index, value: input.value || "", rect: input.getBoundingClientRect() }))
                    .filter((item) => /20\\d{2}-\\d{2}-\\d{2}\\s*~\\s*20\\d{2}-\\d{2}-\\d{2}/.test(item.value));
                if (!dated.length) return -1;
                const anchors = Array.from(document.querySelectorAll("div,span"))
                    .filter(visible)
                    .map((el) => ({ text: el.innerText || el.textContent || "", rect: el.getBoundingClientRect() }))
                    .filter((item) => item.text.includes("客服绩效详情"));
                if (!anchors.length) return dated[dated.length - 1].index;
                const anchor = anchors[anchors.length - 1].rect;
                const below = dated
                    .filter((item) => item.rect.top >= anchor.top)
                    .sort((a, b) => (a.rect.top - b.rect.top) || (a.rect.left - b.rect.left));
                return (below[0] || dated[dated.length - 1]).index;
            }"""
        )
        if index is None or int(index) < 0:
            raise TimeoutError("未找到客服绩效详情日期范围输入框")
        return page.locator("input").nth(int(index))

    def _pick_date_by_visible_month(self, page: Page, target_date: date) -> None:
        for _ in range(10):
            point = page.evaluate(
                """({ year, month, day }) => {
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
                    const disabled = (el) => {
                        let node = el;
                        while (node && node !== document.body) {
                            const className = String(node.className || "");
                            const ariaDisabled = node.getAttribute && node.getAttribute("aria-disabled");
                            if (/disabled|not-allowed/i.test(className) || ariaDisabled === "true") return true;
                            node = node.parentElement;
                        }
                        return false;
                    };
                    const roots = Array.from(document.querySelectorAll("[data-testid='beast-core-datePicker-dropdown-contentRoot'],[class*='datePicker'],[class*='DatePicker'],[class*='RPR_root']"))
                        .filter(visible);
                    const root = roots.length ? roots[roots.length - 1] : document.body;
                    const headers = Array.from(root.querySelectorAll("div,span,input,button"))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.value || el.innerText || el.textContent || "").trim();
                            return { el, rect, text };
                        });
                    const yearItems = headers.filter((item) => item.text.includes(`${year}`));
                    const monthItems = headers.filter((item) => item.text === `${month}月` || item.text === String(month));
                    const areas = [];
                    for (const y of yearItems) {
                        for (const m of monthItems) {
                            if (Math.abs(y.rect.top - m.rect.top) < 80) {
                                areas.push({
                                    left: Math.min(y.rect.left, m.rect.left) - 180,
                                    right: Math.max(y.rect.right, m.rect.right) + 220,
                                    top: Math.max(y.rect.bottom, m.rect.bottom),
                                    bottom: Math.max(y.rect.bottom, m.rect.bottom) + 340
                                });
                            }
                        }
                    }
                    const cells = Array.from(root.querySelectorAll("td,button,span,div"))
                        .filter(visible)
                        .filter((el) => !disabled(el))
                        .filter((el) => {
                            const text = (el.innerText || el.textContent || "").trim();
                            if (text !== String(day)) return false;
                            if (!areas.length) return true;
                            const rect = el.getBoundingClientRect();
                            const cx = rect.left + rect.width / 2;
                            const cy = rect.top + rect.height / 2;
                            return areas.some((area) => cx >= area.left && cx <= area.right && cy >= area.top && cy <= area.bottom);
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    if (!cells.length) return null;
                    const rect = cells[0].getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                {"year": target_date.year, "month": target_date.month, "day": target_date.day},
            )
            if point:
                page.mouse.click(point["x"], point["y"])
                page.wait_for_timeout(350)
                return

            visible_months = self._visible_calendar_months(page)
            target_month = target_date.year * 12 + target_date.month
            if visible_months and target_month < min(visible_months):
                self._click_calendar_nav(page, "prev")
            else:
                self._click_calendar_nav(page, "next")
            page.wait_for_timeout(500)
        raise TimeoutError(f"未能在客服绩效日期控件中点击 {target_date.isoformat()}")

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
                const texts = Array.from(document.querySelectorAll("div,span,input,button"))
                    .filter(visible)
                    .map((el) => (el.value || el.innerText || el.textContent || "").trim())
                    .filter(Boolean);
                const years = texts.map((text) => (text.match(/20\\d{2}/) || [])[0]).filter(Boolean).map(Number);
                const months = texts.map((text) => (text.match(/([1-9]|1[0-2])月/) || [])[1]).filter(Boolean).map(Number);
                const result = new Set();
                for (const year of years) {
                    for (const month of months) {
                        result.add(year * 12 + month);
                    }
                }
                return Array.from(result);
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
                    const candidates = Array.from(document.querySelectorAll("button,span,i,div,[role='button']"))
                        .filter(visible)
                        .filter((el) => {
                            const text = (el.innerText || el.textContent || "").trim();
                            const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                            const className = String(el.className || "");
                            if (direction === "prev") {
                                return text === "<" || text === "‹" || /prev|left/i.test(label + " " + className);
                            }
                            return text === ">" || text === "›" || /next|right/i.test(label + " " + className);
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return direction === "prev" ? ar.left - br.left : br.left - ar.left;
                        });
                    if (!candidates.length) return false;
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
        raise TimeoutError("未找到客服绩效日期控件确认按钮")

    def _wait_for_table_ready(self, page: Page) -> None:
        for _ in range(30):
            body = page.locator("body").inner_text(timeout=1000)
            if "下载表单" in body and ("客服账号" in body or "店铺总计" in body):
                return
            page.wait_for_timeout(500)
        raise TimeoutError("客服绩效表格未在预期时间内刷新完成")

    def _download_form(self, page: Page, output_dir: Path, shop: Shop, date_range: DateRange) -> Path:
        self._common._dismiss_page_popups(page)
        target = self._unique_download_path(output_dir, self._filename(shop, date_range))
        button = page.get_by_text("下载表单", exact=True).last
        with page.expect_download(timeout=30000) as download_info:
            button.click(timeout=5000)
        download = download_info.value
        download.save_as(target)
        return target

    def _download_form_by_api(self, page: Page, output_dir: Path, shop: Shop, date_range: DateRange) -> Path:
        signed_url = self._query_download_url_by_api(page, date_range)
        print(f"客服绩效接口已返回下载地址：{signed_url.split('?')[0]}")
        target = self._unique_download_path(output_dir, self._filename(shop, date_range))
        try:
            self._download_signed_url_by_fetch(page, signed_url, target)
            return target
        except Exception as exc:
            print(f"接口文件流下载失败，回退浏览器下载事件：{exc}")
            return self._download_signed_url_by_browser(page, signed_url, target)

    def _query_download_url_by_api(self, page: Page, date_range: DateRange) -> str:
        start_seconds = self._datetime_seconds(date_range.start)
        end_seconds = self._datetime_seconds(date_range.end)
        url = (
            "/chats/csReportDetail/download"
            f"?starttime={start_seconds}"
            f"&endtime={end_seconds}"
            "&csRemoveRefundSalesAmountGray=true&"
        )
        print(
            "通过接口生成客服绩效下载地址："
            f"{date_range.start.isoformat()} 00:00:00 ~ {date_range.end.isoformat()} 00:00:00；"
            f"url={url}"
        )
        result = page.evaluate(
            """async (url) => {
                const response = await fetch(url, {
                    method: "GET",
                    credentials: "include"
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
            url,
        )
        if not result.get("ok"):
            raise RuntimeError(f"客服绩效下载地址接口请求失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if not data.get("success"):
            raise RuntimeError(f"客服绩效下载地址接口返回失败：{data}")
        signed_url = data.get("result")
        if not signed_url:
            raise RuntimeError(f"客服绩效下载地址接口缺少 result：{data}")
        return str(signed_url)

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
            raise RuntimeError(f"客服绩效文件下载失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        target.write_bytes(base64.b64decode(result["base64"]))

    def _download_signed_url_by_browser(self, page: Page, signed_url: str, target: Path) -> Path:
        with page.expect_download(timeout=30000) as download_info:
            page.evaluate("url => window.open(url, '_blank')", signed_url)
        download = download_info.value
        download.save_as(target)
        return target

    def _datetime_seconds(self, day: date) -> int:
        china = timezone(timedelta(hours=8))
        dt = datetime.combine(day, datetime_time(0, 0, 0), tzinfo=china)
        return int(dt.timestamp())


    def _filename(self, shop: Shop, date_range: DateRange) -> str:
        return export_filename(shop, date_range, "客服绩效", ".xls")

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

    def _has_text(self, page: Page, text: str, timeout: int = 1000) -> bool:
        try:
            page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)
            return True
        except TimeoutError:
            return False
