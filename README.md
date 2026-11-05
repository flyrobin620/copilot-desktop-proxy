# Copilot Desktop Proxy

A Windows loopback proxy that connects **Claude Code**, **Claude Desktop** and
**OpenAI-compatible clients** to the model gateway available through your signed-in
Microsoft Copilot desktop **Code** experience.

**Windows x64 · Python standard library · MIT-licensed proxy source**

这是完整代理源码，包含 Claude App / Claude Code 接入、模型发现、流式响应、
用量统计、后台恢复、诊断、恢复默认配置和离线测试。
第三方运行时不随 Git 仓库分发；安装脚本会从 Python / Anthropic 官方地址下载。

## 前置条件

- Windows 10/11 x64，以及当前用户可写的安装目录。
- Microsoft Copilot 桌面版中可用的 **Code** 页：已登录，并能正常发送消息。
- 账号和组织策略允许使用对应模型。普通 Copilot 网页聊天可用，并不代表 Code 网关可用。
- 如使用 Claude App，另行安装支持第三方推理模式的官方桌面客户端。
- 网络可以访问官方下载站点与账号当前使用的上游网关。

本项目不提供账号、模型权限或订阅，不开启受控的 Code 功能入口。
请只在你有权使用的账号和设备上运行，并遵循服务条款及组织策略。
它不是 Microsoft 或 Anthropic 的官方产品；客户端和上游协议更新可能影响兼容性。

## 推荐安装

```powershell
git clone https://github.com/flyrobin620/copilot-desktop-proxy.git
cd copilot-desktop-proxy
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-runtime.ps1
```

也可在 GitHub 选择 **Code → Download ZIP**，完整解压后运行 **Install Runtime.cmd**。
不要直接在 ZIP 内双击启动脚本。

安装器按 `package-lock.json` 下载 Python 和 Claude Code 到本目录 `runtime/`，
配置嵌入式 Python 的源码搜索路径；不修改系统 PATH、不安装系统服务、不复制账号数据。
已安装的运行时会复用。版本升级时请解压到新目录后安装，避免覆盖正在运行的程序。

如果已安装 Python 3.11+ 与 Claude Code，也可以跳过安装器，将 Python 加入 PATH，
或设置当前终端的 `COPILOT_PROXY_PYTHON` / `COPILOT_PROXY_CLAUDE` 指向现有程序。

## 使用

先在 **Microsoft Copilot 桌面 Code 页发送一条消息**，确认上游账号可用。

### Claude Code

双击 **Launch Claude.cmd**。或在项目目录运行：

```powershell
& 'C:\Tools\copilot-desktop-proxy\claude-copilot.ps1'
```

也可以把项目文件夹拖到 `Launch Claude.cmd` 上。专用启动器为本次 CLI 配置代理，
默认模型为 **Copilot Auto (1M)**，跟随账号模型目录中的最新 Opus。
`/model` 可选择具体模型；`/context` 可检查客户端窗口预算。
1M 是客户端窗口配置，不代表所有上游模型都已通过满窗口验证。

希望普通 `claude` 也使用此代理时，启动代理后运行：

```powershell
.\runtime\python\python.exe -B configure_claude.py --settings "$env:USERPROFILE\.claude\settings.json" --template .\.models\claude-settings.json --migrate-legacy-beta
```

这会合并用户设置并添加 SessionStart 启动钩子，保留无关设置和已有认证值。
如使用另行安装的 Agency，可用 **Enable Agency.cmd** 配置 `agency claude` 和 PowerShell profile。

### Claude 桌面 App

1. 单独安装 [官方 Claude App](https://claude.ai/download)。
2. 双击 **Launch Claude Desktop.cmd**；已有 App 进程时，先保存工作并完整退出，再重新打开。
3. 选择已配置的第三方推理 / 自定义网关；欢迎页显示该配置时点击 **Continue**。

第三方网关模式无需 Claude.ai 登录。它使用独立的本地会话，不提供 Claude.ai 云端同步。
详细配置、策略冲突及撤销方式见 [Claude App 接入](CLAUDE-DESKTOP.md)。

**Enable Claude Clients.cmd** 可一次配置 CLI/Agency 与 App。
两个客户端共用代理和上游额度，会话各自管理。

### API 与用量

双击 **Start API.cmd** 启动代理，然后使用：

| 用途 | 地址 |
| --- | --- |
| Claude Code / Anthropic Messages base URL | `http://127.0.0.1:8787` |
| Claude App gateway base URL | `http://127.0.0.1:8787/desktop` |
| OpenAI Chat Completions base URL | `http://127.0.0.1:8787/v1` |
| 用量报告 | `http://127.0.0.1:8787/usage-viewer` |
| 本地就绪状态 | `http://127.0.0.1:8787/health` |

客户端若要求 API key，可使用非敏感占位字符串 `local-copilot-placeholder`。
不要复制 Copilot token；代理在当前 Windows 用户下读取本地受保护授权，并仅在内存中使用。
代理只监听 `127.0.0.1`，不应暴露到局域网或公网。OpenAI 接口支持 Chat Completions，
不宣称实现所有 OpenAI API（例如 Responses API）。

## 检查、停止与恢复

| 入口 | 作用 |
| --- | --- |
| `Check Setup.cmd` | 检查本地运行时和代理就绪，不发送推理请求 |
| `Check Authorization.cmd` | 发送一次短推理请求，验证实际上游访问 |
| `Open Report.cmd` | 查看经过本机代理的累计用量 |
| `Collect Diagnostics.cmd` | 导出脱敏元数据诊断，不自动上传 |
| `Stop Proxy.cmd` | 停止此目录的代理与恢复任务，保持停止 |
| `Restore Claude Defaults.cmd` | 撤销本项目的 CLI/Agency 设置，保留其他配置和会话 |
| `Restore Claude Desktop.cmd` | 撤销仍由本项目管理的 App 设置 |

启动时会启用后台 watchdog 和当前用户的 Windows 恢复任务；登录 Windows 后可恢复运行。
关闭 Claude 不会停止代理。需要停用请运行 `Stop Proxy.cmd`。
如果设备策略禁止创建计划任务，启动器会说明仅有 worker 级恢复。

## 常见问题

- **找不到账号数据库 / 401**：在同一 Windows 用户的 Copilot 桌面 Code 页登录并发消息，再重试。
  Claude `/login` 不会更新 Copilot 授权。
- **403 / 模型不存在**：确认账号、组织策略及当前 Code 模型目录；运行 `Check Authorization.cmd` 区分本地与上游问题。
- **400 Long-lived prompt caching**：包含 `1h → 5m` TTL 兼容修复，当前范围为已验证的 PROD / Opus 5.5；
  其他模型或后端保持原样。详情见 [兼容性说明](COMPATIBILITY.md)。
- **App 仍走 Claude.ai**：完整退出重开，选择第三方网关模式；CLI 环境变量不能替代 App 配置。
- **端口被另一目录占用**：先从旧目录运行 `Stop Proxy.cmd`，再启动本目录。
- **看不到 Copilot Code 页**：需要客户端功能与账号权限；本项目不会授予这些权限。

## 开发与打包

```powershell
.\runtime\python\python.exe -B -X utf8 -m unittest discover -p "test_*.py" -q
```

上述离线测试使用合成数据与本地假上游。`smoke_test.py`、`smoke_claude.py` 和
`check_authorization.py` 属于另行运行的实际上游测试；不要把离线通过当作账号可用证明。

```powershell
# 生成源码安装包：附安装器，不含第三方二进制和本机数据
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build-portable.ps1
```

更多说明：[快速开始](QUICKSTART.md) · [日常操作](SOP.md) · [开发](DEVELOPMENT-SOP.md) · [自动恢复](RECOVERY.md)。

## 许可证与第三方软件

代理源码采用 [MIT License](LICENSE)。Python、Claude Code、Claude App 和 Microsoft Copilot
仍适用各自许可证或服务条款；本项目不重新许可这些软件。见 [THIRD-PARTY.md](THIRD-PARTY.md)。
