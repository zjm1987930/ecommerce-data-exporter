from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
from pathlib import Path
import shutil
from typing import Callable

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

from exporter.browser import find_edge
from exporter.config import Shop
from exporter.dates import DateRange
from exporter.paths import browser_profile_dir, export_filename, task_screenshot_dir, task_shop_dir


KUAISHOU_HOME_URL = "https://s.kwaixiaodian.com/"
KUAISHOU_ACCOUNT_BILL_URL = "https://s.kwaixiaodian.com/zone/fund/accounting/account-bill"
KUAISHOU_SUB_ACCOUNT_CHANGE_URL = "https://s.kwaixiaodian.com/zone/sub-account/change"
KUAISHOU_PROFILE_ID = "kuaishou_default"
KUAISHOU_BID_BIZ_TYPE = "seller_or_promoter"
KUAISHOU_ACCOUNT_TYPE = "ACCOUNT_TYPE_WALLET_PINGAN"
KUAISHOU_ACCOUNT_ROLE = "ACCOUNT_ROLE_SELLER"
KUAISHOU_TASK_KIND = 10000


@dataclass(frozen=True)
class KuaishouDepositResult:
    status: str
    message: str
    downloaded_file: Path | None = None


class KuaishouDepositAutomator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.platform = "kuaishou"
        self._should_stop: Callable[[], bool] | None = None
        self._user_action_callback: Callable[[str], None] | None = None

    def run(
        self,
        shop: Shop,
        date_range: DateRange,
        *,
        dry_run: bool = False,
        headed: bool = True,
        should_stop: Callable[[], bool] | None = None,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> KuaishouDepositResult:
        return self.run_batch(
            [shop],
            date_range,
            dry_run=dry_run,
            headed=headed,
            should_stop=should_stop,
            user_action_callback=user_action_callback,
        )[shop]

    def run_batch(
        self,
        shops: list[Shop],
        date_range: DateRange,
        *,
        dry_run: bool = False,
        headed: bool = True,
        should_stop: Callable[[], bool] | None = None,
        user_action_callback: Callable[[str], None] | None = None,
    ) -> dict[Shop, KuaishouDepositResult]:
        if not shops:
            return {}

        previous_should_stop = self._should_stop
        previous_user_action_callback = self._user_action_callback
        self._should_stop = should_stop
        self._user_action_callback = user_action_callback
        edge_path = find_edge()
        profile_id = shops[0].profile_id or KUAISHOU_PROFILE_ID
        profile_shop = Shop(
            id=profile_id,
            name="快手默认账号",
            sheet_name="快手默认账号",
            platform=self.platform,
            platform_name="快手",
        )
        user_data_dir = browser_profile_dir(self.project_root, self.platform, profile_shop)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        self._clear_session_restore(user_data_dir)
        results: dict[Shop, KuaishouDepositResult] = {}

        try:
            with sync_playwright() as playwright:
                self._raise_if_stopped()
                context = self._launch_context(playwright, edge_path, user_data_dir, headed=headed)
                page = self._single_page(context)
                try:
                    print(f"打开快手共享登录环境：{user_data_dir}")
                    page.goto(KUAISHOU_HOME_URL, wait_until="domcontentloaded", timeout=60000)
                    self._ensure_logged_in(page)
                    for shop in shops:
                        self._raise_if_stopped()
                        page = self._ready_business_page(context, page)
                        output_dir = task_shop_dir(self.project_root, date_range, "保证金数据", shop)
                        output_dir.mkdir(parents=True, exist_ok=True)
                        screenshot_dir = task_screenshot_dir(self.project_root, date_range, "保证金数据", shop)
                        screenshot_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            self._switch_shop(page, shop)
                            page = self._single_page(context, preferred=page)
                            current_account = self._assert_current_shop(page, shop)
                            self._open_account_bill_page(page)
                            sub_mch_id = self._extract_sub_mch_id(page)
                            operator_id = str(current_account.get("userId") or self._operator_id(page))
                            print(f"{shop.name} 快手资金账单 subMchId：{sub_mch_id} operatorName：{operator_id}")
                            if dry_run:
                                results[shop] = KuaishouDepositResult(
                                    "试运行成功",
                                    f"已确认可进入快手资金账单页，subMchId={sub_mch_id}，未下载 CSV",
                                )
                                continue
                            result = self._create_export_task_with_retry(page, shop, date_range, sub_mch_id, operator_id)
                            download_url = str(result.get("downloadUrl") or "")
                            if not download_url:
                                raise RuntimeError(f"快手保证金导出接口未返回 downloadUrl：{result}")
                            target = self._download_csv(context, download_url, output_dir, shop, date_range)
                            if target.stat().st_size == 0:
                                results[shop] = KuaishouDepositResult("无数据", "快手资金账单导出文件为空")
                                continue
                            print(f"{shop.name} 快手保证金数据已保存：{target}")
                            results[shop] = KuaishouDepositResult("成功", f"已下载快手保证金数据：{target}", target)
                        except Exception as exc:
                            screenshot = screenshot_dir / f"快手保证金自动化失败_{datetime.now():%Y%m%d_%H%M%S}.png"
                            try:
                                page.screenshot(path=str(screenshot), full_page=True)
                                print(f"已保存失败截图：{screenshot}")
                            except Exception:
                                pass
                            results[shop] = KuaishouDepositResult("失败", str(exc))
                            page = self._single_page(context, preferred=page)
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
            return results
        finally:
            self._should_stop = previous_should_stop
            self._user_action_callback = previous_user_action_callback

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
                    "--disable-session-crashed-bubble",
                    "--edge-skip-compat-layer-relaunch",
                    "--no-first-run",
                    "--profile-directory=Default",
                ],
            )
        except Exception as exc:
            if "ProcessSingleton" in str(exc) or "profile is already in use" in str(exc):
                raise RuntimeError("快手共享登录环境被普通 Edge 占用。请关闭快手普通 Edge 窗口后重跑。") from exc
            raise

    def _single_page(self, context: BrowserContext, preferred: Page | None = None) -> Page:
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            return context.new_page()
        if preferred and not preferred.is_closed():
            page = preferred
        else:
            page = next((item for item in pages if "kwaixiaodian.com" in item.url), pages[0])
        for extra_page in pages:
            if extra_page == page:
                continue
            try:
                extra_page.close(run_before_unload=False)
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page

    def _ready_business_page(self, context: BrowserContext, preferred: Page | None = None) -> Page:
        page = self._single_page(context, preferred=preferred)
        if page.is_closed() or "s.kwaixiaodian.com" not in page.url:
            page = self._single_page(context)
            page.goto(KUAISHOU_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            self._ensure_logged_in(page)
        return page

    def _close_unexpected_page(self, popup: Page, preferred: Page) -> None:
        if popup == preferred:
            return
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        try:
            print(f"检测到快手自动化打开了额外页面，已关闭：{popup.url}")
            popup.close(run_before_unload=False)
        except Exception:
            pass

    def _clear_session_restore(self, user_data_dir: Path) -> None:
        default_dir = user_data_dir / "Default"
        sessions_dir = default_dir / "Sessions"
        if sessions_dir.exists():
            try:
                shutil.rmtree(sessions_dir)
            except Exception:
                pass

        preferences_path = default_dir / "Preferences"
        if not preferences_path.exists():
            return
        try:
            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
            profile = preferences.setdefault("profile", {})
            profile["exit_type"] = "Normal"
            profile["exited_cleanly"] = True
            session = preferences.setdefault("session", {})
            session["restore_on_startup"] = 5
            preferences_path.write_text(
                json.dumps(preferences, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _ensure_logged_in(self, page: Page) -> None:
        for _ in range(50):
            self._raise_if_stopped()
            body = self._body_text(page)
            if self._is_login_page(page, body):
                raise RuntimeError("快手登录状态失效。请先运行 login_kuaishou.ps1 登录 kuaishou_default 后重跑。")
            if "订单" in body and ("资金" in body or "店铺" in body or "商品" in body):
                return
            page.wait_for_timeout(500)
        raise TimeoutError("未能确认已进入快手商家后台")

    def _is_login_page(self, page: Page, body: str | None = None) -> bool:
        text = body if body is not None else self._body_text(page)
        return "login" in page.url.lower() or "扫码登录" in text or "验证码" in text or "登录" in text and "订单" not in text

    def _switch_shop(self, page: Page, shop: Shop) -> None:
        account_data = self._fetch_multi_account_list(page)
        current = self._current_kuaishou_account(account_data)
        if current and current.get("kshopName") == shop.name:
            print(f"当前已是目标快手店铺：{shop.name}")
            return

        target = self._find_kuaishou_account(account_data, shop.name)
        if not target:
            available = "、".join(
                str(item.get("kshopName"))
                for item in account_data.get("userInfos", [])
                if item.get("kshopName")
            )
            raise RuntimeError(f"快手账号列表中未找到店铺：{shop.name}。当前可切换店铺：{available}")

        current_name = current.get("kshopName") if current else "未知"
        print(f"准备切换快手店铺：{current_name} -> {shop.name}")
        check_sms = self._need_check_sms(page)
        if bool(check_sms.get("needCheckSms")):
            print("快手接口提示可能需要人工校验，先尝试点击子账号切换页店铺卡片。")
            if self._switch_by_sub_account_page(page, shop.name):
                return
            self._wait_for_manual_shop_switch(page, shop.name)
            return

        token = str(account_data.get("kshop.api.mt") or "")
        if not token:
            raise RuntimeError("快手多账号列表未返回 multiUserToken，无法自动切换店铺")
        phone = str(check_sms.get("phone") or "")
        self._submit_multi_token_login(page, int(target.get("userId")), token, phone)
        self._wait_until_current_shop(page, shop.name)

    def _switch_by_sub_account_page(self, page: Page, shop_name: str) -> bool:
        page.goto(KUAISHOU_SUB_ACCOUNT_CHANGE_URL, wait_until="domcontentloaded", timeout=60000)
        if not self._click_sub_account_card(page, shop_name):
            return False
        try:
            self._wait_until_current_shop(page, shop_name, timeout_seconds=20)
            return True
        except Exception as exc:
            print(f"点击快手子账号店铺卡片后尚未确认切换成功：{exc}")
            return False

    def _fetch_multi_account_list(self, page: Page) -> dict:
        self._ensure_kuaishou_host(page)
        data = self._fetch_form_json(
            page,
            "/pass/kshop/login/multiAccountChangeList",
            {"countryCode": "+86", "sid": "kshop.api"},
            "快手多账号列表",
        )
        if not isinstance(data.get("userInfos"), list):
            raise RuntimeError(f"快手多账号列表格式异常：{data}")
        return data

    def _need_check_sms(self, page: Page) -> dict:
        try:
            return self._fetch_json(page, "POST", "/rest/app/tts/staff/needCheckSms", None, "快手切店短信校验")
        except Exception as exc:
            print(f"快手切店短信校验接口读取失败，按需要人工确认处理：{exc}")
            return {"needCheckSms": True}

    def _current_kuaishou_account(self, account_data: dict) -> dict | None:
        accounts = account_data.get("userInfos") or []
        return next((item for item in accounts if item.get("current") is True), None)

    def _find_kuaishou_account(self, account_data: dict, shop_name: str) -> dict | None:
        accounts = account_data.get("userInfos") or []
        return next((item for item in accounts if item.get("kshopName") == shop_name), None)

    def _assert_current_shop(self, page: Page, shop: Shop) -> dict:
        account_data = self._fetch_multi_account_list(page)
        current = self._current_kuaishou_account(account_data)
        if current and current.get("kshopName") == shop.name:
            return current
        current_name = current.get("kshopName") if current else "未知"
        raise RuntimeError(f"快手当前店铺仍为「{current_name}」，不是目标店铺「{shop.name}」，已停止导出避免串店")

    def _wait_for_manual_shop_switch(self, page: Page, shop_name: str) -> None:
        callback = self._user_action_callback
        page.goto(KUAISHOU_SUB_ACCOUNT_CHANGE_URL, wait_until="domcontentloaded", timeout=60000)
        if callback:
            callback(
                f"快手切换到「{shop_name}」需要短信/人工验证。请在浏览器里手动切换到该店铺，完成后点击继续。"
            )
            self._ensure_kuaishou_host(page)
            self._wait_until_current_shop(page, shop_name)
            return
        raise RuntimeError(f"快手切换到「{shop_name}」需要短信/人工验证，请手动切换到该店铺后重新执行")

    def _wait_until_current_shop(self, page: Page, shop_name: str, *, timeout_seconds: int = 60) -> None:
        for _ in range(timeout_seconds):
            self._raise_if_stopped()
            account_data = self._fetch_multi_account_list(page)
            current = self._current_kuaishou_account(account_data)
            if current and current.get("kshopName") == shop_name:
                print(f"已切换到快手店铺：{shop_name}")
                return
            page.wait_for_timeout(1000)
        raise TimeoutError(f"切换店铺后未能确认当前快手店铺为：{shop_name}")

    def _click_sub_account_card(self, page: Page, shop_name: str) -> bool:
        for _ in range(20):
            self._raise_if_stopped()
            result = page.evaluate(
                """(shopName) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none"
                            && rect.width > 0 && rect.height > 0 && rect.right >= 0 && rect.bottom >= 0;
                    };
                    const nodes = Array.from(document.querySelectorAll("div,section,article,li"))
                        .filter(visible)
                        .filter((el) => (el.innerText || "").includes(`店铺：${shopName}`));
                    if (!nodes.length) return { ok: false };
                    let target = nodes
                        .map((el) => ({ el, rect: el.getBoundingClientRect(), text: el.innerText || "" }))
                        .filter((item) => item.rect.width >= 160 && item.rect.height >= 80)
                        .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height))[0]?.el || nodes[0];
                    target.scrollIntoView({ block: "center", inline: "center" });
                    const rect = target.getBoundingClientRect();
                    return { ok: true, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                shop_name,
            )
            if result.get("ok"):
                page.mouse.click(result["x"], result["y"])
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                return True
            page.wait_for_timeout(500)
        return False

    def _submit_multi_token_login(self, page: Page, user_id: int, multi_user_token: str, phone: str) -> None:
        page.evaluate(
            """({ userId, multiUserToken, phone }) => {
                const form = document.createElement("form");
                form.method = "POST";
                form.action = "https://id.kwaixiaodian.com/pass/kshop/web/login/multiTokenLogin";
                form.style.display = "none";
                const fields = {
                    sid: "kshop.api",
                    countryCode: "+86",
                    phone,
                    userId: String(userId),
                    multiUserToken,
                    encryptHeaders: ""
                };
                for (const [name, value] of Object.entries(fields)) {
                    const input = document.createElement("input");
                    input.name = name;
                    input.value = value;
                    form.appendChild(input);
                }
                document.body.appendChild(form);
                form.submit();
            }""",
            {"userId": user_id, "multiUserToken": multi_user_token, "phone": phone},
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass

    def _ensure_kuaishou_host(self, page: Page) -> None:
        if "s.kwaixiaodian.com" in page.url:
            return
        page.goto(KUAISHOU_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        self._ensure_logged_in(page)

    def _current_shop_is(self, page: Page, shop_name: str) -> bool:
        current_shop = self._current_shop_name(page)
        if current_shop:
            return current_shop == shop_name
        text = self._body_text(page)
        return shop_name in text and not self._switcher_is_open(page)

    def _current_shop_name(self, page: Page) -> str:
        try:
            value = page.evaluate(
                """() => {
                    const candidates = [];
                    const parse = (raw) => {
                        if (!raw) return;
                        try {
                            const data = JSON.parse(raw);
                            const text = JSON.stringify(data);
                            const keys = ["shopName", "shop_name", "sellerName", "merchantName", "storeName"];
                            const stack = [data];
                            while (stack.length) {
                                const node = stack.pop();
                                if (!node || typeof node !== "object") continue;
                                for (const key of keys) {
                                    if (typeof node[key] === "string" && node[key].trim()) candidates.push(node[key].trim());
                                }
                                for (const value of Object.values(node)) {
                                    if (value && typeof value === "object") stack.push(value);
                                }
                            }
                            if (text.includes("店")) candidates.push("");
                        } catch (error) {}
                    };
                    for (const store of [window.localStorage, window.sessionStorage]) {
                        for (let i = 0; i < store.length; i += 1) parse(store.getItem(store.key(i)));
                    }
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none"
                            && rect.width > 0 && rect.height > 0 && rect.top < 140 && rect.left > window.innerWidth * 0.45;
                    };
                    const nodes = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .map((el) => (el.innerText || el.textContent || "").trim())
                        .filter((text) => text && text.length >= 2 && text.length <= 60 && /店|图书|书|文化|母婴|玩具/.test(text));
                    candidates.push(...nodes);
                    return candidates.find(Boolean) || "";
                }"""
            )
            return value or ""
        except Exception:
            return ""

    def _open_shop_switcher(self, page: Page) -> None:
        if self._switcher_is_open(page):
            return
        for _ in range(60):
            self._raise_if_stopped()
            point = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none"
                            && rect.width > 0 && rect.height > 0 && rect.right >= 0 && rect.bottom >= 0;
                    };
                    const texts = ["切换店铺", "切换身份", "切换账号", "店铺"];
                    const nodes = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .map((el) => ({ el, text: (el.innerText || el.textContent || "").trim(), rect: el.getBoundingClientRect() }))
                        .filter((item) => item.rect.top < 120 && item.rect.left > window.innerWidth * 0.45)
                        .filter((item) => texts.some((text) => item.text.includes(text)) || /店|图书|书|文化|母婴|玩具/.test(item.text))
                        .sort((a, b) => b.rect.right - a.rect.right);
                    if (!nodes.length) return null;
                    const rect = nodes[0].rect;
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }"""
            )
            if not point:
                page.wait_for_timeout(500)
                continue
            page.mouse.click(point["x"], point["y"])
            page.wait_for_timeout(800)
            if self._switcher_is_open(page):
                return
        raise RuntimeError("未找到快手店铺切换入口，请确认页面右上角店铺名已加载。")

    def _switcher_is_open(self, page: Page) -> bool:
        text = self._body_text(page)
        return any(item in text for item in ("切换店铺", "选择店铺", "请选择店铺", "店铺切换", "切换身份"))

    def _click_shop_in_switcher(self, page: Page, shop_name: str) -> None:
        for _ in range(5):
            self._raise_if_stopped()
            result = page.evaluate(
                """(shopName) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none"
                            && rect.width > 0 && rect.height > 0 && rect.right >= 0 && rect.bottom >= 0;
                    };
                    const nodes = Array.from(document.querySelectorAll("div,span,button,[role='button']"))
                        .filter(visible)
                        .filter((el) => (el.innerText || el.textContent || "").trim().includes(shopName));
                    if (!nodes.length) return { ok: false };
                    let target = nodes[0];
                    let node = target;
                    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                        const text = node.innerText || "";
                        const rect = node.getBoundingClientRect();
                        if (text.includes(shopName) && rect.width >= 180 && rect.height >= 32 && rect.height <= 180) {
                            target = node;
                            break;
                        }
                    }
                    target.scrollIntoView({ block: "center", inline: "center" });
                    const rect = target.getBoundingClientRect();
                    return { ok: true, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                }""",
                shop_name,
            )
            if result.get("ok"):
                page.mouse.click(result["x"], result["y"])
                return
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(500)
        raise TimeoutError(f"快手店铺切换弹窗中未找到：{shop_name}")

    def _open_account_bill_page(self, page: Page) -> None:
        page.goto(KUAISHOU_ACCOUNT_BILL_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(60):
            self._raise_if_stopped()
            body = self._body_text(page)
            if self._is_login_page(page, body):
                raise RuntimeError("快手登录状态失效。请先补登录后重跑。")
            if "资金" in body and ("账单" in body or "账户" in body):
                return
            page.wait_for_timeout(500)
        raise TimeoutError("未能进入快手资金账单页面")

    def _extract_sub_mch_id(self, page: Page) -> str:
        for _ in range(40):
            self._raise_if_stopped()
            value = page.evaluate(
                """() => {
                    const patterns = [
                        /subMchId["'=:\\s]+(\\d{8,})/,
                        /sub_mch_id["'=:\\s]+(\\d{8,})/,
                        /当前账户[\\s\\S]{0,80}?(5\\d{10,})/,
                        /公司\\s+(5\\d{10,})/
                    ];
                    const scan = (text) => {
                        if (!text) return "";
                        for (const pattern of patterns) {
                            const match = String(text).match(pattern);
                            if (match) return match[1];
                        }
                        return "";
                    };
                    let found = scan(location.href) || scan(document.documentElement.innerHTML);
                    if (found) return found;
                    for (const store of [window.localStorage, window.sessionStorage]) {
                        for (let i = 0; i < store.length; i += 1) {
                            found = scan(store.getItem(store.key(i)));
                            if (found) return found;
                        }
                    }
                    found = scan(document.body ? document.body.innerText : "");
                    if (found) return found;
                    return "";
                }"""
            )
            if value:
                return str(value)
            page.wait_for_timeout(500)
        raise RuntimeError("未能从快手资金账单页面提取 subMchId")

    def _operator_id(self, page: Page) -> str:
        try:
            data = self._fetch_json(page, "GET", "/gateway/pc/workbench/service/stable/info", None, "快手用户信息")
            user_id = (((data.get("data") or {}).get("userInfo") or {}).get("userId"))
            if user_id:
                return str(user_id)
        except Exception:
            pass
        return ""

    def _create_export_task(self, page: Page, date_range: DateRange, sub_mch_id: str, operator_id: str) -> dict:
        start_ms = self._datetime_millis(date_range.start, datetime_time(0, 0, 0))
        end_ms = self._datetime_millis(date_range.end, datetime_time(23, 59, 59, 999000))
        export_config = {
            "startTime": start_ms,
            "endTime": end_ms,
            "dateType": 2,
            "accountType": KUAISHOU_ACCOUNT_TYPE,
            "accountRole": KUAISHOU_ACCOUNT_ROLE,
            "operatorName": operator_id,
            "subMchId": sub_mch_id,
        }
        payload = {
            "accountType": KUAISHOU_ACCOUNT_TYPE,
            "accountRole": KUAISHOU_ACCOUNT_ROLE,
            "operatorName": operator_id,
            "taskKind": KUAISHOU_TASK_KIND,
            "exportConfig": json.dumps(export_config, ensure_ascii=False, separators=(",", ":")),
            "bidBizType": KUAISHOU_BID_BIZ_TYPE,
        }
        return self._fetch_json(
            page,
            "POST",
            "/rest/app/tts/business/api/funds/wallet/create/export/task",
            payload,
            "快手保证金创建导出任务",
        )

    def _create_export_task_with_retry(
        self,
        page: Page,
        shop: Shop,
        date_range: DateRange,
        sub_mch_id: str,
        operator_id: str,
    ) -> dict:
        last_result: dict = {}
        for attempt in range(1, 4):
            self._raise_if_stopped()
            result = self._create_export_task(page, date_range, sub_mch_id, operator_id)
            if result.get("downloadUrl"):
                return result
            last_result = result
            if attempt < 3:
                print(f"{shop.name} 快手保证金导出暂未返回 downloadUrl，等待后重试 {attempt}/3：{result}")
                page.wait_for_timeout(5000 * attempt)
        return last_result

    def _download_csv(
        self,
        context: BrowserContext,
        url: str,
        output_dir: Path,
        shop: Shop,
        date_range: DateRange,
    ) -> Path:
        target = self._unique_download_path(output_dir, export_filename(shop, date_range, "保证金", ".csv"))
        try:
            response = context.request.get(
                url,
                timeout=60000,
                headers={"referer": KUAISHOU_ACCOUNT_BILL_URL},
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status} {response.status_text}")
            target.write_bytes(response.body())
        except Exception as exc:
            raise RuntimeError(f"快手保证金 CSV 下载失败：{exc}") from exc
        return target

    def _fetch_json(self, page: Page, method: str, path: str, payload: dict | None, label: str) -> dict:
        result = page.evaluate(
            """async ({ method, path, payload }) => {
                try {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 15000);
                    const response = await fetch(path, {
                        method,
                        credentials: "include",
                        headers: payload ? { "content-type": "application/json" } : undefined,
                        body: payload ? JSON.stringify(payload) : undefined,
                        signal: controller.signal
                    });
                    clearTimeout(timer);
                    const text = await response.text();
                    let data = null;
                    data = text ? JSON.parse(text) : {};
                    return { ok: response.ok, status: response.status, data, text };
                } catch (error) {
                    return { ok: false, status: 0, text: String(error && error.message ? error.message : error) };
                }
            }""",
            {"method": method, "path": path, "payload": payload},
        )
        if not result.get("ok"):
            raise RuntimeError(f"{label}接口请求失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if int(data.get("result", -1)) != 1:
            raise RuntimeError(f"{label}接口返回失败：{data}")
        return data

    def _fetch_form_json(self, page: Page, path: str, payload: dict, label: str) -> dict:
        result = page.evaluate(
            """async ({ path, payload }) => {
                try {
                    const form = new FormData();
                    for (const [key, value] of Object.entries(payload || {})) {
                        form.append(key, value);
                    }
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 15000);
                    const response = await fetch(path, {
                        method: "POST",
                        credentials: "include",
                        body: form,
                        signal: controller.signal
                    });
                    clearTimeout(timer);
                    const text = await response.text();
                    let data = null;
                    data = text ? JSON.parse(text) : {};
                    return { ok: response.ok, status: response.status, data, text };
                } catch (error) {
                    return { ok: false, status: 0, text: String(error && error.message ? error.message : error) };
                }
            }""",
            {"path": path, "payload": payload},
        )
        if not result.get("ok"):
            raise RuntimeError(f"{label}接口请求失败：HTTP {result.get('status')} {result.get('text', '')[:300]}")
        data = result.get("data") or {}
        if int(data.get("result", -1)) != 1:
            raise RuntimeError(f"{label}接口返回失败：{data}")
        return data

    def _datetime_millis(self, day: date, value: datetime_time) -> int:
        china = timezone(timedelta(hours=8))
        return int(datetime.combine(day, value, tzinfo=china).timestamp() * 1000)

    def _raise_if_stopped(self) -> None:
        if self._should_stop and self._should_stop():
            raise RuntimeError("用户已终止任务。")

    def _body_text(self, page: Page) -> str:
        try:
            return page.locator("body").inner_text(timeout=1500)
        except Exception:
            return ""

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
