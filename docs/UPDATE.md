# 安装与更新

**首次安装和以后每次更新都只运行一个命令：**

```powershell
.\Install.cmd
```

（在仓库根目录；也可以双击。命令行 / AI 执行时用
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1`，它没有结尾的“按任意键”暂停。）
想让 AI 执行：让它按 [更新 skill](../.agents/skills/copilot-proxy-update/SKILL.md) 操作。

## 它会做什么

脚本按顺序完成以下步骤，已完成的部分会跳过，所以重复运行是安全的：

1. **检查正在运行的代理**：如有进行中的请求，先提示再决定是否重启（重启会中断正在生成的回答，Claude 会重连，历史不受影响）。
2. **更新文件**：Git 目录执行 `git pull --ff-only`，然后用拉取到的新版安装脚本继续。
   有未提交的修改时会停止并说明，不会覆盖你的修改。
3. **运行时**：按 `config/package-lock.json` 安装或升级 Python 与 Claude Code。
   已在用 `COPILOT_PROXY_PYTHON`、PATH 上的 Python 或已装的 Claude Code 时沿用，不重复下载。
4. **微软登录**：安装/更新固定版本的登录依赖；旧版“依赖 Copilot 数据库”的配置会静默切换为独立模式。
   没有任何登录配置、也没有 Copilot 桌面版时，会打开一次微软登录窗口。
5. **迁移客户端配置**：本版本调整了目录结构，脚本会把 Claude App 凭据脚本、Agency 启动钩子
   和 PowerShell profile 中的路径改到新位置。
6. **重启并验证**：原来在运行就重启；原来被手动停止就保持停止；按需启动模式保持按需。
   最后检查运行版本、本地就绪，并发送一次短请求确认上游可用。

结束时会打印汇总和 **Needs your attention**（需要你处理的事项）。退出码：0 成功，1 失败，2 仅短请求失败。

## 常用选项

| 选项 | 作用 |
| --- | --- |
| `-SignIn [-Username name@contoso.com]` | 进行一次微软账号登录（首次、换账号、或持续 401） |
| `-StartupMode OnDemand` / `AtLogon` | 改为只在 Claude 需要时启动 / 恢复为登录 Windows 时启动 |
| `-Force` | 有进行中的请求也直接重启 |
| `-NoPull` | 不拉取，直接安装当前文件（例如本地有修改时） |
| `-NoStart` | 只更新文件和配置，不停止 / 启动代理，也不发送验证请求 |
| `-SkipInferenceCheck` | 不发送最后的短请求 |
| `-BundledRuntime` / `-SkipClaude` | 强制在 `runtime\` 安装固定版本 / 不下载 Claude Code |
| `-AuthPython <python.exe>` | 指定用于创建登录环境的完整 Python |

## 需要你注意的情况

- **不需要退出 Claude**，但建议在没有活动 session 时更新。
- **首次微软登录需要完整 Python 3.11+**（python.org 安装版，含 pip）。内置的嵌入式 Python 不能创建登录环境；
  脚本找不到时会提示并继续完成其余步骤。
- **从 1.1.19 及更早版本更新**：根目录的脚本已移到 `scripts/`、`shortcuts/` 等文件夹，
  旧的 `.\stop.ps1`、`.\start.ps1`、`setup-runtime.ps1` 命令不再存在，请改用 `Install.cmd`。
  脚本会自动迁移计划任务和客户端配置。若 Claude App 之后报网关凭据错误，保存工作后完全退出 App 再打开一次。
- **ZIP 安装或换目录**：新目录是一个新的安装（登录缓存、计划任务按目录区分），需要重新运行 `Install.cmd -SignIn`。
  先在旧目录运行 `shortcuts\Stop Proxy.cmd`，避免端口冲突。
- 登录只证明身份，不授予模型权限；403 表示账号或组织没有对应权限。
- 不要从别的机器复制认证缓存，不要手动升级登录依赖中固定的 MSAL 版本。
- 验证失败时运行 `shortcuts\Collect Diagnostics.cmd` 导出脱敏诊断；不要提交数据库、token、认证缓存或 Claude 数据目录。
