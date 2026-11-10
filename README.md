# PDF 查看器

简体中文 | [English](README.en.md)

纯本地、离线、**零第三方依赖**的 PDF 工具

---

## 它凭什么值得一看

1. **真正的涂黑** —— 把文字从内容流里删掉，不是盖一个黑块。`strings` 也搜不出来。
2. **写中文** —— 自写 TTF 子集化引擎，把用到的字形嵌进文件。能选中复制，但改不动（字形已焊死）。
3. **改原文并重新排版** —— 选中一段改写，重新折行，放不下就续到下一页。
4. **批注是 PDF 原生注释** —— 导出后在 Firefox、Acrobat 里能改、能删。

---

## 能做什么

| | |
|---|---|
| **阅读** | 缩放、翻页、单页 / 双栏、缩略图、目录、搜索、阅读书签、反色 |
| **多标签** | 一个窗口开多份文档，各自记住滚动位置、缩放、编辑态与撤销栈（Ctrl+Tab 轮换、Ctrl+W 关闭） |
| **标注** | 高亮 / 下划线 / 波浪线 / 删除线 / 便签 / 方框 → 写成 PDF 原生注释（或许你需要用鼠标选择文字？） |
| **编辑** | 图形对象（矩形/椭圆/直线/文字/涂白）、**插入图片 / 签名**（PNG/JPEG，保留透明）、页面管理、原位替换、段落重排、整篇查找替换 |
| **表格** | 一格一格改字（改长会自动撑高整行，边框跟着走）、按表格线认行、插一行 / 删一行（下面的内容跟着走、边框自动补齐） |
| **涂黑** | 删文字（不是涂白）。可框选，也可给一串关键词全篇处理：**删掉并重排**（后面的字补位）或**涂黑留痕**（版面不动、盖黑块）。零命中直接报错 |
| **新建** | 从零生成空白 PDF（A4/A3/A5/Letter/Legal，可横向，1~200 页），建好直接进编辑模式 |
| **导出** | 一键导出，批注与编辑一并写入，撞名不覆盖；可按页码范围导出、按份拆分、多份合并 |

---

## 快速开始

每次发布给四个 Windows 包，功能完全一样（同一份后端），区别只在「外壳」：

| 包 | 大小 | 说明 |
|---|---|---|
| `PDFViewer-1.1.5-pre-electron-setup.exe` | 96 MB | **独立窗口版 · 安装**。自带 Chromium 内核，系统里装没装 Edge 都无所谓；装到 `%LOCALAPPDATA%\Programs\PDFViewer`，不弹 UAC |
| `PDFViewer-1.1.5-pre-electron.zip` | 125 MB | **独立窗口版 · 免安装**。解压即用，不写注册表 |
| `PDFViewer-1.1.5-pre-setup.exe` | 12 MB | **浏览器版 · 安装**。体积小，用你默认的浏览器打开；装到 `%LOCALAPPDATA%\Programs\PDFViewer-Browser`，与上面互不干扰 |
| `PDFViewer-1.1.5-pre-win64.zip` | 16 MB | **浏览器版 · 免安装**。解压后双击 `PDFViewer.exe`，别只把 exe 拖出来，旁边的 `_internal` 是运行库 |

两个版本共用同一份数据（`%APPDATA%\PDFViewer`），可以同时装、换着用，
标注和编辑记录都在。

编辑数据、导出暂存都在 `%APPDATA%\PDFViewer`，删程序文件夹不影响它们。
卸载时会问一句要不要一并删掉，不点「是」就留着。
没买签名证书，第一次运行 Windows 会拦一下：**更多信息 → 仍要运行**。

**或者直接跑源码**（任意平台）：

```bash
python server.py --dir "你的 PDF 目录"
```

或者也可以双击server.py打开
默认浏览器打开 `http://127.0.0.1:8000`（端口被占会自动顺延）。
不需要 `pip install` 任何东西。

常用命令：

```bash
python run_tests.py        # 全部测试（20 个套件）
python check.py            # 前端静态自检
python fontcheck.py x.ttf  # 换字体前体检
pip install pyinstaller && python -m PyInstaller pdfviewer.spec   # 自己打 exe
```

四个包怎么打（在项目根目录）：

```powershell
# 1) 后端目录版 + 浏览器免安装
python -m PyInstaller pdfviewer.spec --noconfirm
Compress-Archive dist\PDFViewer\* dist\PDFViewer-1.1.5-pre-win64.zip

# 2) 浏览器版安装包（用 electron-builder 缓存里的 makensis，不必另装 NSIS）
& "$env:LOCALAPPDATA\electron-builder\Cache\nsis\nsis-3.0.4.1\Bin\makensis.exe" `
  -DVERSION=1.1.5-pre -DVERNUM=1.1.5.0 packaging\installer.nsi

# 3) Electron 两种包
npx electron-builder --win
Compress-Archive release\win-unpacked\* release\PDFViewer-1.1.5-pre-electron.zip
```

`pdfviewer.iss`（Inno Setup）也留着，装了 ISCC 的话编出来等价。

---

## 已知限制

| | |
|---|---|
| 扫描件 | 不支持。涂黑对图片里的文字无效，需 OCR |
| 整篇重排 | 不支持（表格可以一格一格改、可以删行；整篇重排要真正的排版引擎） |
| 表单 / 加密 | 不支持（签名可以贴一张透明 PNG，但那是一张图，不是可验签的签名域） |
| 插入图片 | 支持 PNG / JPEG。CMYK 的 JPEG、16 位或隔行扫描的 PNG 会被拒绝，提示改用普通 PNG |
| 改过的段落 | 字体是替换成标准字体的，字形与原上下文会有细微差异（或许你可以删除整段文字重排？） |

---

## 目录结构

```
server.py      HTTP 路由 + API + 路径白名单
launcher.py    exe 入口（起服务 + 开浏览器）
appenv.py      源码跑 / exe 跑 的路径规则（资源只读、数据进 AppData）
pdfedit.py     PDF 读写引擎（解析 / 序列化 / 内容流追加）
pdfredact.py   涂黑引擎（内容流解释器 + 整字删除 + 泄漏面清理）
pdfreflow.py   段落重排（折行 + 页内下移 + 跨页续排）
pdfbatch.py    批量：查找替换 / 按词删除或涂黑 / 范围导出 / 拆分 / 合并
pdfannot.py    批注 → PDF 原生注释
pdffont.py     TrueType 子集化
static/        前端（原生 ES Module，无框架无构建）
```

架构细节、核心模型与测试理念见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 字体

`assets/fonts/NotoSansSC-Regular.ttf`（思源黑体，OFL 授权，glyf 轮廓）。
换字体必须满足：OFL 可嵌入、glyf 轮廓、默认实例是常规字重。
可用环境变量 `PDFVIEWER_CJK_FONT` 覆盖路径。fontcheck.py可以给您提供更多信息
