from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import Shop
from .paths import browser_profile_dir


PDD_HOME_URL = "https://mms.pinduoduo.com/home"
PDD_NEGATIVE_EXPERIENCE_URL = "https://mms.pinduoduo.com/aftersales/customer_complain_appeal"
PDD_CUSTOMER_SERVICE_URL = "https://mms.pinduoduo.com/mms-chat/overview/service"
PDD_CUSTOMER_SERVICE_PERFORMANCE_URL = "https://mms.pinduoduo.com/mms-chat/overview/merchant"
DOUYIN_HOME_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"
KUAISHOU_HOME_URL = "https://s.kwaixiaodian.com/"
PDD_DATA_TYPE_URLS = {
    "returns": PDD_HOME_URL,
    "negative_experience": PDD_NEGATIVE_EXPERIENCE_URL,
    "delayed_shipping": PDD_HOME_URL,
    "customer_service_performance": PDD_CUSTOMER_SERVICE_PERFORMANCE_URL,
}


def find_edge() -> str:
    edge = shutil.which("msedge")
    if edge:
        return edge

    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("未找到 Microsoft Edge，请确认已安装 Edge 浏览器。")


def open_shop_browser(project_root: Path, platform: str, shop: Shop, url: str = PDD_HOME_URL) -> Path:
    profile_dir = browser_profile_dir(project_root, platform, shop)
    profile_dir.mkdir(parents=True, exist_ok=True)
    edge_path = find_edge()
    subprocess.Popen(
        [
            edge_path,
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            "--new-window",
            "--no-first-run",
            "--disable-background-mode",
            "--disable-sync",
            "--edge-skip-compat-layer-relaunch",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return profile_dir
