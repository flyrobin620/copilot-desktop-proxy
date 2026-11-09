# Copilot Desktop Proxy

A Windows loopback proxy that connects **Claude Code**, **Claude Desktop** and
**OpenAI-compatible clients** to the model gateway available to your Microsoft
Copilot **Code** account.

**Windows x64 · Python standard library · MIT-licensed proxy source**

这是完整代理源码，包含 Claude App / Claude Code 接入、模型发现、流式响应、
用量统计、后台恢复、诊断、恢复默认配置和离线测试。
第三方运行时不随 Git 仓库分发；安装脚本会从 Python / Anthropic 官方地址下载。

## 前置条件

- Windows 10/11 x64，以及当前用户可写的安装目录。
- 一个具备 Copilot **Code** 模型访问权限的微软工作账号。
  完成一次微软账号登录后，**不再需要安装或打开 Copilot 桌面版**；
  已安装并登录 Copilot Code 的机器也可以直接沿用其登录信息。
- 账号和组织策略允许使用对应模型。普通 Copilot 网页聊天可用，并不代表 Code 网关可用。
- 微软登录需要一次完整的 [Python 3.11+](https://www.python.org/downloads/windows/)（含 pip），
  用于创建独立的登录环境；代理本身使用本目录的内置运行时即可。
- 如使用 Claude App，另行安装支持第三方推理模式的官方桌面客户端。

本项目不提供账号、模型权限或订阅，不开启受控的 Code 功能入口。
请只在你有权使用的账号和设备上运行，并遵循服务条款及组织策略。
它不是 Microsoft 或 Anthropic 的官方产品；客户端和上游协议更新可能影响兼容性。

## 安装与更新：一个入口

```powershell
git clone https://github.com/flyrobin620/copilot-desktop-proxy.git
cd copilot-desktop-proxy
.\Install.cmd
```

以后更新也只运行 **Install.cmd**：它会 `git pull`、按需升级运行时和依赖、迁移旧配置、
重启代理（保留你的启动方式和“已停止”状态）并发送一次短请求验证，最后列出需要你处理的事项。
重复运行是安全的。没有 Copilot 桌面版的机器，首次运行会弹出一次微软账号登录；
已有 Copilot Code 的机器可随时运行 `Install.cmd -SignIn` 改为独立登录。
选项和细节见 [安装与更新](docs/UPDATE.md)，AI 执行请用 [更新 skill](.agents/skills/copilot-proxy-update/SKILL.md)。

ZIP 用户：在 GitHub 选择 **Code → Download ZIP**，完整解压后双击 **Install.cmd**。

## 使用

日常入口都在 [`shortcuts/`](shortcuts) 文件夹，双击即可：

| 入口 | 作用 |
| --- | --- |
| `Launch Claude.cmd` | 启动代理并打开 Claude Code（可把项目文件夹拖到它上面） |
| `Launch Claude Desktop.cmd` | 配置并打开 Claude App（第三方网关模式） |
| `Enable Claude Clients.cmd` | 一次配置 CLI/Agency 与 App |
| `Start API.cmd` / `Stop Proxy.cmd` | 启动 / 停止代理与自动恢复 |
| `Sign In.cmd` | 微软账号登录（换账号或 401 时） |
| `Check Setup.cmd` / `Check Authorization.cmd` | 本地就绪检查 / 发送一次短请求验证上游 |
| `Open Report.cmd` / `Collect Diagnostics.cmd` | 用量报告 / 导出脱敏诊断 |
| `Restore Claude Defaults.cmd` / `Restore Claude Desktop.cmd` | 撤销本项目的 CLI / App 设置 |

### Claude Code

`Launch Claude.cmd` 为本次 CLI 配置代理，默认模型 **Copilot Auto (1M)**，跟随账号模型目录中的最新 Opus。
`/model` 可选择具体模型；`/context` 可检查客户端窗口预算。
1M 是客户端窗口配置，不代表所有上游模型都已通过满窗口验证。
希望普通 `claude` 也使用此代理，运行 `Enable Claude Clients.cmd`（合并用户设置并添加 SessionStart 启动钩子）。

### Claude 桌面 App

1. 单独安装 [官方 Claude App](https://claude.ai/download)。
2. 双击 `Launch Claude Desktop.cmd`；已有 App 进程时，先保存工作并完整退出，再重新打开。
3. 选择已配置的第三方推理 / 自定义网关；欢迎页显示该配置时点击 **Continue**。

第三方网关模式无需 Claude.ai 登录。详细配置、策略冲突及撤销方式见 [Claude App 接入](docs/CLAUDE-DESKTOP.md)。
可选：只在 Claude 需要时启动代理，运行 `Install.cmd -StartupMode OnDemand`，见 [认证与按需启动](docs/TOKEN-REFRESH.md)。

### API 与用量

| 用途 | 地址 |
| --- | --- |
| Claude Code / Anthropic Messages base URL | `http://127.0.0.1:8787` |
| Claude App gateway base URL | `http://127.0.0.1:8787/desktop` |
| OpenAI Chat Completions base URL | `http://127.0.0.1:8787/v1` |
| 用量报告 | `http://127.0.0.1:8787/usage-viewer` |
| 本地就绪状态 | `http://127.0.0.1:8787/health` |

客户端若要求 API key，可使用非敏感占位字符串 `local-copilot-placeholder`。
真实 token 只存在于代理进程内存中；代理只监听 `127.0.0.1`，不应暴露到局域网或公网。
OpenAI 接口支持 Chat Completions，不宣称实现所有 OpenAI API（例如 Responses API）。

默认启动时会启用后台 watchdog 和当前用户的 Windows 恢复任务；关闭 Claude 不会停止代理，
需要停用请运行 `Stop Proxy.cmd`。见 [自动恢复](docs/RECOVERY.md)。

## 常见问题

- **401 / 未就绪**：运行 `shortcuts\Sign In.cmd` 重新登录微软账号。Claude `/login` 不影响本代理。
- **403 / 模型不存在**：账号或组织策略没有该模型权限；`Check Authorization.cmd` 可区分本地与上游问题。
- **400 Long-lived prompt caching**：见 [兼容性说明](docs/COMPATIBILITY.md)。
- **“ToolSearch is no longer available”，MCP 工具全部完整声明**：见 [MCP tool search](docs/TOOL-SEARCH.md)。
- **App 仍走 Claude.ai**：完整退出重开，选择第三方网关模式；CLI 环境变量不能替代 App 配置。
- **端口被另一目录占用**：先从旧目录运行 `Stop Proxy.cmd`，再在本目录运行 `Install.cmd`。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `Install.cmd` | 安装 / 更新的唯一入口 |
| `shortcuts/` | 双击使用的日常入口 |
| `scripts/` | PowerShell 启动、停止、恢复、安装脚本 |
| `src/` | 代理源码（Python 标准库）与用量页面 `web/` |
| `config/` | 运行时版本锁、登录依赖、Claude 设置模板、打包清单 |
| `docs/` | 详细文档 |
| `tests/` | 离线测试与单独运行的实际上游检查 |

运行状态（`runtime/`、`.run/`、`.data/`、日志）留在根目录且不进入 Git。
根目录的 `claude-gateway-auth.ps1` 只是兼容旧版 Claude App 配置的转发脚本。

## 开发与打包

```powershell
python -B -X utf8 -m unittest discover -s tests -t . -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-portable.ps1
```

离线测试使用合成数据与本地假上游，不能当作账号可用证明。
更多说明：[快速开始](docs/QUICKSTART.md) · [安装与更新](docs/UPDATE.md) · [日常操作](docs/SOP.md) ·
[开发](docs/DEVELOPMENT.md) · [自动恢复](docs/RECOVERY.md)。

## 许可证与第三方软件

代理源码采用 [MIT License](LICENSE)。Python、Claude Code、Claude App 和 Microsoft Copilot
仍适用各自许可证或服务条款；本项目不重新许可这些软件。见 [THIRD-PARTY.md](docs/THIRD-PARTY.md)。
