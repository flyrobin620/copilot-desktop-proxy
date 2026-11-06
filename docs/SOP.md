# 日常操作

## 启动与检查

入口在 `shortcuts\`。`Check Setup.cmd` 检查本地状态；`Check Authorization.cmd` 发送一条短请求验证上游。
已用 `Install.cmd -SignIn` 登录时不需要 Copilot 桌面版；仍沿用 Copilot 数据库且有多个账号数据库时，
交互启动器要求选择，非交互运行应显式设置 `COPILOT_PROXY_DB`。
不要在问题报告中粘贴数据库、Authorization header 或完整客户端配置。

## 升级

不需要退出 Claude。建议没有活动 session 时更新；活动中更新可能触发连接重试，
或中断正在生成的回答，需要手动重试。用户可自行选择更新时间。
运行 `Install.cmd`：拉取、升级、迁移、重启和验证一次完成，见 [安装与更新](UPDATE.md)。账号、日志、会话、运行状态不应提交到 Git。

## 诊断与停止

用量报告只统计经过本机代理的流量，不是账号全局额度或账单。
`Collect Diagnostics.cmd` 导出有界元数据，不自动上传。
401 运行 `shortcuts\Sign In.cmd` 重新登录；403、429、400 结合实际上游状态和兼容范围判断。
本地健康接口就绪并不证明推理成功。

`Stop Proxy.cmd` 停止代理与自动恢复；显式启动前保持停止。
`Restore Claude Defaults.cmd` 撤销 CLI/Agency 集成；`Restore Claude Desktop.cmd` 撤销 App 集成。
两者保留不属于本项目的设置和会话。恢复后重新打开终端或 App。
