from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exporter.config import load_config
from exporter.dates import previous_month, split_range
from exporter.paths import ensure_output_dirs
from exporter.runner import ExportRunner, RunSelection
from exporter.workbooks import merge_delayed_shipping
from exporter.automation.returns import ReturnsAutomator


def main() -> None:
    config = load_config(ROOT)
    ReturnsAutomator(ROOT, config.platform, config.platform_name)
    assert len(config.shops) >= 9, len(config.shops)
    data_type_ids = {item.id for item in config.data_types}
    expected_data_type_ids = {
        "returns",
        "negative_experience",
        "delayed_shipping",
        "customer_service_performance",
    }
    assert expected_data_type_ids.issubset(data_type_ids), data_type_ids

    date_range = previous_month()
    ensure_output_dirs(ROOT, date_range, config.platform_name, config.shops[:1], [config.data_types[0].name])
    chunks = split_range(date_range, 7)
    assert chunks, "date split failed"

    sample_dir = ROOT / "示例" / "延迟发货"
    if sample_dir.exists():
        output = ROOT / "原始数据" / "_验证" / "4月拼多多大雁延迟发货.xlsx"
        merge_delayed_shipping(sample_dir, output)
        assert output.exists(), output

    runner = ExportRunner(ROOT, config)
    csv_path, json_path = runner.run_skeleton(
        RunSelection(
            shops=config.shops[:1],
            data_types=config.data_types[:1],
            date_range=date_range,
            open_browser=False,
        )
    )
    assert csv_path.exists(), csv_path
    assert json_path.exists(), json_path

    print("CHECK_OK")
    print(f"shops={len(config.shops)} data_types={len(config.data_types)} month={date_range.month_key}")
    print(f"log={csv_path}")


if __name__ == "__main__":
    main()
