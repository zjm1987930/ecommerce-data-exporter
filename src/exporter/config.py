from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Shop:
    id: str
    name: str
    sheet_name: str
    platform: str = "pinduoduo"
    platform_name: str = "拼多多"
    profile_id: str | None = None


@dataclass(frozen=True)
class DataType:
    id: str
    name: str
    enabled: bool = True
    platform: str = "pinduoduo"


@dataclass(frozen=True)
class AppConfig:
    platform: str
    platform_name: str
    shops: list[Shop]
    data_types: list[DataType]


def load_config(root: Path) -> AppConfig:
    shops_path = root / "config" / "shops.json"
    tasks_path = root / "config" / "tasks.json"

    shops_data = json.loads(shops_path.read_text(encoding="utf-8"))
    tasks_data = json.loads(tasks_path.read_text(encoding="utf-8"))

    default_platform = shops_data["platform"]
    default_platform_name = shops_data["platform_name"]
    shops = []
    for item in shops_data["shops"]:
        platform = item.get("platform", default_platform)
        platform_name = item.get("platform_name", default_platform_name if platform == default_platform else platform)
        shops.append(
            Shop(
                id=item["id"],
                name=item["name"],
                sheet_name=item.get("sheet_name", item["name"]),
                platform=platform,
                platform_name=platform_name,
                profile_id=item.get("profile_id"),
            )
        )
    data_types = [
        DataType(
            id=item["id"],
            name=item["name"],
            enabled=item.get("enabled", True),
            platform=item.get("platform", default_platform),
        )
        for item in tasks_data["data_types"]
    ]
    return AppConfig(
        platform=shops_data["platform"],
        platform_name=shops_data["platform_name"],
        shops=shops,
        data_types=data_types,
    )
