from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
import sys
import threading
from pathlib import Path
from typing import Callable

from .automation.customer_service_performance import CustomerServicePerformanceAutomator
from .automation.delayed_shipping import DELAYED_TAB_ORDER, DelayedShippingAutomator
from .automation.douyin_deposit import DouyinDepositAutomator
from .automation.kuaishou_deposit import KuaishouDepositAutomator
from .automation.negative_experience import NegativeExperienceAutomator
from .automation.returns import ReturnsAutomator
from .browser import open_shop_browser, PDD_DATA_TYPE_URLS, PDD_HOME_URL
from .config import AppConfig, Shop, DataType
from .dates import DateRange
from .logging import ExportLogger, ExportLogRow
from .paths import ensure_output_dirs, export_filename, log_dir, shop_dir


@dataclass(frozen=True)
class RunSelection:
    shops: list[Shop]
    data_types: list[DataType]
    date_range: DateRange
    open_browser: bool = True
    browser_mode: str = "first_shop"
    user_action_callback: Callable[[str], None] | None = None
    stop_event: threading.Event | None = None
    dry_run: bool = False


class TaskCancelled(RuntimeError):
    pass


class ExportRunner:
    def __init__(self, project_root: Path, config: AppConfig) -> None:
        self.project_root = project_root
        self.config = config

    def run(self, selection: RunSelection, status_callback=None) -> tuple[Path, Path]:
        ensure_output_dirs(
            self.project_root,
            selection.date_range,
            self.config.platform_name,
            selection.shops,
            [data_type.name for data_type in selection.data_types],
        )
        logger = ExportLogger(log_dir(self.project_root, selection.date_range))
        has_real_automation = any(
            data_type.id in {
                "returns",
                "negative_experience",
                "delayed_shipping",
                "customer_service_performance",
                "douyin_deposit",
                "kuaishou_deposit",
            }
            for data_type in selection.data_types
        )
        shops_to_open = self._shops_to_open(selection)

        if has_real_automation:
            self._status(
                status_callback,
                "自动化任务将直接启动浏览器执行；如登录失效，请用 GUI 的“补登录选中店铺”补登录后单店补跑。",
            )
        if selection.dry_run:
            self._status(status_callback, "当前为试运行模式：支持的任务只查询或只生成下载地址，不保存导出文件。")

        delayed_automator = (
            DelayedShippingAutomator(self.project_root, self.config.platform, self.config.platform_name)
            if self._data_type_by_id(selection.data_types, "delayed_shipping")
            else None
        )
        returns_automator = (
            ReturnsAutomator(self.project_root, self.config.platform, self.config.platform_name)
            if self._data_type_by_id(selection.data_types, "returns")
            else None
        )
        negative_automator = (
            NegativeExperienceAutomator(self.project_root, self.config.platform, self.config.platform_name)
            if self._data_type_by_id(selection.data_types, "negative_experience")
            else None
        )
        customer_service_automator = (
            CustomerServicePerformanceAutomator(self.project_root, self.config.platform, self.config.platform_name)
            if self._data_type_by_id(selection.data_types, "customer_service_performance")
            else None
        )
        douyin_deposit_automator = (
            DouyinDepositAutomator(self.project_root)
            if self._data_type_by_id(selection.data_types, "douyin_deposit")
            else None
        )
        kuaishou_deposit_automator = (
            KuaishouDepositAutomator(self.project_root)
            if self._data_type_by_id(selection.data_types, "kuaishou_deposit")
            else None
        )

        douyin_deposit_type = self._data_type_by_id(selection.data_types, "douyin_deposit")
        if (
            douyin_deposit_automator
            and douyin_deposit_type
            and all(data_type.id == "douyin_deposit" for data_type in selection.data_types)
        ):
            failures = self._run_douyin_deposit_batch(
                douyin_deposit_automator,
                logger,
                douyin_deposit_type,
                selection,
                status_callback,
            )
            if failures:
                self._status(status_callback, "全部选中任务已执行完成，但存在失败项，需要补跑：")
                for item in failures:
                    self._status(status_callback, item)
            else:
                self._status(status_callback, "全部选中任务已执行完成，没有失败项。")
            return logger.write()

        kuaishou_deposit_type = self._data_type_by_id(selection.data_types, "kuaishou_deposit")
        if (
            kuaishou_deposit_automator
            and kuaishou_deposit_type
            and all(data_type.id == "kuaishou_deposit" for data_type in selection.data_types)
        ):
            failures = self._run_kuaishou_deposit_batch(
                kuaishou_deposit_automator,
                logger,
                kuaishou_deposit_type,
                selection,
                status_callback,
            )
            if failures:
                self._status(status_callback, "全部选中任务已执行完成，但存在失败项，需要补跑：")
                for item in failures:
                    self._status(status_callback, item)
            else:
                self._status(status_callback, "全部选中任务已执行完成，没有失败项。")
            return logger.write()

        failures: list[str] = []
        for shop in selection.shops:
            self._raise_if_cancelled(selection)
            if selection.open_browser and shop in shops_to_open and not has_real_automation:
                url = self._initial_url(selection.data_types)
                self._status(status_callback, f"打开 {shop.name} 的独立登录环境")
                open_shop_browser(self.project_root, self.config.platform, shop, url)
            elif selection.open_browser and shop in shops_to_open and has_real_automation:
                self._status(status_callback, f"{shop.name}: 已跳过额外打开登录环境，避免占用自动化 profile")

            for data_type in selection.data_types:
                self._raise_if_cancelled(selection)
                if data_type.platform != shop.platform:
                    continue
                if data_type.id == "delayed_shipping":
                    assert delayed_automator is not None
                    status, reason = self._run_delayed_shipping_shop(delayed_automator, logger, shop, data_type, selection, status_callback)
                elif data_type.id == "returns":
                    assert returns_automator is not None
                    status, reason = self._run_returns_shop(returns_automator, logger, shop, data_type, selection, status_callback)
                elif data_type.id == "negative_experience":
                    assert negative_automator is not None
                    status, reason = self._run_negative_experience_shop(
                        negative_automator,
                        logger,
                        shop,
                        data_type,
                        selection,
                        status_callback,
                    )
                elif data_type.id == "customer_service_performance":
                    assert customer_service_automator is not None
                    status, reason = self._run_customer_service_performance_shop(
                        customer_service_automator,
                        logger,
                        shop,
                        data_type,
                        selection,
                        status_callback,
                    )
                elif data_type.id == "douyin_deposit":
                    assert douyin_deposit_automator is not None
                    status, reason = self._run_douyin_deposit_shop(
                        douyin_deposit_automator,
                        logger,
                        shop,
                        data_type,
                        selection,
                        status_callback,
                    )
                elif data_type.id == "kuaishou_deposit":
                    assert kuaishou_deposit_automator is not None
                    status, reason = self._run_kuaishou_deposit_shop(
                        kuaishou_deposit_automator,
                        logger,
                        shop,
                        data_type,
                        selection,
                        status_callback,
                    )
                else:
                    self._log_skeleton_task(logger, shop, data_type, selection, status_callback)
                    status, reason = "待接入", ""
                if self._is_failed_status(status):
                    failures.append(f"{shop.name} - {data_type.name}: {reason}")

        if failures:
            self._status(status_callback, "全部选中任务已执行完成，但存在失败项，需要补跑：")
            for item in failures:
                self._status(status_callback, item)
        else:
            self._status(status_callback, "全部选中任务已执行完成，没有失败项。")
        return logger.write()

    def _run_douyin_deposit_batch(
        self,
        automator: DouyinDepositAutomator,
        logger: ExportLogger,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> list[str]:
        shops = [shop for shop in selection.shops if shop.platform == data_type.platform]
        failures: list[str] = []
        started_at_map = {shop: datetime.now().isoformat(timespec="seconds") for shop in shops}
        for shop in shops:
            self._raise_if_cancelled(selection)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 等待抖音共享浏览器批量执行")

        writer = _StatusWriter(status_callback)
        with redirect_stdout(writer), redirect_stderr(writer):
            results = automator.run_batch(
                shops,
                selection.date_range,
                dry_run=selection.dry_run,
                should_stop=lambda: bool(selection.stop_event and selection.stop_event.is_set()),
            )
        writer.flush()
        self._raise_if_cancelled(selection)

        for shop in shops:
            self._raise_if_cancelled(selection)
            result = results.get(shop, None)
            status = result.status if result else "失败"
            reason = result.message if result else "未取得执行结果"
            file_path = str(result.downloaded_file) if result and result.downloaded_file else ""
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=shop.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at_map.get(shop, datetime.now().isoformat(timespec="seconds")),
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if status == "无数据":
                self._status(status_callback, f"{shop.name} - {data_type.name}: 无数据，已跳过下载，继续下一家")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
            if self._is_failed_status(status):
                self._status(status_callback, f"{shop.name} - {data_type.name}: {reason}")
                failures.append(f"{shop.name} - {data_type.name}: {reason}")
        return failures

    def _run_kuaishou_deposit_batch(
        self,
        automator: KuaishouDepositAutomator,
        logger: ExportLogger,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> list[str]:
        shops = [shop for shop in selection.shops if shop.platform == data_type.platform]
        failures: list[str] = []
        started_at_map = {shop: datetime.now().isoformat(timespec="seconds") for shop in shops}
        self._status(status_callback, "快手保证金当前按平台月账单导出；选择月内任意日期范围时，平台返回该月份整月账单。")
        for shop in shops:
            self._raise_if_cancelled(selection)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 等待快手共享浏览器批量执行")

        writer = _StatusWriter(status_callback)
        with redirect_stdout(writer), redirect_stderr(writer):
            results = automator.run_batch(
                shops,
                selection.date_range,
                dry_run=selection.dry_run,
                should_stop=lambda: bool(selection.stop_event and selection.stop_event.is_set()),
                user_action_callback=selection.user_action_callback,
            )
        writer.flush()
        self._raise_if_cancelled(selection)

        for shop in shops:
            self._raise_if_cancelled(selection)
            result = results.get(shop, None)
            status = result.status if result else "失败"
            reason = result.message if result else "未取得执行结果"
            file_path = str(result.downloaded_file) if result and result.downloaded_file else ""
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=shop.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at_map.get(shop, datetime.now().isoformat(timespec="seconds")),
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if status == "无数据":
                self._status(status_callback, f"{shop.name} - {data_type.name}: 无数据，已跳过下载，继续下一家")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
            if self._is_failed_status(status):
                self._status(status_callback, f"{shop.name} - {data_type.name}: {reason}")
                failures.append(f"{shop.name} - {data_type.name}: {reason}")
        return failures

    def run_skeleton(self, selection: RunSelection, status_callback=None) -> tuple[Path, Path]:
        ensure_output_dirs(
            self.project_root,
            selection.date_range,
            self.config.platform_name,
            selection.shops,
            [data_type.name for data_type in selection.data_types],
        )
        logger = ExportLogger(log_dir(self.project_root, selection.date_range))

        shops_to_open = self._shops_to_open(selection)

        for shop in selection.shops:
            if selection.open_browser and shop in shops_to_open:
                url = self._initial_url(selection.data_types)
                self._status(status_callback, f"打开 {shop.name} 的独立登录环境")
                open_shop_browser(self.project_root, self.config.platform, shop, url)

            for data_type in selection.data_types:
                started_at = datetime.now().isoformat(timespec="seconds")
                target_dir = shop_dir(
                    self.project_root,
                    selection.date_range,
                    self.config.platform_name,
                    shop,
                )
                reason = "第一版骨架已创建任务，页面自动导出逻辑待接入"
                status = "待接入"
                file_path = ""

                if data_type.id == "delayed_shipping":
                    file_path = str(target_dir / self._delayed_shipping_name(shop, selection.date_range))
                    reason = "已预留延迟发货原始文件目录和整合输出路径"

                logger.add(
                    ExportLogRow(
                        month=selection.date_range.month_key,
                        platform=self.config.platform_name,
                        shop=shop.name,
                        data_type=data_type.name,
                        start_date=selection.date_range.start.isoformat(),
                        end_date=selection.date_range.end.isoformat(),
                        status=status,
                        file_path=file_path,
                        reason=reason,
                        started_at=started_at,
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                    )
                )
                self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")

        return logger.write()

    def _run_delayed_shipping_shop(
        self,
        automator: DelayedShippingAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始执行前三个 TAB 导出")
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run_shop(
                    shop,
                    selection.date_range,
                    tabs=DELAYED_TAB_ORDER,
                    dry_run=selection.dry_run,
                    user_action_callback=selection.user_action_callback,
                )
            status = result.status
            reason = result.message
            if result.output_file:
                file_path = str(result.output_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if status == "无数据":
                self._status(status_callback, f"{shop.name} - {data_type.name}: 无数据，已跳过下载，继续下一家")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=self.config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _run_douyin_deposit_shop(
        self,
        automator: DouyinDepositAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始下载抖音体验保证金")
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run(
                    shop,
                    selection.date_range,
                    dry_run=selection.dry_run,
                    should_stop=lambda: bool(selection.stop_event and selection.stop_event.is_set()),
                )
            self._raise_if_cancelled(selection)
            status = result.status
            reason = result.message
            if result.downloaded_file:
                file_path = str(result.downloaded_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=shop.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _run_kuaishou_deposit_shop(
        self,
        automator: KuaishouDepositAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始下载快手保证金")
        self._status(status_callback, "快手保证金当前按平台月账单导出；选择月内任意日期范围时，平台返回该月份整月账单。")
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run(
                    shop,
                    selection.date_range,
                    dry_run=selection.dry_run,
                    should_stop=lambda: bool(selection.stop_event and selection.stop_event.is_set()),
                    user_action_callback=selection.user_action_callback,
                )
            self._raise_if_cancelled(selection)
            status = result.status
            reason = result.message
            if result.downloaded_file:
                file_path = str(result.downloaded_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=shop.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _run_returns_shop(
        self,
        automator: ReturnsAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始执行退货导出")
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run(
                    shop,
                    selection.date_range,
                    dry_run=selection.dry_run,
                    user_action_callback=selection.user_action_callback,
                )
            status = result.status
            reason = result.message
            if result.downloaded_file:
                file_path = str(result.downloaded_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=self.config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _run_negative_experience_shop(
        self,
        automator: NegativeExperienceAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始抓取负向体验")
        if selection.dry_run:
            status = "试运行跳过"
            reason = "负向体验暂无只查询不写文件模式；试运行未抓取和写入工作簿"
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=self.config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            self._status(status_callback, reason)
            return status, reason
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run_batch(
                    [shop],
                    selection.date_range,
                    user_action_callback=selection.user_action_callback,
                )
            status = "失败" if result.failures and shop.name in result.failures else result.status
            reason = (result.failures or {}).get(shop.name, result.message)
            if result.output_file:
                file_path = str(result.output_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=self.config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _run_customer_service_performance_shop(
        self,
        automator: CustomerServicePerformanceAutomator,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> tuple[str, str]:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "失败"
        file_path = ""
        reason = ""
        self._status(status_callback, f"{shop.name} - {data_type.name}: 开始下载客服绩效")
        try:
            writer = _StatusWriter(status_callback)
            with redirect_stdout(writer), redirect_stderr(writer):
                result = automator.run(
                    shop,
                    selection.date_range,
                    dry_run=selection.dry_run,
                    user_action_callback=selection.user_action_callback,
                )
            status = result.status
            reason = result.message
            if result.downloaded_file:
                file_path = str(result.downloaded_file)
            self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")
            if file_path:
                self._status(status_callback, f"{shop.name} - 输出文件: {file_path}")
        except TaskCancelled:
            raise
        except Exception as exc:
            reason = str(exc)
            self._status(status_callback, f"{shop.name} - {data_type.name}: 失败")
            self._status(status_callback, reason)
        finally:
            logger.add(
                ExportLogRow(
                    month=selection.date_range.month_key,
                    platform=self.config.platform_name,
                    shop=shop.name,
                    data_type=data_type.name,
                    start_date=selection.date_range.start.isoformat(),
                    end_date=selection.date_range.end.isoformat(),
                    status=status,
                    file_path=file_path,
                    reason=reason,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return status, reason

    def _log_skeleton_task(
        self,
        logger: ExportLogger,
        shop: Shop,
        data_type: DataType,
        selection: RunSelection,
        status_callback,
    ) -> None:
        started_at = datetime.now().isoformat(timespec="seconds")
        target_dir = shop_dir(
            self.project_root,
            selection.date_range,
            self.config.platform_name,
            shop,
        )
        reason = "第一版骨架已创建任务，页面自动导出逻辑待接入"
        status = "待接入"
        file_path = ""

        if data_type.id == "delayed_shipping":
            file_path = str(target_dir / self._delayed_shipping_name(shop, selection.date_range))
            reason = "已预留延迟发货原始文件目录和整合输出路径"
        elif data_type.id == "customer_service_performance":
            file_path = str(target_dir / self._customer_service_performance_name(shop, selection.date_range))
            reason = "客服绩效数据入口已预留，页面规则和自动化待接入"

        logger.add(
            ExportLogRow(
                month=selection.date_range.month_key,
                platform=self.config.platform_name,
                shop=shop.name,
                data_type=data_type.name,
                start_date=selection.date_range.start.isoformat(),
                end_date=selection.date_range.end.isoformat(),
                status=status,
                file_path=file_path,
                reason=reason,
                started_at=started_at,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        )
        self._status(status_callback, f"{shop.name} - {data_type.name}: {status}")

    def _data_type_by_id(self, data_types: list[DataType], data_type_id: str) -> DataType | None:
        return next((data_type for data_type in data_types if data_type.id == data_type_id), None)

    def _delayed_shipping_name(self, shop: Shop, date_range: DateRange) -> str:
        return export_filename(shop, date_range, "延迟发货", ".xlsx")

    def _customer_service_performance_name(self, shop: Shop, date_range: DateRange) -> str:
        return export_filename(shop, date_range, "客服绩效", ".xls")

    def _shops_to_open(self, selection: RunSelection) -> set[Shop]:
        if not selection.open_browser:
            return set()
        if selection.browser_mode == "none":
            return set()
        if selection.browser_mode == "all":
            return set(selection.shops)
        if not selection.shops:
            return set()
        return {selection.shops[0]}

    def _initial_url(self, data_types: list[DataType]) -> str:
        if not data_types:
            return PDD_HOME_URL
        return PDD_DATA_TYPE_URLS.get(data_types[0].id, PDD_HOME_URL)

    def _status(self, callback, message: str) -> None:
        if callback:
            callback(message)

    def _raise_if_cancelled(self, selection: RunSelection) -> None:
        if selection.stop_event and selection.stop_event.is_set():
            raise TaskCancelled("用户已终止任务。")

    def _is_failed_status(self, status: str) -> bool:
        return "失败" in status or "澶辫触" in status


class _StatusWriter:
    def __init__(self, callback) -> None:
        self.callback = callback
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        if not self.callback:
            sys.__stdout__.write(text)
            return len(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self.callback(line)
        return len(text)

    def flush(self) -> None:
        if self.callback and self._buffer.strip():
            self.callback(self._buffer.strip())
        elif not self.callback and self._buffer:
            sys.__stdout__.write(self._buffer)
        self._buffer = ""
