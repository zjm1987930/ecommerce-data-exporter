from __future__ import annotations

import os
from pathlib import Path
import re

from .config import Shop
from .dates import DateRange


def exports_root(project_root: Path) -> Path:
    return project_root / "原始数据"


def month_dir(project_root: Path, date_range: DateRange) -> Path:
    return exports_root(project_root) / date_range.month_key


def log_dir(project_root: Path, date_range: DateRange) -> Path:
    return exports_root(project_root) / "导出日志" / date_range.range_key


def data_type_dir(project_root: Path, date_range: DateRange, data_type_name: str) -> Path:
    return exports_root(project_root) / _safe_path_part(data_type_name)


def task_shop_dir(
    project_root: Path,
    date_range: DateRange,
    data_type_name: str,
    shop: Shop,
) -> Path:
    return data_type_dir(project_root, date_range, data_type_name)


def task_screenshot_dir(
    project_root: Path,
    date_range: DateRange,
    data_type_name: str,
    shop: Shop,
) -> Path:
    return data_type_dir(project_root, date_range, data_type_name) / "截图" / _safe_path_part(shop.name)


def export_filename(shop: Shop, date_range: DateRange, data_type_name: str, suffix: str) -> str:
    short_name = shop.sheet_name
    platform_name = shop.platform_name
    if short_name.startswith(platform_name):
        short_name = short_name[len(platform_name) :]
    return f"{date_range.range_key}{platform_name}{short_name}{data_type_name}{suffix}"


def platform_dir(project_root: Path, date_range: DateRange, platform_name: str) -> Path:
    return month_dir(project_root, date_range) / platform_name


def shop_dir(
    project_root: Path,
    date_range: DateRange,
    platform_name: str,
    shop: Shop,
) -> Path:
    return platform_dir(project_root, date_range, platform_name) / shop.name


def browser_profile_dir(project_root: Path, platform: str, shop: Shop) -> Path:
    profile_root = os.environ.get("PDD_EXPORTER_PROFILE_ROOT")
    if profile_root:
        return Path(profile_root) / platform / shop.id
    return project_root / "browser_profiles" / platform / shop.id


def ensure_output_dirs(
    project_root: Path,
    date_range: DateRange,
    platform_name: str,
    shops: list[Shop],
    data_type_names: list[str] | None = None,
) -> None:
    if not data_type_names:
        for shop in shops:
            base = shop_dir(project_root, date_range, platform_name, shop)
            (base / "延迟发货_平台原始文件").mkdir(parents=True, exist_ok=True)
            (base / "截图").mkdir(parents=True, exist_ok=True)
        return
    for data_type_name in data_type_names:
        base_type_dir = data_type_dir(project_root, date_range, data_type_name)
        base_type_dir.mkdir(parents=True, exist_ok=True)
        for shop in shops:
            base = task_screenshot_dir(project_root, date_range, data_type_name, shop)
            base.mkdir(parents=True, exist_ok=True)


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip()
    return cleaned or "未命名"
