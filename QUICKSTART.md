# 快速开始

完整步骤见 [README](README.md)。

1. 克隆或下载仓库，解压到可写目录，例如 `C:\Tools\copilot-desktop-proxy`。
2. 双击 **Install Runtime.cmd**，将官方 Python 与 Claude Code 安装到本目录。
3. 登录 Microsoft Copilot 桌面版 **Code** 页，发消息确认可用。
4. CLI 用 **Launch Claude.cmd**；App 用 **Launch Claude Desktop.cmd**。
   Claude App 需另行安装，完整重开后选择第三方网关并点击 Continue。
5. 需要实际连通性测试时运行 **Check Authorization.cmd**，它会发送一次短推理请求。

仅 API：运行 **Start API.cmd**，Chat Completions base URL 为 `http://127.0.0.1:8787/v1`。
退出 Claude 不停止后台代理；停止请用 **Stop Proxy.cmd**。
账号不随安装包迁移，每台电脑都需要独立具备 Code 权限并登录。
