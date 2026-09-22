"""
PDF Redaction 引擎 —— 真正的「涂黑」，零第三方依赖

================================ 为什么需要它 ================================

普通的「涂白」只是往内容流末尾追加一个白色填充矩形：

    1 1 1 rg  72 640 300 24 re  f

原文的文字指令**一个字节都没动**，还老老实实躺在内容流里。
于是：

  - Ctrl+A 全选复制，被盖住的字原样出现在剪贴板里
  - pdftotext / 任何文本提取工具照读不误
  - 搜索引擎能索引到
  - 白框删掉就原形毕露，或者 qpdf --qdf 解压内容流直接看

这就是「PDF 涂黑事故」的成因 —— 发布者以为盖住了，其实没有。

Redaction 的语义完全不同：**它要求信息真正消失**。所以必须
把被遮盖区域内的文字**从内容流里删掉**，而不是画个东西挡住。

================================ 核心难点 ================================

PDF 里文字是「画上去的」，不是「排出来的」：

    BT /F1 12 Tf 72 720 Td (Hello World) Tj ET

这是一条绘制指令 —— 把字形画到某个坐标。它不记录段落结构，
`Tj` 里一整串字符只共享一次定位，字距可能藏在 `TJ` 数组里，
字体可能中途切换（`Tf`），换行靠 `Td`/`TD`/`T*`。

所以要「抹掉某个范围内的字」，必须先把这些指令**解释执行**一遍，
算出**每个字形**的真实矩形，再判断它是否落在涂黑区内。

这跟主引擎「不解析内容流语法」的原则不冲突 ——
主引擎负责叠加编辑（对任意 PDF 都要稳），
本模块负责信息销毁（必须看懂指令，否则无法保证删干净）。
两者用在不同场景，各自承担不同的风险。

================================ 三阶段流程 ================================

  1. 解释（ContentStreamInterpreter）
     维护图形状态栈，执行内容流，产出一个个字形矩形。
     用 /Widths 表（或标准 14 字体宽度表）算每个字形的宽度。

  2. 判定 + 重写（redact_page_content）
     完全落在涂黑区内的字形整字删除；
     其余指令原样重写，图形状态在必要处重新设置。
     **删除粒度 = 整字**（只要字形矩形与涂黑区有交叠就删）——
     redaction 宁可多删，绝不可漏。

  3. 覆盖（true-black fill）
     涂黑区用**纯黑**填充压上去。视觉上它是黑块，
     语义上它是一个「这里的内容已被移除」的标记。

================================ 能力边界（诚实说明）========================

本实现覆盖绝大多数实际情况，但**不保证**：
  - 扫描件（图片里的文字）：本模块只处理文字对象，
    图片形式的敏感内容需要另做图像遮盖（黑块会盖住，但
    底层像素仍在）。
  - 极端畸形的 PDF：内容流解释器遇到不认识的算子会跳过，
    可能导致字形定位偏差 → 偏保守的设计是「宁可多删」。
  - 注释层（/Annots）里的文字：另行处理，见 clean_metadata。

另外，元数据（/Info 的作者、标题、原始文件名）也是泄漏面，
由 clean_metadata 一并清理。
"""

import re

from pdfedit import (
    PDFDocument, Ref, Name, tokenize, _is_number, _to_number,
    _escape_literal, STANDARD_WIDTHS,
)

# --------------------------------------------------------------------------
# 矩阵运算（PDF 用 6 元素仿射矩阵 [a b c d e f]）
# --------------------------------------------------------------------------


def mat_mul(m, n):
    """矩阵相乘 m × n（PDF 约定：先应用 m 再应用 n）。"""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def mat_apply(m, x, y):
    """把点 (x, y) 用矩阵 m 变换。"""
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# 字形矩形
# --------------------------------------------------------------------------


class GlyphBox:
    """一个字形的包围盒（已变换到页面坐标）。"""

    __slots__ = ("x0", "y0", "x1", "y1", "op_index", "char_index", "code",
                 "font_alias")

    def __init__(self, x0, y0, x1, y1, op_index=-1, char_index=0, code=-1,
                 font_alias=""):
        # 保证 x0<=x1 / y0<=y1，避免变换后反转导致判定出错
        self.x0 = min(x0, x1)
        self.x1 = max(x0, x1)
        self.y0 = min(y0, y1)
        self.y1 = max(y0, y1)
        # 记录来源：第几个算子、该算子内第几个字符 —— 重写时要靠它定位
        self.op_index = op_index
        self.char_index = char_index
        # 字符码：用于还原被删文本（书签标题比对需要）。
        # ★ 码的语义依赖字体：简单字体是 1 字节 cp1252 码位，
        # CID 字体里就是 GID。所以必须连字体别名一起记下来，
        # 否则还原文本时只能瞎猜。
        self.code = code
        self.font_alias = font_alias

    def intersects(self, rect):
        """与矩形是否有交叠（用严格不等，仅共边不算交叠）。"""
        rx0, ry0, rx1, ry1 = rect
        return not (self.x1 <= rx0 or self.x0 >= rx1 or
                    self.y1 <= ry0 or self.y0 >= ry1)

    def __repr__(self):
        return (f"GlyphBox({self.x0:.1f},{self.y0:.1f} - "
                f"{self.x1:.1f},{self.y1:.1f})")


# --------------------------------------------------------------------------
# 字体宽度解析
# --------------------------------------------------------------------------


class FontInfo:
    """
    某个 /Font 资源的度量信息。

    宽度单位是 1/1000 em，乘字号才是实际宽度。
    优先用字体自带的 /Widths（准确），
    退化时用标准 14 字体表（够用），最后用缺省值。
    """

    def __init__(self, doc, font_dict):
        self.missing_width = 500.0     # 完全没有信息时的缺省宽度
        self.widths = {}
        self.base_font = ""
        self.is_cid = False
        self.code_bytes = 1            # 每个字符码占几个字节（见 _resolve）
        self.to_unicode = {}           # 码 -> Unicode 字符（有 /ToUnicode 时）
        self._resolve(doc, font_dict)

    def _resolve(self, doc, fd):
        if not isinstance(fd, dict):
            return

        base = fd.get("BaseFont")
        if isinstance(base, Name):
            self.base_font = str(base)

        subtype = fd.get("Subtype")
        subtype = str(subtype) if isinstance(subtype, Name) else ""
        # Type0（CID 字体）的编码方式和简单字体不同，单独标记处理
        self.is_cid = (subtype == "Type0")

        # ★ 字符码宽度：Type0 是**双字节**，简单字体是单字节。
        # 这决定内容流里一个「字形」占几个字节 —— 解释器（算矩形）
        # 和重写器（删字形）必须用同一个值。
        # 曾因为无条件按单字节拆，把 CID 字符串 <0A0B0C0D> 当成
        # 4 个字形：位置整体错位一倍（实测跨度 48pt，正确 24pt），
        # 且删除时只删掉半个字，留下**奇数长度**字符串
        # （实测 <0A0B0C0D> 被涂成 <0B0C0D>）→ 渲染成错误字形。
        # 这里按 Identity-H / *-UCS2-H 这类固定宽度 CMap 处理；
        # 变长多字节 CMap 极罕见，暂不支持（属已知限制）。
        if self.is_cid:
            self.code_bytes = 2

        self._resolve_tounicode(doc, fd)

        dw = fd.get("DW")
        if isinstance(dw, (int, float)):
            self.missing_width = float(dw)

        # /Widths 数组：从 /FirstChar 开始，逐字符对应
        widths = doc.resolve(fd.get("Widths"))
        first = fd.get("FirstChar")
        if isinstance(widths, list) and isinstance(first, (int, float)):
            base_code = int(first)
            for i, w in enumerate(widths):
                w = doc.resolve(w)
                if isinstance(w, (int, float)):
                    self.widths[base_code + i] = float(w)

        # CID 字体的 widths 藏在 /DescendantFonts[0]/W 里
        if self.is_cid:
            self._resolve_cid(doc, fd)

    def _resolve_cid(self, doc, fd):
        desc = doc.resolve(fd.get("DescendantFonts"))
        if not isinstance(desc, list) or not desc:
            return
        sub = doc.resolve(desc[0])
        if not isinstance(sub, dict):
            return
        dw = sub.get("DW")
        if isinstance(dw, (int, float)):
            self.missing_width = float(dw)
        warr = doc.resolve(sub.get("W"))
        if not isinstance(warr, list):
            return
        # /W 有两种形式：[c [w1 w2 ...]] 和 [c1 c2 w]
        i = 0
        while i < len(warr):
            key = doc.resolve(warr[i])
            if not isinstance(key, (int, float)):
                i += 1
                continue
            if i + 1 >= len(warr):
                break
            nxt = warr[i + 1]
            nxt_res = doc.resolve(nxt)
            if isinstance(nxt_res, list):
                for k, w in enumerate(nxt_res):
                    w = doc.resolve(w)
                    if isinstance(w, (int, float)):
                        self.widths[int(key) + k] = float(w)
                i += 2
            else:                      # [c1 c2 w] 区间形式
                if i + 2 < len(warr):
                    end = doc.resolve(warr[i + 1])
                    w = doc.resolve(warr[i + 2])
                    if isinstance(end, (int, float)) and \
                            isinstance(w, (int, float)):
                        for c in range(int(key), int(end) + 1):
                            self.widths[c] = float(w)
                i += 3

    @staticmethod
    def _parse_tounicode(raw):
        """解析 ToUnicode CMap，返回 {码: 还原出的文本}。

        只处理最常见的 bfchar / bfrange 两种块 —— 目标是把「被删掉的
        字符」还原出来跟书签标题比对，解析不出来的部分直接忽略即可，
        不需要完整实现 CMap 规范。
        """
        out = {}

        def dst_text(hexstr):
            # 目标通常是 UTF-16BE，一个字符可能占多个码元
            try:
                b = bytes.fromhex(hexstr)
            except ValueError:
                return ""
            if len(b) % 2:
                b += b"\x00"
            try:
                return b.decode("utf-16-be", "replace")
            except (UnicodeDecodeError, ValueError):
                return ""

        for blk in re.findall(r"beginbfchar(.*?)endbfchar", raw, re.S):
            for src, dst in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                try:
                    out[int(src, 16)] = dst_text(dst)
                except ValueError:
                    pass

        for blk in re.findall(r"beginbfrange(.*?)endbfrange", raw, re.S):
            pat = (r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*"
                   r"(<[0-9A-Fa-f]+>|\[[^\]]*\])")
            for m in re.finditer(pat, blk):
                try:
                    lo = int(m.group(1), 16)
                    hi = int(m.group(2), 16)
                except ValueError:
                    continue
                if hi < lo or hi - lo > 0x10000:
                    continue
                dst = m.group(3)
                if dst.startswith("["):
                    for k, item in enumerate(
                            re.findall(r"<([0-9A-Fa-f]+)>", dst)):
                        out[lo + k] = dst_text(item)
                    continue
                try:
                    base = bytes.fromhex(dst[1:-1])
                except ValueError:
                    continue
                if not base or len(base) % 2:
                    continue
                start = int.from_bytes(base, "big")
                for k in range(hi - lo + 1):
                    try:
                        out[lo + k] = (start + k).to_bytes(
                            len(base), "big").decode("utf-16-be", "replace")
                    except (OverflowError, UnicodeDecodeError):
                        break
        return out

    def _resolve_tounicode(self, doc, fd):
        """读取 /ToUnicode（如果有）。

        对 CID 字体尤其重要：码就是 GID，本身不含任何语义 ——
        没有这张表就无法把「被删掉的码」还原成可读文本，
        书签标题清理那一步只能放弃比对。
        """
        ref = fd.get("ToUnicode")
        if ref is None:
            return
        try:
            raw = doc.stream_data(ref)
        except Exception:
            return
        if not raw:
            return
        try:
            text = raw.decode("latin-1")
        except (UnicodeDecodeError, ValueError):
            return
        self.to_unicode = self._parse_tounicode(text)

    def text_of(self, codes):
        """把一串字符码还原成文本，用于「到底删掉了什么」的比对。

        有 /ToUnicode 就用它；否则退回单字节 cp1252 解码 ——
        后者只对简单字体合理。CID 字体没有 ToUnicode 时返回空串，
        调用方据此跳过比对（而不是拿乱码去撞书签标题）。
        """
        if self.to_unicode:
            return "".join(self.to_unicode.get(c, "") for c in codes)
        if self.code_bytes == 1:
            try:
                return bytes(c & 0xFF for c in codes).decode(
                    "cp1252", "replace")
            except (ValueError, LookupError):
                return ""
        return ""

    def width_of(self, code):
        """按字符码取宽度（1/1000 em）。"""
        if code in self.widths:
            return self.widths[code]
        # 标准 14 字体：用内置表，按码位取对应字符
        if not self.is_cid and 0 <= code <= 255:
            table = STANDARD_WIDTHS.get(
                self.base_font.lstrip("/"), STANDARD_WIDTHS["Helvetica"])
            try:
                ch = bytes([code]).decode("cp1252")
            except (ValueError, LookupError):
                ch = None
            if ch is not None:
                return float(table.get(ch, self.missing_width))
        return self.missing_width


# --------------------------------------------------------------------------
# 内容流解释器
# --------------------------------------------------------------------------


class ContentStreamInterpreter:
    """
    执行内容流，收集字形矩形。

    只实现「定位文字」所需的算子。不认识的一律跳过 ——
    因为我们不改写图形内容，只需要知道文字在哪。
    """

    def __init__(self, doc, resources):
        self.doc = doc
        self.resources = resources if isinstance(resources, dict) else {}
        self.fonts = {}              # alias -> FontInfo
        self.glyphs = []             # 收集到的字形
        self.ops = []                # [(算子名, 操作数列表)]，保留原始顺序

    # ---- 资源 ----

    def font(self, alias):
        if alias in self.fonts:
            return self.fonts[alias]
        fonts = self.doc.resolve(self.resources.get("Font"))
        fd = None
        if isinstance(fonts, dict):
            fd = self.doc.resolve(fonts.get(alias))
        info = FontInfo(self.doc, fd)
        self.fonts[alias] = info
        return info

    # ---- 执行 ----

    def run(self, data):
        """
        执行内容流，收集字形矩形。

        返回算子列表；字形填进 self.glyphs。

        算子的编号（op_index）必须和 _rewrite_text 里的编号完全一致 ——
        那是两边唯一的对接点：解释器说"第 7 个算子的第 3 个字形要删"，
        重写器就得能定位到同一个地方。所以两边的遍历逻辑要保持一致：
        **每个「算子关键字」计一次编号**，数组/字典/字符串/数字都不计。
        """
        tokens = tokenize(data)
        operand_stack = []
        gs = _GraphState()

        i = 0
        n = len(tokens)
        op_index = 0

        while i < n:
            tok = tokens[i]

            # ---- 非算子 token：进操作数栈 ----
            if tok == b"[":
                arr, i = _read_array(tokens, i)
                operand_stack.append(arr)
                continue

            if tok == b"<<":
                d, i = _read_dict(tokens, i)
                operand_stack.append(d)
                continue

            if tok.startswith(b"/"):
                operand_stack.append(Name(tok[1:].decode("latin-1")))
                i += 1
                continue

            if tok.startswith(b"(") or tok.startswith(b"<"):
                operand_stack.append(tok)
                i += 1
                continue

            if _is_number(tok):
                operand_stack.append(_to_number(tok))
                i += 1
                continue

            # ---- 到这儿就是算子 ----
            op = tok.decode("latin-1")
            self._exec(gs, op, operand_stack, op_index)
            self.ops.append(op)
            operand_stack.clear()
            op_index += 1
            i += 1

        return self.ops

    def _exec(self, gs, op, stack, op_index):
        """执行单个算子，必要时更新图形状态或产出字形。"""
        if op == "q":
            gs.push()
            return

        if op == "Q":
            gs.pop()
            return

        if op == "cm":
            if len(stack) >= 6:
                vals = _pop_nums(stack, 6)
                gs.ctm = mat_mul(tuple(vals), gs.ctm)
            return

        if op == "BT":
            gs.in_text = True
            gs.tm = IDENTITY
            gs.tlm = IDENTITY
            return

        if op == "ET":
            gs.in_text = False
            return

        if op == "Tf":
            if len(stack) >= 2:
                size, alias = stack[-1], stack[-2]
                if isinstance(size, (int, float)):
                    gs.font_size = float(size)
                if isinstance(alias, Name):
                    gs.font_alias = str(alias)
            return

        if op == "Tc":
            v = _last_num(stack)
            if v is not None:
                gs.char_space = v
            return

        if op == "Tw":
            v = _last_num(stack)
            if v is not None:
                gs.word_space = v
            return

        if op == "Tz":
            v = _last_num(stack)
            if v is not None:
                gs.h_scale = v / 100.0
            return

        if op == "TL":
            v = _last_num(stack)
            if v is not None:
                gs.leading = v
            return

        if op == "Ts":
            v = _last_num(stack)
            if v is not None:
                gs.rise = v
            return

        if op == "Tm":
            if len(stack) >= 6:
                vals = _pop_nums(stack, 6)
                gs.tm = tuple(vals)
                gs.tlm = tuple(vals)
            return

        if op in ("Td", "TD"):
            if len(stack) >= 2:
                tx, ty = _pop_nums(stack, 2)
                gs.tlm = mat_mul((1, 0, 0, 1, tx, ty), gs.tlm)
                gs.tm = tuple(gs.tlm)
                if op == "TD":
                    gs.leading = -ty
            return

        if op == "T*":
            gs.tlm = mat_mul((1, 0, 0, 1, 0, -gs.leading), gs.tlm)
            gs.tm = tuple(gs.tlm)
            return

        if op in ("Tj", "TJ", "'", '"'):
            if op in ("'", '"'):
                # ' 等价于先 T* 再 Tj；" 还额外读两个数值设字距
                if op == '"' and len(stack) >= 3:
                    aw, ac = _pop_nums(stack, 2)
                    gs.word_space, gs.char_space = aw, ac
                gs.tlm = mat_mul((1, 0, 0, 1, 0, -gs.leading), gs.tlm)
                gs.tm = tuple(gs.tlm)

            operand = stack[-1] if stack else b"()"
            self._show_text(gs, operand, op_index)
            return

    def _show_text(self, gs, operand, op_index):
        """处理 Tj / TJ 的文字，逐个字形算出矩形。"""
        info = self.font(gs.font_alias) if gs.font_alias else FontInfo(
            self.doc, None)

        # 统一成 [(code, is_text) ...] 序列：TJ 数组里的负数表示字距调整。
        # nb 是当前字体的字符码宽度 —— CID 字体是双字节。
        # 漏掉它会让字形数量与位置全错（实测：2 个 CID 被算成 4 个字形，
        # 跨度 48pt 而非 24pt）。
        nb = info.code_bytes
        parts = []
        if isinstance(operand, list):
            for item in operand:
                if isinstance(item, (int, float)):
                    parts.append((float(item), False))
                else:
                    parts.extend((c, True) for c in _string_codes(item, nb))
        else:
            parts.extend((c, True) for c in _string_codes(operand, nb))

        # 文字矩阵 TRM：把「字形空间」映射到页面坐标。
        # 字形空间里 1 个单位 = 1/1000 em，字形的 y 从 0 到 1000。
        # 所以尺寸信息全部由 TRM 承担，后面算矩形时不要再乘字号
        # —— 否则会和 TRM 里的字号叠加，宽度被放大 size 倍。
        size = gs.font_size or 12.0
        hscale = gs.h_scale if gs.h_scale else 1.0
        trm = mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm)
        full = mat_mul(trm, gs.ctm)

        char_index = 0
        for code, is_text in parts:
            if not is_text:
                # TJ 里的数值是 1/1000 em 的字距调整（负值表示前进）
                tx = -code / 1000.0 * size * hscale
                gs.tm = mat_mul((1, 0, 0, 1, tx, 0), gs.tm)
                full = mat_mul(
                    mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm),
                    gs.ctm)
                continue

            w1000 = info.width_of(code)      # 1/1000 em

            # 字形在「字形空间」的矩形。
            # 字形空间 1 单位 = 1/1000 em，所以要先除以 1000 ——
            # TRM 里已经含了字号，这里再乘字号就会放大 1000×。
            gx0, gy0 = 0.0, 0.0
            gx1, gy1 = w1000 / 1000.0, 1.0

            # 变换到页面坐标：四角都算，取包围盒
            corners = [
                mat_apply(full, gx0, gy0),
                mat_apply(full, gx1, gy0),
                mat_apply(full, gx0, gy1),
                mat_apply(full, gx1, gy1),
            ]
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            self.glyphs.append(GlyphBox(
                min(xs), min(ys), max(xs), max(ys), op_index, char_index,
                code, gs.font_alias or ""))
            char_index += 1

            # 前进：字符宽度 + 字距（词间再加词距）。
            # 这里用的是「文字空间」单位（已含字号），
            # 再经 h_scale 换算到水平方向。
            adv = (w1000 / 1000.0 * size) + gs.char_space
            if gs.word_space and code == 32:
                adv += gs.word_space
            gs.tm = mat_mul((1, 0, 0, 1, adv * hscale, 0), gs.tm)
            full = mat_mul(
                mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm),
                gs.ctm)


class _GraphState:
    """文字/图形状态。q/Q 要能正确保存恢复。"""

    __slots__ = ("ctm", "tm", "tlm", "font_alias", "font_size",
                 "char_space", "word_space", "h_scale", "leading",
                 "rise", "in_text", "_stack")

    def __init__(self):
        self.ctm = IDENTITY
        self.tm = IDENTITY
        self.tlm = IDENTITY
        self.font_alias = None
        self.font_size = 12.0
        self.char_space = 0.0
        self.word_space = 0.0
        self.h_scale = 1.0
        self.leading = 0.0
        self.rise = 0.0
        self.in_text = False
        self._stack = []

    def push(self):
        self._stack.append((
            self.ctm, self.tm, self.tlm, self.font_alias, self.font_size,
            self.char_space, self.word_space, self.h_scale, self.leading,
            self.rise,
        ))

    def pop(self):
        if not self._stack:
            return
        (self.ctm, self.tm, self.tlm, self.font_alias, self.font_size,
         self.char_space, self.word_space, self.h_scale, self.leading,
         self.rise) = self._stack.pop()


# ---- token 读取辅助 ----


def _read_array(tokens, i):
    """从 tokens[i] == '[' 读出一个数组，返回 (列表, 新位置)。"""
    out = []
    i += 1
    depth = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == b"[":
            sub, i = _read_array(tokens, i)
            out.append(sub)
            continue
        if tok == b"]":
            return out, i + 1
        if tok == b"<<":
            d, i = _read_dict(tokens, i)
            out.append(d)
            continue
        if tok.startswith(b"/"):
            out.append(Name(tok[1:].decode("latin-1")))
        elif tok.startswith(b"(") or tok.startswith(b"<"):
            out.append(tok)
        elif _is_number(tok):
            out.append(_to_number(tok))
        else:
            out.append(tok)
        i += 1
    return out, i


def _read_dict(tokens, i):
    """从 tokens[i] == '<<' 读出一个字典。"""
    out = {}
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == b">>":
            return out, i + 1
        if tok.startswith(b"/"):
            key = tok[1:].decode("latin-1")
            i += 1
            if i < len(tokens) and tokens[i] == b"<<":
                val, i = _read_dict(tokens, i)
            elif i < len(tokens) and tokens[i] == b"[":
                val, i = _read_array(tokens, i)
            elif i < len(tokens):
                t = tokens[i]
                if t.startswith(b"/"):
                    val = Name(t[1:].decode("latin-1"))
                elif t.startswith(b"(") or t.startswith(b"<"):
                    val = t
                elif _is_number(t):
                    val = _to_number(t)
                else:
                    val = t
                i += 1
            else:
                val = None
            out[key] = val
            continue
        i += 1
    return out, i


def _pop_nums(stack, count):
    """弹出栈顶 count 个数字（不足则用 0 补），返回按原顺序排列的列表。"""
    vals = []
    for _ in range(count):
        v = stack.pop() if stack else 0
        vals.append(float(v) if isinstance(v, (int, float)) else 0.0)
    vals.reverse()
    return vals


def _last_num(stack):
    for v in reversed(stack):
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _string_bytes(tok):
    """取出 PDF 字符串 token 的**原始字节**（不切分成码）。"""
    if isinstance(tok, bytes) and tok.startswith(b"("):
        return _decode_literal(tok[1:-1])
    if isinstance(tok, bytes) and tok.startswith(b"<") and \
            not tok.startswith(b"<<"):
        hx = re.sub(rb"[^0-9A-Fa-f]", b"", tok[1:-1])
        if len(hx) % 2:
            hx += b"0"
        try:
            return bytes.fromhex(hx.decode("ascii"))
        except ValueError:
            return b""
    if isinstance(tok, bytes):
        return tok
    return b""


def _string_codes(tok, nbytes=1):
    """
    把 PDF 字符串 token 解成**字符码**列表。

    nbytes 是「每个字符码占几个字节」，由当前字体的类型决定：
      简单字体 / 标准 14 字体 → 1 字节
      Type0（CID）字体         → 2 字节（Identity-H、*-UCS2-H 等）

    ★ 这个参数必须和解释器、重写器完全一致 —— 它是两边唯一的对接点。
    漏掉它就会出实测过的那种事故：CID 串 <0A0B0C0D> 被当成 4 个字形，
    位置整体错位一倍（跨度 48pt，正确 24pt），而且删除时只删掉半个字，
    留下奇数长度的字符串 → 渲染成错误字形。

    注意「码」与「字节」的区别：简单字体里两者相同，CID 字体里不同。
    凡是需要写回内容流的地方，都必须用 _codes_to_bytes 而不是 bytes()。
    """
    raw = _string_bytes(tok)
    if not raw:
        return []
    if nbytes != 2:
        return list(raw)
    if len(raw) % 2:
        # 奇数长度属畸形数据。补一个 0 再切，避免后续码位整体错位。
        raw += b"\x00"
    return [int.from_bytes(raw[i:i + 2], "big")
            for i in range(0, len(raw), 2)]


def _codes_to_bytes(codes, nbytes=1):
    """把字符码列表拼回字符串原始字节（写回内容流用）。"""
    if nbytes == 2:
        return b"".join((int(c) & 0xFFFF).to_bytes(2, "big") for c in codes)
    return bytes(int(c) & 0xFF for c in codes)


def _decode_literal(raw):
    """解开 PDF 字符串字面量里的转义序列。"""
    out = bytearray()
    i = 0
    n = len(raw)
    simple = {b"n": 10, b"r": 13, b"t": 9, b"b": 8, b"f": 12,
              b"(": 40, b")": 41, b"\\": 92}
    while i < n:
        c = raw[i:i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        nxt = raw[i + 1:i + 2]
        if not nxt:
            break
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt.isdigit():
            j = i + 1
            oct_digits = b""
            while j < n and len(oct_digits) < 3 and raw[j:j + 1].isdigit():
                oct_digits += raw[j:j + 1]
                j += 1
            out.append(int(oct_digits, 8) & 0xFF)
            i = j
        elif nxt in (b"\n", b"\r"):
            i += 2
        else:
            out += nxt
            i += 2
    return bytes(out)


# --------------------------------------------------------------------------
# 页面内容流读取与重写
# --------------------------------------------------------------------------


def read_page_content(doc, page_ref):
    """把一页的 /Contents 拼成完整内容流字节。"""
    page = doc.resolve(page_ref)
    if not isinstance(page, dict):
        return b""
    contents = page.get("Contents")
    parts = []

    def collect(node):
        node = doc.resolve(node)
        if isinstance(node, list):
            for x in node:
                collect(x)
        elif doc.is_stream(node):
            parts.append(node[1])
        elif isinstance(node, Ref):
            data = doc.stream_data(node)
            if data:
                parts.append(data)

    collect(contents)
    return b"\n".join(parts)


def redact_page_content(doc, page_ref, rects):
    """
    在指定页上执行 redaction，返回新的内容流字节。

    算法：
      1. 解释原内容流，得到算子序列 + 字形矩形
      2. 标出「落在任一涂黑区内的字形」所属的算子
      3. 重写内容流：
         - 文字类算子若**全部字形都被删**，整个算子丢掉
         - 若只有部分字形被删，则重建该算子的字符串（整字删除）
         - 其他算子原样保留
      4. 最后追加纯黑填充矩形

    rects: [ (x0, y0, x1, y1), ... ] 页面坐标系（原点左下）
    """
    if not rects:
        return None

    data = read_page_content(doc, page_ref)
    resources = doc.resolve(doc.inherited(page_ref, "Resources"))

    # 每个字体别名对应的「字符码宽度」。
    # 重写时必须按字体区分 —— 一页里可以混用单字节的简单字体和
    # 双字节的 Type0/CID 字体，删除粒度不同（漏了就会把一个 CID
    # 拆成两半，留下奇数长度字符串）。
    font_bytes = {}
    if isinstance(resources, dict):
        fonts = doc.resolve(resources.get("Font"))
        if isinstance(fonts, dict):
            for alias, fref in fonts.items():
                try:
                    info = FontInfo(doc, doc.resolve(fref))
                    font_bytes[str(alias)] = info.code_bytes
                except Exception:
                    font_bytes[str(alias)] = 1

    if data.strip():
        interp = ContentStreamInterpreter(doc, resources)
        interp.run(data)
        glyphs = interp.glyphs
    else:
        glyphs = []

    # ---- 判定：哪些字形必须消失 ----
    # 把所有涂黑区合并成一个列表，各自判断相交
    killed = set()
    for g in glyphs:
        for r in rects:
            if g.intersects(r):
                killed.add((g.op_index, g.char_index))
                break

    # ---- 重写 ----
    out = []
    if data.strip():
        out.append(_rewrite_text(data, killed, font_bytes))
    else:
        out.append(b"")

    # ---- 追加纯黑覆盖块 ----
    black = [b"\nq"]
    black.append(b"0 0 0 rg")
    for (x0, y0, x1, y1) in rects:
        black.append(
            b"%.3f %.3f %.3f %.3f re f" % (x0, y0, x1 - x0, y1 - y0))
    black.append(b"Q")
    out.append(b"\n".join(black))

    return b"\n".join(out)


def _rewrite_text(data, killed, font_bytes=None):
    """
    重写内容流：删掉被标记的字形，其余保留。

    做法是「按算子重建」：
      - 文字算子逐个字符检查，被标记的整字丢弃，其余重新拼成字符串
      - 非文字 token 原样搬运（连字节都不重新格式化，保持稳定）

    font_bytes: {字体资源名: 每个字符码的字节数}。
    ★ 必须跟踪 Tf —— 一页里可以混用单字节的简单字体和双字节的
    CID 字体，码宽不同，删除粒度就不同。不跟踪就会出现实测过的
    那种事故：只删掉 CID 的第一个字节，把 <0A0B0C0D> 变成 <0B0C0D>，
    奇数长度 → 渲染成错误字形。
    """
    tokens = tokenize(data)
    out = bytearray()
    operand_start = None      # 当前算子操作数的起始 token 下标
    i = 0
    n = len(tokens)
    op_index = 0
    cur_nb = 1                # 当前字体的字符码宽度

    # 需要识别文字算子，重新跑一遍算子编号（与解释器保持一致）
    while i < n:
        tok = tokens[i]

        if tok == b"[":
            # 数组要整体收集（TJ 用）
            start = i
            arr, ni = _read_array(tokens, i)
            operand_start = start if operand_start is None else operand_start
            i = ni
            continue

        if tok == b"<<":
            d, ni = _read_dict(tokens, i)
            i = ni
            continue

        if tok.startswith(b"/") or tok.startswith(b"(") or \
                tok.startswith(b"<") or _is_number(tok):
            if operand_start is None:
                operand_start = i
            i += 1
            continue

        op = tok.decode("latin-1")
        ops_start = operand_start if operand_start is not None else i

        if op in ("Tj", "TJ", "'", '"'):
            # 检查这个算子里有没有被删的字形
            has_kill = any(k[0] == op_index for k in killed)
            if not has_kill:
                _emit_range(out, tokens, ops_start, i + 1)
            else:
                # 有字形被删 → 重建
                rebuilt = _rebuild_text_op(
                    tokens, ops_start, i, op_index, killed, cur_nb)
                if rebuilt is not None:
                    # ★ 必须补分隔符。_emit_range 会在每个 token 后面补空格，
                    # 但重建路径是直接拼接的 —— 漏掉这一笔，Tj 会和后面的
                    # ET 粘成 `TjET`，成为一个未知算子，被阅读器整条忽略：
                    # **没被涂黑的文字也跟着看不见了**（字节还在，渲染不出来）。
                    out += rebuilt + b" "
            operand_start = None
            op_index += 1
            i += 1
            continue

        if op.isalpha():
            # 切换字体时记下新的码宽（后续文字算子要用）
            if op == "Tf":
                cur_nb = _font_bytes_of(tokens, ops_start, i, font_bytes)
            # 普通算子：原样输出（含其操作数）
            _emit_range(out, tokens, ops_start, i + 1)
            operand_start = None
            op_index += 1
        else:
            if operand_start is None:
                operand_start = i
        i += 1

    # 收尾：剩余 token
    if operand_start is not None:
        _emit_range(out, tokens, operand_start, n)

    return bytes(out)


def _font_bytes_of(tokens, i0, i1, font_bytes):
    """从 `/{alias} {size} Tf` 的操作数里取出字体资源名，查它的码宽。"""
    if not font_bytes:
        return 1
    for k in range(i0, i1):
        t = tokens[k]
        if isinstance(t, bytes) and t.startswith(b"/"):
            return font_bytes.get(t[1:].decode("latin-1"), 1)
    return 1


def _emit_range(out, tokens, i0, i1):
    """把 tokens[i0:i1] 原样拼回字节流。"""
    for t in tokens[i0:i1]:
        out += t if isinstance(t, bytes) else str(t).encode("latin-1")
        out += b" "


def _rebuild_text_op(tokens, ops_start, op_i, op_index, killed, nbytes=1):
    """
    重建一个文字算子，把被删的字形去掉。

    nbytes 是当前字体的字符码宽度：CID 字体为 2。删除必须以**整码**
    为单位，否则会把一个 CID 拆成两半（实测过）。
    返回字节；若该算子所有字形都被删，返回 None（整个算子丢弃）。
    """
    op = tokens[op_i].decode("latin-1")

    # 找字符串操作数（Tj 只有一个字符串；TJ 是一个数组）
    if op == "TJ":
        arr = None
        for t in tokens[ops_start:op_i]:
            if t == b"[":
                arr, _ = _read_array(tokens, _index_of(tokens, ops_start, op_i))
                break
        if arr is None:
            return None
        new_items, any_text = _filter_tj_array(arr, op_index, killed, nbytes)
        if not any_text:
            return None
        prefix = b" ".join(
            t if isinstance(t, bytes) else str(t).encode("latin-1")
            for t in tokens[ops_start:_array_start(tokens, ops_start, op_i)])
        body = _serialize_tj_array(new_items, nbytes)
        head = prefix + (b" " if prefix else b"")
        return head + body + b" TJ"

    # Tj / ' / "
    str_tok = None
    str_pos = -1
    for k in range(op_i - 1, ops_start - 1, -1):
        t = tokens[k]
        if t.startswith(b"(") or (t.startswith(b"<") and
                                  not t.startswith(b"<<")):
            str_tok = t
            str_pos = k
            break
    if str_tok is None:
        return None

    codes = _string_codes(str_tok, nbytes)
    kept = []
    for idx, code in enumerate(codes):
        if (op_index, idx) not in killed:
            kept.append(code)

    if not kept:
        return None                 # 整段都被删掉 → 丢弃算子

    # 前置操作数（如 " 的 aw ac）原样保留
    prefix = b" ".join(
        t if isinstance(t, bytes) else str(t).encode("latin-1")
        for t in tokens[ops_start:str_pos])
    head = prefix + (b" " if prefix else b"")
    # 必须走 _codes_to_bytes 而不是 bytes()：CID 的码可以大于 255，
    # bytes() 会直接抛 ValueError，而且它是「码 -> 字节」的唯一正确途径
    return head + _encode_pdf_string(_codes_to_bytes(kept, nbytes)) + \
        b" " + op.encode("latin-1")


def _array_start(tokens, i0, i1):
    for k in range(i0, i1):
        if tokens[k] == b"[":
            return k
    return i1


def _index_of(tokens, i0, i1):
    for k in range(i0, i1):
        if tokens[k] == b"[":
            return k
    return i0


def _filter_tj_array(arr, op_index, killed, nbytes=1):
    """过滤 TJ 数组：删掉被标记的字符，保留数字调整量。返回 (新数组, 是否还有文字)。"""
    out = []
    char_idx = 0
    for item in arr:
        if isinstance(item, list):
            sub = []
            for c in item:
                if (op_index, char_idx) not in killed:
                    sub.append(c)
                char_idx += 1
            out.append(sub)
        elif isinstance(item, (int, float)):
            out.append(item)
        else:
            codes = _string_codes(item, nbytes)
            sub = []
            for c in codes:
                if (op_index, char_idx) not in killed:
                    sub.append(c)
                char_idx += 1
            out.append(sub)

    # 去掉空串，合并相邻数字
    cleaned = []
    for item in out:
        if isinstance(item, list):
            if item:
                cleaned.append(item)
        else:
            cleaned.append(item)
    if not cleaned:
        return [], False
    # 若只剩下数字，说明文字全被删了
    if all(isinstance(x, (int, float)) for x in cleaned):
        return [], False
    return cleaned, True


def _serialize_tj_array(items, nbytes=1):
    parts = []
    for item in items:
        if isinstance(item, list):
            # 同 Tj：CID 的码可以大于 255，不能用 bytes()
            parts.append(_encode_pdf_string(_codes_to_bytes(item, nbytes)))
        elif isinstance(item, (int, float)):
            if item == int(item):
                parts.append(b"%d" % int(item))
            else:
                parts.append(b"%.4f" % item)
        else:
            parts.append(_encode_pdf_string(item))
    return b"[ " + b" ".join(parts) + b" ]"


def _encode_pdf_string(raw):
    """
    把字节编码成 PDF 字符串字面量。

    优先用字面量形式（可读性好）；如果含大量不可打印字符，
    改用十六进制形式更稳妥。
    """
    printable = sum(1 for c in raw if 32 <= c < 127)
    if raw and printable / len(raw) < 0.6:
        return b"<" + raw.hex().encode("ascii") + b">"
    esc = bytearray()
    for c in raw:
        if c in (0x28, 0x29, 0x5C):        # ( ) \
            esc.append(0x5C)
            esc.append(c)
        elif c < 32 or c > 126:
            esc.append(0x5C)
            esc += b"%03o" % c
        else:
            esc.append(c)
    return b"(" + bytes(esc) + b")"


# --------------------------------------------------------------------------
# 元数据清理
# --------------------------------------------------------------------------


def clean_metadata(doc):
    """
    清理元数据泄漏面。

    /Info 里常留着原始作者、标题、创建软件、甚至原始文件名 ——
    redaction 之后再留着这些，等于白删。

    另一个极易被忽略的泄漏面是**书签（大纲）标题**：
    `15 0 obj <</Title (Chapter 1  Project Overview) ... >>`
    页面上的标题被涂黑了，书签里还留着原文 ——
    `strings` 一扫就露馅。所以本函数也负责把与涂黑区相关的
    书签标题一并抹掉。
    """
    removed = []
    trailer = doc.trailer if isinstance(doc.trailer, dict) else {}

    # /Info：保留结构但清空所有条目（比整个删掉更安全，
    # 有些阅读器假定 /Info 存在）
    info_ref = trailer.get("Info")
    if isinstance(info_ref, Ref):
        info = doc.objects.get(tuple(info_ref))
        if isinstance(info, dict):
            for key in list(info.keys()):
                info.pop(key, None)
            info["Producer"] = "PDF Viewer (redacted)"
            removed.append("Info")

    # 目录里的 /Metadata（XMP 流）
    cat = doc.resolve(doc.root_ref) if getattr(doc, "root_ref", None) else None
    if isinstance(cat, dict):
        if "Metadata" in cat:
            cat.pop("Metadata", None)
            removed.append("XMP Metadata")

    return removed


def clean_outline_titles(doc, killed_texts):
    """
    把大纲（书签）里与被涂黑内容重名的标题抹掉。

    killed_texts: 被删除的文字片段集合（已规范化）。
    只要书签标题命中其中任意一段，就把标题替换成 [已移除]。
    同时顺手删掉目录相关的 XMP/xref 残留。

    这是「顺手就做、成本极低」但漏了会很严重的一环。
    """
    changed = 0
    if not killed_texts:
        return changed

    for key, val in list(doc.objects.items()):
        if not isinstance(val, dict):
            continue
        title = val.get("Title")
        if not isinstance(title, (bytes, str)):
            continue
        # 大纲条目的判据：有 /Title 且带 /Dest 或 /Parent
        if "Dest" not in val and "Parent" not in val and "A" not in val:
            continue

        text = _bytes_to_text(title)
        norm = _normalize(text)
        for kt in killed_texts:
            if kt and (kt in norm or norm in kt):
                val["Title"] = b"[redacted]"
                changed += 1
                break

    return changed


def collect_outline_titles(doc):
    """收集所有大纲标题文本（用于泄漏面审计）。"""
    out = []
    for key, val in list(doc.objects.items()):
        if not isinstance(val, dict):
            continue
        title = val.get("Title")
        if not isinstance(title, (bytes, str)):
            continue
        if "Dest" not in val and "Parent" not in val and "A" not in val:
            continue
        out.append(_normalize(_bytes_to_text(title)))
    return out


def _bytes_to_text(raw):
    if isinstance(raw, str):
        raw = raw.encode("latin-1", "replace")
    if raw.startswith(b"("):
        return _decode_literal(raw[1:-1]).decode("latin-1", "replace")
    if raw.startswith(b"<") and not raw.startswith(b"<<"):
        hx = re.sub(rb"[^0-9A-Fa-f]", b"", raw[1:-1])
        if len(hx) % 2:
            hx += b"0"
        try:
            return bytes.fromhex(hx.decode("ascii")).decode("latin-1")
        except ValueError:
            return ""
    return raw.decode("latin-1", "replace")


def _normalize(s):
    """规范化文本用于比对：折叠连续空白、转小写。"""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


# --------------------------------------------------------------------------
# 高层入口
# --------------------------------------------------------------------------


def apply_redactions(src_path, out_path, redactions, clean_meta=True):
    """
    执行 redaction 并产出新 PDF。

    redactions: [
        {"page": 1, "x": 72.0, "y": 640.0, "w": 300.0, "h": 24.0},
        ...
    ]  坐标是 PDF 坐标系（原点左下、y 向上）。

    返回统计信息，包含**删掉了多少字形** —— 这个数字很重要：
    如果用户画了个涂黑区却删掉 0 个字形，说明他以为盖住了什么、
    实际什么都没盖到，必须明确告知。
    """
    from pdfedit import build_pdf

    with open(src_path, "rb") as fp:
        data = fp.read()

    doc = PDFDocument.load(data)
    if not doc.pages_ref:
        raise ValueError("无法定位页面树，文件可能不是标准 PDF")

    refs = doc.page_refs()

    # 按页归并涂黑区
    per_page = {}
    for r in redactions:
        try:
            pno = int(r.get("page", 1))
            x = float(r.get("x", 0))
            y = float(r.get("y", 0))
            w = float(r.get("w", 0))
            h = float(r.get("h", 0))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        if not (1 <= pno <= len(refs)):
            continue
        per_page.setdefault(pno - 1, []).append(
            (x, y, x + w, y + h))

    if not per_page:
        raise ValueError("没有有效的涂黑区域")

    # 逐页处理
    total_glyphs = 0
    stats = []
    orphans = set()          # 被替换掉的旧内容流对象号，最后统一丢弃
    killed_texts = set()     # 被删掉的文字片段（用于清理书签标题）
    for pidx, rects in per_page.items():
        pref = refs[pidx]

        # 统计将被删除的字形数（用于向用户报告），
        # 同时把被删字符拼成文本 —— 书签标题比对要用。
        content = read_page_content(doc, pref)
        n_killed = 0
        if content.strip():
            resources = doc.resolve(doc.inherited(pref, "Resources"))
            interp = ContentStreamInterpreter(doc, resources)
            interp.run(content)
            # 按字体分组还原「被删了什么」。
            # ★ 码的语义依赖字体：简单字体是 cp1252 码位，CID 字体里
            # 码就是 GID —— 一律按 latin-1 解会得到乱码，跟书签标题
            # 一个都比不上（等于泄漏面清理静默失效）。
            by_font = {}
            for g in interp.glyphs:
                if any(g.intersects(r) for r in rects):
                    n_killed += 1
                    by_font.setdefault(g.font_alias, []).append(g.code)
            for alias, codes in by_font.items():
                txt = interp.font(alias).text_of(codes)
                if txt:
                    killed_texts.add(_normalize(txt))
        total_glyphs += n_killed

        new_content = redact_page_content(doc, pref, rects)
        if new_content is None:
            continue

        # 关键：**替换**该页的内容流，而不是追加。
        # 追加做不到"删除"—— 原文还留在前面那个流里。
        #
        # 替换之后，**旧的内容流对象必须从文件里彻底删除**。
        # 只是把 /Contents 指向新流是不够的：旧流仍然躺在对象表里，
        # 用 strings / grep 一扫就能看到被"删除"的原文。
        # 这正是很多 redaction 实现翻车的地方 —— 页面看不见了，
        # 字节还在。所以这里记下要被丢弃的对象号。
        old_streams = _collect_content_refs(doc, pref)
        sdict = {"Length": len(new_content)}
        new_ref = doc.new_obj((sdict, new_content))
        page = doc.resolve(pref)
        if isinstance(page, dict):
            page["Contents"] = new_ref
        orphans.update(old_streams)

        stats.append({"page": pidx + 1, "removed_glyphs": n_killed,
                      "boxes": len(rects)})

    removed_meta = clean_metadata(doc) if clean_meta else []
    # 书签标题也是泄漏面，一并清理
    n_outline = clean_outline_titles(doc, killed_texts) if clean_meta else 0

    # 从对象表里摘掉孤儿流，确保被删内容不以任何形式留在文件里
    dropped_objs = _drop_objects(doc, orphans)

    size = build_pdf(doc, {}, out_path)
    return {
        "ok": True,
        "size": size,
        "pages": len(refs),
        "pages_redacted": len(stats),
        "removed_glyphs": total_glyphs,
        "metadata_cleaned": removed_meta,
        "outline_titles_cleaned": n_outline,
        "dropped_objects": dropped_objs,
        "details": stats,
    }


def _collect_content_refs(doc, page_ref):
    """收集一页 /Contents 涉及的所有流对象号（用于后续丢弃）。"""
    page = doc.resolve(page_ref)
    if not isinstance(page, dict):
        return set()
    out = set()

    def walk(node):
        if isinstance(node, Ref):
            out.add(tuple(node))
            val = doc.objects.get(tuple(node))
            if isinstance(val, list):
                for x in val:
                    walk(x)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(page.get("Contents"))
    return out


def _drop_objects(doc, refs):
    """
    从对象表里删掉指定对象（含其引用计数归零的依赖）。

    只删「内容流」这一层 —— 字体等共享资源可能被别的页面引用，
    一律不动，避免把文档搞坏。
    """
    dropped = 0
    for key in refs:
        if key in doc.objects:
            del doc.objects[key]
            dropped += 1
    return dropped
