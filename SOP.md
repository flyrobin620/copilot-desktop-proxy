# 日常操作

## 启动与检查

确认 Copilot Code 登录有效，按 README 启动。`Check Setup.cmd` 检查本地状态；
`Check Authorization.cmd` 发送一条短请求验证上游。
多个账号数据库时交互启动器要求选择；非交互运行应显式设置 `COPILOT_PROXY_DB`。
不要在问题报告中粘贴数据库、Authorization header 或完整客户端配置。

## 升级

保存工作，等待请求结束，在原目录运行 `Stop Proxy.cmd`。
将新版解压到新目录，运行安装器后启动；重新配置客户端以更新启动钩子路径。
旧 `.data/usage.sqlite3` 可保留统计，仅在两边均停止时按需复制。
账号、日志、会话、运行状态不应提交到 Git。

## 诊断与停止

用量报告只统计经过本机代理的流量，不是账号全局额度或账单。
`Collect Diagnostics.cmd` 导出有界元数据，不自动上传。
401 检查 Copilot 授权；403、429、400 结合实际上游状态和兼容范围判断。
本地健康接口就绪并不证明推理成功。

`Stop Proxy.cmd` 停止代理与自动恢复；显式启动前保持停止。
`Restore Claude Defaults.cmd` 撤销 CLI/Agency 集成；`Restore Claude Desktop.cmd` 撤销 App 集成。
两者保留不属于本项目的设置和会话。恢复后重新打开终端或 App。
