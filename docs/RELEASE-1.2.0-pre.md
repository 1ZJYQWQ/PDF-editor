# V1.2.0-pre

两个大件：**加密**，和**像 Word 一样改字**。

Two big pieces: **encryption**, and **editing text in place**.

---

## 简体中文

### 🔐 加密：读写双向

以前只能打开别人的加密 PDF，现在能产出自己的。

- **导出时设口令**：AES-256（默认）/ AES-128。工具栏锁形按钮，或侧栏「加密导出…」
- **权限位**：禁打印 / 禁复制 / 禁修改，各有独立勾选，另有权限口令可解除限制
- **读的全能写**：RC4 40/128、AES-128、AES-256（R5 / R6）六档全部支持读取；
  写侧只产出现代档位，不产出 RC4 —— 2026 年再发 RC4 文件，比不加密更危险

> 权限位是**阅读器层面**的约定：Acrobat / 福昕会遵守，Chrome / Edge 内置的
> PDFium 根本不看。能挡随手操作，挡不住专门工具 —— 弹窗里也是这么写的。
>
> 口令丢了文件就真打不开。没有找回途径，没有后门，`/O` `/U` 里只有哈希和密钥。

验证方式不是"自己加密自己解"：产物同时交给本仓库解密器、**pdf.js**（浏览器同引擎），
再逐字节比对明文版的内容流。两边一起错会互相抵消，所以必须有第三方裁判。

### ✍️ 双击正文，原地改字

不是"把整页转成图片再画字"，是真的在原位置改：

- 双击正文任意一行 → 直接在原文上编辑
- 字体度量按 canvas 实测对齐（基线、字号、行内位置），不靠写死的常量
- 改完自动重新折行，**下方内容跟着移动**，放不下就续到下一页
- 中文**避头尾**：`，。、；：？！）】》」` 悬挂在行尾，`（【《「` 推到下一行。
  前端 CSS 与服务端禁则**同一套规则** —— 否则编辑时看到的换行和导出后不一样

### 🖨️ 打印

- 应用端不再弹「选择打开方式」：落盘成临时 PDF，交给系统默认阅读器
- 打印的是**你当前看到的**（含编辑与批注），不是原始文件
- 临时文件 24 小时后自动清理

### 🧭 其他

- **书签管理** + 快捷键面板（按 `?` 打开）
- 多标签各自记住滚动位置、缩放、编辑态与撤销栈

### 🐛 修复

- **从本地拖入的文件不能编辑** —— 这类文件没有磁盘路径，编辑链路拿不到它。
  现在会先落到本机暂存、拿到路径再打开。「请先打开一个文档」这句误导性提示也没了
- **段落编辑框不显示** —— 局部变量 `t` 遮蔽了翻译函数，点击后静默抛异常，
  界面上表现成"点了没反应"
- **选中文字「复制」崩溃** —— 同一个根因
- **打印取的是改动前的字节** —— 改为取当前状态

### 📦 升级

覆盖安装即可。数据（标注 / 编辑记录）在 `%APPDATA%\PDFViewer`，不动。
四个包共用同一份数据目录，可以换着用。

### ⚖️ 许可证变更

本项目从 **GPL-3.0** 改为 **Apache-2.0**（从本次发布起生效）。

Apache-2.0 带明确的专利授权，也不强制衍生作品开源 —— 更适合"拿去集成进内部流程"
这种正常用法。GPL 的传染性会挡住它。

> **注意**：之前发布的 `V1.0.0` / `V1.1.5-pre` 仍是 GPL-3.0。许可变更不追溯历史提交。

---

## English

### 🔐 Encryption, both directions

It could already open other people's encrypted PDFs. Now it can produce its own.

- **Set a password on export**: AES-256 (default) / AES-128 — via the lock button in the
  toolbar or "Encrypt & export…" in the sidebar
- **Permission bits**: no-print / no-copy / no-modify, each toggled independently, plus an
  owner password that lifts the restrictions
- **Everything readable is writable**: RC4 40/128, AES-128, AES-256 (R5 / R6) all open;
  the write side only produces modern ciphers. Shipping an RC4 file in 2026 is worse than
  shipping nothing

> Permission bits are a **reader-level agreement**: Acrobat and Foxit honour them, the
> PDFium inside Chrome and Edge ignores them entirely. They stop casual use, not a
> determined tool — the export dialog says exactly that.
>
> Lose the password and the file is gone. No recovery, no backdoor; `/O` and `/U` hold
> hashes and derived keys, not a byte that can rebuild the plaintext.

Verification is not "encrypt with our code, decrypt with our code": every artefact goes
through this repo's decryptor, **pdf.js** (the engine inside the browser), and a
byte-for-byte comparison of content streams against the unencrypted export. Two sides
wrong in the same way cancel out — an independent referee is mandatory.

### ✍️ Double-click body text, edit it right there

Not "flatten the page to an image and draw on top" — the real thing:

- Double-click any line and edit it where it sits
- Font metrics measured live on canvas (baseline, size, inline position) — no hardcoded
  constants
- Re-wraps automatically; **everything below moves down**; overflow continues onto the
  next page
- Chinese **kinsoku**: `，。、；：？！）】》」` hang past the line end, `（【《「` push to the
  next line. Front-end CSS and the server-side rule set are **the same rules** — otherwise
  the wrapping you see while editing would differ from what gets exported

### 🖨️ Printing

- No more "choose an app" dialog in the desktop build: the file is written to a temp PDF
  and handed to your default reader
- What prints is **what you see** — edits and annotations included
- Temp files are cleaned up after 24 hours

### 🧭 Also

- **Bookmark manager** + a shortcuts panel (press `?`)
- Tabs each keep their own scroll position, zoom, edit state and undo stack

### 🐛 Fixes

- **Files dragged in from disk could not be edited** — they have no on-disk path, so the
  edit chain could not reach them. They are now staged locally first, then opened. The
  misleading "open a document first" prompt is gone too
- **The paragraph editor never appeared** — a local variable `t` shadowed the translation
  function, throwing silently on click. On screen it looked like "the button does nothing"
- **Copying selected text crashed** — same root cause
- **Printing used the pre-edit bytes** — now prints the current state

### 📦 Upgrading

Install over the old version. Your data (annotations, edit records) lives in
`%APPDATA%\PDFViewer` and is untouched. All four packages share that directory, so you
can switch between them freely.

### ⚖️ License change

GPL-3.0 → **Apache-2.0**, effective from this release.

Apache-2.0 carries an explicit patent grant and does not force downstream code to be
open — a better fit for integrating this into an internal workflow, which GPL's
copyleft would block.

> **Note**: previously published `V1.0.0` / `V1.1.5-pre` remain GPL-3.0. The change is
> not retroactive.

---

**Full changelog**: [CHANGELOG.md](CHANGELOG.md)
