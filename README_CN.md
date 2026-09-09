# Printer AI Skills

> **说明**：本项目是 [NullYing/Printer-AI-Skills](https://github.com/NullYing/Printer-AI-Skills) 的一个 fork，其文档以英文版 [README.md](README.md) 为准并在那里持续维护。本中文文档可能滞后于英文版，缺少最新的功能说明、限制说明（Limitations）和安全说明（Security notes）。如果两者不一致，请以 README.md 为准。

跨平台本地打印机 CLI，让 AI 助手通过 `printer-ai` 命令操作本地打印机。配合 OpenClaw / Cursor / Claude 等 AI Skill 系统使用。

## 功能特性

- 🌍 **跨平台支持**: Windows、macOS、Linux
- 🖨️ **打印机管理**: 获取打印机列表、状态查询、能力查询
- 📄 **文件打印**: 支持自定义打印选项（纸张、颜色、双面等）
- 📊 **任务管理**: 打印任务状态查询和取消
- 🤖 **AI 就绪**: CLI + SKILL.md，AI 即装即用

## 快速开始

### 安装

```bash
# 全局安装（推荐）
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# 或本地开发安装
git clone https://github.com/michiphx/Printer-AI-Skills.git
cd Printer-AI-Skills
uv sync
```

### 使用

```bash
printer-ai printers              # 列出打印机
printer-ai status 1              # 查看打印机状态
printer-ai print file.pdf        # 打印文件
printer-ai job-status 123        # 查询任务状态
```

## AI Skill 集成

```bash
# 1. 安装 CLI
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# 2. 验证
printer-ai printers
```

安装完成后，AI 会自动按 SKILL.md 中的流程调用 CLI 命令完成打印操作。

## 命令参考

| 命令 | 说明 |
|------|------|
| `printers [--json]` | 列出所有打印机 |
| `status [INDEX] [--json]` | 获取打印机状态 |
| `attrs [INDEX]` | 获取打印机能力/属性 |
| `print FILE [--index N] [--options JSON]` | 打印文件 |
| `jobs [--printer NAME] [--json]` | 列出打印任务 |
| `job-status JOB_ID` | 查询任务状态 |
| `cancel-job JOB_ID` | 取消打印任务 |

## 打印选项

打印选项通过 `--options` 参数传入 JSON 字符串，格式因平台而异：

### macOS / Linux

```bash
printer-ai print file.pdf --options '{"copies":"2","media":"A4","print_color_mode":"color"}'
```

### Windows

```bash
printer-ai print file.pdf --options '{"dmCopies":2,"dmPaperSize":9,"dmColor":2}'
```

详细选项请参考 SKILL.md。

## 许可证

MIT License

## 另请参阅

### [Printer AI MCP](https://github.com/NullYing/printer-ai-mcp)

需要一个持续运行的 **MCP 服务器**而不是 CLI 工具？可以看看 [printer-ai-mcp](https://github.com/NullYing/printer-ai-mcp)。它基于 FastMCP 构建，AI 助手通过 HTTP 连接，支持 Docker 部署。适合常驻服务或多客户端场景。

## 相关链接

- [Printer AI MCP（服务器版本）](https://github.com/NullYing/printer-ai-mcp)
- [CUPS 文档](https://www.cups.org/)
