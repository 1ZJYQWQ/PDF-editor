
import re

from pdfedit import (
    PDFDocument, Ref, Name, tokenize, _is_number, _to_number,
    _escape_literal, STANDARD_WIDTHS,
)



def mat_mul(m, n):
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
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)




class GlyphBox:

    __slots__ = ("x0", "y0", "x1", "y1", "op_index", "char_index", "code",
                 "font_alias", "font_size")

    def __init__(self, x0, y0, x1, y1, op_index=-1, char_index=0, code=-1,
                 font_alias="", font_size=12.0):
        self.x0 = min(x0, x1)
        self.x1 = max(x0, x1)
        self.y0 = min(y0, y1)
        self.y1 = max(y0, y1)
        self.op_index = op_index
        self.char_index = char_index
        self.code = code
        self.font_alias = font_alias
        self.font_size = font_size

    def intersects(self, rect):
        rx0, ry0, rx1, ry1 = rect
        return not (self.x1 <= rx0 or self.x0 >= rx1 or
                    self.y1 <= ry0 or self.y0 >= ry1)

    def __repr__(self):
        return (f"GlyphBox({self.x0:.1f},{self.y0:.1f} - "
                f"{self.x1:.1f},{self.y1:.1f})")




class FontInfo:

    def __init__(self, doc, font_dict):
        self.missing_width = 500.0     
        self.widths = {}
        self.base_font = ""
        self.is_cid = False
        self.code_bytes = 1            
        self.to_unicode = {}           
        self._resolve(doc, font_dict)

    def _resolve(self, doc, fd):
        if not isinstance(fd, dict):
            return

        base = fd.get("BaseFont")
        if isinstance(base, Name):
            self.base_font = str(base)

        subtype = fd.get("Subtype")
        subtype = str(subtype) if isinstance(subtype, Name) else ""
        self.is_cid = (subtype == "Type0")

        if self.is_cid:
            self.code_bytes = 2

        self._resolve_tounicode(doc, fd)

        dw = fd.get("DW")
        if isinstance(dw, (int, float)):
            self.missing_width = float(dw)

        widths = doc.resolve(fd.get("Widths"))
        first = fd.get("FirstChar")
        if isinstance(widths, list) and isinstance(first, (int, float)):
            base_code = int(first)
            for i, w in enumerate(widths):
                w = doc.resolve(w)
                if isinstance(w, (int, float)):
                    self.widths[base_code + i] = float(w)

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
            else:                      
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
        out = {}

        def dst_text(hexstr):
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
        if code in self.widths:
            return self.widths[code]
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




class ContentStreamInterpreter:

    def __init__(self, doc, resources):
        self.doc = doc
        self.resources = resources if isinstance(resources, dict) else {}
        self.fonts = {}              
        self.glyphs = []             
        self.ops = []                


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


    def run(self, data):
        tokens = tokenize(data)
        operand_stack = []
        gs = _GraphState()

        i = 0
        n = len(tokens)
        op_index = 0

        while i < n:
            tok = tokens[i]

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

            op = tok.decode("latin-1")
            self._exec(gs, op, operand_stack, op_index)
            self.ops.append(op)
            operand_stack.clear()
            op_index += 1
            i += 1

        return self.ops

    def _exec(self, gs, op, stack, op_index):
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
                if op == '"' and len(stack) >= 3:
                    aw, ac = _pop_nums(stack, 2)
                    gs.word_space, gs.char_space = aw, ac
                gs.tlm = mat_mul((1, 0, 0, 1, 0, -gs.leading), gs.tlm)
                gs.tm = tuple(gs.tlm)

            operand = stack[-1] if stack else b"()"
            self._show_text(gs, operand, op_index)
            return

    def _show_text(self, gs, operand, op_index):
        info = self.font(gs.font_alias) if gs.font_alias else FontInfo(
            self.doc, None)

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

        size = gs.font_size or 12.0
        hscale = gs.h_scale if gs.h_scale else 1.0
        trm = mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm)
        full = mat_mul(trm, gs.ctm)

        char_index = 0
        for code, is_text in parts:
            if not is_text:
                tx = -code / 1000.0 * size * hscale
                gs.tm = mat_mul((1, 0, 0, 1, tx, 0), gs.tm)
                full = mat_mul(
                    mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm),
                    gs.ctm)
                continue

            w1000 = info.width_of(code)      

            gx0, gy0 = 0.0, 0.0
            gx1, gy1 = w1000 / 1000.0, 1.0

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
                code, gs.font_alias or "", gs.font_size or 12.0))
            char_index += 1

            adv = (w1000 / 1000.0 * size) + gs.char_space
            if gs.word_space and code == 32:
                adv += gs.word_space
            gs.tm = mat_mul((1, 0, 0, 1, adv * hscale, 0), gs.tm)
            full = mat_mul(
                mat_mul((size * hscale, 0, 0, size, 0, gs.rise), gs.tm),
                gs.ctm)


class _GraphState:

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




def _read_array(tokens, i):
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
    raw = _string_bytes(tok)
    if not raw:
        return []
    if nbytes != 2:
        return list(raw)
    if len(raw) % 2:
        raw += b"\x00"
    return [int.from_bytes(raw[i:i + 2], "big")
            for i in range(0, len(raw), 2)]


def _codes_to_bytes(codes, nbytes=1):
    if nbytes == 2:
        return b"".join((int(c) & 0xFFFF).to_bytes(2, "big") for c in codes)
    return bytes(int(c) & 0xFF for c in codes)


def _decode_literal(raw):
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




def read_page_content(doc, page_ref):
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
    if not rects:
        return None

    data = read_page_content(doc, page_ref)
    resources = doc.resolve(doc.inherited(page_ref, "Resources"))

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

    killed = set()
    for g in glyphs:
        for r in rects:
            if g.intersects(r):
                killed.add((g.op_index, g.char_index))
                break

    out = []
    if data.strip():
        out.append(_rewrite_text(data, killed, font_bytes))
    else:
        out.append(b"")

    black = [b"\nq"]
    black.append(b"0 0 0 rg")
    for (x0, y0, x1, y1) in rects:
        black.append(
            b"%.3f %.3f %.3f %.3f re f" % (x0, y0, x1 - x0, y1 - y0))
    black.append(b"Q")
    out.append(b"\n".join(black))

    return b"\n".join(out)


def _rewrite_text(data, killed, font_bytes=None):
    tokens = tokenize(data)
    out = bytearray()
    operand_start = None      
    i = 0
    n = len(tokens)
    op_index = 0
    cur_nb = 1                

    while i < n:
        tok = tokens[i]

        if tok == b"[":
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
            has_kill = any(k[0] == op_index for k in killed)
            if not has_kill:
                _emit_range(out, tokens, ops_start, i + 1)
            else:
                rebuilt = _rebuild_text_op(
                    tokens, ops_start, i, op_index, killed, cur_nb)
                if rebuilt is not None:
                    out += rebuilt + b" "
            operand_start = None
            op_index += 1
            i += 1
            continue

        if op == "Tf":
            cur_nb = _font_bytes_of(tokens, ops_start, i, font_bytes)
        _emit_range(out, tokens, ops_start, i + 1)
        operand_start = None
        op_index += 1
        i += 1

    if operand_start is not None:
        _emit_range(out, tokens, operand_start, n)

    return bytes(out)


def _font_bytes_of(tokens, i0, i1, font_bytes):
    if not font_bytes:
        return 1
    for k in range(i0, i1):
        t = tokens[k]
        if isinstance(t, bytes) and t.startswith(b"/"):
            return font_bytes.get(t[1:].decode("latin-1"), 1)
    return 1


def _emit_range(out, tokens, i0, i1):
    for t in tokens[i0:i1]:
        out += t if isinstance(t, bytes) else str(t).encode("latin-1")
        out += b" "


def _rebuild_text_op(tokens, ops_start, op_i, op_index, killed, nbytes=1):
    op = tokens[op_i].decode("latin-1")

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
        return None                 

    prefix = b" ".join(
        t if isinstance(t, bytes) else str(t).encode("latin-1")
        for t in tokens[ops_start:str_pos])
    head = prefix + (b" " if prefix else b"")
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

    cleaned = []
    for item in out:
        if isinstance(item, list):
            if item:
                cleaned.append(item)
        else:
            cleaned.append(item)
    if not cleaned:
        return [], False
    if all(isinstance(x, (int, float)) for x in cleaned):
        return [], False
    return cleaned, True


def _serialize_tj_array(items, nbytes=1):
    parts = []
    for item in items:
        if isinstance(item, list):
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
    printable = sum(1 for c in raw if 32 <= c < 127)
    if raw and printable / len(raw) < 0.6:
        return b"<" + raw.hex().encode("ascii") + b">"
    esc = bytearray()
    for c in raw:
        if c in (0x28, 0x29, 0x5C):        
            esc.append(0x5C)
            esc.append(c)
        elif c < 32 or c > 126:
            esc.append(0x5C)
            esc += b"%03o" % c
        else:
            esc.append(c)
    return b"(" + bytes(esc) + b")"




def clean_metadata(doc):
    removed = []
    trailer = doc.trailer if isinstance(doc.trailer, dict) else {}

    info_ref = trailer.get("Info")
    if isinstance(info_ref, Ref):
        info = doc.objects.get(tuple(info_ref))
        if isinstance(info, dict):
            for key in list(info.keys()):
                info.pop(key, None)
            info["Producer"] = "PDF Viewer (redacted)"
            removed.append("Info")

    cat = doc.resolve(doc.root_ref) if getattr(doc, "root_ref", None) else None
    if isinstance(cat, dict):
        if "Metadata" in cat:
            cat.pop("Metadata", None)
            removed.append("XMP Metadata")

    return removed


def clean_outline_titles(doc, killed_texts):
    changed = 0
    if not killed_texts:
        return changed

    for key, val in list(doc.objects.items()):
        if not isinstance(val, dict):
            continue
        title = val.get("Title")
        if not isinstance(title, (bytes, str)):
            continue
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
    return re.sub(r"\s+", " ", (s or "").strip()).lower()




def apply_redactions(src_path, out_path, redactions, clean_meta=True):
    from pdfedit import build_pdf

    with open(src_path, "rb") as fp:
        data = fp.read()

    doc = PDFDocument.load(data)
    if not doc.pages_ref:
        raise ValueError("无法定位页面树，文件可能不是标准 PDF")

    refs = doc.page_refs()

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

    total_glyphs = 0
    stats = []
    orphans = set()          
    killed_texts = set()     
    for pidx, rects in per_page.items():
        pref = refs[pidx]

        content = read_page_content(doc, pref)
        n_killed = 0
        if content.strip():
            resources = doc.resolve(doc.inherited(pref, "Resources"))
            interp = ContentStreamInterpreter(doc, resources)
            interp.run(content)
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
    n_outline = clean_outline_titles(doc, killed_texts) if clean_meta else 0

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


def apply_text_replace(src_path, out_path, replacements, clean_meta=True):
    from pdfedit import (ContentBuilder, build_cjk_font, build_pdf,
                         has_non_latin, _attach_font_resource)

    with open(src_path, "rb") as fp:
        data = fp.read()
    doc = PDFDocument.load(data)
    if not doc.pages_ref:
        raise ValueError("无法定位页面树，文件可能不是标准 PDF")
    refs = doc.page_refs()

    per_page = {}
    for r in replacements:
        try:
            pno = int(r.get("page", 1))
            x = float(r.get("x", 0)); y = float(r.get("y", 0))
            w = float(r.get("w", 0)); h = float(r.get("h", 0))
            txt = str(r.get("text") or "")
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or not txt:
            continue
        if not (1 <= pno <= len(refs)):
            continue
        per_page.setdefault(pno - 1, []).append(
            ((x, y, x + w, y + h), txt))

    if not per_page:
        raise ValueError("没有有效的替换区域")

    stats = []
    total_replaced = 0
    for pidx, items in per_page.items():
        pref = refs[pidx]
        content = read_page_content(doc, pref)
        resources = doc.resolve(doc.inherited(pref, "Resources"))

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

        glyphs = []
        if content.strip():
            interp = ContentStreamInterpreter(doc, resources)
            interp.run(content)
            glyphs = interp.glyphs

        all_killed = set()
        anchors = []          
        for rect, txt in items:
            hit = [g for g in glyphs if g.intersects(rect)]
            if not hit:
                raise ValueError("第 %d 页的替换区域没有框到任何文字"
                                 % (pidx + 1))
            all_killed |= {(g.op_index, g.char_index) for g in hit}
            anchor = min(hit, key=lambda g: (g.op_index, g.char_index))
            anchors.append((anchor, txt))

        del_content = _rewrite_text(content, all_killed, font_bytes)

        all_text = "".join(txt for _, txt in anchors)
        cjk_ref = cjk_subset = None
        if has_non_latin(all_text):
            cjk_ref, cjk_subset = build_cjk_font(
                doc, {ord(c) for c in all_text})

        new_builder = ContentBuilder()
        if cjk_subset is not None:
            new_builder.set_cid_font(cjk_ref, cjk_subset)
        for anchor, txt in anchors:
            anchor_baseline = anchor.y0 + 0.2 * (anchor.font_size or 12.0)
            new_builder.text(anchor.x0, anchor_baseline, txt,
                            anchor.font_size or 12.0, (0, 0, 0))
        new_stream_bytes = new_builder.to_bytes()

        old_streams = _collect_content_refs(doc, pref)
        page = doc.resolve(pref)
        new_refs = [
            doc.new_obj(({"Length": len(del_content)}, del_content)),
            doc.new_obj(({"Length": len(new_stream_bytes)}, new_stream_bytes)),
        ]
        if isinstance(page, dict):
            page["Contents"] = new_refs
        fr = new_builder.font_resources()
        if fr:
            _attach_font_resource(doc, pref, fr)
        _drop_objects(doc, old_streams)

        replaced = len(all_killed)
        total_replaced += replaced
        stats.append({"page": pidx + 1, "replaced_glyphs": replaced,
                      "items": len(anchors)})

    size = build_pdf(doc, {}, out_path)
    return {"ok": True, "size": size, "pages": len(refs),
            "pages_replaced": len(stats),
            "replaced_glyphs": total_replaced, "details": stats}


def _collect_content_refs(doc, page_ref):
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
    dropped = 0
    for key in refs:
        if key in doc.objects:
            del doc.objects[key]
            dropped += 1
    return dropped
