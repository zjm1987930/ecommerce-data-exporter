from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import copy
import json
from pathlib import Path
import math
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Page, Request, Response, TimeoutError, sync_playwright

from exporter.automation.returns import ReturnsAutomator
from exporter.browser import find_edge, PDD_NEGATIVE_EXPERIENCE_URL
from exporter.config import Shop
from exporter.dates import DateRange, split_range
from exporter.paths import browser_profile_dir, data_type_dir, task_shop_dir
from exporter.workbooks import NegativeExperienceRecord, write_negative_experience_workbook


@dataclass(frozen=True)
class NegativeExperienceResult:
    status: str
    message: str
    output_file: Path | None = None
    total_records: int = 0
    failures: dict[str, str] | None = None


@dataclass
class _NegativeExperienceApiSnapshot:
    request: Request
    response_json: Any
    records: list[NegativeExperienceRecord]
    total: int | None


@dataclass
class _NegativeExperienceApiCapture:
    snapshots: list[_NegativeExperienceApiSnapshot]
    listener: Any


class LoginRequiredError(RuntimeError):
    pass


class NegativeExperienceAutomator:
    def __init__(self, project_root: Path, platform: str, platform_name: str) -> None:
        self.project_root = project_root
        self.platform = platform
        self.platform_name = platform_name
        self._common = ReturnsAutomator(project_root, platform, platform_name)
        self._user_action_callback: Callable[[str], None] | None = None

    def run_batch(
        self,
        shops: list[Shop],
        date_range: DateRange,
        *,
        headed: bool = True,
        manual_date: bool = False,
        stop_on_error: bool = False,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> NegativeExperienceResult:
        self._user_action_callback = user_action_callback
        chunks = split_range(date_range, 7)
        records_by_sheet: dict[str, list[NegativeExperienceRecord]] = {}
        failures: dict[str, str] = {}
        for index, shop in enumerate(shops, start=1):
            print("")
            print(f"===== [{index}/{len(shops)}] {shop.name} =====")
            try:
                records = self.run_shop(
                    shop,
                    chunks,
                    headed=headed,
                    manual_date=manual_date,
                    user_action_callback=user_action_callback,
                )
            except Exception as exc:
                failures[shop.name] = str(exc)
                records = []
                print(f"{shop.name} 抓取失败，已保留空 sheet：{exc}")
                if stop_on_error:
                    raise
            records_by_sheet[shop.sheet_name] = records
            print(f"{shop.name} 抓取完成：{len(records)} 条")

        output = self._output_path(date_range)
        write_negative_experience_workbook(records_by_sheet, output)
        total = sum(len(items) for items in records_by_sheet.values())
        if failures:
            return NegativeExperienceResult(
                "部分失败",
                f"已生成负向体验数据：{output}；失败店铺 {len(failures)} 个",
                output,
                total,
                failures,
            )
        return NegativeExperienceResult("成功", f"已生成负向体验数据：{output}", output, total, {})

    def run_shop(
        self,
        shop: Shop,
        chunks: list[DateRange],
        *,
        headed: bool = True,
        manual_date: bool = False,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> list[NegativeExperienceRecord]:
        self._user_action_callback = user_action_callback
        edge_path = find_edge()
        user_data_dir = browser_profile_dir(self.project_root, self.platform, shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir = task_shop_dir(self.project_root, chunks[0], "消费者负向体验扣款数据", shop) / "截图"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = self._launch_shop_context(playwright, edge_path, user_data_dir, headed=headed)
            page = self._first_page(context)
            try:
                print(f"打开店铺浏览器环境：{shop.name}")
                page.goto(PDD_NEGATIVE_EXPERIENCE_URL, wait_until="domcontentloaded")
                self._common._dismiss_page_popups(page)
                self._wait_for_negative_page(page)
                self._common._dismiss_page_popups(page)

                all_records: list[NegativeExperienceRecord] = []
                seen: set[tuple[str, ...]] = set()
                for chunk in chunks:
                    print(f"抓取区间：{chunk.start.isoformat()} 至 {chunk.end.isoformat()}")
                    chunk_records = self._query_and_scrape_chunk(page, chunk, manual_date=manual_date)
                    total = len({self._record_key(record) for record in chunk_records})
                    if total == 0:
                        continue
                    if False:
                        print("当前区间无数据")
                        continue
                    added = 0
                    for record in chunk_records:
                        key = self._record_key(record)
                        if key in seen:
                            continue
                        seen.add(key)
                        all_records.append(record)
                        added += 1
                    print(f"当前区间页面显示 {total} 条，新增 {added} 条")
                return all_records
            except LoginRequiredError as exc:
                print(f"登录状态失效：{exc}")
                raise
            except Exception as exc:
                screenshot = screenshot_dir / f"负向体验自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
                try:
                    page.screenshot(path=str(screenshot), full_page=True)
                    print(f"已保存失败截图：{screenshot}")
                except Exception:
                    pass
                print(f"错误详情：{type(exc).__name__}: {exc}")
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
            if not self._is_edge_startup_closed_error(exc):
                raise
            raise RuntimeError(
                "Edge 浏览器环境启动失败。该店铺对应的普通 Edge 登录窗口可能仍打开，"
                "或浏览器环境被临时占用。请关闭这个店铺打开的空白页签/Edge 窗口，"
                "必要时等待几秒后单独重跑该店铺。"
            ) from exc

    def _is_edge_startup_closed_error(self, exc: Exception) -> bool:
        text = str(exc)
        return (
            "launch_persistent_context" in text
            and "Target page, context or browser has been closed" in text
        )

    def _query_and_scrape_chunk(
        self,
        page: Page,
        chunk: DateRange,
        *,
        manual_date: bool = False,
    ) -> list[NegativeExperienceRecord]:
        last_mismatch = ""
        for attempt in range(1, 4):
            if attempt > 1:
                print(f"当前区间抓取数量与页面总数不一致，自动重试第 {attempt} 次。")
            self._set_date_range(page, chunk, manual_date=manual_date)
            capture = self._start_negative_api_capture(page)
            page_total = self._query_and_get_total(page)
            if self._page_shows_no_negative_records(page):
                print("当前区间无数据")
                return []
            api_snapshot = self._best_negative_api_snapshot(capture, page, expected_total=page_total)
            total = api_snapshot.total if api_snapshot and api_snapshot.total is not None else page_total
            if api_snapshot and api_snapshot.total is not None and api_snapshot.total != page_total:
                print(f"页面分页总数 {page_total} 条，接口总数 {api_snapshot.total} 条；本次以接口总数为准。")
            if total == 0:
                print("当前区间无数据")
                return []
            records = self._scrape_from_api(page, api_snapshot, total) if api_snapshot else []
            if records:
                print(f"已通过页面接口采集 {len(records)} 条，跳过虚拟表格 DOM 读取。")
            else:
                print("接口列表字段不完整或未能采集完整分页，回退到 DOM 表格采集。")
                page_size_hint = len(api_snapshot.records) if api_snapshot else None
                records = self._scrape_all_pages(page, total, page_size_hint=page_size_hint)
            unique_count = len({self._record_key(record) for record in records})
            if unique_count == total:
                self._ensure_first_page(page)
                return records
            last_mismatch = f"页面总数 {total} 条，实际抓取去重后 {unique_count} 条"
            print(last_mismatch)
            self._ensure_first_page(page)
            page.wait_for_timeout(1000)
        raise RuntimeError(f"负向体验当前区间抓取数量校验失败：{last_mismatch}")

    def _first_page(self, context: BrowserContext) -> Page:
        if context.pages:
            return context.pages[0]
        return context.new_page()

    def _wait_for_negative_page(self, page: Page) -> None:
        for _ in range(24):
            if self._is_negative_page(page):
                return
            if self._is_login_page(page):
                raise LoginRequiredError(
                    "当前店铺已掉登录。请关闭自动化打开的登录页，使用普通 Edge 补登录："
                    "powershell -ExecutionPolicy Bypass -File .\\login_shop.ps1 --shop-id 对应店铺ID，"
                    "登录完成并关闭窗口后再单独补跑该店铺。"
                )
            self._common._dismiss_page_popups(page)
            page.wait_for_timeout(500)
        if self._is_login_page(page):
            raise LoginRequiredError(
                "当前店铺已掉登录。请使用 login_shop.ps1 普通 Edge 登录环境补登录后重跑。"
            )
        raise TimeoutError("未能进入消费者负向体验页面")

    def _is_negative_page(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return "消费者负向体验补偿明细" in body or ("创建时间" in body and "处理状态" in body and "订单编号" in body)

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

    def _set_date_range(self, page: Page, date_range: DateRange, *, manual_date: bool = False) -> None:
        self._common._dismiss_page_popups(page)
        value = f"{date_range.start.isoformat()} ~ {date_range.end.isoformat()}"
        label = page.get_by_text("创建时间", exact=True).first
        date_input = label.locator("xpath=following::input[1]")
        date_input.wait_for(state="visible", timeout=5000)
        date_input.click(timeout=5000)
        if manual_date:
            print(f"请手动选择创建时间：{value}。选完开始和结束日期后回到终端按 Enter。")
            self._wait_for_user_action()
        else:
            print(f"自动选择创建时间：{value}")
            try:
                self._pick_date_by_visible_month(page, date_range.start)
                self._pick_date_by_visible_month(page, date_range.end)
                self._wait_for_date_range_value(date_input, date_range)
            except Exception as exc:
                print(f"自动选择日期失败，切换为手动选择：{exc}")
                print(f"请手动选择创建时间：{value}。选完开始和结束日期后回到终端按 Enter。")
                self._wait_for_user_action()
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        current_value = date_input.input_value(timeout=5000).strip()
        if date_range.start.isoformat() not in current_value or date_range.end.isoformat() not in current_value:
            raise RuntimeError(
                "创建时间未正确设置，已停止查询，避免抓取错误区间。"
                f" 期望：{value}；当前页面显示：{current_value or '空'}"
            )

    def _wait_for_date_range_value(self, date_input, date_range: DateRange) -> None:
        expected_start = date_range.start.isoformat()
        expected_end = date_range.end.isoformat()
        for _ in range(20):
            current_value = date_input.input_value(timeout=1000).strip()
            if expected_start in current_value and expected_end in current_value:
                return
            date_input.page.wait_for_timeout(250)
        current_value = date_input.input_value(timeout=1000).strip()
        raise RuntimeError(
            "创建时间选择后没有自动写入输入框。"
            f" 期望包含：{expected_start} 和 {expected_end}；当前：{current_value or '空'}"
        )

    def _pick_date_by_visible_month(self, page: Page, target) -> None:
        for _ in range(8):
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
                    const panelHeaders = Array.from(document.querySelectorAll("[class*='RPR_headerSelector']"))
                        .filter(visible)
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        })
                        .map((header) => {
                            const values = Array.from(header.querySelectorAll("input"))
                                .map((item) => item.value || "")
                                .filter(Boolean);
                            const yearText = values.find((value) => /20\\d{2}/.test(value)) || header.innerText || "";
                            const monthText = (
                                ((header.querySelector("[class*='RPR_dateText']") || {}).innerText || "")
                                || header.innerText
                                || ""
                            );
                            const y = Number((yearText.match(/20\\d{2}/) || [])[0]);
                            const m = Number((monthText.match(/([1-9]|1[0-2])月/) || [])[1]);
                            const rect = header.getBoundingClientRect();
                            return { y, m, rect };
                        });
                    const wrappers = Array.from(document.querySelectorAll("[class*='RPR_tableWrapper']"))
                        .filter(visible)
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    const panelIndex = panelHeaders.findIndex((item) => item.y === year && item.m === month);
                    if (panelIndex < 0 || !wrappers[panelIndex]) return null;
                    const wrapper = wrappers[panelIndex];
                    const cells = Array.from(wrapper.querySelectorAll("[role='date-cell']"))
                        .filter(visible)
                        .filter((cell) => {
                            const className = String(cell.className || "");
                            if (/disabled|not-allowed/i.test(className)) return false;
                            if (className.includes("RPR_outOfMonth")) return false;
                            const inner = cell.querySelector("[class*='RPR_cell']") || cell;
                            const text = (inner.innerText || inner.textContent || "").trim();
                            return text === String(day);
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    if (!cells.length) return null;
                    const target = cells[0].querySelector("[class*='RPR_cell']") || cells[0];
                    const rect = target.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                {"year": target.year, "month": target.month, "day": target.day},
            )
            if point:
                page.mouse.click(point["x"], point["y"])
                page.wait_for_timeout(350)
                return

            visible_months = self._visible_negative_months(page)
            target_month = target.year * 12 + target.month
            if visible_months and target_month < min(visible_months):
                self._click_negative_calendar_nav(page, "prev")
            else:
                self._click_negative_calendar_nav(page, "next")
            page.wait_for_timeout(500)
        raise TimeoutError(f"未能在负向体验日期控件中点击 {target.isoformat()}")

    def _visible_negative_months(self, page: Page) -> list[int]:
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
                return Array.from(document.querySelectorAll("[class*='RPR_headerSelector']"))
                    .filter(visible)
                    .map((header) => {
                        const values = Array.from(header.querySelectorAll("input"))
                            .map((item) => item.value || "")
                            .filter(Boolean);
                        const yearText = values.find((value) => /20\\d{2}/.test(value)) || header.innerText || "";
                        const monthText = (
                            ((header.querySelector("[class*='RPR_dateText']") || {}).innerText || "")
                            || header.innerText
                            || ""
                        );
                        const y = Number((yearText.match(/20\\d{2}/) || [])[0]);
                        const m = Number((monthText.match(/([1-9]|1[0-2])月/) || [])[1]);
                        return y && m ? y * 12 + m : null;
                    })
                    .filter(Boolean);
            }"""
        )

    def _click_negative_calendar_nav(self, page: Page, direction: str) -> None:
        point = page.evaluate(
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
                const outer = Array.from(document.querySelectorAll("[class*='RPR_outerPickerWrapper']"))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    })[0];
                if (!outer) return null;
                const buttons = Array.from(outer.querySelectorAll("svg,button,span,div,[role='button']"))
                    .filter(visible)
                    .filter((el) => {
                        const cls = String(el.className || "");
                        const text = (el.innerText || el.textContent || "").trim();
                        const aria = el.getAttribute("aria-label") || el.getAttribute("title") || "";
                        if (direction === "prev") return /left|prev/i.test(cls + " " + aria) || text === "<";
                        return /right|next/i.test(cls + " " + aria) || text === ">";
                    })
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return direction === "prev" ? ar.left - br.left : br.left - ar.left;
                    });
                if (buttons.length) {
                    const rect = buttons[0].getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }
                const rect = outer.getBoundingClientRect();
                return {
                    x: direction === "prev" ? rect.left + 18 : rect.right - 18,
                    y: rect.top + 18
                };
            }""",
            direction,
        )
        if not point:
            raise TimeoutError(f"未能点击负向体验日期控件 {direction} 翻页")
        page.mouse.click(point["x"], point["y"])

    def _pick_date_in_panel(self, page: Page, panel_index: int, target) -> None:
        try:
            self._common._pick_date_in_panel(page, panel_index, target)
            return
        except Exception:
            pass

        try:
            self._common._set_calendar_panel_month(page, panel_index, target)
        except Exception:
            pass

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
                const disabled = (el) => {
                    let node = el;
                    while (node && node !== document.body) {
                        const className = String(node.className || "");
                        if (/disabled|not-allowed/i.test(className) || node.getAttribute("aria-disabled") === "true") return true;
                        node = node.parentElement;
                    }
                    return false;
                };
                const panelHeaders = Array.from(document.querySelectorAll("[class*='RPR_panelHeader'],[class*='panel-header'],[class*='calendar-header']"))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    });
                const panels = [];
                for (const header of panelHeaders) {
                    let node = header;
                    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                        const cells = Array.from(node.querySelectorAll("td,button,span,div,[role='date-cell']"))
                            .filter(visible)
                            .filter((el) => /^\\d{1,2}$/.test((el.innerText || el.textContent || "").trim()));
                        if (cells.length >= 20) {
                            panels.push(node);
                            break;
                        }
                    }
                }
                if (!panels.length) {
                    const containers = Array.from(document.querySelectorAll("[class*='RPR_tableWrapper'],table,[class*='calendar'],[class*='picker']"))
                        .filter(visible)
                        .filter((el) => {
                            const cells = Array.from(el.querySelectorAll("td,button,span,div,[role='date-cell']"))
                                .filter(visible)
                                .filter((cell) => /^\\d{1,2}$/.test((cell.innerText || cell.textContent || "").trim()));
                            return cells.length >= 20;
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    panels.push(...containers);
                }
                const panel = panels[panelIndex];
                if (!panel) return false;
                const candidates = Array.from(panel.querySelectorAll("td,button,span,div,[role='date-cell']"))
                    .filter(visible)
                    .filter((el) => !disabled(el))
                    .filter((el) => {
                        const text = (el.innerText || el.textContent || "").trim();
                        if (text !== String(day)) return false;
                        const className = String(el.className || "");
                        return !/disabled|not-allowed/i.test(className);
                    })
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        const areaA = ar.width * ar.height;
                        const areaB = br.width * br.height;
                        return areaA - areaB || (ar.top - br.top) || (ar.left - br.left);
                    });
                if (!candidates.length) return false;
                const rect = candidates[0].getBoundingClientRect();
                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
            }""",
            {"panelIndex": panel_index, "day": target.day},
        )
        if not click_point:
            raise TimeoutError(f"未能在第 {panel_index + 1} 个日期面板点击 {target.isoformat()}")
        page.mouse.click(click_point["x"], click_point["y"])
        page.wait_for_timeout(300)

    def _start_negative_api_capture(self, page: Page) -> _NegativeExperienceApiCapture:
        snapshots: list[_NegativeExperienceApiSnapshot] = []

        def on_response(response: Response) -> None:
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            try:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower() and "api" not in response.url.lower():
                    return
                payload = response.json()
            except Exception:
                return
            records, total = self._records_from_api_payload(payload)
            if records:
                snapshots.append(_NegativeExperienceApiSnapshot(request, payload, records, total))

        page.on("response", on_response)
        return _NegativeExperienceApiCapture(snapshots, on_response)

    def _best_negative_api_snapshot(
        self,
        capture: _NegativeExperienceApiCapture,
        page: Page,
        *,
        expected_total: int,
    ) -> _NegativeExperienceApiSnapshot | None:
        page.wait_for_timeout(1200)
        try:
            page.off("response", capture.listener)
        except Exception:
            pass
        candidates = [item for item in capture.snapshots if item.records]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item.total == expected_total,
                item.total is not None,
                len(item.records),
            ),
            reverse=True,
        )
        selected = candidates[0]
        total_text = selected.total if selected.total is not None else "未知"
        print(f"识别到负向体验列表接口：首屏 {len(selected.records)} 条，接口总数 {total_text}")
        return selected

    def _scrape_from_api(
        self,
        page: Page,
        snapshot: _NegativeExperienceApiSnapshot,
        total: int,
    ) -> list[NegativeExperienceRecord]:
        page_size = max(1, len(snapshot.records))
        total_pages = max(1, math.ceil(total / page_size))
        if not self._api_records_have_report_fields(snapshot.records, len(snapshot.records)):
            print("首屏接口列表缺少报表所需展示字段，直接改用 DOM 表格内容。")
            return []
        records_by_key: dict[tuple[str, ...], NegativeExperienceRecord] = {}
        for record in snapshot.records:
            records_by_key[self._record_key(record)] = record
        if total_pages <= 1:
            return list(records_by_key.values())

        direct_api_ok = True
        for page_number in range(2, total_pages + 1):
            payload = self._fetch_negative_api_page(page, snapshot.request, page_number, page_size)
            if payload is None:
                direct_api_ok = False
                break
            page_records, _ = self._records_from_api_payload(payload)
            if not page_records:
                direct_api_ok = False
                break
            for record in page_records:
                records_by_key[self._record_key(record)] = record
        if direct_api_ok:
            records = list(records_by_key.values())
            if self._api_records_have_report_fields(records, total):
                return records
            print("接口列表缺少报表所需展示字段，改用 DOM 表格内容。")
            return []

        print("直接复用接口翻页未成功，改为点击分页并采集页面接口响应。")
        return self._scrape_api_by_clicking_pages(page, snapshot, total, total_pages)

    def _scrape_api_by_clicking_pages(
        self,
        page: Page,
        first_snapshot: _NegativeExperienceApiSnapshot,
        total: int,
        total_pages: int,
    ) -> list[NegativeExperienceRecord]:
        records_by_key: dict[tuple[str, ...], NegativeExperienceRecord] = {}
        for record in first_snapshot.records:
            records_by_key[self._record_key(record)] = record

        self._ensure_first_page(page)
        self._wait_table_stable(page)
        for page_number in range(2, total_pages + 1):
            previous_signature = self._table_signature(page)
            capture = self._start_negative_api_capture(page)
            try:
                self._goto_page(page, page_number, previous_signature=previous_signature)
            except Exception as exc:
                print(f"点击分页第 {page_number} 页失败，回退 DOM：{exc}")
                return []
            page_snapshot = self._best_negative_api_snapshot(capture, page, expected_total=total)
            if not page_snapshot or not page_snapshot.records:
                print(f"未捕获到第 {page_number} 页列表接口响应，回退 DOM。")
                return []
            for record in page_snapshot.records:
                records_by_key[self._record_key(record)] = record
        records = list(records_by_key.values())
        if self._api_records_have_report_fields(records, total):
            return records
        print("分页接口响应缺少报表所需展示字段，改用 DOM 表格内容。")
        return []

    def _api_records_have_report_fields(self, records: list[NegativeExperienceRecord], total: int) -> bool:
        if not records:
            return False
        if len({self._record_key(record) for record in records}) != total:
            return False
        rich_count = 0
        for record in records:
            has_status_text = bool(record.status) and not re.fullmatch(r"\d+", record.status.strip())
            if (
                record.product_name
                and record.problem_type
                and record.compensation_amount
                and record.deduction_progress
                and has_status_text
            ):
                rich_count += 1
        return rich_count >= max(1, math.ceil(len(records) * 0.8))

    def _record_key(self, record: NegativeExperienceRecord) -> tuple[str, ...]:
        return (
            record.order_no.strip(),
            record.product_id.strip(),
            record.created_at.strip(),
            record.problem_type.strip(),
            record.compensation_amount.strip(),
            record.status.strip(),
        )

    def _fetch_negative_api_page(
        self,
        page: Page,
        request: Request,
        page_number: int,
        page_size: int,
    ) -> Any | None:
        method = request.method.upper()
        url = request.url
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"content-length", "host", "connection", "accept-encoding"}
        }
        data: str | None = None
        post_data = request.post_data
        if post_data:
            try:
                payload = json.loads(post_data)
                payload = self._with_page_params(payload, page_number, page_size)
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                data = self._replace_page_params_in_form(post_data, page_number, page_size)
        else:
            url = self._replace_page_params_in_url(url, page_number, page_size)

        try:
            response = page.request.fetch(url, method=method, headers=headers, data=data, timeout=15000)
            if not response.ok:
                print(f"接口第 {page_number} 页请求失败：HTTP {response.status}")
                return None
            return response.json()
        except Exception as exc:
            print(f"接口第 {page_number} 页请求失败，回退 DOM：{exc}")
            return None

    def _records_from_api_payload(self, payload: Any) -> tuple[list[NegativeExperienceRecord], int | None]:
        best_items: list[dict[str, Any]] = []
        best_total: int | None = None

        def visit(value: Any, inherited_total: int | None = None) -> None:
            nonlocal best_items, best_total
            if isinstance(value, dict):
                local_total = self._extract_total_from_dict(value)
                if local_total is None:
                    local_total = inherited_total
                for child in value.values():
                    visit(child, local_total)
                return
            if isinstance(value, list):
                dict_items = [item for item in value if isinstance(item, dict)]
                score = sum(1 for item in dict_items if self._looks_like_negative_record(item))
                if score and score >= max(1, len(dict_items) // 2):
                    if score > len(best_items):
                        best_items = dict_items
                        best_total = inherited_total
                for child in value:
                    visit(child, inherited_total)

        visit(payload)
        if not best_items:
            return [], best_total
        records = [self._record_from_api_item(item) for item in best_items]
        records = [record for record in records if record.order_no]
        return records, best_total

    def _extract_total_from_dict(self, item: dict[str, Any]) -> int | None:
        total_keys = {
            "total",
            "totalcount",
            "totalnum",
            "totalsize",
            "recordcount",
            "recordtotal",
            "count",
        }
        for key, value in item.items():
            normalized = self._normalize_api_key(key)
            if normalized in total_keys or "总数" in key or "总条" in key:
                number = self._int_value(value)
                if number is not None:
                    return number
        return None

    def _looks_like_negative_record(self, item: dict[str, Any]) -> bool:
        values = [str(value) for value in item.values() if value is not None]
        text = "\n".join(values)
        has_order = any(re.search(r"\b\d{10,}\b", value) for value in values)
        has_record_hint = any(
            self._find_api_value(item, keys) not in {"", None}
            for keys in [
                ["problemType", "appealType", "complainType", "issueType", "问题类型"],
                ["goodsName", "productName", "商品名称", "商品信息"],
                ["createTime", "createdAt", "createdTime", "创建时间"],
            ]
        )
        return has_order and (has_record_hint or "补偿" in text or "扣款" in text or "申诉" in text)

    def _record_from_api_item(self, item: dict[str, Any]) -> NegativeExperienceRecord:
        order_no = self._string_value(
            self._find_api_value(item, ["orderNo", "orderSn", "orderSN", "orderId", "orderNumber", "订单编号"])
        )
        product_name = self._string_value(
            self._find_api_value(item, ["goodsName", "productName", "productTitle", "goodsTitle", "商品名称", "商品信息"])
        )
        product_id_value = self._find_api_value(item, ["goodsId", "goodsID", "productId", "productID", "商品ID", "商品编号"])
        product_id = self._string_value(product_id_value)
        if product_id and not product_id.startswith("ID"):
            product_id = f"ID：{product_id}"
        problem_type = self._string_value(
            self._find_api_value(item, ["problemType", "problemTypeName", "appealType", "complainType", "issueType", "问题类型"])
        )
        compensation_amount = self._amount_value(
            self._find_api_value(item, ["compensationAmount", "compensateAmount", "amount", "compensation", "补偿金额"])
        )
        deduction_progress = self._string_value(
            self._find_api_value(item, ["deductionProgress", "deductProgress", "deductStatus", "扣款进度"])
        )
        created_at = self._time_value(
            self._find_api_value(item, ["createTime", "createdAt", "createdTime", "gmtCreate", "创建时间"])
        )
        status = self._string_value(
            self._find_api_value(item, ["statusName", "handleStatus", "processStatus", "appealStatus", "status", "处理状态"])
        )
        operation = self._string_value(self._find_api_value(item, ["operation", "操作"])) or "查看详情"
        suggestion = "查看历史整改建议" if "查看历史整改建议" in json.dumps(item, ensure_ascii=False) else ""
        return NegativeExperienceRecord(
            order_no=order_no,
            product_name=product_name,
            product_id=product_id,
            problem_type=problem_type,
            compensation_amount=compensation_amount,
            deduction_progress=deduction_progress,
            created_at=created_at,
            status=status,
            operation=operation,
            suggestion=suggestion,
        )

    def _find_api_value(self, item: Any, keys: list[str]) -> Any:
        targets = {self._normalize_api_key(key) for key in keys}
        if isinstance(item, dict):
            for key, value in item.items():
                if self._normalize_api_key(key) in targets:
                    return value
            for value in item.values():
                found = self._find_api_value(value, keys)
                if found not in {"", None}:
                    return found
        elif isinstance(item, list):
            for value in item:
                found = self._find_api_value(value, keys)
                if found not in {"", None}:
                    return found
        return None

    def _normalize_api_key(self, key: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(key)).lower()

    def _string_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    def _amount_value(self, value: Any) -> str:
        if isinstance(value, int) and abs(value) >= 100:
            return f"{value / 100:.2f}"
        if isinstance(value, float):
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return self._string_value(value)

    def _time_value(self, value: Any) -> str:
        if isinstance(value, (int, float)):
            timestamp = value / 1000 if value > 10_000_000_000 else value
            try:
                return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return self._string_value(value)
        return self._string_value(value)

    def _int_value(self, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None

    def _with_page_params(self, value: Any, page_number: int, page_size: int) -> Any:
        cloned = copy.deepcopy(value)

        def visit(item: Any) -> bool:
            changed = False
            if isinstance(item, dict):
                for key in list(item.keys()):
                    normalized = self._normalize_api_key(key)
                    if normalized in {"page", "pageno", "pagenum", "pageindex", "current", "currentpage"}:
                        item[key] = page_number
                        changed = True
                    elif normalized in {"pagesize", "size", "limit"}:
                        item[key] = page_size
                        changed = True
                    elif normalized in {"offset", "start"} and isinstance(item[key], (int, float, str)):
                        item[key] = (page_number - 1) * page_size
                        changed = True
                    else:
                        changed = visit(item[key]) or changed
            elif isinstance(item, list):
                changed = any(visit(child) for child in item)
            return changed

        changed = visit(cloned)
        if not changed and isinstance(cloned, dict):
            cloned.setdefault("pageNo", page_number)
            cloned.setdefault("pageSize", page_size)
        return cloned

    def _replace_page_params_in_form(self, post_data: str, page_number: int, page_size: int) -> str:
        pairs = parse_qsl(post_data, keep_blank_values=True)
        if not pairs:
            return post_data
        changed = False
        replaced: list[tuple[str, str]] = []
        for key, value in pairs:
            normalized = self._normalize_api_key(key)
            if normalized in {"page", "pageno", "pagenum", "pageindex", "current", "currentpage"}:
                value = str(page_number)
                changed = True
            elif normalized in {"pagesize", "size", "limit"}:
                value = str(page_size)
                changed = True
            elif normalized in {"offset", "start"}:
                value = str((page_number - 1) * page_size)
                changed = True
            replaced.append((key, value))
        if not changed:
            replaced.extend([("pageNo", str(page_number)), ("pageSize", str(page_size))])
        return urlencode(replaced)

    def _replace_page_params_in_url(self, url: str, page_number: int, page_size: int) -> str:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not pairs:
            return url
        changed = False
        replaced: list[tuple[str, str]] = []
        for key, value in pairs:
            normalized = self._normalize_api_key(key)
            if normalized in {"page", "pageno", "pagenum", "pageindex", "current", "currentpage"}:
                value = str(page_number)
                changed = True
            elif normalized in {"pagesize", "size", "limit"}:
                value = str(page_size)
                changed = True
            elif normalized in {"offset", "start"}:
                value = str((page_number - 1) * page_size)
                changed = True
            replaced.append((key, value))
        if not changed:
            replaced.extend([("pageNo", str(page_number)), ("pageSize", str(page_size))])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(replaced), parts.fragment))

    def _query_and_get_total(self, page: Page) -> int:
        self._common._dismiss_page_popups(page)
        page.get_by_role("button", name="查询").last.click(timeout=5000)
        self._wait_for_query_result(page)
        self._wait_table_stable(page)
        body = page.locator("body").inner_text(timeout=5000)
        if self._is_negative_empty_text(body):
            return 0
        footer_total = self._pagination_total_count(page)
        if footer_total is not None:
            return footer_total
        for pattern in [r"全部\s*(\d+)", r"共查询到\s*(\d+)\s*条", r"共有\s*(\d+)\s*条"]:
            match = re.search(pattern, body)
            if match:
                return int(match.group(1))
        footer_total = self._pagination_total_count(page)
        if footer_total is not None:
            return footer_total
        return len(self._scrape_current_page(page))

    def _is_negative_empty_text(self, text: str) -> bool:
        return (
            "暂无申诉记录" in text
            or re.search(r"共有\s*0\s*条", text) is not None
            or re.search(r"共查询到\s*0\s*条", text) is not None
        )

    def _wait_for_query_result(self, page: Page) -> None:
        for _ in range(40):
            state = page.evaluate(
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
                    const body = document.body.innerText || "";
                    if (body.includes("暂无申诉记录") || /共有\\s*0\\s*条/.test(body) || /共查询到\\s*0\\s*条/.test(body)) {
                        return "empty";
                    }
                    const tables = Array.from(document.querySelectorAll("table"))
                        .filter(visible)
                        .filter((table) => {
                            const text = table.innerText || "";
                            return text.includes("订单编号") && text.includes("商品信息") && text.includes("创建时间");
                        });
                    const table = tables[tables.length - 1];
                    if (table && Array.from(table.querySelectorAll("tbody tr")).filter(visible).length) {
                        return "rows";
                    }
                    const paginations = Array.from(document.querySelectorAll("[data-testid='beast-core-pagination']"))
                        .filter(visible);
                    const pagination = paginations[paginations.length - 1];
                    if (pagination && /共有\\s*\\d+\\s*条/.test(pagination.innerText || pagination.textContent || "")) {
                        return "pagination";
                    }
                    return "";
                }"""
            )
            if state:
                return
            page.wait_for_timeout(250)

    def _scrape_all_pages(self, page: Page, total: int, *, page_size_hint: int | None = None) -> list[NegativeExperienceRecord]:
        if self._page_shows_no_negative_records(page):
            return []
        records: list[NegativeExperienceRecord] = []
        self._ensure_first_page(page)
        self._wait_table_stable(page)
        if self._page_shows_no_negative_records(page):
            return []
        total_pages = self._pagination_total_pages(page)
        if total_pages <= 0:
            page_size = max(1, len(self._scrape_current_page_all_rows(page, expected_count=page_size_hint)))
            total_pages = max(1, math.ceil(total / page_size))
        page_size = page_size_hint or (20 if total_pages > 1 else total)
        for page_number in range(1, total_pages + 1):
            if page_number > 1:
                previous_signature = self._table_signature(page)
                self._goto_page(page, page_number, previous_signature=previous_signature)
                self._wait_table_stable(page)
            expected_count = self._expected_page_count(total, total_pages, page_number, page_size)
            records.extend(self._scrape_current_page_all_rows(page, expected_count=expected_count))
        return records

    def _page_shows_no_negative_records(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return self._is_negative_empty_text(body)

    def _expected_page_count(self, total: int, total_pages: int, page_number: int, page_size: int) -> int | None:
        if total <= 0 or total_pages <= 0 or page_size <= 0:
            return None
        if page_number < total_pages:
            return min(page_size, total)
        remaining = total - page_size * (total_pages - 1)
        return remaining if remaining > 0 else min(page_size, total)

    def _pagination_total_count(self, page: Page) -> int | None:
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
                const paginations = Array.from(document.querySelectorAll("[data-testid='beast-core-pagination']"))
                    .filter(visible);
                const pagination = paginations[paginations.length - 1];
                if (!pagination) return null;
                const text = pagination.innerText || pagination.textContent || "";
                const match = text.match(/共有\\s*(\\d+)\\s*条/);
                return match ? Number(match[1]) : null;
            }"""
        )
        return int(value) if value is not None else None

    def _pagination_total_pages(self, page: Page) -> int:
        return int(
            page.evaluate(
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
                    const paginations = Array.from(document.querySelectorAll("[data-testid='beast-core-pagination']"))
                        .filter(visible);
                    const pagination = paginations[paginations.length - 1];
                    if (!pagination) return 0;
                    const nums = Array.from(pagination.querySelectorAll("li[class*='PGT_pagerItem']"))
                        .map((li) => Number((li.innerText || li.textContent || "").trim()))
                        .filter((value) => Number.isFinite(value));
                    return nums.length ? Math.max(...nums) : 0;
                }"""
            )
            or 0
        )

    def _scrape_current_page_all_rows(
        self,
        page: Page,
        *,
        expected_count: int | None = None,
    ) -> list[NegativeExperienceRecord]:
        records_by_key: dict[tuple[str, ...], NegativeExperienceRecord] = {}
        stagnant_rounds = 0
        last_count = 0
        self._reset_table_scroll(page)
        page.wait_for_timeout(150)
        for _ in range(24):
            self._wait_table_stable(page)
            for record in self._scrape_current_page(page):
                records_by_key[self._record_key(record)] = record
            if expected_count is not None and len(records_by_key) >= expected_count:
                break
            if len(records_by_key) == last_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                last_count = len(records_by_key)
            if stagnant_rounds >= 3:
                break
            moved = self._advance_table_scroll(page)
            if not moved and stagnant_rounds >= 1:
                break
            page.wait_for_timeout(250)
        return list(records_by_key.values())

    def _reset_table_scroll(self, page: Page) -> None:
        page.evaluate(
            """() => {
                const findTable = () => {
                    const tables = Array.from(document.querySelectorAll("table"))
                        .filter((table) => {
                            const text = table.innerText || "";
                            return text.includes("订单编号") && text.includes("商品信息") && text.includes("创建时间");
                        });
                    return tables[tables.length - 1] || null;
                };
                const scrollableAncestors = (el) => {
                    const items = [];
                    let node = el;
                    while (node && node !== document.body && node !== document.documentElement) {
                        const style = window.getComputedStyle(node);
                        const canScroll = node.scrollHeight > node.clientHeight + 5
                            && /(auto|scroll|overlay)/.test(style.overflowY + style.overflow);
                        if (canScroll) items.push(node);
                        node = node.parentElement;
                    }
                    items.push(document.scrollingElement || document.documentElement);
                    return items;
                };
                const table = findTable();
                if (!table) {
                    window.scrollTo(0, 0);
                    return;
                }
                for (const item of scrollableAncestors(table)) {
                    item.scrollTop = 0;
                }
                table.scrollIntoView({ block: "start" });
            }"""
        )

    def _advance_table_scroll(self, page: Page) -> bool:
        return bool(
            page.evaluate(
                """() => {
                    const findTable = () => {
                        const tables = Array.from(document.querySelectorAll("table"))
                            .filter((table) => {
                                const text = table.innerText || "";
                                return text.includes("订单编号") && text.includes("商品信息") && text.includes("创建时间");
                            });
                        return tables[tables.length - 1] || null;
                    };
                    const table = findTable();
                    const candidates = [];
                    let node = table;
                    while (node && node !== document.body && node !== document.documentElement) {
                        const style = window.getComputedStyle(node);
                        const canScroll = node.scrollHeight > node.clientHeight + 5
                            && /(auto|scroll|overlay)/.test(style.overflowY + style.overflow);
                        if (canScroll) candidates.push(node);
                        node = node.parentElement;
                    }
                    candidates.push(document.scrollingElement || document.documentElement);
                    for (const item of candidates) {
                        const before = item.scrollTop;
                        const max = Math.max(0, item.scrollHeight - item.clientHeight);
                        if (before < max - 2) {
                            item.scrollTop = Math.min(max, before + Math.max(300, Math.floor(item.clientHeight * 0.65)));
                            return item.scrollTop !== before;
                        }
                    }
                    return false;
                }"""
            )
        )

    def _scrape_current_page(self, page: Page) -> list[NegativeExperienceRecord]:
        rows = page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width > 0
                        && rect.height > 0;
                };
                const tables = Array.from(document.querySelectorAll("table"))
                    .filter(visible)
                    .filter((table) => {
                        const text = table.innerText || "";
                        return text.includes("订单编号") && text.includes("商品信息") && text.includes("创建时间");
                    });
                const table = tables[tables.length - 1];
                if (!table) return [];
                return Array.from(table.querySelectorAll("tbody tr"))
                    .filter(visible)
                    .map((tr) => Array.from(tr.querySelectorAll("td"))
                        .map((td) => (td.innerText || td.textContent || "").replace(/\\u00a0/g, " ").trim()))
                    .filter((cells) => cells.length >= 7 && !cells.join("").includes("暂无"));
            }"""
        )
        return [self._record_from_cells(cells) for cells in rows]

    def _record_from_cells(self, cells: list[str]) -> NegativeExperienceRecord:
        padded = list(cells) + [""] * 8
        product_name, product_id = self._split_product_info(padded[1])
        problem_type = padded[2].replace("查看历史整改建议", "").strip()
        return NegativeExperienceRecord(
            order_no=padded[0],
            product_name=product_name,
            product_id=product_id,
            problem_type=problem_type,
            compensation_amount=padded[3],
            deduction_progress=padded[4],
            created_at=padded[5],
            status=padded[6],
            operation=padded[7],
            suggestion=self._extract_suggestion(padded[2]),
        )

    def _split_product_info(self, value: str) -> tuple[str, str]:
        lines = [line.strip() for line in re.split(r"\r?\n+", value) if line.strip()]
        product_id = ""
        product_lines: list[str] = []
        for line in lines:
            if re.match(r"^(ID|ID：|ID:)", line):
                product_id = line.replace("ID:", "ID：", 1)
            elif "查看历史整改建议" not in line:
                product_lines.append(line)
        return "\n".join(product_lines), product_id

    def _extract_suggestion(self, value: str) -> str:
        return "查看历史整改建议" if "查看历史整改建议" in value else ""

    def _goto_page(self, page: Page, page_number: int, *, previous_signature: str = "") -> None:
        self._common._dismiss_page_popups(page)
        if page_number == 1:
            self._ensure_first_page(page)
            return
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)
        clicked = False
        try:
            pagination = page.locator("[data-testid='beast-core-pagination']").last
            pagination.wait_for(state="visible", timeout=5000)
            pagination.locator("li[class*='PGT_pagerItem']").filter(
                has_text=re.compile(rf"^{page_number}$")
            ).last.click(timeout=5000, force=True)
            clicked = True
        except TimeoutError:
            clicked = bool(page.evaluate(self._NEXT_PAGE_JS))
        if not clicked:
            raise TimeoutError(f"未能进入第 {page_number} 页")
        self._wait_for_active_page(page, page_number)
        if previous_signature:
            self._wait_table_changed(page, previous_signature)

    def _ensure_first_page(self, page: Page) -> None:
        self._common._dismiss_page_popups(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(150)
        if self._active_page_number(page) is None:
            return
        if self._active_page_number(page) == 1:
            return
        pagination = page.locator("[data-testid='beast-core-pagination']").last
        first_page = pagination.locator("li[class*='PGT_pagerItem']").filter(has_text=re.compile(r"^1$")).last
        if first_page.count() == 0:
            return
        first_page.click(timeout=5000, force=True)
        print("已点击分页第 1 页，确保从第一页开始抓取。")
        self._wait_for_active_page(page, 1)
        page.wait_for_timeout(500)

    def _active_page_number(self, page: Page) -> int | None:
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
                const paginations = Array.from(document.querySelectorAll("[data-testid='beast-core-pagination']"))
                    .filter(visible);
                const pagination = paginations[paginations.length - 1];
                if (!pagination) return null;
                const item = Array.from(pagination.querySelectorAll("li"))
                    .find((li) => /PGT_pagerItemActive/.test(String(li.className || "")));
                const text = item ? (item.innerText || item.textContent || "").trim() : "";
                const number = Number(text);
                return Number.isFinite(number) ? number : null;
            }"""
        )
        return int(value) if value is not None else None

    def _wait_for_active_page(self, page: Page, page_number: int) -> None:
        expected = str(page_number)
        for _ in range(30):
            active = self._active_page_number(page)
            if active is None:
                return
            if str(active) == expected:
                return
            page.wait_for_timeout(200)
        raise TimeoutError(f"分页未切换到第 {page_number} 页")

    def _wait_table_changed(self, page: Page, previous_signature: str) -> None:
        for _ in range(40):
            current = self._table_signature(page)
            if current and current != previous_signature:
                return
            page.wait_for_timeout(250)
        raise TimeoutError("翻页后表格内容未刷新")

    def _wait_table_stable(self, page: Page) -> None:
        last = ""
        stable_count = 0
        for _ in range(30):
            current = self._table_signature(page)
            if current and current == last:
                stable_count += 1
                if stable_count >= 2:
                    return
            else:
                stable_count = 0
                last = current
            page.wait_for_timeout(250)

    def _table_signature(self, page: Page) -> str:
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
                const tables = Array.from(document.querySelectorAll("table"))
                    .filter(visible)
                    .filter((table) => {
                        const text = table.innerText || "";
                        return text.includes("订单编号") && text.includes("商品信息") && text.includes("创建时间");
                    });
                const table = tables[tables.length - 1];
                if (!table) return "";
                const rows = Array.from(table.querySelectorAll("tbody tr")).filter(visible);
                return rows.slice(0, 3).map((row) => (row.innerText || "").trim()).join("\\n---\\n");
            }"""
        )

    def _output_path(self, date_range: DateRange) -> Path:
        filename = f"{date_range.range_key}拼多多负向体验.xlsx"
        return data_type_dir(self.project_root, date_range, "消费者负向体验扣款数据") / filename

    _NEXT_PAGE_JS = """() => {
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden"
                && style.display !== "none"
                && rect.width > 0
                && rect.height > 0
                && rect.top > window.innerHeight * 0.45;
        };
        const disabled = (el) => {
            let node = el;
            while (node && node !== document.body) {
                const className = String(node.className || "");
                if (/disabled/i.test(className) || node.getAttribute("aria-disabled") === "true") return true;
                node = node.parentElement;
            }
            return false;
        };
        const candidates = Array.from(document.querySelectorAll("button,[role='button'],li"))
            .filter((el) => visible(el) && !disabled(el))
            .filter((el) => {
                const text = (el.innerText || el.textContent || "").trim();
                const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                const className = String(el.className || "");
                return text === ">" || /next|right|下一页/i.test(label + " " + className);
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return br.left - ar.left;
            });
        if (!candidates.length) return false;
        candidates[0].click();
        return true;
    }"""
