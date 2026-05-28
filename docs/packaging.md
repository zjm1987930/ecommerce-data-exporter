# EXE 打包说明

## 打包命令

在开发电脑的项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

打包完成后输出目录：

```text
dist/拼多多数据自动导出工具/
```

把整个 `拼多多数据自动导出工具` 文件夹复制到其他 Windows 电脑即可运行，不要只复制 EXE。

## 目标电脑要求

- 不需要安装 Python。
- 不需要安装项目 Python 依赖。
- 需要有 Microsoft Edge。Windows 10/11 通常已自带 Edge。

## 打包目录内容

- `拼多多数据自动导出工具.exe`：主程序。
- `config/`：店铺和任务配置，支持在目标电脑上修改。
- `docs/`：使用和规则文档。
- `原始数据/`：导出结果目录。
- `browser_profiles/`：各店铺浏览器登录状态目录。
- `_internal/`：EXE 运行依赖目录，不要删除。

## 注意事项

- 如果删除 `browser_profiles`，不会影响程序本身，但会清掉所有店铺登录状态，需要重新补登录。
- 如果修改 `config/shops.json`，需要重启软件后生效。
- 打包产物是目录版，不是单文件版；目录版启动更快，也更适合 Playwright 这类带驱动文件的工具。
