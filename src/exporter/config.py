from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Shop:
    id: str
    name: str
    sheet_name: str


@dataclass(frozen=True)
class DataType:
    id: str
    name: str
    enabled: bool = True


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

    shops = [
        Shop(
            id=item["id"],
            name=item["name"],
            sheet_name=item.get("sheet_name", item["name"]),
        )
        for item in shops_data["shops"]
    ]
    data_types = [
        DataType(
            id=item["id"],
            name=item["name"],
            enabled=item.get("enabled", True),
        )
        for item in tasks_data["data_types"]
    ]
    return AppConfig(
        platform=shops_data["platform"],
        platform_name=shops_data["platform_name"],
        shops=shops,
        data_types=data_types,
    )
