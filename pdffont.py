import os
import struct

ARG_1_AND_2_ARE_WORDS = 0x0001
WE_HAVE_A_SCALE = 0x0008
MORE_COMPONENTS = 0x0020
WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
WE_HAVE_A_TWO_BY_TWO = 0x0080
WE_HAVE_INSTRUCTIONS = 0x0100

KEEP_TABLES = ("head", "hhea", "maxp", "hmtx", "loca", "glyf", "OS/2", "post")

DROP_HINTING = ("cvt ", "fpgm", "prep", "gasp")
DROP_LAYOUT = ("GDEF", "GPOS", "GSUB", "BASE")
DROP_VAR = ("fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR", "STAT")
DROP_EXTRA = ("name", "cmap", "vhea", "vmtx", "meta", "kern")

def _read_table_dir(data, base=0):
    if len(data) < base + 12:
        raise ValueError("数据太短，不是 sfnt")
    num = struct.unpack(">H", data[base + 4:base + 6])[0]
    tables = {}
    for i in range(num):
        off = base + 12 + i * 16
        if off + 16 > len(data):
            break
        tag, _cks, o, ln = struct.unpack(">4sIII", data[off:off + 16])
        tables[tag.decode("latin-1")] = (o, ln)
    return tables

def _cmap_format4(data, off):
    out = {}
    seg_x2 = struct.unpack(">H", data[off + 6:off + 8])[0]
    seg = seg_x2 // 2
    if seg <= 0:
        return out
    end_base = off + 14
    start_base = end_base + seg * 2 + 2
    delta_base = start_base + seg * 2
    range_base = delta_base + seg * 2

    for i in range(seg):
        end = struct.unpack(">H", data[end_base + i * 2:
                                       end_base + i * 2 + 2])[0]
        start = struct.unpack(">H", data[start_base + i * 2:
                                         start_base + i * 2 + 2])[0]
        delta = struct.unpack(">h", data[delta_base + i * 2:
                                         delta_base + i * 2 + 2])[0]
        ro = struct.unpack(">H", data[range_base + i * 2:
                                      range_base + i * 2 + 2])[0]
        if start > end:
            continue
        for c in range(start, end + 1):
            if ro == 0:
                gid = (c + delta) & 0xFFFF
            else:
                pos = range_base + i * 2 + ro + (c - start) * 2
                if pos + 2 > len(data):
                    continue
                gid = struct.unpack(">H", data[pos:pos + 2])[0]
                if gid:
                    gid = (gid + delta) & 0xFFFF
            if gid:
                out[c] = gid
    return out

def _cmap_format12(data, off):
    out = {}
    ngroups = struct.unpack(">I", data[off + 12:off + 16])[0]
    p = off + 16
    for _ in range(ngroups):
        if p + 12 > len(data):
            break
        s, e, g = struct.unpack(">III", data[p:p + 12])
        p += 12
        if e < s or e - s > 0x10FFFF:
            continue
        for c in range(s, e + 1):
            out[c] = g + (c - s)
    return out

def parse_cmap(data, tables):
    if "cmap" not in tables:
        return {}
    off = tables["cmap"][0]
    if off + 4 > len(data):
        return {}
    n = struct.unpack(">H", data[off + 2:off + 4])[0]
    out = {}
    for i in range(n):
        p = off + 4 + i * 8
        if p + 8 > len(data):
            break
        _pid, _eid, sub_off = struct.unpack(">HHI", data[p:p + 8])
        so = off + sub_off
        if so + 2 > len(data):
            continue
        fmt = struct.unpack(">H", data[so:so + 2])[0]
        try:
            if fmt == 12:
                out.update(_cmap_format12(data, so))
            elif fmt == 4:
                for c, g in _cmap_format4(data, so).items():
                    out.setdefault(c, g)
        except (struct.error, IndexError):
            continue
    return out

def composite_gids(gd):
    if len(gd) < 10:
        return []
    nc = struct.unpack(">h", gd[:2])[0]
    if nc >= 0:
        return []
    out = []
    p = 10
    while p + 4 <= len(gd):
        flags, gi = struct.unpack(">HH", gd[p:p + 4])
        out.append(gi)
        p += 4
        p += 4 if (flags & ARG_1_AND_2_ARE_WORDS) else 2
        if flags & WE_HAVE_A_SCALE:
            p += 2
        elif flags & WE_HAVE_AN_X_AND_Y_SCALE:
            p += 4
        elif flags & WE_HAVE_A_TWO_BY_TWO:
            p += 8
        if not (flags & MORE_COMPONENTS):
            break
    return out

def subset_glyph(gd, gid_map):
    if len(gd) < 10:
        return gd
    nc = struct.unpack(">h", gd[:2])[0]

    if nc >= 0:
        end = 10 + nc * 2
        if end + 2 > len(gd):
            return gd
        ilen = struct.unpack(">H", gd[end:end + 2])[0]
        if end + 2 + ilen > len(gd):
            return gd
        return gd[:end] + b"\x00\x00" + gd[end + 2 + ilen:]

    comps = []
    p = 10
    while p + 4 <= len(gd):
        start = p
        flags, gi = struct.unpack(">HH", gd[p:p + 4])
        p += 4
        p += 4 if (flags & ARG_1_AND_2_ARE_WORDS) else 2
        if flags & WE_HAVE_A_SCALE:
            p += 2
        elif flags & WE_HAVE_AN_X_AND_Y_SCALE:
            p += 4
        elif flags & WE_HAVE_A_TWO_BY_TWO:
            p += 8
        comps.append((start, flags, gi))
        if not (flags & MORE_COMPONENTS):
            break
    if not comps:
        return gd

    body = bytearray(gd[:p])
    for start, _flags, gi in comps:
        body[start + 2:start + 4] = struct.pack(">H", gid_map.get(gi, 0))

    tail = gd[p:]
    last_start, last_flags, _ = comps[-1]
    if last_flags & WE_HAVE_INSTRUCTIONS:
        body[last_start:last_start + 2] = struct.pack(
            ">H", last_flags & ~WE_HAVE_INSTRUCTIONS)
        if len(tail) >= 2:
            ilen = struct.unpack(">H", tail[:2])[0]
            tail = tail[2 + ilen:]
    return bytes(body) + tail

def _checksum(data):
    if len(data) % 4:
        data = data + b"\x00" * (4 - len(data) % 4)
    s = 0
    for i in range(0, len(data), 4):
        s = (s + struct.unpack(">I", data[i:i + 4])[0]) & 0xFFFFFFFF
    return s

def build_sfnt(tables, sfnt_version=b"\x00\x01\x00\x00"):
    tags = sorted(tables)
    n = len(tags)
    entry_selector = max(0, n.bit_length() - 1)
    search_range = (1 << entry_selector) * 16
    range_shift = n * 16 - search_range

    header = struct.pack(">4sHHHH", sfnt_version, n, search_range,
                         entry_selector, range_shift)
    rec_size = 16 * n
    offset = len(header) + rec_size

    records = []
    blobs = []
    head_offset = None
    for tag in tags:
        body = tables[tag]
        pad = (-len(body)) % 4
        records.append([tag, _checksum(body), offset, len(body)])
        if tag == "head":
            head_offset = offset
        blobs.append(body + b"\x00" * pad)
        offset += len(body) + pad

    rec_bytes = b"".join(
        struct.pack(">4sIII", t.encode("latin-1"), cks, off, ln)
        for t, cks, off, ln in records)

    out = bytearray(header + rec_bytes + b"".join(blobs))

    if head_offset is not None:
        adj = (0xB1B0AFBA - _checksum(bytes(out))) & 0xFFFFFFFF
        struct.pack_into(">I", out, head_offset + 8, adj)
    return bytes(out)

class SubsetFont:

    __slots__ = ("data", "gid_of", "old_to_new", "upem", "widths", "bbox",
                 "ascent", "descent", "num_glyphs", "missing", "ps_name")

    def __init__(self):
        self.data = b""
        self.gid_of = {}
        self.old_to_new = {}
        self.widths = {}
        self.upem = 1000
        self.bbox = (0, 0, 0, 0)
        self.ascent = 0
        self.descent = 0
        self.num_glyphs = 0
        self.missing = []
        self.ps_name = ""

    def encode(self, text):
        out = bytearray()
        miss = []
        for ch in text:
            g = self.gid_of.get(ord(ch))
            if g is None:
                miss.append(ch)
                continue
            out += struct.pack(">H", g)
        return bytes(out), miss

class TrueTypeFont:

    def __init__(self, data):
        if data[:4] == b"ttcf":
            raise ValueError("这是 TTC 字体集合，请先提取单个 face")
        self.data = data
        self.tables = _read_table_dir(data, 0)
        if "glyf" not in self.tables:
            raise ValueError("没有 glyf 表 —— 不是 TrueType 轮廓，"
                             "CFF/CFF2 本项目不支持")
        self.upem = self._head_u16(18) or 1000
        self.index_to_loc = self._head_u16(50)
        self.num_glyphs = struct.unpack(
            ">H", self._table_slice("maxp", 4, 2))[0]
        self.num_hmetrics = struct.unpack(
            ">H", self._table_slice("hhea", 34, 2))[0]
        self._loca = self._read_loca()
        self.ps_name = _postscript_name(data, self.tables)

    def _head_u16(self, off):
        try:
            return struct.unpack(">H", self._table_slice("head", off, 2))[0]
        except (struct.error, KeyError):
            return 0

    def _table_slice(self, tag, off, ln):
        base = self.tables[tag][0]
        return self.data[base + off: base + off + ln]

    def _read_loca(self):
        off, _ln = self.tables["loca"]
        n = self.num_glyphs + 1
        if self.index_to_loc == 0:
            raw = self.data[off:off + 2 * n]
            if len(raw) < 2 * n:
                return [0] * n
            return [2 * v for v in struct.unpack(">%dH" % n, raw)]
        raw = self.data[off:off + 4 * n]
        if len(raw) < 4 * n:
            return [0] * n
        return list(struct.unpack(">%dI" % n, raw))

    def glyph(self, gid):
        if gid < 0 or gid + 1 >= len(self._loca):
            return b""
        a, b = self._loca[gid], self._loca[gid + 1]
        if b <= a:
            return b""
        goff = self.tables["glyf"][0]
        return self.data[goff + a: goff + b]

    def advance(self, gid):
        off = self.tables["hmtx"][0]
        if gid < self.num_hmetrics:
            raw = self.data[off + gid * 4: off + gid * 4 + 2]
        else:
            raw = self.data[off + (self.num_hmetrics - 1) * 4:
                            off + (self.num_hmetrics - 1) * 4 + 2]
        if len(raw) < 2:
            return self.upem
        return struct.unpack(">H", raw)[0]

    def lsb(self, gid):
        off = self.tables["hmtx"][0]
        if gid < self.num_hmetrics:
            raw = self.data[off + gid * 4 + 2: off + gid * 4 + 4]
        else:
            raw = self.data[off + (self.num_hmetrics - 1) * 4 + 2:
                            off + (self.num_hmetrics - 1) * 4 + 4]
        if len(raw) < 2:
            return 0
        return struct.unpack(">h", raw)[0]

    def cmap_map(self):
        return parse_cmap(self.data, self.tables)

    def metrics(self):
        head = self.tables["head"][0]
        bbox = struct.unpack(">hhhh",
                             self.data[head + 36: head + 44])
        hhea = self.tables["hhea"][0]
        asc, desc = struct.unpack(">hh",
                                  self.data[hhea + 4: hhea + 8])
        return bbox, asc, desc

    def subset(self, unicodes, ps_name=None):
        cmap = self.cmap_map()
        want = {0}
        missing = []
        for u in sorted(set(unicodes)):
            g = cmap.get(u)
            if g is None:
                missing.append(u)
            else:
                want.add(g)

        stack = list(want)
        while stack:
            g = stack.pop()
            for c in composite_gids(self.glyph(g)):
                if c not in want:
                    want.add(c)
                    stack.append(c)

        old_gids = sorted(want)
        gid_map = {old: new for new, old in enumerate(old_gids)}

        glyf = bytearray()
        loca = [0]
        for old in old_gids:
            gd = subset_glyph(self.glyph(old), gid_map)
            glyf += gd
            loca.append(len(glyf))

        hmtx = bytearray()
        for old in old_gids:
            hmtx += struct.pack(">Hh", self.advance(old), self.lsb(old))

        n_new = len(old_gids)
        out_tables = {}

        head = bytearray(self._table_slice("head", 0, 54))
        struct.pack_into(">I", head, 8, 0)
        struct.pack_into(">h", head, 50, 1)
        out_tables["head"] = bytes(head)

        hhea = bytearray(self._table_slice("hhea", 0, 36))
        struct.pack_into(">H", hhea, 34, n_new)
        out_tables["hhea"] = bytes(hhea)

        maxp = bytearray(self._table_slice("maxp", 0, 32))
        struct.pack_into(">H", maxp, 4, n_new)
        out_tables["maxp"] = bytes(maxp)

        out_tables["hmtx"] = bytes(hmtx)
        out_tables["glyf"] = bytes(glyf)
        out_tables["loca"] = struct.pack(">%dI" % (n_new + 1), *loca)

        if "OS/2" in self.tables and self.tables["OS/2"][1] >= 78:
            out_tables["OS/2"] = self._table_slice(
                "OS/2", 0, min(self.tables["OS/2"][1], 96))

        out_tables["post"] = struct.pack(
            ">iiihhIIII", 0x00030000, 0, 0, 0, 0, 0, 0, 0, 0)

        res = SubsetFont()
        res.data = build_sfnt(out_tables)
        res.old_to_new = dict(gid_map)
        res.gid_of = {u: gid_map[cmap[u]] for u in cmap
                      if u in cmap and cmap[u] in gid_map}
        res.upem = self.upem
        res.num_glyphs = n_new
        res.missing = missing
        res.ps_name = ps_name or self.ps_name or "Subset"

        bbox, asc, desc = self.metrics()
        res.bbox = bbox
        res.ascent = asc
        res.descent = desc
        scale = 1000.0 / (self.upem or 1000)
        for old in old_gids:
            res.widths[gid_map[old]] = int(round(self.advance(old) * scale))
        return res

def _postscript_name(data, tables):
    if "name" not in tables:
        return ""
    off = tables["name"][0]
    if off + 6 > len(data):
        return ""
    count, str_off = struct.unpack(">HH", data[off + 2:off + 6])
    base = off + str_off
    for i in range(count):
        p = off + 6 + i * 12
        if p + 12 > len(data):
            break
        pid, eid, _lid, nid, ln, of = struct.unpack(">HHHHHH",
                                                   data[p:p + 12])
        if nid != 6:
            continue
        raw = data[base + of: base + of + ln]
        try:
            if pid == 3 and eid == 1:
                return raw.decode("utf-16-be")
            return raw.decode("latin-1")
        except (UnicodeDecodeError, ValueError):
            return ""
    return ""

def load(path):
    with open(path, "rb") as f:
        return TrueTypeFont(f.read())

def load_default(path=None):
    if path is None:
        path = os.environ.get("PDFVIEWER_CJK_FONT")
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "assets", "fonts",
                            "NotoSansSC-Regular.ttf")
        if not os.path.exists(path):
            raise FileNotFoundError(
                "找不到中文字体资产：%s\n"
                "请先用 makefont.py 生成（看该脚本头部说明）：\n"
                "  <venv>/Scripts/python.exe makefont.py 400\n"
                "或用环境变量 PDFVIEWER_CJK_FONT 指定其他字体。" % path)
    if not os.path.exists(path):
        raise FileNotFoundError("找不到中文字体：%s" % path)
    return TrueTypeFont(open(path, "rb").read())
