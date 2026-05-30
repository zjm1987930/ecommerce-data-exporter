from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "电商平台原始数据自动导出工具"


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def copy_dir(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    dist_root = project_root / "dist"
    app_dist = dist_root / APP_NAME

    print("清理旧打包目录...")
    remove_path(project_root / "build")
    remove_path(app_dist)

    print("开始打包 EXE...")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        APP_NAME,
        "--paths",
        str(project_root / "src"),
        "--collect-all",
        "playwright",
        "--collect-all",
        "openpyxl",
        str(project_root / "src" / "main.py"),
    ]
    subprocess.run(command, cwd=project_root, check=True)

    print("复制运行配置和文档...")
    copy_dir(project_root / "config", app_dist / "config")
    copy_dir(project_root / "docs", app_dist / "docs")
    (app_dist / "原始数据").mkdir(exist_ok=True)
    (app_dist / "browser_profiles").mkdir(exist_ok=True)

    readme = """电商平台原始数据自动导出工具

使用方式：
1. 双击“电商平台原始数据自动导出工具.exe”启动。
2. 首次使用或登录失效时，在 GUI 中点击“补登录选中店铺”。
3. 导出的文件会保存到本目录下的“原始数据”。
4. 店铺配置在本目录下的“config\\shops.json”，修改后重启软件生效。

注意：
- 本软件不需要目标电脑安装 Python 或 Python 依赖。
- 目标电脑需要 Windows 系统自带或已安装 Microsoft Edge。
- 当前已支持拼多多和抖音；抖音多店铺共用同一个登录状态，并在后台自动切换店铺。
- 请把整个文件夹一起复制到其他电脑，不要只复制 EXE。
"""
    (app_dist / "使用说明.txt").write_text(readme, encoding="utf-8")
    remove_path(project_root / "build")
    remove_path(project_root / f"{APP_NAME}.spec")
    print(f"打包完成：{app_dist}")


if __name__ == "__main__":
    main()
