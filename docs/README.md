# 电商平台原始数据自动导出工具

本项目用于从拼多多商家后台批量导出原始数据，并按固定目录和命名规则归档，供既有报表工具继续处理。

工具只负责“导出原始数据”和“记录运行结果”，不负责报表汇总、绩效计算或业务分析。

## 当前范围

当前 GUI 主流程已接入拼多多以下数据类型：

- 退货数据
- 消费者负向体验扣款数据
- 延迟发货数据
- 客服绩效数据

日常导出建议统一从 GUI 执行：

```powershell
.\run.ps1
```

基础自检：

```powershell
.\check.ps1
```

## 文档入口

为避免同一规则在多个文档中重复、过期，当前文档只保留入口索引。具体规则以对应文档为准：

- GUI 日常使用、补登录、人工介入、终止任务、工具箱：[gui-usage.md](./gui-usage.md)
- 输出目录、文件命名、日志规则：[file-rules.md](./file-rules.md)
- 拼多多页面路径、平台限制、接口规则：[platform-rules.md](./platform-rules.md)
- EXE 打包和分发说明：[packaging.md](./packaging.md)
- 需求范围和不做内容：[requirements.md](./requirements.md)
- 开发维护约定：[development-rules.md](./development-rules.md)
- 每日开发记录和历史决策：[development-log.md](./development-log.md)

## 常用辅助脚本

补登录单店：

```powershell
powershell -ExecutionPolicy Bypass -File .\login_shop.ps1 --shop-id dayan_tushu
```

批量补登录：

```powershell
powershell -ExecutionPolicy Bypass -File .\login_shops_batch.ps1
```

退货单店试运行，只查询不下载：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_returns_once.ps1 --shop-id dayan_tushu --start 2026-04-01 --end 2026-04-30
```

客服绩效单店试运行，只请求下载地址不保存文件：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_customer_service_performance_once.ps1 --shop-id dayan_tushu --start 2026-04-01 --end 2026-04-30 --dry-run
```

接口失效或页面变化时，可用 GUI 工具箱里的“生成调试命令”生成当前店铺和日期范围对应的监听命令。
