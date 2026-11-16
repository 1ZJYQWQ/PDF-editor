# V1.0.0 · 稳定版 / Stable

> 目前唯一的正式版本。功能比 `pre` 通道少，但经过完整测试，适合日常使用。
>
> The only stable release so far. It ships fewer features than the `pre` channel,
> but it has been fully tested and is safe for daily use.

---

## 简体中文

### 它是什么

纯本地、离线的 PDF 编辑工具。**不联网、不上传**，安装包自带运行时 —— 不需要装 Python，
也不需要 `pip install` 任何东西。

### 核心能力

- **涂黑是真的删字** —— 文字从内容流里被移除，不是盖一块黑。删完用 `strings`
  也搜不出来，复制不到，转存带不出去
- **改原文并重新排版** —— 改写一段，自动重新折行；一页放不下就续到下一页
- **表格** —— 按横线识别行，可改字、插一行、删一行，边框自动补齐
- **批注是 PDF 原生注释** —— 导出后在 Firefox、Acrobat 里仍可修改和删除
- **中文可选中、可复制** —— 自行实现 TrueType 子集化，只嵌入用到的字形
- **从零新建空白 PDF** —— A4/A3/A5/Letter/Legal，可横向，1~200 页
- **整篇查找替换**、**按关键词删除**（可选重新排版或留黑块）、**拆分**、**合并**

### 四个包

功能完全一样（同一份后端），区别只在"外壳"：

| 包 | 体积 | 是什么 |
|---|---|---|
| `PDFViewer-1.0.0-pre-electron-setup.exe` | 91 MB | **独立窗口 · 安装**。自带 Chromium，系统装没装 Edge 都无所谓 |
| `PDFViewer-1.0.0-pre-electron.zip` | 119 MB | **独立窗口 · 免安装**。解压即用，不写注册表 |
| `PDFViewer-1.0.0-pre-setup.exe` | 13 MB | **浏览器标签页 · 安装**。体积最小，用你默认的浏览器 |
| `PDFViewer-1.0.0-pre-win64.zip` | 15 MB | **浏览器标签页 · 免安装**。解压后双击 `PDFViewer.exe` |

两个版本共用同一份数据目录（`%APPDATA%\PDFViewer`），可以都装、换着用。

没买代码签名证书，第一次运行 Windows 会拦一下：**更多信息 → 仍要运行**。

> 文件名里的 `-pre` 是当时打包脚本的版本号写死留下的，**这个 release 本身是正式版**，
> 不是预发布。

### 和 `pre` 通道的差别

稳定版落后于开发进度，这是刻意的取舍：

| | V1.0.0 稳定版 | `pre` 通道 |
|---|:--:|:--:|
| 涂黑 / 重排 / 表格 / 批注 | ✅ | ✅ |
| 加密 PDF 读取（RC4 / AES-128 / AES-256） | ❌ | ✅ |
| 加密导出（设口令 + 权限位） | ❌ | ✅ |
| 双击正文原地改字 | ❌ | ✅ |
| 打印走系统阅读器 | ❌ | ✅ |
| 稳定性 | 完整测试通过 | 功能新，可能有问题 |

想用加密或原地改字，去 [预发布](https://github.com/NEWSfuturestudio/PDF-editor/releases/tag/pre_version)
下载 `pre` 通道的包。两边的数据目录是同一个，可以并存。

### 已知边界

| | |
|---|---|
| 扫描件 | 不支持。涂黑对图片里的文字无效，那需要 OCR |
| 整篇重排 | 不支持（表格能一格一格改、能删行；整篇重排要真正的排版引擎） |
| 表单域 | 不支持填写与生成 |
| 插入图片 | 只收 PNG / JPEG。CMYK 的 JPEG、16 位或隔行扫描的 PNG 会被拒绝 |
| 系统 | 打包只出 Windows；源码可在任意平台跑 |

---

## English

### What it is

A local, offline PDF editor. **No network, no uploads.** The installers bundle their own
runtime — you don't need Python, and there is nothing to `pip install`.

### Core capabilities

- **Redaction really deletes text** — glyphs are removed from the content stream, not
  painted over. `strings` won't find them afterwards, copy won't pick them up, and
  re-saving won't carry them out
- **Edit a paragraph and it re-wraps** — whatever no longer fits continues onto the next page
- **Tables** — rows detected by ruling lines; edit a cell, insert a row, delete a row, and
  the borders are redrawn to match
- **Annotations are native PDF comments** — still editable and deletable in Firefox or
  Acrobat after export
- **Chinese stays selectable and copyable** — a hand-rolled TrueType subsetter embeds only
  the glyphs you use
- **Create a blank PDF** — A4/A3/A5/Letter/Legal, landscape, 1–200 pages
- **Document-wide find & replace**, **delete by keyword** (reflow or leave a black mark),
  **split**, **merge**

### The four packages

Identical features (same backend) — the only difference is the shell:

| Package | Size | What it is |
|---|---|---|
| `PDFViewer-1.0.0-pre-electron-setup.exe` | 91 MB | **Standalone window · installer.** Bundles Chromium, so it doesn't matter whether Edge is installed |
| `PDFViewer-1.0.0-pre-electron.zip` | 119 MB | **Standalone window · portable.** Unzip and run. Writes nothing to the registry |
| `PDFViewer-1.0.0-pre-setup.exe` | 13 MB | **Browser tab · installer.** Smallest; opens in your default browser |
| `PDFViewer-1.0.0-pre-win64.zip` | 15 MB | **Browser tab · portable.** Unzip and run `PDFViewer.exe` |

Both flavours share one data directory (`%APPDATA%\PDFViewer`), so you can install them
side by side.

No code-signing certificate, so the first launch is blocked by Windows once:
**More info → Run anyway**.

> The `-pre` in the filenames is a leftover from a hardcoded version in the build script.
> **This release itself is stable**, not a pre-release.

### How it differs from the `pre` channel

The stable build lags behind development on purpose:

| | V1.0.0 stable | `pre` channel |
|---|:--:|:--:|
| Redaction / reflow / tables / annotations | ✅ | ✅ |
| Reading encrypted PDFs (RC4 / AES-128 / AES-256) | ❌ | ✅ |
| Encrypted export (password + permission bits) | ❌ | ✅ |
| Double-click in-place text editing | ❌ | ✅ |
| Printing through the system reader | ❌ | ✅ |
| Stability | Fully tested | Newer features, may have bugs |

For encryption or in-place editing, grab the
[pre-release](https://github.com/NEWSfuturestudio/PDF-editor/releases/tag/pre_version).
Both channels share the same data directory and can coexist.

### Known limits

| | |
|---|---|
| Scanned documents | Not supported. Redaction can't touch text inside images — that needs OCR |
| Whole-document reflow | Not supported (tables can be edited cell by cell; reflowing everything needs a real layout engine) |
| Form fields | Neither filling nor creating |
| Insert images | PNG / JPEG only. CMYK JPEG, 16-bit and interlaced PNGs are rejected |
| Platforms | Packages are Windows-only; the source runs anywhere |
