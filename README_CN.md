# Printer AI Skills

> **说明**：本项目是 [NullYing/Printer-AI-Skills](https://github.com/NullYing/Printer-AI-Skills) 的一个 fork，其文档以英文版 [README.md](README.md) 为准并在那里持续维护。本中文文档可能滞后于英文版，网络发现、`setup` 安装流程、Windows 辅助脚本和安全说明（Security notes）等章节只在英文版中完整给出。如果两者不一致，请以 README.md 为准。

跨平台本地打印机 CLI，让 AI 助手通过 `printer-ai` 命令操作本地打印机。配合 OpenClaw / Cursor / Claude 等 AI Skill 系统使用。

## 功能特性

- 🌍 **跨平台支持**: Windows、macOS、Linux
- 🖨️ **打印机管理**: 获取打印机列表、状态查询、能力查询
- 📄 **打印任意文件**: PDF、图片、文本与代码、Markdown、HTML/SVG、Office 文档和
  CSV 会在打印前自动转换为 PDF —— 不需要再让用户"先转成 PDF"
- 🔄 **单独转换**: `convert` 只生成 PDF 而不打印，`formats` 列出本机当前支持哪些
  格式、缺少什么
- ⚙️ **打印选项**: 纸张、颜色、双面、份数、质量、纸盒 —— 三个平台上都交给驱动处理
- 📊 **任务管理**: 打印任务状态查询和取消
- 🤖 **AI 就绪**: CLI + SKILL.md，AI 即装即用

## 支持的文件类型

`print` 会先把文件规范化为 PDF，再交给平台后端，因此不需要手工转换。
`printer-ai formats` 会针对**本机**输出同样的信息，并说明哪些转换器当前可用。

| 类型 | 扩展名 | 转换方式 | 依赖 |
|---|---|---|---|
| PDF / PostScript / 打印流 | `.pdf` `.ps` `.prn` | 原样透传 | — |
| 图片 | `.jpg` `.jpeg` `.jpe` `.jfif` `.png` `.gif` `.bmp` `.dib` `.tif` `.tiff` `.webp` `.ico` `.ppm` `.pgm` `.pbm` `.tga` | Pillow 解码，每一帧一页，等比缩放并居中，留 0.5 英寸页边距 | 已随包安装（Pillow + reportlab） |
| 文本、代码、日志、配置、数据 | `.txt` `.log` `.json` `.yaml` `.toml` `.ini` `.xml` `.sql` `.diff` `.py` `.js` `.ts` `.sh` `.ps1` `.c` `.cpp` `.go` `.rs` `.java` `.css` …（共约 75 种） | reportlab，Courier 10pt，按页宽硬换行，页眉为文件名、页脚为页码 | 已随包安装（reportlab） |
| Markdown | `.md` `.markdown` `.mdown` `.mkd` `.mdtext` | 先渲染为带样式的 HTML，再按 HTML 打印；若完全没有 HTML 渲染器，则直接把 Markdown 源码当作纯文本打印，而不是报错 | 浏览器，或 LibreOffice（均为可选） |
| HTML / SVG | `.html` `.htm` `.xhtml` `.svg` | 无头 Chromium 系浏览器（`--headless=new --print-to-pdf`）；找不到浏览器、或浏览器失败/超时时改用 LibreOffice（只支持基础 HTML/CSS） | Chromium 系浏览器，或 LibreOffice |
| Office 文档 | `.doc` `.docx` `.docm` `.dot(x)` `.odt` `.ott` `.rtf` `.wps` `.pages` `.xls` `.xlsx` `.xlsm` `.xlt(x)` `.ods` `.ppt` `.pptx` `.pps(x)` `.pot(x)` `.odp` `.odg` `.otg` `.vsd` `.vsdx` … | Windows：装有 Microsoft Office 时通过 COM 调用，否则用 LibreOffice。macOS/Linux：LibreOffice 无头模式（始终使用独立的用户配置目录，避免已打开的 LibreOffice 窗口导致转换失败） | LibreOffice，或 Windows 上的 Microsoft Office |
| CSV / TSV | `.csv` `.tsv` | LibreOffice 排成表格；没有电子表格程序时退回纯文本渲染 | LibreOffice（可选） |

格式优先按扩展名判断。扩展名缺失或未知时按文件头字节（magic bytes）判断；
如果一个通用的文本类扩展名下其实是 PDF、图片或 Office 文档，则以字节为准 ——
内容是 PDF 的 `.txt` 会按 PDF 打印。

以上都不是的文件（未知二进制）会以 `415` 返回并附带 `hint`；如果打印机本身
认识该格式，可以加 `--raw`。

## 限制说明

使用前请先阅读，这些都是真实存在的限制。

- **转换依赖未随包安装的外部工具。** Office 文档（`.docx`、`.xlsx`、`.pptx`、
  `.odt` 等）需要 **LibreOffice**，Windows 上也可用 Microsoft Office。
  HTML、SVG 和 Markdown 需要 **Chromium 系浏览器**（Windows 自带 Edge），
  LibreOffice 可作为只支持基础 HTML/CSS 的后备。两者都没有时，`print` 和
  `convert` 会返回 `415`，并在 `hint` 中说明该装什么。用 `printer-ai formats`
  查看现状。PDF、图片、文本/代码和 CSV 开箱即用。
- **Windows 上是位图打印。** PDF 先用 pypdfium2 栅格化，再通过 GDI 绘制到打印机
  设备上下文。因此任何已安装的打印机都能打印，`--options`（DEVMODE）也会交给
  驱动 —— 但输出是位图：文字比原生 PDF 打印略"软"，后台打印文件更大。渲染分辨率
  为打印机自身 DPI，上限 300（可用 `PRINTER_AI_RENDER_DPI` 覆盖）；
  `dmPrintQuality` 会传给驱动，但**不会**改变该渲染 DPI。
- **驱动忽略的 DEVMODE 字段仍然会被忽略。** `--options` 只是传递，并不强制生效；
  不支持双面的队列不会因此双面打印。用 `printer-ai attrs` 查看驱动声明的能力。
- **排版类转换只针对一种纸张尺寸。** 图片、文本/代码和 Markdown 按 A4 排版，
  `en_US` 一类区域设置下按 Letter；可用 `PRINTER_AI_PAGE_SIZE=A4|Letter` 覆盖。
  Office 文档和 HTML 保持各自渲染器选择的纸张尺寸。
- **`--raw` 只适用于打印机原生数据。** 它跳过转换，原样发送字节（CUPS `-o raw`、
  Windows RAW 后台打印）。设备不认识该格式时只会打印出乱码或什么都不打印。
- **macOS/Linux 需要 CUPS 头文件才能编译 pycups。** 请先安装
  （`apt install libcups2-dev` / `brew install cups`），否则打印后端返回 `501`。

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

Pillow、reportlab、markdown 会作为依赖一起安装（Windows 上还有 pypdfium2），
因此 PDF、图片、文本/代码和 CSV 无需其他组件即可打印。**可选的外部工具**只在
对应格式需要时才用得上：

- **LibreOffice** —— Office 文档、CSV 表格排版，以及 HTML 的后备渲染。
  `apt install libreoffice` / `dnf install libreoffice` /
  `brew install --cask libreoffice` / <https://www.libreoffice.org/download/>
- **Chromium 系浏览器**（Edge、Chrome、Chromium、Brave）—— HTML、SVG 和
  Markdown。Windows 自带 Microsoft Edge。

用 `printer-ai formats` 查看这些工具是否已被找到。

### 使用

```bash
printer-ai printers              # 列出打印机
printer-ai status 1              # 查看打印机状态
printer-ai print report.docx     # 打印任意文件（先自动转成 PDF）
printer-ai convert notes.md      # 只转换，在源文件旁生成 notes.pdf
printer-ai formats               # 本机支持哪些格式、缺少什么
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
| `print FILE [--index N] [--options JSON] [--raw] [--keep-pdf]` | 打印文件（先转成 PDF；`--raw` 原样发送字节，`--keep-pdf` 保留转换后的 PDF 并在 `pdf_path` 中返回路径） |
| `convert FILE [--out PATH_OR_DIR] [--json]` | 只转换为 PDF，不打印（默认输出到源文件旁；`--out` 以 `.pdf` 结尾表示文件名，否则视为目录） |
| `formats [--json]` | 本机可打印哪些文件类型：每个转换器的扩展名、`available`、`via`、缺失时的 `install_hint`，以及当前生效的 `page_size`。始终以 `0` 退出 |
| `jobs [--printer NAME] [--json]` | 列出打印任务 |
| `job-status JOB_ID` | 查询任务状态 |
| `cancel-job JOB_ID` | 取消打印任务 |

每条命令都会输出带 `code` 的 JSON 结果。只有 `code` 为 `200` 时进程退出码才是
`0`，因此脚本和 AI 可以只根据退出码判断成败。

打印成功的结果里包含 `job_id`、`printer_name`、`file_path`；发生过转换时还会带上
`converter`、`converted`、`converted_from` 和 `conversion_notes`（加了
`--keep-pdf` 时还有 `pdf_path`）。Windows 上另有 `method`（`"gdi"` 或 `"raw"`）、
`pages`、`dpi`、`copies` 和 `copies_handled_by`（`"driver"` 或 `"client"`）；
macOS/Linux 上则有 `raw`。无法转换的文件会返回 `415`，`data.hint` 说明该装什么。

## 环境变量

均为可选，用于覆盖自动检测的默认值。

| 变量 | 作用 |
|---|---|
| `PRINTER_AI_PAGE_SIZE` | `A4` 或 `Letter` —— 图片、文本/代码和 Markdown 的排版纸张。默认 A4，`en_US` 一类区域设置下为 Letter。也支持 `Legal`、`A3`、`A5` |
| `PRINTER_AI_BROWSER` | 用于渲染 HTML/SVG/Markdown 的 Chromium 系浏览器路径或命令名。可用系统路径分隔符给出多个候选 |
| `PRINTER_AI_SOFFICE` | LibreOffice `soffice` 可执行文件的完整路径，适用于它不在 `PATH` 或标准安装位置的情况 |
| `PRINTER_AI_RENDER_DPI` | Windows GDI 打印的栅格化 DPI，范围 `36`–`1200`。默认取打印机自身 DPI，上限 300 |

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

## 在 AI 对话中打印

这个工具的意义在于：一句自然语言请求对应一条命令。
"把 `~/Downloads/invoice.docx` 用黑白、双面打印出来" 对应：

```bash
# macOS / Linux（CUPS/IPP 选项名）
printer-ai print ~/Downloads/invoice.docx \
  --options '{"print_color_mode":"monochrome","sides":"two-sided-long-edge"}'
```

```powershell
# Windows（DEVMODE 选项名）
printer-ai print "$env:USERPROFILE\Downloads\invoice.docx" `
  --options '{"dmColor":1,"dmDuplex":2}'
```

`.docx` 会在打印过程中自动转成 PDF（LibreOffice，或 Windows 上的 Microsoft
Office）—— AI 助手不需要要求用户先导出 PDF。如果缺少转换工具，`415` 结果的
`hint` 会说明该装什么，`printer-ai formats` 则给出完整情况。想同时保留 PDF 就加
`--keep-pdf`；只要 PDF 不打印就用 `printer-ai convert`。

## 许可证

MIT License

## 另请参阅

### [Printer AI MCP](https://github.com/NullYing/printer-ai-mcp)

需要一个持续运行的 **MCP 服务器**而不是 CLI 工具？可以看看 [printer-ai-mcp](https://github.com/NullYing/printer-ai-mcp)。它基于 FastMCP 构建，AI 助手通过 HTTP 连接，支持 Docker 部署。适合常驻服务或多客户端场景。

## 相关链接

- [Printer AI MCP（服务器版本）](https://github.com/NullYing/printer-ai-mcp)
- [CUPS 文档](https://www.cups.org/)
