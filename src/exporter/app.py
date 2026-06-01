from __future__ import annotations

from pathlib import Path
import calendar
from datetime import datetime
import json
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .browser import DOUYIN_HOME_URL, KUAISHOU_HOME_URL, PDD_HOME_URL, open_shop_browser
from .config import AppConfig, load_config, Shop, DataType
from .dates import previous_month, parse_date, DateRange
from .runner import ExportRunner, RunSelection, TaskCancelled


class ExporterApp(tk.Tk):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root
        self.config: AppConfig = load_config(project_root)
        self.runner = ExportRunner(project_root, self.config)
        self.status_queue: queue.Queue[str] = queue.Queue()
        self.shop_vars: dict[str, tk.BooleanVar] = {}
        self.type_vars: dict[str, tk.BooleanVar] = {}
        self.user_action_event = threading.Event()
        self.stop_event = threading.Event()
        self.dry_run_var = tk.BooleanVar(value=False)
        self.platform_var = tk.StringVar(value=self.config.platform)
        self.shop_grid: ttk.Frame | None = None
        self.type_frame: ttk.LabelFrame | None = None

        self.title("电商平台原始数据自动导出工具")
        self.geometry("980x720")
        self.minsize(880, 620)
        self._build_ui()
        self.after(200, self._drain_status)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        platform_frame = ttk.LabelFrame(root, text="平台")
        platform_frame.pack(fill=tk.X, pady=(0, 12))
        platform_values = [f"{platform} | {name}" for platform, name in self._platform_options()]
        self.platform_combo = ttk.Combobox(platform_frame, values=platform_values, width=32, state="readonly")
        self.platform_combo.pack(side=tk.LEFT, padx=8, pady=8)
        self.platform_combo.current(0)
        self.platform_combo.bind("<<ComboboxSelected>>", self._platform_changed)

        date_range = previous_month()
        date_frame = ttk.LabelFrame(root, text="日期范围")
        date_frame.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(date_frame, text="开始日期").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        self.start_var = tk.StringVar(value=date_range.start.isoformat())
        ttk.Entry(date_frame, textvariable=self.start_var, width=16).grid(row=0, column=1, padx=(8, 2), pady=8)
        ttk.Button(date_frame, text="选择", command=lambda: self._pick_date(self.start_var)).grid(
            row=0,
            column=2,
            padx=(2, 8),
            pady=8,
        )
        self.end_var = tk.StringVar(value=date_range.end.isoformat())
        ttk.Label(date_frame, text="结束日期").grid(row=0, column=3, padx=8, pady=8, sticky=tk.W)
        ttk.Entry(date_frame, textvariable=self.end_var, width=16).grid(row=0, column=4, padx=(8, 2), pady=8)
        ttk.Button(date_frame, text="选择", command=lambda: self._pick_date(self.end_var)).grid(
            row=0,
            column=5,
            padx=(2, 8),
            pady=8,
        )
        shop_frame = ttk.LabelFrame(root, text="店铺")
        shop_frame.pack(fill=tk.X, pady=(0, 12))
        shop_actions = ttk.Frame(shop_frame)
        shop_actions.pack(fill=tk.X, padx=8, pady=(8, 0))
        ttk.Button(shop_actions, text="全选店铺", command=lambda: self._set_all(self.shop_vars, True)).pack(side=tk.LEFT)
        ttk.Button(shop_actions, text="清空店铺", command=lambda: self._set_all(self.shop_vars, False)).pack(side=tk.LEFT, padx=8)

        self.shop_grid = ttk.Frame(shop_frame)
        self.shop_grid.pack(fill=tk.X, padx=8, pady=8)

        self.type_frame = ttk.LabelFrame(root, text="数据类型")
        self.type_frame.pack(fill=tk.X, pady=(0, 12))
        self._render_platform_scope()

        action_frame = ttk.Frame(root)
        action_frame.pack(fill=tk.X, pady=(0, 12))
        self.run_button = ttk.Button(action_frame, text="开始生成任务", command=self._start_run)
        self.run_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(action_frame, text="终止任务", command=self._stop_run, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=8)
        self.login_button = ttk.Button(action_frame, text="补登录选中店铺", command=self._start_login_selected)
        self.login_button.pack(side=tk.LEFT, padx=8)
        self.continue_button = ttk.Button(
            action_frame,
            text="我已完成人工操作，继续",
            command=self._continue_after_user_action,
            state=tk.DISABLED,
        )
        self.continue_button.pack(side=tk.LEFT, padx=8)
        ttk.Button(action_frame, text="打开输出目录", command=self._open_output_dir).pack(side=tk.LEFT, padx=8)

        toolbox_frame = ttk.LabelFrame(root, text="工具箱")
        toolbox_frame.pack(fill=tk.X, pady=(0, 12))
        ttk.Checkbutton(
            toolbox_frame,
            text="试运行：只查询/只生成下载地址，不保存导出文件",
            variable=self.dry_run_var,
        ).pack(side=tk.LEFT, padx=8, pady=8)
        ttk.Button(toolbox_frame, text="生成调试命令", command=self._show_debug_commands).pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbox_frame, text="添加店铺配置", command=self._add_shop_config).pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbox_frame, text="删除店铺配置", command=self._delete_shop_config).pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbox_frame, text="清理原始数据目录", command=self._clean_output_dir).pack(side=tk.LEFT, padx=8)

        log_frame = ttk.LabelFrame(root, text="运行状态")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=18, wrap=tk.WORD)
        self.log_text.tag_configure("normal", foreground="#1f2937")
        self.log_text.tag_configure("task", foreground="#0f766e", font=("TkDefaultFont", 10, "bold"))
        self.log_text.tag_configure("error", foreground="#b91c1c", font=("TkDefaultFont", 10, "bold"))
        self.log_text.tag_configure("action", foreground="#92400e", font=("TkDefaultFont", 10, "bold"))
        self.log_text.tag_configure("success", foreground="#166534", font=("TkDefaultFont", 10, "bold"))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._log("第一版骨架已就绪。选择店铺、数据类型和日期范围后开始。")

    def _set_all(self, vars_by_id: dict[str, tk.BooleanVar], value: bool) -> None:
        for var in vars_by_id.values():
            var.set(value)

    def _selected_shops(self) -> list[Shop]:
        platform = self._current_platform()
        return [shop for shop in self.config.shops if shop.platform == platform and self.shop_vars[shop.id].get()]

    def _selected_types(self) -> list[DataType]:
        platform = self._current_platform()
        return [item for item in self.config.data_types if item.platform == platform and self.type_vars[item.id].get()]

    def _platform_options(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        seen[self.config.platform] = self.config.platform_name
        for shop in self.config.shops:
            seen.setdefault(shop.platform, shop.platform_name)
        for data_type in self.config.data_types:
            if data_type.platform == "douyin":
                seen.setdefault(data_type.platform, "抖音")
            elif data_type.platform == "kuaishou":
                seen.setdefault(data_type.platform, "快手")
            else:
                seen.setdefault(data_type.platform, data_type.platform)
        return list(seen.items())

    def _current_platform(self) -> str:
        value = self.platform_combo.get() if hasattr(self, "platform_combo") else self.platform_var.get()
        if "|" in value:
            return value.split("|", 1)[0].strip()
        return value.strip() or self.config.platform

    def _platform_changed(self, _event=None) -> None:
        self.platform_var.set(self._current_platform())
        self._render_platform_scope()

    def _render_platform_scope(self) -> None:
        platform = self._current_platform()
        self.shop_vars.clear()
        self.type_vars.clear()
        if self.shop_grid is not None:
            for child in self.shop_grid.winfo_children():
                child.destroy()
            shops = [shop for shop in self.config.shops if shop.platform == platform]
            for index, shop in enumerate(shops):
                var = tk.BooleanVar(value=True)
                self.shop_vars[shop.id] = var
                ttk.Checkbutton(self.shop_grid, text=shop.name, variable=var).grid(
                    row=index // 3,
                    column=index % 3,
                    sticky=tk.W,
                    padx=8,
                    pady=4,
                )
        if self.type_frame is not None:
            for child in self.type_frame.winfo_children():
                child.destroy()
            data_types = [item for item in self.config.data_types if item.platform == platform]
            for index, data_type in enumerate(data_types):
                var = tk.BooleanVar(value=True)
                self.type_vars[data_type.id] = var
                ttk.Checkbutton(self.type_frame, text=data_type.name, variable=var).grid(
                    row=0,
                    column=index,
                    sticky=tk.W,
                    padx=12,
                    pady=8,
                )

    def _date_range(self) -> DateRange:
        start = parse_date(self.start_var.get())
        end = parse_date(self.end_var.get())
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        return DateRange(start, end)

    def _start_run(self) -> None:
        try:
            shops = self._selected_shops()
            data_types = self._selected_types()
            date_range = self._date_range()
            if not shops:
                raise ValueError("请至少选择一个店铺")
            if not data_types:
                raise ValueError("请至少选择一种数据类型")
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.stop_event.clear()
        self.run_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._log("开始生成任务...")
        selection = RunSelection(
            shops=shops,
            data_types=data_types,
            date_range=date_range,
            open_browser=False,
            browser_mode="first_shop",
            user_action_callback=self._wait_for_user_action_from_worker,
            stop_event=self.stop_event,
            dry_run=self.dry_run_var.get(),
        )
        thread = threading.Thread(target=self._run_worker, args=(selection,), daemon=True)
        thread.start()

    def _stop_run(self) -> None:
        self.stop_event.set()
        self.user_action_event.set()
        self.stop_button.configure(state=tk.DISABLED)
        self.continue_button.configure(state=tk.DISABLED)
        self._log("已请求终止任务。当前浏览器步骤结束后将停止，不再继续后续店铺或数据类型。")

    def _start_login_selected(self) -> None:
        shops = self._selected_shops()
        if not shops:
            messagebox.showerror("参数错误", "请至少选择一个要补登录的店铺")
            return
        self.login_button.configure(state=tk.DISABLED)
        thread = threading.Thread(target=self._login_selected_worker, args=(shops,), daemon=True)
        thread.start()

    def _login_selected_worker(self, shops: list[Shop]) -> None:
        try:
            targets = self._login_targets(shops)
            for index, (display_name, platform, profile_shop, url) in enumerate(targets, start=1):
                self.status_queue.put(f"补登录 [{index}/{len(targets)}]: 打开 {display_name} 的普通 Edge 登录环境")
                open_shop_browser(self.project_root, platform, profile_shop, url)
                if index < len(targets):
                    self._wait_for_user_action_from_worker(
                        f"请在普通 Edge 中完成 {display_name} 登录，确认进入后台后关闭该 Edge 窗口，再点击“我已完成人工操作，继续”打开下一个登录环境。"
                    )
            self.status_queue.put("补登录流程已完成。请确认所有普通 Edge 登录窗口均已关闭，再回 GUI 执行任务。")
        except Exception as exc:
            self.status_queue.put(f"补登录失败: {exc}")
        finally:
            self.status_queue.put("__LOGIN_DONE__")

    def _login_targets(self, shops: list[Shop]) -> list[tuple[str, str, Shop, str]]:
        targets: list[tuple[str, str, Shop, str]] = []
        seen: set[tuple[str, str]] = set()
        for shop in shops:
            if shop.platform == "douyin":
                profile_id = shop.profile_id or "douyin_default"
                key = (shop.platform, profile_id)
                if key in seen:
                    continue
                seen.add(key)
                profile_shop = Shop(
                    id=profile_id,
                    name="抖音默认账号",
                    sheet_name="抖音默认账号",
                    platform=shop.platform,
                    platform_name=shop.platform_name,
                )
                targets.append(("抖音默认账号", shop.platform, profile_shop, DOUYIN_HOME_URL))
            elif shop.platform == "kuaishou":
                profile_id = shop.profile_id or "kuaishou_default"
                key = (shop.platform, profile_id)
                if key in seen:
                    continue
                seen.add(key)
                profile_shop = Shop(
                    id=profile_id,
                    name=shop.name,
                    sheet_name=shop.sheet_name,
                    platform=shop.platform,
                    platform_name=shop.platform_name,
                )
                targets.append((shop.name, shop.platform, profile_shop, KUAISHOU_HOME_URL))
            else:
                key = (shop.platform, shop.id)
                if key in seen:
                    continue
                seen.add(key)
                targets.append((shop.name, shop.platform, shop, PDD_HOME_URL))
        return targets

    def _wait_for_user_action_from_worker(self, message: str) -> None:
        self.user_action_event.clear()
        self.status_queue.put("__USER_ACTION_REQUIRED__")
        self.status_queue.put(message)
        self.user_action_event.wait()
        if self.stop_event.is_set():
            raise TaskCancelled("用户已终止任务。")
        self.status_queue.put("已收到继续信号，自动化继续执行。")

    def _continue_after_user_action(self) -> None:
        self.user_action_event.set()
        self.continue_button.configure(state=tk.DISABLED)

    def _run_worker(self, selection: RunSelection) -> None:
        try:
            csv_path, json_path = self.runner.run(selection, self.status_queue.put)
            self.status_queue.put(f"日志已生成: {csv_path}")
            self.status_queue.put(f"机器日志已生成: {json_path}")
        except TaskCancelled as exc:
            self.status_queue.put(f"任务已终止: {exc}")
        except Exception as exc:
            self.status_queue.put(f"运行失败: {exc}")
        finally:
            self.status_queue.put("__RUN_DONE__")

    def _drain_status(self) -> None:
        while True:
            try:
                message = self.status_queue.get_nowait()
            except queue.Empty:
                break
            if message == "__RUN_DONE__":
                self.run_button.configure(state=tk.NORMAL)
                self.stop_button.configure(state=tk.DISABLED)
                self.continue_button.configure(state=tk.DISABLED)
            elif message == "__LOGIN_DONE__":
                self.login_button.configure(state=tk.NORMAL)
            elif message == "__USER_ACTION_REQUIRED__":
                self.continue_button.configure(state=tk.NORMAL)
            else:
                self._log(message)
        self.after(200, self._drain_status)

    def _open_output_dir(self) -> None:
        output_dir = self.project_root / "原始数据"
        output_dir.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(output_dir)

    def _clean_output_dir(self) -> None:
        output_dir = self.project_root / "原始数据"
        if not output_dir.exists():
            self._log("原始数据目录不存在，无需清理。")
            return
        confirm = messagebox.askyesno(
            "确认清理",
            "将删除“原始数据”目录下的所有导出文件和日志。\n\n确定要继续吗？",
        )
        if not confirm:
            return
        try:
            for child in output_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            self._log("原始数据目录已清理完成。")
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc))

    def _show_debug_commands(self) -> None:
        try:
            shops = self._selected_shops()
            date_range = self._date_range()
            if not shops:
                raise ValueError("请至少选择一个店铺")
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        shop = shops[0]
        start = date_range.start.isoformat()
        end = date_range.end.isoformat()
        if shop.platform == "douyin":
            commands = [
                "可复制到 PowerShell 执行。以下命令默认使用当前选中的第一个店铺和当前日期范围：",
                "",
                "1. 补登录抖音共享账号：",
                "powershell -ExecutionPolicy Bypass -File .\\login_douyin.ps1 --profile-id douyin_default --profile-name 抖音默认账号",
                "",
                "2. 抖音保证金接口监听：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_douyin_deposit_api.ps1 --shop-id douyin_default --shop-name {shop.name} --sheet-name {shop.sheet_name} --start {start} --end {end}",
                "",
                "3. 抖音切店接口监听：",
                "powershell -ExecutionPolicy Bypass -File .\\debug_douyin_shop_switch_api.ps1 --profile-id douyin_default",
                "",
                "4. 抖音切店弹窗 DOM 调试：",
                "powershell -ExecutionPolicy Bypass -File .\\debug_douyin_shop_switch_dom.ps1",
            ]
        elif shop.platform == "kuaishou":
            profile_id = shop.profile_id or "kuaishou_default"
            commands = [
                "可复制到 PowerShell 执行。以下命令默认使用当前选中的第一个店铺和当前日期范围：",
                "",
                "1. 补登录快手账号：",
                f"powershell -ExecutionPolicy Bypass -File .\\login_kuaishou.ps1 --profile-id {profile_id} --profile-name {shop.name}",
                "",
                "2. 快手保证金接口监听：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_kuaishou_deposit_api.ps1 --shop-id {shop.id} --shop-name {shop.name} --sheet-name {shop.sheet_name} --profile-id {profile_id} --start {start} --end {end}",
                "",
                "3. 快手切店接口监听：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_kuaishou_shop_switch_api.ps1 --profile-id {profile_id}",
            ]
        else:
            commands = [
                "可复制到 PowerShell 执行。以下命令默认使用当前选中的第一个店铺和当前日期范围：",
                "",
                "1. 补登录当前店铺：",
                f"powershell -ExecutionPolicy Bypass -File .\\login_shop.ps1 --shop-id {shop.id}",
                "",
                "2. 退货单店试运行，只查询不下载：",
                f"powershell -ExecutionPolicy Bypass -File .\\run_returns_once.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "3. 客服绩效单店试运行，只请求下载地址不保存文件：",
                f"powershell -ExecutionPolicy Bypass -File .\\run_customer_service_performance_once.ps1 --shop-id {shop.id} --start {start} --end {end} --dry-run",
                "",
                "4. 延迟发货单店试运行，查询前三个 TAB 不下载：",
                f"powershell -ExecutionPolicy Bypass -File .\\run_delayed_shipping_once.ps1 --shop-id {shop.id} --start {start} --end {end} --all-tabs",
                "",
                "5. 客服绩效接口失效时使用：打开监听窗口，手动选日期并点击下载表单，完成后把生成的 JSON 发给开发者：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_customer_service_performance_api.ps1 --shop-id {shop.id} --start {start} --end {end} --manual-date",
                "",
                "6. 退货接口失效时使用：打开监听窗口，手动完成筛选、查询、批量导出、确认生成和下载，完成后把生成的 JSON 发给开发者：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_returns_api.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "7. 延迟发货查询接口失效时使用：监听前三个 TAB 查询接口请求与响应：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_delayed_shipping_query_api.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "8. 延迟发货导出接口失效时使用：监听生成报表、导出列表和下载接口：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_delayed_shipping_export_api.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "9. 退货日期控件调试：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_returns_date_picker.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "10. 延迟发货日期控件调试：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_delayed_shipping_date_picker.ps1 --shop-id {shop.id} --start {start} --end {end}",
                "",
                "11. 负向体验日期控件调试：",
                f"powershell -ExecutionPolicy Bypass -File .\\debug_negative_experience_date_picker.ps1 --shop-id {shop.id} --start {start} --end {end}",
            ]
        for line in commands:
            if line:
                self._log(line)
        messagebox.showinfo("调试命令", "\n".join(commands))

    def _add_shop_config(self) -> None:
        ShopEditorDialog(self, self.project_root, on_saved=self._shop_config_changed)

    def _delete_shop_config(self) -> None:
        ShopDeleteDialog(self, self.project_root, on_saved=self._shop_config_changed)

    def _shop_config_changed(self, message: str) -> None:
        self._log(message)
        messagebox.showinfo("配置已更新", f"{message}\n\n请重启 GUI 后使用最新店铺列表。")

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", self._log_tag(message))
        self.log_text.see(tk.END)

    def _log_tag(self, message: str) -> str:
        if any(word in message for word in ["失败", "报错", "错误", "超时", "失效", "运行失败", "澶辫触"]):
            return "error"
        if any(word in message for word in ["人工", "手动", "继续", "补登录"]):
            return "action"
        if any(word in message for word in ["成功", "完成", "已生成", "全部选中任务", "鎴愬姛"]):
            return "success"
        if " - " in message or "=====" in message:
            return "task"
        return "normal"

    def _pick_date(self, target_var: tk.StringVar) -> None:
        try:
            current = parse_date(target_var.get())
        except Exception:
            current = previous_month().start
        DatePicker(self, current.year, current.month, target_var)


def main() -> None:
    if getattr(sys, "frozen", False):
        project_root = Path(sys.executable).resolve().parent
    else:
        project_root = Path(__file__).resolve().parents[2]
    app = ExporterApp(project_root)
    app.mainloop()


class ShopEditorDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, project_root: Path, on_saved) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.on_saved = on_saved
        self.title("添加店铺配置")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.platform_var = tk.StringVar(value="pinduoduo")
        self.shop_id_var = tk.StringVar()
        self.shop_name_var = tk.StringVar()
        self.sheet_name_var = tk.StringVar()
        self.profile_id_var = tk.StringVar(value="")
        self._build()
        self.after(50, self.lift)
        self.after(60, self.focus_force)

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="平台").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        platform_combo = ttk.Combobox(
            frame,
            textvariable=self.platform_var,
            values=["pinduoduo | 拼多多", "douyin | 抖音", "kuaishou | 快手"],
            width=39,
            state="readonly",
        )
        platform_combo.grid(row=0, column=1, sticky=tk.EW, pady=6)
        platform_combo.current(0)
        platform_combo.bind("<<ComboboxSelected>>", lambda _event: self._platform_changed())

        ttk.Label(frame, text="店铺 ID").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(frame, textvariable=self.shop_id_var, width=42).grid(row=1, column=1, sticky=tk.EW, pady=6)
        self.shop_id_hint = ttk.Label(frame, text="英文/数字/下划线，例如 dayan_new")
        self.shop_id_hint.grid(row=2, column=1, sticky=tk.W, pady=(0, 8))

        ttk.Label(frame, text="店铺完整名称").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(frame, textvariable=self.shop_name_var, width=42).grid(row=3, column=1, sticky=tk.EW, pady=6)
        self.shop_name_hint = ttk.Label(frame, text="例如 拼多多大雁图书专营店")
        self.shop_name_hint.grid(row=4, column=1, sticky=tk.W, pady=(0, 8))

        ttk.Label(frame, text="店铺短名").grid(row=5, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(frame, textvariable=self.sheet_name_var, width=42).grid(row=5, column=1, sticky=tk.EW, pady=6)
        self.sheet_name_hint = ttk.Label(frame, text="用于文件名和负向体验 sheet，例如 拼多多大雁")
        self.sheet_name_hint.grid(row=6, column=1, sticky=tk.W, pady=(0, 8))

        ttk.Label(frame, text="登录状态 ID").grid(row=7, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        self.profile_entry = ttk.Entry(frame, textvariable=self.profile_id_var, width=42)
        self.profile_entry.grid(row=7, column=1, sticky=tk.EW, pady=6)
        self.profile_hint = ttk.Label(frame, text="拼多多无需填写；抖音默认 douyin_default，多店铺共用登录状态。")
        self.profile_hint.grid(row=8, column=1, sticky=tk.W, pady=(0, 8))

        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(actions, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="保存", command=self._save).pack(side=tk.LEFT)
        self._platform_changed()

    def _platform_changed(self) -> None:
        platform = self._selected_platform()
        if platform in {"douyin", "kuaishou"}:
            if not self.profile_id_var.get().strip():
                self.profile_id_var.set("douyin_default" if platform == "douyin" else "kuaishou_default")
            self.shop_id_hint.configure(text="建议英文/数字/下划线，例如 douyin_new_shop")
            if platform == "douyin":
                self.shop_name_hint.configure(text="例如 字在图书精选店")
                self.sheet_name_hint.configure(text="用于文件名，例如 抖音字在图书；会共用 douyin_default 登录状态")
                self.profile_hint.configure(text="拼多多无需填写；抖音默认 douyin_default，多店铺共用登录状态。")
            else:
                self.shop_name_hint.configure(text="例如 快手某某图书店")
                self.sheet_name_hint.configure(text="用于文件名，例如 快手某某图书；默认共用 kuaishou_default 登录状态")
                self.profile_hint.configure(text="拼多多无需填写；快手默认 kuaishou_default，可按账号拆分登录状态。")
            self.profile_entry.configure(state=tk.NORMAL)
        else:
            self.profile_id_var.set("")
            self.shop_id_hint.configure(text="英文/数字/下划线，例如 dayan_new")
            self.shop_name_hint.configure(text="例如 拼多多大雁图书专营店")
            self.sheet_name_hint.configure(text="用于文件名和负向体验 sheet，例如 拼多多大雁")
            self.profile_hint.configure(text="拼多多无需填写；抖音默认 douyin_default，多店铺共用登录状态。")
            self.profile_entry.configure(state=tk.DISABLED)

    def _selected_platform(self) -> str:
        return self.platform_var.get().split("|", 1)[0].strip() or "pinduoduo"

    def _save(self) -> None:
        shop_id = self.shop_id_var.get().strip()
        shop_name = self.shop_name_var.get().strip()
        sheet_name = self.sheet_name_var.get().strip()
        platform = self._selected_platform()
        if not shop_id or not shop_name or not sheet_name:
            messagebox.showerror("参数错误", "三个字段都填写后才能保存。", parent=self)
            return
        if not all(("a" <= ch.lower() <= "z") or ch.isdigit() or ch == "_" for ch in shop_id):
            messagebox.showerror("参数错误", "店铺 ID 只能包含英文、数字和下划线。", parent=self)
            return
        shops_path = self.project_root / "config" / "shops.json"
        try:
            data = json.loads(shops_path.read_text(encoding="utf-8"))
            shops = data.setdefault("shops", [])
            if any(item.get("id") == shop_id for item in shops):
                raise ValueError(f"店铺 ID 已存在：{shop_id}")
            item = {"id": shop_id, "name": shop_name, "sheet_name": sheet_name}
            if platform == "douyin":
                item.update(
                    {
                        "platform": "douyin",
                        "platform_name": "抖音",
                        "profile_id": self.profile_id_var.get().strip() or "douyin_default",
                    }
                )
            elif platform == "kuaishou":
                item.update(
                    {
                        "platform": "kuaishou",
                        "platform_name": "快手",
                        "profile_id": self.profile_id_var.get().strip() or "kuaishou_default",
                    }
                )
            shops.append(item)
            shops_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("添加失败", str(exc), parent=self)
            return
        self.destroy()
        self.on_saved(f"已添加店铺配置：{shop_name}")


class ShopDeleteDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, project_root: Path, on_saved) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.on_saved = on_saved
        self.title("删除店铺配置")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.shop_var = tk.StringVar()
        self.shops: list[dict] = []
        self._build()
        self.after(50, self.lift)
        self.after(60, self.focus_force)

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        try:
            data = json.loads((self.project_root / "config" / "shops.json").read_text(encoding="utf-8"))
            self.shops = list(data.get("shops", []))
        except Exception as exc:
            ttk.Label(frame, text=f"读取店铺配置失败：{exc}").pack(padx=8, pady=8)
            ttk.Button(frame, text="关闭", command=self.destroy).pack(anchor=tk.E)
            return

        values = [
            f"{item.get('id')} | {item.get('platform_name', '拼多多')} | {item.get('name')}"
            for item in self.shops
        ]
        ttk.Label(frame, text="选择要删除的店铺配置").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        combo = ttk.Combobox(frame, textvariable=self.shop_var, values=values, width=56, state="readonly")
        combo.grid(row=1, column=0, sticky=tk.EW, pady=6)
        if values:
            combo.current(0)
        ttk.Label(frame, text="只删除配置，不删除浏览器登录状态或已导出文件。").grid(row=2, column=0, sticky=tk.W, pady=(0, 12))

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, sticky=tk.E)
        ttk.Button(actions, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="删除", command=self._delete).pack(side=tk.LEFT)

    def _delete(self) -> None:
        selected = self.shop_var.get()
        if not selected:
            messagebox.showerror("参数错误", "请选择要删除的店铺。", parent=self)
            return
        shop_id = selected.split("|", 1)[0].strip()
        parts = [part.strip() for part in selected.split("|")]
        shop_name = parts[-1] if len(parts) >= 2 else shop_id
        if not messagebox.askyesno("确认删除", f"确定从配置中删除：{shop_name}？", parent=self):
            return
        shops_path = self.project_root / "config" / "shops.json"
        try:
            data = json.loads(shops_path.read_text(encoding="utf-8"))
            shops = data.get("shops", [])
            data["shops"] = [item for item in shops if item.get("id") != shop_id]
            if len(data["shops"]) == len(shops):
                raise ValueError(f"未找到店铺 ID：{shop_id}")
            shops_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self.destroy()
        self.on_saved(f"已删除店铺配置：{shop_name}")


class DatePicker(tk.Toplevel):
    def __init__(self, parent: tk.Tk, year: int, month: int, target_var: tk.StringVar) -> None:
        super().__init__(parent)
        self.target_var = target_var
        self.year = year
        self.month = month
        self.title("选择日期")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, columnspan=7, sticky=tk.EW, pady=(0, 8))
        ttk.Button(header, text="<", width=4, command=self._prev_month).pack(side=tk.LEFT)
        self.title_label = ttk.Label(header, width=16, anchor=tk.CENTER)
        self.title_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(header, text=">", width=4, command=self._next_month).pack(side=tk.LEFT)

        self.days_frame = ttk.Frame(frame)
        self.days_frame.grid(row=1, column=0, columnspan=7)
        self._render_days()

    def _render_days(self) -> None:
        for child in self.days_frame.winfo_children():
            child.destroy()
        self.title_label.configure(text=f"{self.year}年 {self.month}月")

        week_names = ["一", "二", "三", "四", "五", "六", "日"]
        for column, name in enumerate(week_names):
            ttk.Label(self.days_frame, text=name, width=5, anchor=tk.CENTER).grid(row=0, column=column, pady=2)

        month_calendar = calendar.monthcalendar(self.year, self.month)
        for row_index, week in enumerate(month_calendar, start=1):
            for column, day in enumerate(week):
                if day == 0:
                    ttk.Label(self.days_frame, text="", width=5).grid(row=row_index, column=column, padx=2, pady=2)
                    continue
                ttk.Button(
                    self.days_frame,
                    text=str(day),
                    width=5,
                    command=lambda value=day: self._select_day(value),
                ).grid(row=row_index, column=column, padx=2, pady=2)

    def _select_day(self, day: int) -> None:
        self.target_var.set(f"{self.year:04d}-{self.month:02d}-{day:02d}")
        self.destroy()

    def _prev_month(self) -> None:
        self.month -= 1
        if self.month == 0:
            self.month = 12
            self.year -= 1
        self._render_days()

    def _next_month(self) -> None:
        self.month += 1
        if self.month == 13:
            self.month = 1
            self.year += 1
        self._render_days()


if __name__ == "__main__":
    main()
