"""
PDF 编辑引擎 —— 零第三方依赖

这是一个**精简但真实可用**的 PDF 读写器，只做「内容流编辑」路线该做的事：

  1. 解析原 PDF 的对象结构（保留全部原始对象，一个字节都不丢）
  2. 改页面树（删除 / 旋转 / 重排 / 插入空白页）
  3. 往页面内容流追加绘图指令（涂白、文字、图形）
  4. 重新序列化，产出完整的全新 PDF

关键设计取舍：
  - **不做回流重排**。PDF 里没有段落/换行的结构信息，重排必然有损。
    本引擎只在原内容之上"叠加"，原文绘制指令原样保留。
  - **不解析内容流语法**。我们只往后追加，不读懂原有指令，
    这是它能对任意 PDF 都稳定的根本原因。
  - **不压缩输出**。省掉 zlib 压缩逻辑，代价是文件略大，
    换来的是实现简单、可控、出错点少。

坐标系：PDF 原点在**左下角**，y 轴向上；而前端用的是屏幕坐标，
原点在左上角，y 轴向下。转换在 ContentBuilder 里统一处理。

中文字体：标准 14 字体里一个汉字字形都没有，所以写中文必须**嵌入字体**。
`pdffont.py` 负责按需子集化（只含实际用到的字形），本模块负责把子集
包成 PDF 要求的 /Type0 + /CIDFontType2 + /FontFile2 + /ToUnicode 链条。
"""

import hashlib
import re
import zlib

import pdffont

# --------------------------------------------------------------------------
# 词法：把 PDF 里的 token 切出来
# --------------------------------------------------------------------------

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"


def tokenize(data):
    """
    把一段 PDF 语法切成 token 列表（bytes 或字节串标记）。

    只做词法切分，不建语法树 —— 我们改写时是「原样搬运」，
    不需要理解语义，只需要知道边界在哪。
    """
    tokens = []
    i = 0
    n = len(data)
    while i < n:
        ch = data[i:i + 1]

        # 空白
        if ch in WHITESPACE:
            i += 1
            continue

        # 注释：% 到行末
        if ch == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue

        # 字典 / 名字 / 数组的定界符
        if ch in b"[]{}":
            tokens.append(ch)
            i += 1
            continue

        # 名字 /obj
        if ch == b"/":
            j = i + 1
            while j < n and data[j:j + 1] not in WHITESPACE and \
                    data[j:j + 1] not in DELIMITERS:
                j += 1
            tokens.append(data[i:j])
            i = j
            continue

        # 字符串 (literal)
        if ch == b"(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                c = data[j:j + 1]
                if c == b"\\":
                    j += 2
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                j += 1
            tokens.append(data[i:j])
            i = j
            continue

        # 十六进制字符串 <...>（注意和平凡字典 << 区分）
        if ch == b"<" and data[i:i + 2] != b"<<":
            j = data.find(b">", i)
            j = n if j < 0 else j + 1
            tokens.append(data[i:j])
            i = j
            continue

        if data[i:i + 2] == b"<<":
            tokens.append(b"<<")
            i += 2
            continue
        if data[i:i + 2] == b">>":
            tokens.append(b">>")
            i += 2
            continue

        # 普通 token（数字 / 关键字）
        j = i
        while j < n and data[j:j + 1] not in WHITESPACE and \
                data[j:j + 1] not in DELIMITERS:
            j += 1
        if j == i:      # 不应发生，防御性跳过
            j = i + 1
        tokens.append(data[i:j])
        i = j
    return tokens


def parse_object(tokens, pos):
    """
    从 tokens[pos] 开始解析一个对象，返回 (对象, 新位置)。

    对象用 Python 原生类型表示：
      dict -> dict，array -> list，number -> int/float，
      name -> Name，string -> bytes，ref -> Ref，
      true/false/null -> bool/None
    """
    if pos >= len(tokens):
        return None, pos

    tok = tokens[pos]

    if tok == b"<<":
        pos += 1
        out = {}
        while pos < len(tokens) and tokens[pos] != b">>":
            key = tokens[pos]
            if not key.startswith(b"/"):
                # 容错：跳过意外 token
                pos += 1
                continue
            key = decode_name(key)
            val, pos = parse_object(tokens, pos + 1)
            out[key] = val
        return out, pos + 1

    if tok == b"[":
        pos += 1
        out = []
        while pos < len(tokens) and tokens[pos] != b"]":
            val, pos = parse_object(tokens, pos)
            out.append(val)
        return out, pos + 1

    if tok.startswith(b"/"):
        return Name(decode_name(tok)), pos + 1

    if tok.startswith(b"(") or tok.startswith(b"<"):
        return tok, pos + 1

    if tok == b"true":
        return True, pos + 1
    if tok == b"false":
        return False, pos + 1
    if tok == b"null":
        return None, pos + 1

    # 数字，可能是「引用」的一部分： 12 0 R
    if _is_number(tok) and pos + 2 < len(tokens):
        if _is_int(tok) and _is_int(tokens[pos + 1]) and tokens[pos + 2] == b"R":
            return Ref(int(tok), int(tokens[pos + 1])), pos + 3

    if _is_number(tok):
        return _to_number(tok), pos + 1

    # 未知关键字（如 obj / endobj / stream），原样返回
    return tok, pos + 1


def _is_number(b):
    try:
        float(b)
        return True
    except (ValueError, TypeError):
        return False


def _is_int(b):
    return bool(re.fullmatch(rb"[+-]?\d+", b or b""))


def _to_number(b):
    return int(b) if _is_int(b) else float(b)


class Name(str):
    """PDF 名字对象，与普通字符串区分开。"""
    __slots__ = ()


class Ref(tuple):
    """间接引用，形如 12 0 R。"""
    __slots__ = ()

    def __new__(cls, num, gen):
        return super().__new__(cls, (num, gen))

    @property
    def num(self):
        return self[0]

    @property
    def gen(self):
        return self[1]


def decode_name(raw):
    """去掉前导 /，并解码 #XX 转义。"""
    if not raw.startswith(b"/"):
        return raw.decode("latin-1")
    body = raw[1:]
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i:i + 1] == b"#" and i + 2 < len(body):
            try:
                out.append(int(body[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(body[i])
        i += 1
    return bytes(out).decode("latin-1")


def encode_name(s):
    """把名字编码回 /XXXX 形式，必要时做 #XX 转义。"""
    out = bytearray(b"/")
    for byte in s.encode("latin-1", "replace"):
        if byte < 0x21 or byte > 0x7E or byte in b"()<>[]{}/%#":
            out += b"#%02X" % byte
        else:
            out.append(byte)
    return bytes(out)


# --------------------------------------------------------------------------
# 对象表：把整个 PDF 的所有间接对象读进来
# --------------------------------------------------------------------------

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")


class PDFDocument:
    """
    一个 PDF 文件的对象表。

    objects: {(num, gen): 值}
      值可能是 dict / list / bytes，或 **(字典, 流数据 bytes) 二元组**。

    重要设计：流对象用元组 (dict, data) 表示，**只存在 objects 这一处**。
    早期版本把流数据另存一个 streams 字典，结果序列化时漏掉了流数据
    （遍历 objects 只能看到字典，看不到数据），导致内容流全丢。
    单点存放从根上消除了这种"两处状态不一致"的隐患。
    """

    def __init__(self):
        self.objects = {}
        self.trailer = {}
        self.root_ref = None
        self.pages_ref = None
        self.max_obj = 0

    # ---- 流访问助手 ----

    @staticmethod
    def is_stream(val):
        return (isinstance(val, tuple) and len(val) == 2
                and isinstance(val[0], dict) and isinstance(val[1], bytes))

    def stream_data(self, ref):
        """取某个流对象的原始数据，没有则返回 None。"""
        val = self.objects.get(tuple(ref)) if isinstance(ref, Ref) else None
        return val[1] if self.is_stream(val) else None

    # ---- 解析 ----

    @classmethod
    def load(cls, data):
        doc = cls()
        doc._parse_objects(data)
        doc._find_root(data)
        return doc

    def _parse_objects(self, data):
        for m in OBJ_RE.finditer(data):
            num = int(m.group(1))
            gen = int(m.group(2))
            body_start = m.end()
            end = data.find(b"endobj", body_start)
            if end < 0:
                end = len(data)
            body = data[body_start:end]

            # 先看有没有 stream
            sm = re.search(rb"\bstream\r?\n?", body)
            head = body[:sm.start()] if sm else body
            tokens = tokenize(head)
            value, _ = parse_object(tokens, 0)

            if sm:
                # 流数据从 stream 关键字后开始，长度由 /Length 决定。
                # 优先按 /Length 精确读取（最可靠），否则回退到找 endstream。
                sdata = self._read_stream(body, sm, value, data, body_start)
                if isinstance(value, dict):
                    # 关键：流以 (字典, 数据) 元组形式存进 objects，
                    # 并去掉 /Filter —— 数据已经解压，带 Filter 会让
                    # 其他阅读器按压缩流去解读，必然出错。
                    value = dict(value)
                    value.pop("Filter", None)
                    value.pop("DecodeParms", None)
                    value["Length"] = len(sdata)
                    self.objects[(num, gen)] = (value, sdata)
                    self.max_obj = max(self.max_obj, num)
                    continue

            self.objects[(num, gen)] = value
            self.max_obj = max(self.max_obj, num)

    @staticmethod
    def _read_stream(body, sm, value, full_data, body_start):
        """读取流数据。按 /Length 精确定位，必要时解压。"""
        raw_start = sm.end()
        # 跳过 stream 后的换行
        if body[raw_start:raw_start + 1] == b"\r":
            raw_start += 1
        if body[raw_start:raw_start + 1] == b"\n":
            raw_start += 1

        length = None
        if isinstance(value, dict):
            length = value.get("Length")
            if isinstance(length, Ref):
                length = None      # 间接长度，退回查找 endstream

        if isinstance(length, int) and length >= 0:
            raw = body[raw_start:raw_start + length]
        else:
            e = body.find(b"endstream", raw_start)
            raw = body[raw_start:e] if e >= 0 else body[raw_start:]
            if raw.endswith(b"\r\n"):
                raw = raw[:-2]
            elif raw.endswith(b"\n"):
                raw = raw[:-1]

        # 解压。FlateDecode 最常见，其他编码保持原样返回。
        filters = value.get("Filter") if isinstance(value, dict) else None
        fname = None
        if isinstance(filters, Name):
            fname = filters
        elif isinstance(filters, list) and filters:
            first = filters[0]
            fname = first if isinstance(first, Name) else None

        if fname in ("FlateDecode", "Fl"):
            try:
                return zlib.decompress(raw)
            except zlib.error:
                try:
                    return zlib.decompressobj().decompress(raw)
                except zlib.error:
                    return raw
        return raw

    def _find_root(self, data):
        """找到 trailer 里的 /Root，以及 catalog 和页面树。"""
        # 从后往前找最后一个 trailer（增量更新的最后一份才是最新的）
        idx = data.rfind(b"trailer")
        while idx >= 0:
            end = data.find(b"startxref", idx)
            chunk = data[idx:end if end > 0 else idx + 4096]
            tm = re.search(rb"trailer\s*", chunk)
            if tm:
                tokens = tokenize(chunk[tm.end():])
                obj, _ = parse_object(tokens, 0)
                if isinstance(obj, dict):
                    self.trailer.update(obj)
                    if "Root" in obj:
                        break
            idx = data.rfind(b"trailer", 0, idx)

        # 有些 PDF（尤其 1.5+ 交叉引用流）没有 trailer 关键字，
        # 那就全表扫一遍找 /Type /Catalog
        root = self.trailer.get("Root")
        if isinstance(root, Ref):
            self.root_ref = root
        else:
            self.root_ref = self._find_catalog()

        if self.root_ref and self.root_ref in self.objects:
            cat = self.objects[self.root_ref]
            if isinstance(cat, dict) and isinstance(cat.get("Pages"), Ref):
                self.pages_ref = cat["Pages"]
            else:
                self.pages_ref = self._find_pages()
        else:
            self.pages_ref = self._find_pages()

    def _find_catalog(self):
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Catalog":
                return Ref(num, gen)
        return None

    def _find_pages(self):
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Pages" \
                    and "Parent" not in val:
                return Ref(num, gen)
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Pages":
                return Ref(num, gen)
        return None

    # ---- 取对象 ----

    def get(self, ref):
        """按引用取值，自动解 Ref。"""
        if isinstance(ref, Ref):
            return self.objects.get(tuple(ref))
        return ref

    def resolve(self, ref):
        """把 Ref 追到底，返回实际对象。"""
        seen = 0
        while isinstance(ref, Ref) and seen < 32:
            ref = self.objects.get(tuple(ref))
            seen += 1
        return ref

    def new_obj(self, value):
        """分配一个新对象号。"""
        self.max_obj += 1
        self.objects[(self.max_obj, 0)] = value
        return Ref(self.max_obj, 0)

    # ---- 页面树 ----

    def page_refs(self):
        """按顺序返回所有页面的 Ref（深度优先展平页面树）。"""
        out = []
        self._walk_pages(self.pages_ref, out, set())
        return out

    def _walk_pages(self, ref, out, visited):
        key = tuple(ref) if isinstance(ref, Ref) else None
        if key and key in visited:
            return
        if key:
            visited.add(key)

        node = self.resolve(ref)
        if not isinstance(node, dict):
            return
        ntype = node.get("Type")
        if ntype == "Page":
            out.append(ref if isinstance(ref, Ref) else None)
            return
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list):
            for kid in kids:
                if isinstance(kid, Ref):
                    self._walk_pages(kid, out, visited)

    def page_dict(self, ref):
        return self.resolve(ref)

    def inherited(self, ref, key, default=None):
        """沿 Parent 链向上找继承属性（MediaBox / Resources / Rotate）。"""
        seen = 0
        node = self.resolve(ref)
        while isinstance(node, dict) and seen < 32:
            if key in node:
                return node[key]
            parent = node.get("Parent")
            if not parent:
                break
            node = self.resolve(parent)
            seen += 1
        return default


# --------------------------------------------------------------------------
# 序列化：把对象表写回字节流
# --------------------------------------------------------------------------

def serialize_value(val, out):
    """把一个对象序列化成 PDF 语法，追加到 out（bytearray）。"""
    if val is None:
        out += b"null"
    elif val is True:
        out += b"true"
    elif val is False:
        out += b"false"
    elif isinstance(val, Name):
        out += encode_name(str(val))
    elif isinstance(val, Ref):
        out += b"%d %d R" % (val.num, val.gen)
    elif isinstance(val, (int, float)):
        if isinstance(val, float):
            if val == int(val) and abs(val) < 1e15:
                out += b"%d" % int(val)
            else:
                out += (b"%.6f" % val).rstrip(b"0").rstrip(b".")
        else:
            out += b"%d" % val
    elif isinstance(val, bytes):
        # 字符串：判断是 literal 还是 hex
        if val.startswith(b"(") or val.startswith(b"<"):
            out += val
        else:
            out += _escape_literal(val)
    elif isinstance(val, str):
        out += _escape_literal(val.encode("latin-1", "replace"))
    elif isinstance(val, list):
        out += b"["
        for i, item in enumerate(val):
            if i:
                out += b" "
            serialize_value(item, out)
        out += b"]"
    elif isinstance(val, dict):
        out += b"<<"
        for k, v in val.items():
            out += encode_name(k) + b" "
            serialize_value(v, out)
            out += b" "
        out += b">>"
    else:
        out += b"null"


def _escape_literal(raw):
    """把 bytes 写成 PDF literal string，转义特殊字符。"""
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):        # ( ) \
            out += b"\\" + bytes([byte])
        elif byte == 0x0A:
            out += b"\\n"
        elif byte == 0x0D:
            out += b"\\r"
        elif byte == 0x09:
            out += b"\\t"
        elif byte < 0x20 or byte > 0x7E:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    out += b")"
    return bytes(out)


def build_pdf(doc, extra_streams, out_path):
    """
    把文档写成完整的 PDF 文件。

    extra_streams: {page_ref_tuple: content_bytes}
      每页要追加的内容流（我们的编辑绘制指令）。

    做法：为有编辑内容的页面创建一个新的内容流对象，
    并把该页的 /Contents 变成「原内容 + 新内容」的数组 ——
    这样原文一个字节没动，编辑层叠加在后面。
    """
    out = bytearray()
    out += b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"

    # 1) 先把每个页面的 Contents 接上新流
    for (pnum, pgen), content in extra_streams.items():
        if not content:
            continue
        sdict = {
            "Length": len(content),
        }
        stream_ref = doc.new_obj((sdict, content))

        page = doc.objects.get((pnum, pgen))
        if not isinstance(page, dict):
            continue
        old = page.get("Contents")
        if old is None:
            page["Contents"] = stream_ref
        elif isinstance(old, list):
            page["Contents"] = list(old) + [stream_ref]
        else:
            page["Contents"] = [old, stream_ref]

    # 2) 序列化所有对象，记录偏移
    offsets = {}
    for (num, gen), val in sorted(doc.objects.items()):
        offsets[(num, gen)] = len(out)
        out += b"%d %d obj\n" % (num, gen)
        if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], dict):
            # (字典, 流数据)
            sdict, sdata = val
            serialize_value(sdict, out)
            out += b"\nstream\n"
            out += sdata
            out += b"\nendstream"
        else:
            serialize_value(val, out)
        out += b"\nendobj\n"

    # 3) 交叉引用表
    xref_pos = len(out)
    count = doc.max_obj + 1
    out += b"xref\n"
    out += b"0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for i in range(1, count):
        off = offsets.get((i, 0))
        if off is None:
            out += b"0000000000 65535 f \n"
        else:
            out += b"%010d 00000 n \n" % off

    # 4) trailer
    trailer = {
        "Size": count,
        "Root": doc.root_ref,
    }
    # 保留原有 Info（文档属性）
    if isinstance(doc.trailer.get("Info"), Ref):
        trailer["Info"] = doc.trailer["Info"]
    out += b"trailer\n"
    serialize_value(trailer, out)
    out += b"\nstartxref\n%d\n%%%%EOF\n" % xref_pos

    with open(out_path, "wb") as fp:
        fp.write(out)
    return len(out)


# --------------------------------------------------------------------------
# 内容流绘制指令生成
# --------------------------------------------------------------------------

class ContentBuilder:
    """
    把编辑对象翻译成 PDF 绘图指令。

    坐标系要点：PDF 原点在左下、y 向上；前端给的是屏幕坐标、y 向下。
    这里统一约定：调用方传进来的坐标已经是「PDF 坐标系的」，
    即 y 已经翻转过、原点已归到页面左下角。
    """

    # 嵌入的中文字体在页面资源里用的别名（与 F1/F2 这类标准字体别名分开）
    CID_ALIAS = "CJK"

    def __init__(self):
        self.ops = []
        # 别名 -> 字体资源值。标准 14 字体放**普通 dict**；
        # 嵌入的 CID 字体放**一个 Ref**（指向 /Type0 对象）。
        # 两者混在同一个字典里，走同一个挂载路径。
        self.fonts = {}
        self.cid_ref = None         # 嵌入字体的 Ref（可为 None）
        self.cid_subset = None      # pdffont.SubsetFont
        self.cid_used = False       # 是否真的用到（没用到就不必挂资源）
        self.missing_chars = set()  # 字体里没有的字符 —— 要上报，不能静默丢

    def set_cid_font(self, font_ref, subset):
        """启用嵌入的中文字体。

        之后凡是含 latin-1 之外字符的文字都走它 —— 一条 Tj 只能用一个
        字体，所以整串一起走（嵌入字体本身也含 ASCII 字形）。
        """
        self.cid_ref = font_ref
        self.cid_subset = subset

    def _font_alias(self, base_font):
        """注册标准 14 字体，返回资源名。不嵌入字体程序。"""
        for alias, val in self.fonts.items():
            if isinstance(val, dict) and str(val.get("BaseFont")) == base_font:
                return alias
        n = sum(1 for v in self.fonts.values() if isinstance(v, dict)) + 1
        alias = "F%d" % n
        self.fonts[alias] = {
            "Type": Name("Font"),
            "Subtype": Name("Type1"),
            "BaseFont": Name(base_font),
            "Encoding": Name("WinAnsiEncoding"),
        }
        return alias

    # ---- 基础图形 ----

    def rect(self, x, y, w, h, rgb, fill=True, stroke=False, line_width=1.0):
        r, g, b = rgb
        if fill:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        if stroke:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} RG")
            self.ops.append(f"{line_width:.4f} w")
        self.ops.append(f"{x:.3f} {y:.3f} {w:.3f} {h:.3f} re")
        if fill and stroke:
            self.ops.append("B")
        elif fill:
            self.ops.append("f")
        else:
            self.ops.append("S")

    def ellipse(self, x, y, w, h, rgb, fill=True, stroke=False, line_width=1.0):
        """用四段贝塞尔曲线近似椭圆。"""
        k = 0.5523
        rx, ry = w / 2.0, h / 2.0
        cx, cy = x + rx, y + ry
        r, g, b = rgb
        if fill:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        if stroke:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} RG")
            self.ops.append(f"{line_width:.4f} w")
        p = []
        p.append(f"{cx + rx:.3f} {cy:.3f} m")
        p.append(f"{cx + rx:.3f} {cy + ry * k:.3f} {cx + rx * k:.3f} "
                 f"{cy + ry:.3f} {cx:.3f} {cy + ry:.3f} c")
        p.append(f"{cx - rx * k:.3f} {cy + ry:.3f} {cx - rx:.3f} "
                 f"{cy + ry * k:.3f} {cx - rx:.3f} {cy:.3f} c")
        p.append(f"{cx - rx:.3f} {cy - ry * k:.3f} {cx - rx * k:.3f} "
                 f"{cy - ry:.3f} {cx:.3f} {cy - ry:.3f} c")
        p.append(f"{cx + rx * k:.3f} {cy - ry:.3f} {cx + rx:.3f} "
                 f"{cy - ry * k:.3f} {cx + rx:.3f} {cy:.3f} c")
        p.append("h")
        self.ops.extend(p)
        if fill and stroke:
            self.ops.append("B")
        elif fill:
            self.ops.append("f")
        else:
            self.ops.append("S")

    def line(self, x1, y1, x2, y2, rgb, line_width=1.0):
        r, g, b = rgb
        self.ops.append(
            f"{r:.4f} {g:.4f} {b:.4f} RG {line_width:.4f} w "
            f"{x1:.3f} {y1:.3f} m {x2:.3f} {y2:.3f} l S"
        )

    # ---- 文字 ----

    def text(self, x, y, s, size, rgb, base_font="Helvetica"):
        """写一段文字，**支持多行**（按换行拆）。

        两条路：
          - **含 latin-1 之外的字符（中文等）** → 走嵌入的 CID 字体
            （Type0 / Identity-H，码是 2 字节 GID）。没有这一步，
            中文会被静默替换成 ?。
          - 纯 latin-1 → 走标准 14 字体，不嵌字体程序、输出更小。
        """
        if s and self.cid_subset is not None and has_non_latin(s):
            self._text_cid(x, y, s, size, rgb)
            return

        alias = self._font_alias(base_font)
        r, g, b = rgb
        leading = line_leading(size)
        self.ops.append("BT")
        self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        self.ops.append(f"/{alias} {size:.3f} Tf")
        for i, ln in enumerate(split_lines(s)):
            # 第一行绝对定位；后续行用相对 Td 下移一个行高。
            # ★ Td 的偏移是相对**当前行矩阵** Tlm 的，所以这样连用正好
            #   逐行下移。反过来「自己算 y - i×行高」再用相对 Td 会双重偏移。
            self.ops.append(f"{x:.3f} {y:.3f} Td" if i == 0
                            else f"0 -{leading:.3f} Td")
            self.ops.append(f"({_pdf_text_encode(ln, base_font)}) Tj")
        self.ops.append("ET")

    def _text_cid(self, x, y, s, size, rgb):
        """用嵌入的中文字体写字，**支持多行**。

        码是 2 字节 GID，所以用十六进制字符串形式 <..> 写，
        避免字面量里的字节被当成转义序列或控制字符。
        """
        encs = []
        for ln in split_lines(s):
            enc, missing = self.cid_subset.encode(ln)
            if missing:
                # 字体里没有的字形必须上报 —— 静默丢字是最糟的结果：
                # 用户以为写进去了，实际没有
                self.missing_chars.update(missing)
            encs.append(enc)
        if not any(encs):
            return
        self.cid_used = True
        r, g, b = rgb
        leading = line_leading(size)
        self.ops.append("BT")
        self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        self.ops.append(f"/{self.CID_ALIAS} {size:.3f} Tf")
        for i, enc in enumerate(encs):
            # 空行也照样下移 —— 空行本身就是一次纯粹的纵向推进
            self.ops.append(f"{x:.3f} {y:.3f} Td" if i == 0
                            else f"0 -{leading:.3f} Td")
            if enc:
                self.ops.append("<%s> Tj" % enc.hex().upper())
        self.ops.append("ET")

    def measure_text(self, s, size, base_font="Helvetica"):
        """
        估算文字宽度。用标准字体的近似字宽表。
        用于前端对齐参考 —— 精度要求不高。
        """
        widths = STANDARD_WIDTHS.get(base_font, STANDARD_WIDTHS["Helvetica"])
        total = 0.0
        for ch in s:
            total += widths.get(ch, 0.556)
        return total * size

    # ---- 输出 ----

    def to_bytes(self):
        return ("\n".join(self.ops) + "\n").encode("latin-1", "replace")

    def font_resources(self):
        """返回 {别名: 字体资源值}。

        值是**普通 dict**（标准 14 字体）或**一个 Ref**（嵌入的 CID 字体）。
        由 _attach_font_resource 原样合并进页面 Resources —— 不再经过
        「拼字符串再正则解回来」那一圈，因为带 Ref 的字体字典没法那样表达。
        """
        out = dict(self.fonts)
        if self.cid_used and self.cid_ref is not None:
            out[self.CID_ALIAS] = self.cid_ref
        return out or None


# Helvetica 字符宽度表（1000 单位 em），用于文字宽度估算
STANDARD_WIDTHS = {
    "Helvetica": {
        " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
        "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
        ".": 278, "/": 278, "0": 556, "1": 556, "2": 556, "3": 556, "4": 556,
        "5": 556, "6": 556, "7": 556, "8": 556, "9": 556, ":": 278, ";": 278,
        "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
        "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
        "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
        "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
        "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
        "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
        "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
        "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
        "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
        "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
        "{": 334, "|": 260, "}": 334, "~": 584,
    },
}
STANDARD_WIDTHS["Helvetica-Bold"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Helvetica-Oblique"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Times-Roman"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Courier"] = {}


def split_lines(s):
    """按换行拆行（同时吃 \\r\\n 与 \\r）。

    前端文字输入框写着「可多行」，但原来导出只发一个 Tj，
    换行符被当成普通字符塞进字符串里 —— 渲染器把它当字符码 10 去画，
    没有对应字形，等于白画。**UI 承诺了就得兑现。**
    """
    return s.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def line_leading(size):
    """行高 = 字号 × 1.25。

    必须与前端 confirmTextPop 里的 lineH 保持一致，
    否则预览的行距和导出对不上。
    """
    return (size or 12.0) * 1.25


def _pdf_text_encode(s, base_font="Helvetica"):
    """
    把 Python 字符串编码成 PDF 字符串字面量内容。

    标准 14 字体用 WinAnsiEncoding，只覆盖 latin-1 范围。
    中文等字符无法用标准字体渲染 —— 这里替换成 '?'，
    并在调用方层面统计上报（不能静默吞掉）。

    控制字符必须转义：**裸换行写进字面量字符串会被渲染器当成
    字符码 10 去画** —— 没有对应字形。所以 \n 这类一律走 PDF 转义序列。
    """
    out = []
    for ch in s:
        code = ord(ch)
        if code > 255:
            out.append("?")
            continue
        if ch in ("(", ")", "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 32:
            out.append("\\%03o" % code)
        else:
            out.append(ch)
    return "".join(out)


def has_non_latin(s):
    """检测字符串里有没有标准字体渲染不了的字符。"""
    return any(ord(ch) > 255 for ch in s)


# --------------------------------------------------------------------------
# 页面管理
# --------------------------------------------------------------------------

def reorder_pages(doc, order):
    """
    重排页面。order 是「原页面下标（0-based）的数组」，
    表示新顺序。

    做法：把页面树的 /Kids 直接替换成按新顺序排列的引用数组。
    不动任何页面对象本身 —— 所以是零风险的。
    """
    refs = doc.page_refs()
    kids = [refs[i] for i in order if 0 <= i < len(refs)]
    pages_node = doc.resolve(doc.pages_ref)
    if isinstance(pages_node, dict):
        pages_node["Kids"] = kids
        pages_node["Count"] = len(kids)
    # 同步修正 Parent 指针
    for ref in kids:
        p = doc.resolve(ref)
        if isinstance(p, dict):
            p["Parent"] = doc.pages_ref
    return len(kids)


def rotate_page(doc, index, delta):
    """把第 index 页（0-based）旋转 delta 度（顺时针）。"""
    refs = doc.page_refs()
    if not (0 <= index < len(refs)):
        return False
    page = doc.resolve(refs[index])
    if not isinstance(page, dict):
        return False
    cur = doc.inherited(refs[index], "Rotate", 0) or 0
    try:
        cur = int(cur)
    except (TypeError, ValueError):
        cur = 0
    page["Rotate"] = (cur + delta) % 360
    return True


def insert_blank_page(doc, index, width, height):
    """
    在 index 位置插入一张空白页。

    新建的页面对象必须自带 MediaBox —— 因为它没有父节点可继承。
    """
    refs = doc.page_refs()
    # 取参考页尺寸，保持视觉一致
    if refs:
        mb = doc.inherited(refs[0], "MediaBox")
        if isinstance(mb, list) and len(mb) == 4:
            width, height = _num(mb[2]) - _num(mb[0]), _num(mb[3]) - _num(mb[1])

    # 空白页也要有一个内容流，否则某些阅读器会对 /Contents 缺失报错
    content_ref = doc.new_obj(({"Length": 0}, b""))

    page = {
        "Type": Name("Page"),
        "Parent": doc.pages_ref,
        "MediaBox": [0, 0, float(width), float(height)],
        "Resources": {},
        "Contents": content_ref,
    }
    ref = doc.new_obj(page)

    pages_node = doc.resolve(doc.pages_ref)
    if not isinstance(pages_node, dict):
        return None
    kids = pages_node.get("Kids")
    if not isinstance(kids, list):
        kids = []
    index = max(0, min(index, len(kids)))
    kids.insert(index, ref)
    pages_node["Kids"] = kids
    pages_node["Count"] = len(kids)
    return ref


def delete_pages(doc, indices):
    """删除指定下标（0-based）的页面。"""
    refs = doc.page_refs()
    drop = {i for i in indices if 0 <= i < len(refs)}
    keep = [r for i, r in enumerate(refs) if i not in drop]
    pages_node = doc.resolve(doc.pages_ref)
    if isinstance(pages_node, dict):
        pages_node["Kids"] = keep
        pages_node["Count"] = len(keep)
    return len(keep)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 高层入口：应用全部编辑
# --------------------------------------------------------------------------

def _to_rgb(color, default=(0.0, 0.0, 0.0)):
    """把 '#rrggbb' / 'r,g,b' 转成 0~1 的三元组。"""
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        vals = []
        for c in color[:3]:
            c = _num(c, 0.0)
            vals.append(c / 255.0 if c > 1.0 else c)
        return tuple(vals)
    if not isinstance(color, str):
        return default
    s = color.strip().lstrip("#")
    if len(s) in (3, 6) and re.fullmatch(r"[0-9a-fA-F]+", s):
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        return (int(s[0:2], 16) / 255.0,
                int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0)
    return default


# --------------------------------------------------------------------------
# 中文字体嵌入（Type0 / Identity-H）
# --------------------------------------------------------------------------


def _width_array(widths):
    """/W 宽度数组。

    两种形式都合法：`[c [w1 w2 ...]]` 逐码列出，`[c1 c2 w]` 区间同宽。
    连续同宽的码合并成区间能小很多 —— CJK 全宽字基本都 1000。
    """
    if not widths:
        return []
    out = []
    gids = sorted(widths)
    n = len(gids)
    i = 0
    while i < n:
        j = i
        w = widths[gids[i]]
        while (j + 1 < n and gids[j + 1] == gids[j] + 1
               and widths[gids[j + 1]] == w):
            j += 1
        if j - i >= 2:
            out += [gids[i], gids[j], w]              # 区间形式
        else:
            for k in range(i, j + 1):
                out += [gids[k], [widths[gids[k]]]]
        i = j + 1
    return out


def _utf16be_hex(ch):
    """一个字符的 UTF-16BE 十六进制（BMP 内是 4 位）。"""
    return ch.encode("utf-16-be").hex().upper()


def _unicode_pref(u):
    """同一个 GID 对应多个码位时的取舍优先级（越小越优先）。

    ★ 字体里「康熙部首」「CJK 部首补充」「兼容表意文字」会和标准汉字
    **共用同一个字形** —— 例如 ⽂(U+2F82) 与 文(U+6587)、
    ⿊(U+2FCA) 与 黑(U+9ED1)。不做取舍的话，按码位升序会选中部首，
    提取出来就成了 `中⽂` 而不是 `中文`（踩过）。
    """
    if 0x20 <= u <= 0x7E:                                   # ASCII 可打印
        return 0
    if 0x4E00 <= u <= 0x9FFF:                               # CJK 基本区
        return 1
    if 0x3000 <= u <= 0x30FF or 0xFF00 <= u <= 0xFFEF:      # 标点/假名/全角
        return 1
    if 0x3400 <= u <= 0x4DBF or 0x20000 <= u <= 0x3FFFF:    # 扩展 A / B+
        return 2
    if 0xF900 <= u <= 0xFAFF:                               # 兼容表意文字
        return 8
    if 0x2E80 <= u <= 0x2FDF:                               # 部首（含康熙）
        return 9
    return 5


def to_unicode_cmap(gid_of):
    """生成 /ToUnicode CMap：GID -> Unicode。

    ★ 这张表是「能选中、能复制」的**唯一保证**。
    CID 字体里的码就是 GID，不含任何语义；而且不像标准 14 字体那样有
    内置编码可以兜底（WinAnsiEncoding 就是 cp1252，人人都有）——
    没有 ToUnicode，提取出来就是乱码。

    gid_of: {码位: GID}。同一个 GID 被多个码位指向时，按 _unicode_pref
    取「更像正常文字」的那个（否则会还原成部首）。
    """
    best = {}
    for u, g in gid_of.items():
        prev = best.get(g)
        if prev is None or (_unicode_pref(u), u) < (_unicode_pref(prev), prev):
            best[g] = u
    pairs = sorted(best.items())

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS)"
        " /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    # beginbfchar 每块最多 100 条（规范上限）
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        lines.append("%d beginbfchar" % len(chunk))
        for g, u in chunk:
            lines.append("<%04X> <%s>" % (g, _utf16be_hex(chr(u))))
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return "\n".join(lines).encode("ascii")


def embed_cid_font(doc, subset, ps_name="CJK-Subset", unicode_map=None):
    """把一个 pdffont 子集字体嵌进文档，返回 /Type0 字体对象的 Ref。

    完整链条（缺一项都会出问题）：
        /Type0                        ← 顶层字体，/Encoding /Identity-H
          ├ /DescendantFonts[0] = /CIDFontType2
          │     ├ /FontDescriptor
          │     │     └ /FontFile2 = 子集 TTF 字节流
          │     ├ /W             宽度数组（按 GID）
          │     └ /CIDToGIDMap /Identity  ← CID 就是 GID，最省事
          └ /ToUnicode            ← 码(GID) -> Unicode，可搜可复制靠它
    """
    upem = subset.upem or 1000
    scale = 1000.0 / upem

    def s(v):
        return int(round(v * scale))

    font_data = subset.data
    # FontFile2：/Length1 是**解压后**的长度。我们输出不压缩，所以两者相同。
    # 注意流对象要传 (字典, 数据) **元组**作为单个参数。
    fontfile = doc.new_obj((
        {"Length": len(font_data), "Length1": len(font_data)},
        font_data,
    ))

    bbox = [s(v) for v in (subset.bbox or (0, -200, 1000, 800))]
    ascent = s(subset.ascent) or 880
    descent = s(subset.descent) or -120

    descriptor = doc.new_obj({
        "Type": Name("FontDescriptor"),
        "FontName": Name(ps_name),
        # Flags=4 表示 Symbolic。Identity-H 的 CID 子集字体惯例如此。
        "Flags": 4,
        "FontBBox": bbox,
        "ItalicAngle": 0,
        "Ascent": ascent,
        "Descent": descent,
        "CapHeight": ascent,
        "StemV": 80,
        "FontFile2": fontfile,
    })

    cid_font = doc.new_obj({
        "Type": Name("Font"),
        "Subtype": Name("CIDFontType2"),
        "BaseFont": Name(ps_name),
        # 注意：这里是**字面量字符串**（规范要求），不是名字对象
        "CIDSystemInfo": {"Registry": "Adobe",
                          "Ordering": "Identity",
                          "Supplement": 0},
        "FontDescriptor": descriptor,
        "DW": 1000,
        "W": _width_array(subset.widths),
        "CIDToGIDMap": Name("Identity"),
    })

    cmap_data = to_unicode_cmap(
        unicode_map if unicode_map is not None else subset.gid_of)
    tounicode = doc.new_obj(({"Length": len(cmap_data)}, cmap_data))

    return doc.new_obj({
        "Type": Name("Font"),
        "Subtype": Name("Type0"),
        "BaseFont": Name(ps_name),
        "Encoding": Name("Identity-H"),
        "DescendantFonts": [cid_font],
        "ToUnicode": tounicode,
    })


def build_cjk_font(doc, chars, base_name="NotoSansSC"):
    """按需子集化中文字体并嵌入，返回 (Ref, SubsetFont)。

    ★ **全篇只做一次**：先汇总所有文字用到的字符，再子集化。
    每页各做一次会产出多个字体对象、白白增大体积。

    chars: 用到的码位集合。
    """
    font = pdffont.load_default()
    subset = font.subset(chars)

    # 子集命名惯例：6 位大写标签 + 字体名。标签由字符集决定，
    # 所以同样内容产出同样的名字，便于比对。
    tag = hashlib.sha1(
        (",".join(str(c) for c in sorted(chars))).encode("ascii")
    ).hexdigest()[:6].upper()
    ps_name = "%s+%s-Subset" % (tag, base_name)

    # ★ ToUnicode 只按**实际用到的字符**建，不用字体里的全部别名。
    # 字体里部首/兼容字会和标准汉字共用同一个字形（⽂ 与 文），
    # 不限定范围就会还原成部首 —— 实测踩过：`中文` 变 `中⽂`。
    used = {}
    for c in chars:
        g = subset.gid_of.get(c)
        if g is not None:
            used[c] = g
    ref = embed_cid_font(doc, subset, ps_name=ps_name, unicode_map=used)
    return ref, subset


def apply_edits(src_path, out_path, edits):
    """
    应用全部编辑，产出新 PDF。

    edits 结构：
      {
        "pages": {
            "order":  [2, 0, 1],         # 可选，重排
            "delete": [3],               # 可选，删除
            "rotate": {0: 90},           # 可选，页 -> 增量角度
            "insert": [{"at": 1, "width": 595, "height": 842}]
        },
        "objects": [
          {"page": 1, "kind": "whiteout", "x":.., "y":.., "w":.., "h":..},
          {"page": 1, "kind": "text", "x":.., "y":.., "text":.., "size":.., ...},
          ...
        ]
      }

    坐标约定：objects 里的 x/y/w/h 都是 **PDF 坐标**（原点左下、y 向上），
    前端负责翻转。这样服务端不用猜页面的 MediaBox 偏移。
    """
    with open(src_path, "rb") as fp:
        data = fp.read()

    doc = PDFDocument.load(data)
    if not doc.pages_ref:
        raise ValueError("无法定位页面树，文件可能不是标准 PDF")

    pages_cfg = edits.get("pages") or {}

    # 页面操作要按「先删除 -> 再重排 -> 再旋转」的顺序，
    # 因为下标是相对当时的状态而言的，前端提交的也应当是这个语义。

    # a) 删除
    dels = pages_cfg.get("delete") or []
    if dels:
        delete_pages(doc, [int(i) for i in dels])
        # 删除后，后续操作的「页序号」需要重映射
        refs_after = doc.page_refs()
        old_refs = []
        doc2_pages = doc.resolve(doc.pages_ref)  # noqa: F841
        # 重新按「原来的下标」算映射表
        # 注意：这里需要的是「删除前的顺序」，故重建一次
        # （delete_pages 已经把 Kids 改掉了，所以用重映射表）
        remap = {}
        kept = 0
        original_order = _original_indices(len(doc.page_refs()) + len(dels), dels)
        for old_i in original_order:
            remap[old_i] = kept
            kept += 1
    else:
        remap = None
        refs_after = doc.page_refs()

    # b) 重排
    order = pages_cfg.get("order")
    if order:
        order = [int(i) for i in order]
        if remap:
            order = [remap.get(i, i) for i in order]
        reorder_pages(doc, order)

    # c) 旋转
    rots = pages_cfg.get("rotate") or {}
    if rots:
        for k, v in rots.items():
            idx = int(k)
            if remap:
                idx = remap.get(idx, idx)
            try:
                rotate_page(doc, idx, int(v))
            except (TypeError, ValueError):
                continue

    # d) 插入空白页（放最后，避免影响前面的下标语义）
    for spec in pages_cfg.get("insert") or []:
        try:
            at = int(spec.get("at", 0))
            w = _num(spec.get("width"), 595)
            h = _num(spec.get("height"), 842)
            insert_blank_page(doc, at, w, h)
        except (TypeError, ValueError):
            continue

    # e) 编辑对象 -> 每页一个内容流
    refs = doc.page_refs()
    per_page = {}
    for obj in edits.get("objects") or []:
        try:
            pno = int(obj.get("page", 1))
        except (TypeError, ValueError):
            continue
        if not (1 <= pno <= len(refs)):
            continue
        per_page.setdefault(pno - 1, []).append(obj)

    # e0) 中文字体：先汇总**全篇**要写的字符，再子集化一次。
    #     每页各做一次会产出多个字体对象、白白增大体积。
    all_text = "".join(
        str(o.get("text") or "")
        for o in (edits.get("objects") or [])
        if (o.get("kind") or "").lower() == "text"
    )
    cjk_chars = {ord(ch) for ch in all_text}
    cjk_ref = None
    cjk_subset = None
    if has_non_latin(all_text):
        cjk_ref, cjk_subset = build_cjk_font(doc, cjk_chars)

    extra_streams = {}
    missing_cjk = set()
    for pidx, objs in per_page.items():
        builder = ContentBuilder()
        if cjk_subset is not None:
            builder.set_cid_font(cjk_ref, cjk_subset)
        for obj in objs:
            _emit_object(builder, obj)
        content = builder.to_bytes()
        if not content.strip():
            continue
        pref = refs[pidx]
        extra_streams[tuple(pref)] = content
        # 注册字体资源：往该页 Resources 里塞 /Font
        fr = builder.font_resources()
        if fr:
            _attach_font_resource(doc, refs[pidx], fr)
        missing_cjk |= builder.missing_chars

    size = build_pdf(doc, extra_streams, out_path)
    return {
        "ok": True,
        "size": size,
        "pages": len(doc.page_refs()),
        "objects": sum(len(v) for v in per_page.values()),
        # 中文字体统计。**缺字必须上报** —— 静默丢字是最糟的结果：
        # 用户以为写进去了，实际没有。
        "cjk_glyphs": cjk_subset.num_glyphs if cjk_subset else 0,
        "cjk_chars": len(cjk_chars) if cjk_subset else 0,
        "cjk_missing": "".join(sorted(missing_cjk)),
    }


def _original_indices(total, deleted):
    """算出删除后保留下来的「原始下标」序列。"""
    return [i for i in range(total) if i not in set(deleted)]


def _emit_object(b, obj):
    """把单个编辑对象翻译成绘图指令。"""
    kind = (obj.get("kind") or "").lower()
    x = _num(obj.get("x"))
    y = _num(obj.get("y"))
    w = _num(obj.get("w"))
    h = _num(obj.get("h"))

    if kind == "whiteout":
        # 涂白：就是画一个填充矩形，颜色默认纯白
        b.rect(x, y, w, h, _to_rgb(obj.get("color"), (1.0, 1.0, 1.0)),
               fill=True, stroke=False)

    elif kind == "rect":
        b.rect(x, y, w, h, _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               fill=bool(obj.get("fill")),
               stroke=bool(obj.get("stroke", True)),
               line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "ellipse":
        b.ellipse(x, y, w, h, _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
                  fill=bool(obj.get("fill")),
                  stroke=bool(obj.get("stroke", True)),
                  line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "line":
        b.line(x, y, _num(obj.get("x2")), _num(obj.get("y2")),
               _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "text":
        size = _num(obj.get("size"), 12.0)
        # PDF 的 Td 定位是文字基线，前端给的是框顶，需要下移一个字高
        b.text(x, y, str(obj.get("text") or ""), size,
               _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               base_font=obj.get("font") or "Helvetica")


def _attach_font_resource(doc, page_ref, font_map):
    """
    把 /Font 资源挂到页面上。

    font_map: {别名: 字体资源值}。
    值是**普通 dict**（标准 14 字体）或**一个 Ref**（嵌入的 CID 字体）。

    资源可能是页面的直接属性，也可能从父节点继承 ——
    继承时必须在本页新建一份，否则会污染整棵树。
    """
    page = doc.resolve(page_ref)
    if not isinstance(page, dict):
        return
    res = page.get("Resources")
    if isinstance(res, Ref):
        res = doc.resolve(res)
    if not isinstance(res, dict):
        res = {}
        page["Resources"] = res

    fonts = res.get("Font")
    if isinstance(fonts, Ref):
        fonts = doc.resolve(fonts)
    if not isinstance(fonts, dict):
        fonts = {}
        res["Font"] = fonts

    for alias, val in (font_map or {}).items():
        if alias in fonts:
            continue
        fonts[alias] = val
