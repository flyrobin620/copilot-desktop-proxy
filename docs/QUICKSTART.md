# 快速开始

完整说明见 [README](../README.md)。

1. 克隆或下载仓库，解压到可写目录，例如 `C:\Tools\copilot-desktop-proxy`。
2. 双击 **Install.cmd**。它安装运行时；没有 Copilot 桌面版时会弹出一次微软账号登录
   （需要完整 Python 3.11+，脚本会提示）。结束时查看汇总和 “Needs your attention”。
3. CLI 用 `shortcuts\Launch Claude.cmd`；App 用 `shortcuts\Launch Claude Desktop.cmd`。
   Claude App 需另行安装，完整重开后选择第三方网关并点击 Continue。
4. 需要实际连通性测试时运行 `shortcuts\Check Authorization.cmd`，它会发送一次短推理请求。

仅 API：运行 `shortcuts\Start API.cmd`，Chat Completions base URL 为 `http://127.0.0.1:8787/v1`。
退出 Claude 不停止后台代理；停止请用 `shortcuts\Stop Proxy.cmd`。
账号不随安装包迁移，每台电脑、每个安装目录都需要独立登录一次。

以后更新：再次运行 **Install.cmd** 即可，详见 [安装与更新](UPDATE.md)。
