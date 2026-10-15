# unstable 分支改进记录

> 本分支在 `main` 之上做的一组增强，均已通过完整测试（`run_tests.py` 9 套件全绿）。
> 每项改动走独立功能分支 `git merge --no-ff` 合入，历史可追溯。

## 1. 修复：编辑面板与页面管理面板的定位

- 文件：`static/style.css`
- 问题：`.edit-palette` / `.page-panel` 用 `position: absolute` 相对 `.viewer`
  （它自带滚动），被钉在内容流顶部 —— 既挡住 PDF 正文，又随滚动一起滚走。
- 修复：改为 `position: fixed`（编辑栏贴视口底部居中、页面面板贴右上并让开主工具栏），
  不再遮挡正文、也不再随内容滚走。

## 2. 修复：路径白名单校验（403）

- 文件：`server.py`
- 问题 A：`is_within` 用 `startswith(root + os.sep)`，盘符根 `C:\` 自带尾分隔符，
  再拼一个 `os.sep` 会得到 `C:\\`，与真实路径 `C:\Program Files\...` 前缀不符 ——
  扫盘符根时**所有文件**被误判越权（整盘 403）。
- 问题 B：`api_file` 读内容时只校验全局 `SCAN_DIRS`，而 `api_files` 扫描用的是
  "全局 + 前端临时 `?dir=`"，两边不一致 —— 前端「添加目录」加进来的位置，
  列表能列出来、点开任何一个都 403。
- 修复：`is_within` 改用 `os.path.commonpath`（正确处理盘符根/大小写/分隔符），
  新增 `_clean_path` 兼容 junction 与 Windows `\\?\` 长路径前缀；
  新增 `_resolve_dirs` 把临时目录并入全局，并让所有取文件/读信息的端点共用同一份目录集。

## 3. 增强：预览字体与嵌入字体一致

- 文件：`static/style.css`、`server.py`
- 问题：编辑模式预览用系统兜底字体（≈雅黑），而导出嵌入 Noto Sans SC —— 不一致。
- 修复：新增 `@font-face` 指向 `assets/fonts/NotoSansSC-Regular.ttf`，
  `.ed-text` 字体栈加入该字体（拉丁仍走 Helvetica 对齐标准 14 字体）；
  服务端新增 `/assets/` 静态路由提供字体文件。

## 4. 优化：预览与导出的垂直位置对齐

- 文件：`static/edit.js`
- 问题：PDF 的 `Td` 定位的是**基线**，而 HTML 的 `top` 落在**行盒顶部**，
  两端语义不同 → 预览文字整体比导出**低约一行**（README 长期挂着的待办）。
- 修复：预览文字按**基线**对齐锚点 —— 用 canvas 实测字体度量算出
  "行盒顶部→基线"的距离再上移盒子（中英文字体行高不同，不写死比例）。

## 5. 新增：对象按角缩放（拖四角改变大小）

- 文件：`static/geom.js`、`static/edit.js`、`static/app.js`、`static/style.css`、`webtest.mjs`
- 选中矩形 / 椭圆 / 涂白 / 涂黑对象后，四角出现缩放手柄，拖动即改变大小（对角固定）。
- 几何换算抽成纯函数 `resizedRect`，并补 8 项 node 断言。

## 测试

- `run_tests.py`：9 套件全绿；`webtest.mjs` 从 59 项增至 **67 项**。
- `/assets/` 路由端到端：字体 200 / `font/ttf` / 10.4MB；路径穿越被拒（403）。
- `is_within` 边界用例：盘符根放行、`Documents2` 前缀陷阱仍被拒。

## 待人工验收（需肉眼）

- 编辑面板 / 页面管理面板的 `fixed` 定位效果；
- 预览与导出的垂直对齐（涉及字体度量，建议实机对照一次）；
- 对象缩放的拖拽手感。
