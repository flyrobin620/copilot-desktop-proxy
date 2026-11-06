# Claude App 接入

推荐先完成 [README 安装](../README.md#安装与更新一个入口)，并安装支持第三方推理的官方 Claude App。

1. 运行 `Install.cmd`，确认汇总中短请求验证成功。
2. 双击 `shortcuts\Launch Claude Desktop.cmd`；仅配置可用 `shortcuts\Configure Claude Desktop.cmd`。
3. App 已运行时保存工作，完整退出并重开。
4. 选择已配置的第三方网关，或在欢迎页点击 **Continue**。第三方模式无需 Claude.ai 登录。

## 配置与策略

App gateway 为 `http://127.0.0.1:8787/desktop`，不读取 CLI 的 `ANTHROPIC_BASE_URL`。
配置包括 gateway provider、非敏感 bearer 占位值、模型发现、1M 窗口偏好、工具搜索，
以及 Chat / Cowork / Code 入口。工具执行和权限仍由 App 管理；Cowork 的虚拟化、VM
与组件下载仍须满足客户端自身要求。

脚本优先使用可写且无冲突的 `HKCU\SOFTWARE\Policies\Claude`。
没有受管策略且该路径不可写时，使用 `%LOCALAPPDATA%\Claude-3p\configLibrary`。
归属记录在 `%LOCALAPPDATA%\CopilotDesktopProxy\claude-desktop-integration.json`。
不修改 HKLM、ACL 或已有企业部署。报告策略/本地配置冲突时先检查既有部署，
不要删除整个 App 数据目录。`Check Claude Desktop.cmd` 只读检查配置，不发送推理请求。

## 模型与会话

`/desktop/v1/models` 提供具体标准模型 ID。App 选定具体模型；CLI Auto 每次解析最新 Opus。
1M 是客户端窗口策略，不保证每个上游型号均支持相同容量。
第三方模式的本地会话与 Claude.ai 历史分开，不提供云端同步、账号或远程会话服务。
远端机器上的 `127.0.0.1` 不指向此电脑。兼容范围见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 撤销

运行 `Restore Claude Desktop.cmd`，然后完整退出重开 App。只删除本项目创建且仍未修改
的配置，保留用户后续修改、其他配置和会话。CLI 恢复入口是 `Restore Claude Defaults.cmd`。

官方说明：[第三方部署](https://claude.com/docs/third-party/claude-desktop/overview)、
[Gateway](https://claude.com/docs/third-party/claude-desktop/gateway)、
[配置参考](https://claude.com/docs/third-party/claude-desktop/configuration)。
