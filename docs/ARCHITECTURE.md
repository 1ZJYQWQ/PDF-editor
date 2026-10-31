# 架构与核心概念

README 只讲"能做什么"。这里讲"为什么这样做"和"坑在哪"。

---

## 数据模型

编辑与标注都不进 PDF，单独存 JSON，键是文档指纹 `sha1(文件名|文件大小)` 前 16 位：

```
annotations/<hash>.json    标注
edits/<hash>.json          编辑对象与页面操作
```

指纹只看文件名和大小，**挪动文件位置不丢标注**。

标注存的是**内容锚点**，不是像素坐标：

```js
{ page, parts: [{ itemIndex, charStart, charEnd }], text }
```

所以缩放、旋转、切换视图后，标注自动贴回原文。

---

## 文件地图

```
服务端（零依赖 PDF 引擎）
  server.py           HTTP 路由 + API + 路径白名单 + 端口顺延
  pdfedit.py          PDF 读写引擎（解析/序列化/对象表/内容流追加）
  pdfredact.py        ★ 涂黑引擎（内容流解释器 + 整字删除 + 泄漏面清理）
  pdfannot.py         批注 → PDF 原生注释（/Annots、/QuadPoints、UTF-16BE 备注）
  pdfpara.py          段落模型（按行距与首行缩进聚行、聚段）
  pdfreflow.py        ★ 段落重排（重新折行 + 页内下移 + 跨页续排）
  pdffont.py          ★ TrueType 子集化（glyf 重排 + 复合字形重映射）
  check.py            前端静态自检
  fontcheck.py        字体骨架 + 字重体检
  makefont.py         开发期工具：可变字体实例化（依赖 fontTools，仅开发期用）

前端（原生 ES Module）
  static/core.js      共享状态、els、api()、工具
  static/app.js       主逻辑（渲染、搜索、交互）—— 最大文件
  static/annot.js     标注模块
  static/edit.js      编辑 + 涂黑 + 替换 + 段落模块
  static/geom.js      纯几何逻辑（可测，无 DOM 依赖）
  static/vendor/      pdf.js（本地）

测试（run_tests.py 一条命令跑全部）
  pdffont_test.py     子集化（48 项）
  pdfembed_test.py    中文嵌入（49 项）
  pdfredact_test.py   涂黑引擎（52 项）
  pdfedit_test.py     编辑引擎
  pdfedit_verify.py   输出结构校验
  editapi_test.py     编辑 API（25 项）
  redactapi_test.py   涂黑 API（28 项）
  webtest.mjs         前端纯逻辑（67 项，node 跑）
```

---

## 两个技术高点

**`pdfredact.py`** —— 最难的是一个**内容流解释器**：维护完整图形状态栈
（`q`/`Q`/`cm`/`Tf`/`Td`/`Tm`/`Tc`/`Tw`/`Tz`/`TL`/`Ts`），算出每个字形在页面上的精确矩形
（TRM × CTM、字形空间 1/1000 em、字体宽度三级回退、CID 的 `/W`）。
算子编号必须与解释器**完全一致**，否则整字删除会静默失效——这是踩过的坑。

**`pdffont.py`** —— 用标准库从零写 TTF 子集化：重排 `glyf/loca/hmtx`、
重写复合字形组件索引、组装 sfnt 并算 `checkSumAdjustment`。一般这事要引 fontTools。

---

## 三个必须理解的模型

### 1. 「预览层」vs「焊死」

| | 编辑模式里 | 点「导出」之后 |
|---|---|---|
| 是什么 | 编辑数据里的一条记录（HTML 覆盖层） | 新 PDF 内容流里的绘制指令 |
| 能改吗 | 双击改、拖动、删除 | 定型了 |
| 能选中复制吗 | 浏览器选 HTML 文字 | 靠 `/ToUnicode` |

"焊死"不是配置项，**是"导出"这个动作本身**。导出产物是快照，原文件永远是工作台。

### 2. 涂黑 = 删字，不是盖块

PDF 里文字是"画上去的"（`BT /F1 12 Tf 72 720 Td (Hello) Tj ET`），不记录字符边界。
要"删"必须解释执行绘制指令，算出每个字形在哪，再把它从 `Tj`/`TJ` 里割掉。

两个最容易翻车的泄漏面：

- **孤儿内容流** —— 只把 `/Contents` 指向新流不够，旧流对象还躺在对象表里，
  `strings` 一扫就能看到"删除"的原文。必须从对象表删除。
- **书签标题** —— 页面上涂黑了，大纲 `/Title (...)` 里原文还在。必须比对后替换。

### 3. 「焊死」为什么改不动

子集字体里**只有用户写过的字形**。别人想改字 → 目标字形不存在 → 改不动。
能选中复制靠 `/ToUnicode`（CID 字体的唯一线索；标准 14 字体有内置 WinAnsiEncoding 兜底）。

---

## 测试的判据不是"跑通"，是"证明"

- **涂黑**：用攻击者手段（`strings` / 正则）验"字真的读不出来"
- **重写**：把输出**重新解释一遍**，看字形对不对（字节级断言会放过结构性错误）
- **中文**：用 `/ToUnicode` 把 GID 还原，与原文比对
- **跨引擎**：涂黑引擎能解释新写入的中文，涂黑后真的删掉

**硬约定**：测试**永不写用户的真实目录**（`documents/`、`exports/`、`edits/`、`annotations/`），
全部走 `_scratch/`，并用"跑前跑后快照对比"证明目录未变。
