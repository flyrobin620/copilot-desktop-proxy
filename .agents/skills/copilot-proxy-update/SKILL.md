---
name: copilot-proxy-update
description: 安装或更新 Windows 本机的 copilot-desktop-proxy（git pull、运行时与登录依赖、旧配置迁移、重启、验证），保留账号、启动方式和停止状态。用于安装此代理、更新它或让已拉取的版本生效；不用于升级 Claude App 本身。
---

# 安装 / 更新 Copilot Desktop Proxy

所有步骤都由一个幂等脚本完成，不要手动拆成 pull / stop / start / check。
仓库根目录是本文件向上三级（`../../..`）；在根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

不要用 `Install.cmd`（结尾有 `pause`，会卡住非交互执行）。脚本在非交互环境不会弹出提问。
步骤和选项说明见 [docs/UPDATE.md](../../../docs/UPDATE.md)。

## 运行前只需决定两件事

1. **活动 session**：脚本发现进行中的请求时，在非交互环境会以退出码 1 停下并说明。
   把提示转告用户（重启会中断正在生成的回答，Claude 会重连，历史不丢），由用户选择：
   等空闲后重跑，或加 `-Force` 立即更新。用户已表态的沿用，不重复询问。
   注意：如果你自己就运行在经过此代理的 Claude 会话里，重启会中断你当前的回复；
   先把要告诉用户的话说完，再加 `-Force` 执行，或请用户在终端自行运行。
2. **是否需要登录**：只有用户要求、或脚本提示需要时才加 `-SignIn`。
   它会弹出微软登录窗口，必须由用户本人完成，要提前告知。

其他选项只在用户明确要求时使用：`-StartupMode OnDemand|AtLogon`、`-NoPull`（本地有未提交修改且用户要保留时）、
`-SkipInferenceCheck`（用户不允许发送请求时）、`-NoStart`。更新请求不包含 commit、push 或发布的授权。

## 读结果

- 退出码 0：成功。1：失败，原因在 `ERROR:` 一行。2：只有最后的短请求失败。
- 原样转告汇总里的版本、启动方式、验证结果，以及 **Needs your attention** 下的每一条。
  这些提示是写给用户的（例如 Claude App 需要重开一次、需要安装完整 Python、需要 `-SignIn`），不要省略。
- 失败时按 `ERROR:` 的内容处理：`git pull` 失败或有未提交修改时，说明情况并让用户决定，
  不要 reset、stash 或换分支；401 建议 `-SignIn`；403 说明是账号或组织权限问题；
  其他情况运行 `shortcuts\Collect Diagnostics.cmd` 并使用其脱敏结果。不要无限重试或反复发送推理请求。

## 不要做

- 不要按进程名结束 Python/Claude，不要删除 `.run`、认证缓存或 profile 充当重置。
- 不要打印或复制 token、认证缓存、Copilot 数据库或完整客户端配置。
- 不要直接运行 `src\token_helper.py acquire`（私有管道协议）。
- 不要从别的目录或机器复制登录状态；新目录就用 `-SignIn` 重新登录。
