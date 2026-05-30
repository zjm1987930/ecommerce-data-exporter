from __future__ import annotations

from playwright.sync_api import Page


PDD_PROTECTED_POPUP_TEXTS = [
    "批量导出报表",
    "近30个已生成报表",
    "确认生成",
    "生成成功",
    "导出",
    "下载表单",
    "查询",
]

DOUYIN_PROTECTED_POPUP_TEXTS = [
    "请选择店铺",
    "切换组织/店铺",
    "交易流水",
    "导出明细",
    "查询",
]


def dismiss_page_popups(
    page: Page,
    *,
    protected_texts: list[str] | None = None,
    attempts: int = 6,
    wait_ms: int = 350,
) -> None:
    protected = protected_texts or []
    for _ in range(attempts):
        target = page.evaluate(
            """(protectedTexts) => {
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
                const textOf = (el) => ((el.innerText || el.textContent || "").trim());
                const hasProtectedAncestor = (el) => {
                    let node = el;
                    while (node && node !== document.body) {
                        const text = (node.innerText || "").slice(0, 900);
                        if (protectedTexts.some((item) => item && text.includes(item))) {
                            return true;
                        }
                        node = node.parentElement;
                    }
                    return false;
                };
                const overlayScore = (el) => {
                    let node = el;
                    let score = 0;
                    while (node && node !== document.body) {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        const className = String(node.className || "");
                        const role = node.getAttribute("role") || "";
                        if (/fixed|sticky/.test(style.position)) score += 2;
                        if (/modal|dialog|drawer|popup|popover|mask|overlay|banner/i.test(className)) score += 2;
                        if (/dialog|alertdialog/.test(role)) score += 3;
                        const zIndex = Number.parseInt(style.zIndex || "0", 10);
                        if (Number.isFinite(zIndex) && zIndex >= 10) score += 1;
                        if (Number.isFinite(zIndex) && zIndex >= 100) score += 2;
                        if (rect.width > window.innerWidth * 0.25 && rect.height > window.innerHeight * 0.15) score += 1;
                        node = node.parentElement;
                    }
                    return score;
                };
                const overlayContext = (el) => {
                    let node = el;
                    while (node && node !== document.body) {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        const className = String(node.className || "");
                        const role = node.getAttribute("role") || "";
                        const isDialog = /dialog|alertdialog/.test(role)
                            || /modal|dialog|drawer|popup|popover|mask|overlay|banner/i.test(className);
                        const isFixedPanel = style.position === "fixed"
                            && rect.width > 120
                            && rect.height > 80;
                        const isCenteredPanel = rect.left > window.innerWidth * 0.12
                            && rect.right < window.innerWidth * 0.95
                            && rect.top > 20
                            && rect.bottom < window.innerHeight * 0.98
                            && rect.width > window.innerWidth * 0.18
                            && rect.height > window.innerHeight * 0.08;
                        const zIndex = Number.parseInt(style.zIndex || "0", 10);
                        if (isDialog || (Number.isFinite(zIndex) && zIndex >= 50 && (isFixedPanel || isCenteredPanel))) {
                            return true;
                        }
                        node = node.parentElement;
                    }
                    return false;
                };
                const nearTopRightOfCenteredPanel = (el) => {
                    const own = el.getBoundingClientRect();
                    const cx = own.left + own.width / 2;
                    const cy = own.top + own.height / 2;
                    let node = el.parentElement;
                    while (node && node !== document.body) {
                        const rect = node.getBoundingClientRect();
                        const text = (node.innerText || "").trim();
                        const isCenteredPanel = rect.left > window.innerWidth * 0.15
                            && rect.right < window.innerWidth * 0.9
                            && rect.top > window.innerHeight * 0.08
                            && rect.bottom < window.innerHeight * 0.95
                            && rect.width > window.innerWidth * 0.25
                            && rect.height > window.innerHeight * 0.18
                            && text.length > 8;
                        const nearRight = cx >= rect.right - 34 && cx <= rect.right + 28;
                        const nearTop = cy >= rect.top - 28 && cy <= rect.top + 38;
                        if (isCenteredPanel && nearRight && nearTop) return true;
                        node = node.parentElement;
                    }
                    return false;
                };
                const isCloseControl = (el, text, label, className, nearPanelClose) => {
                    if (el.closest("a[href]")) return false;
                    if (/^(×|x|X|关闭|關閉)$/.test(text)) return true;
                    if (/^(关闭|close|dismiss)$/i.test(label)) return true;
                    if (/close|modal-close|dialog-close|drawer-close|popup-close|icon-close/i.test(className)) return true;
                    const rect = el.getBoundingClientRect();
                    if (
                        nearPanelClose
                        && ["BUTTON", "I", "SPAN", "DIV", "svg"].includes(el.tagName)
                        && rect.width <= 64
                        && rect.height <= 64
                        && text.length <= 2
                    ) return true;
                    return false;
                };
                const scoreElement = (el) => {
                    if (!visible(el) || hasProtectedAncestor(el)) return -1;
                    const rect = el.getBoundingClientRect();
                    const text = textOf(el);
                    const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
                    const className = String(el.className || "");
                    const role = el.getAttribute("role") || "";
                    const nearPanelClose = nearTopRightOfCenteredPanel(el);
                    const inOverlay = overlayContext(el) || nearPanelClose;
                    if (!inOverlay) return -1;
                    if (!isCloseControl(el, text, label, className, nearPanelClose)) return -1;
                    if (rect.width > 120 || rect.height > 120) return -1;
                    let score = 0;
                    if (/^(×|x|X|关闭|關閉)$/.test(text)) score += 10;
                    if (/^(关闭|close|dismiss)$/i.test(label)) score += 10;
                    if (/close|modal-close|dialog-close|drawer-close|popup-close|icon-close/i.test(className)) score += 8;
                    if (nearPanelClose) score += 10;
                    if (role === "button" || ["BUTTON", "A", "I", "SPAN", "DIV", "svg"].includes(el.tagName)) score += 1;
                    if (rect.width <= 90 && rect.height <= 90) score += 3;
                    if (rect.width <= 44 && rect.height <= 44) score += 2;
                    score += Math.min(overlayScore(el), 8);
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (cx > window.innerWidth * 0.35 && cx < window.innerWidth * 0.9 && cy < window.innerHeight * 0.85) score += 1;
                    if (!text && !label && !/close/i.test(className)) score -= 3;
                    return score;
                };
                const elements = Array.from(document.querySelectorAll("button,a,i,span,div,svg,[role='button'],[aria-label],[title]"));
                const candidates = elements
                    .map((el) => ({ el, score: scoreElement(el), rect: el.getBoundingClientRect(), text: textOf(el) }))
                    .filter((item) => item.score >= 12)
                    .sort((a, b) => b.score - a.score);
                if (!candidates.length) return null;
                const best = candidates[0];
                return {
                    x: best.rect.left + best.rect.width / 2,
                    y: best.rect.top + best.rect.height / 2,
                    text: best.text,
                    score: best.score
                };
            }""",
            protected,
        )
        if not target:
            return
        page.mouse.click(target["x"], target["y"])
        page.wait_for_timeout(wait_ms)
