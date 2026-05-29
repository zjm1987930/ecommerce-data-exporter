from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

from exporter.browser import find_edge
from exporter.config import Shop
from exporter.dates import DateRange
from exporter.paths import browser_profile_dir, task_shop_dir


DOUYIN_HOME_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"
DOUYIN_DEPOSIT_URL = "https://fxg.jinritemai.com/ffa/content/shop_deposit?tab_type=2"
DOUYIN_PROFILE_ID = "douyin_default"


@dataclass(frozen=True)
class DouyinDepositResult:
    status: str
    message: str
    downloaded_file: Path | None = None


class DouyinDepositAutomator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.platform = "douyin"

    def run(
        self,
        shop: Shop,
        date_range: DateRange,
        *,
        dry_run: bool = False,
        headed: bool = True,
    ) -> DouyinDepositResult:
        return self.run_batch([shop], date_range, dry_run=dry_run, headed=headed)[shop]

    def run_batch(
        self,
        shops: list[Shop],
        date_range: DateRange,
        *,
        dry_run: bool = False,
        headed: bool = True,
    ) -> dict[Shop, DouyinDepositResult]:
        if not shops:
            return {}

        edge_path = find_edge()
        profile_id = shops[0].profile_id or DOUYIN_PROFILE_ID
        profile_shop = Shop(
            id=profile_id,
            name="抖音默认账号",
            sheet_name="抖音默认账号",
            platform=self.platform,
            platform_name="抖音",
        )
        user_data_dir = browser_profile_dir(self.project_root, self.platform, profile_shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        results: dict[Shop, DouyinDepositResult] = {}

        with sync_playwright() as playwright:
            context = self._launch_context(playwright, edge_path, user_data_dir, headed=headed)
            page = self._first_page(context)
            try:
                print(f"打开抖音共享登录环境：{user_data_dir}")
                page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=60000)
                self._dismiss_page_popups(page)
                self._ensure_logged_in(page)
                for shop in shops:
                    output_dir = task_shop_dir(self.project_root, date_range, "保证金数据", shop)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    screenshot_dir = output_dir / "截图"
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        self._switch_shop(page, shop)
                        self._dismiss_page_popups(page)
                        self._open_deposit_page(page)
                        self._dismiss_page_popups(page)
                        total = self._query_total(page, date_range)
                        print(f"{shop.name} 保证金查询结果数量：{total}")
                        if total == 0:
                            print(f"{shop.name} 保证金数据无数据，跳过下载，继续下一家。")
                            results[shop] = DouyinDepositResult("无数据", "保证金交易流水查询结果为 0")
                            continue
                        if dry_run:
                            results[shop] = DouyinDepositResult("试运行成功", f"查询到 {total} 条，未下载 CSV")
                            continue
                        downloaded = self._download_csv(page, output_dir, shop, date_range)
                        results[shop] = DouyinDepositResult("成功", f"已下载保证金数据：{downloaded}", downloaded)
                    except Exception as exc:
                        screenshot = screenshot_dir / f"抖音保证金自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
                        try:
                            page.screenshot(path=str(screenshot), full_page=True)
                            print(f"已保存失败截图：{screenshot}")
                        except Exception:
                            pass
                        results[shop] = DouyinDepositResult("失败", str(exc))
            finally:
                try:
                    context.close()
                except Exception:
                    pass
        return results

    def _launch_context(self, playwright, edge_path: str, user_data_dir: Path, *, headed: bool) -> BrowserContext:
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
            if "ProcessSingleton" in str(exc) or "profile is already in use" in str(exc):
                raise RuntimeError("抖音共享登录环境被普通 Edge 占用。请关闭抖音普通 Edge 窗口后重跑。") from exc
            raise

    def _first_page(self, context: BrowserContext) -> Page:
        for page in context.pages:
            if not page.is_closed():
                return page
        return context.new_page()

    def _ensure_logged_in(self, page: Page) -> None:
        for _ in range(40):
            body = self._body_text(page)
            if self._is_login_page(page, body):
                raise RuntimeError("抖音登录状态失效。请先运行 login_douyin.ps1 登录 douyin_default 后重跑。")
            if "资金" in body and ("订单" in body or "商品" in body or "售后" in body):
                return
            page.wait_for_timeout(500)
        raise TimeoutError("未能确认已进入抖音商家后台")

    def _is_login_page(self, page: Page, body: str | None = None) -> bool:
        text = body if body is not None else self._body_text(page)
        return "/login" in page.url or "登录抖店" in text or "扫码登录" in text or "验证码" in text

    def _switch_shop(self, page: Page, shop: Shop) -> None:
        if self._current_shop_is(page, shop.name):
            print(f"当前已是目标店铺：{shop.name}")
            return
        print(f"切换抖音店铺：{shop.name}")
        self._dismiss_page_popups(page)
        self._open_account_menu(page)
        self._click_switch_shop_entry(page)
        self._reset_shop_switcher_scroll(page)
        self._click_shop_in_switcher(page, shop.name)
        for _ in range(50):
            page.wait_for_timeout(500)
            if self._current_shop_is(page, shop.name):
                print(f"已切换到店铺：{shop.name}")
                return
        raise TimeoutError(f"切换店铺后未能确认当前店铺为：{shop.name}")

    def _current_shop_is(self, page: Page, shop_name: str) -> bool:
        text = self._body_text(page)
        return shop_name in text and "请选择店铺" not in text

    def _open_account_menu(self, page: Page) -> None:
        if self._has_visible_text(page, "切换组织/店铺"):
            return
        for _ in range(2):
            point = page.evaluate(
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
                    const candidates = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .map((el) => ({ el, text: (el.innerText || el.textContent || "").trim(), rect: el.getBoundingClientRect() }))
                        .filter((item) => item.rect.top < 90 && item.rect.left > window.innerWidth * 0.65 && /店|图书|文化/.test(item.text))
                        .sort((a, b) => b.rect.right - a.rect.right);
                    if (!candidates.length) return { x: window.innerWidth - 80, y: 30 };
                    const rect = candidates[0].rect;
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }"""
            )
            page.mouse.click(point["x"], point["y"])
            page.wait_for_timeout(600)
            if self._has_visible_text(page, "切换组织/店铺"):
                return
        for offset in (70, 115, 165, 215):
            point = page.evaluate(
                """(offset) => ({ x: window.innerWidth - offset, y: 32 })""",
                offset,
            )
            page.mouse.click(point["x"], point["y"])
            page.wait_for_timeout(600)
            if self._has_visible_text(page, "切换组织/店铺"):
                return
        print("未能通过文本确认账号菜单，但将继续尝试点击“切换组织/店铺”。")

    def _click_switch_shop_entry(self, page: Page) -> None:
        for _ in range(3):
            try:
                page.get_by_text("切换组织/店铺", exact=False).last.click(timeout=1000)
                for _inner in range(10):
                    page.wait_for_timeout(300)
                    if self._has_visible_text(page, "请选择店铺"):
                        return
            except TimeoutError:
                pass
            point = page.evaluate(
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
                    const nodes = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .filter((el) => (el.innerText || el.textContent || "").includes("切换组织/店铺"));
                    if (!nodes.length) return null;
                    let best = nodes[nodes.length - 1];
                    let node = best;
                    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                        const rect = node.getBoundingClientRect();
                        const text = node.innerText || "";
                        if (visible(node) && text.includes("切换组织/店铺") && rect.width >= 180 && rect.height >= 30) {
                            best = node;
                            break;
                        }
                    }
                    const rect = best.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }"""
            )
            if not point:
                raise RuntimeError("未找到“切换组织/店铺”入口")
            page.mouse.click(point["x"], point["y"])
            for _inner in range(10):
                page.wait_for_timeout(300)
                if "请选择店铺" in self._body_text(page):
                    return
        raise TimeoutError("点击“切换组织/店铺”后未出现店铺选择弹窗")

    def _click_shop_in_switcher(self, page: Page, shop_name: str) -> None:
        for _ in range(3):
            result = page.evaluate(
                """(shopName) => {
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
                    const exactNodes = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .filter((el) => (el.innerText || el.textContent || "").trim() === shopName);
                    if (!exactNodes.length) return { ok: false, reason: "not_found" };
                    let card = exactNodes[0];
                    let node = exactNodes[0];
                    for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
                        const text = node.innerText || "";
                        const rect = node.getBoundingClientRect();
                        if (
                            text.includes(shopName)
                            && text.includes("抖店工作台")
                            && text.includes("正常营业")
                            && rect.width >= 240
                            && rect.height >= 55
                            && rect.height <= 180
                        ) {
                            card = node;
                            break;
                        }
                    }
                    card.scrollIntoView({ block: "center", inline: "center" });
                    const rect = card.getBoundingClientRect();
                    return {
                        ok: true,
                        x: Math.min(rect.right - 24, rect.left + Math.max(rect.width * 0.82, 120)),
                        y: rect.top + rect.height / 2
                    };
                }""",
                shop_name,
            )
            if result.get("ok"):
                page.wait_for_timeout(300)
                page.mouse.click(result["x"], result["y"])
                return
            self._reset_shop_switcher_scroll(page)
            page.wait_for_timeout(300)
        raise TimeoutError(f"店铺切换弹窗中未找到：{shop_name}")

    def _reset_shop_switcher_scroll(self, page: Page) -> None:
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
                const scrollables = Array.from(document.querySelectorAll("*"))
                    .filter(visible)
                    .filter((el) => el.scrollHeight > el.clientHeight + 20);
                for (const item of scrollables) {
                    item.scrollTop = 0;
                }
                document.documentElement.scrollTop = 0;
                document.body.scrollTop = 0;
            }"""
        )
        page.mouse.move(760, 380)
        page.mouse.wheel(0, -2400)
        page.wait_for_timeout(500)

    def _scroll_shop_switcher(self, page: Page, *, direction: str) -> None:
        delta = 360 if direction == "down" else -360
        page.evaluate(
            """(delta) => {
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
                const scrollables = Array.from(document.querySelectorAll("*"))
                    .filter(visible)
                    .filter((el) => el.scrollHeight > el.clientHeight + 20)
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                for (const target of scrollables) {
                    target.scrollTop = (target.scrollTop || 0) + delta;
                }
            }""",
            delta,
        )
        page.mouse.move(760, 520)
        page.mouse.wheel(0, 700 if direction == "down" else -700)

    def _open_deposit_page(self, page: Page) -> None:
        page.goto(DOUYIN_DEPOSIT_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(40):
            self._dismiss_page_popups(page)
            body = self._body_text(page)
            if "交易流水" in body and "体验保证金" in body and "导出明细" in body:
                return
            page.wait_for_timeout(500)
        raise TimeoutError("未能进入抖音体验保证金页面")

    def _query_total(self, page: Page, date_range: DateRange) -> int:
        params = self._base_params()
        params.update(
            {
                "page": 0,
                "pageSize": 10,
                "create_time": self._datetime_seconds(date_range.start, datetime_time(0, 0, 0)),
                "end_time": self._datetime_seconds(date_range.end, datetime_time(23, 59, 59)),
                "remark_type_new": 0,
            }
        )
        try:
            data = self._fetch_get_json(page, "/governance/shop/tpledgecash/flow/list", params, "抖音保证金查询")
            total = data.get("total")
            if total is None:
                page_total = self._page_total(page)
                if page_total is not None:
                    return page_total
                raise RuntimeError(f"抖音保证金查询接口缺少 total：{data}")
            return int(total)
        except Exception as exc:
            page_total = self._page_total(page)
            if page_total is not None:
                print(f"接口查询异常，已使用页面总数兜底：{page_total}；原始错误：{exc}")
                return page_total
            raise

    def _download_csv(self, page: Page, output_dir: Path, shop: Shop, date_range: DateRange) -> Path:
        params = self._base_params()
        params.update(
            {
                "remark_type_new": 0,
                "create_time": f"{date_range.start.isoformat()} 00:00:00",
                "end_time": f"{date_range.end.isoformat()} 23:59:59",
                "trade_no": "",
                "appid": 1,
            }
        )
        url = "/governance/shop/tpledgecash/record/download?" + urlencode(params)
        target = self._unique_download_path(output_dir, f"{date_range.range_key}{shop.sheet_name}保证金.csv")
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
            raise RuntimeError(f"抖音保证金 CSV 下载失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        target.write_bytes(base64.b64decode(result["base64"]))
        return target

    def _fetch_get_json(self, page: Page, path: str, params: dict, label: str) -> dict:
        url = path + "?" + urlencode(params)
        result = page.evaluate(
            """async (url) => {
                const response = await fetch(url, { credentials: "include" });
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
            raise RuntimeError(f"{label}接口请求失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if int(data.get("code", -1)) != 0 or int(data.get("st", -1)) != 0:
            raise RuntimeError(f"{label}接口返回失败：{data}")
        return data

    def _base_params(self) -> dict:
        return {
            "bizId": "62000070001",
            "marginPlatform": "fxg_pc",
            "base_margin_platform": "fxg_pc",
            "csrf": "",
        }

    def _datetime_seconds(self, day: date, value: datetime_time) -> int:
        china = timezone(timedelta(hours=8))
        return int(datetime.combine(day, value, tzinfo=china).timestamp())

    def _dismiss_page_popups(self, page: Page) -> None:
        for _ in range(4):
            target = page.evaluate(
                """() => {
                    const protectedTexts = ["请选择店铺", "切换组织/店铺", "交易流水"];
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
                    const protectedAncestor = (el) => {
                        let node = el;
                        while (node && node !== document.body) {
                            const text = (node.innerText || "").slice(0, 500);
                            if (protectedTexts.some((item) => text.includes(item))) return true;
                            node = node.parentElement;
                        }
                        return false;
                    };
                    const candidates = Array.from(document.querySelectorAll("button,a,i,span,div,[role='button']"))
                        .filter((el) => visible(el) && !protectedAncestor(el))
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || "").trim();
                            const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                            const className = String(el.className || "");
                            let score = 0;
                            if (/^(×|x|X|关闭|取消)$/.test(text)) score += 8;
                            if (/关闭|close/i.test(label)) score += 8;
                            if (/close|modal-close|dialog-close/i.test(className)) score += 6;
                            if (rect.width <= 90 && rect.height <= 90) score += 3;
                            return { el, score, rect };
                        })
                        .filter((item) => item.score >= 8)
                        .sort((a, b) => b.score - a.score);
                    if (!candidates.length) return null;
                    const rect = candidates[0].rect;
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }"""
            )
            if not target:
                return
            page.mouse.click(target["x"], target["y"])
            page.wait_for_timeout(400)

    def _body_text(self, page: Page) -> str:
        try:
            return page.locator("body").inner_text(timeout=1500)
        except Exception:
            return ""

    def _has_visible_text(self, page: Page, text: str, timeout: int = 500) -> bool:
        try:
            page.get_by_text(text, exact=False).last.wait_for(state="visible", timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _page_total(self, page: Page) -> int | None:
        import re

        text = self._body_text(page)
        match = re.search(r"共\s*(\d+)\s*条", text)
        if match:
            return int(match.group(1))
        if "暂无数据" in text:
            return 0
        return None

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
