# -*- coding: utf-8 -*-
"""
把前端批注写成 PDF **原生注释**（/Annots）。

为什么不把批注画进内容流：
  · 原生注释是 PDF 规范里的对象，Acrobat / Edge / 微信预览 / macOS 预览
    都能看见、能编辑、能打印；画进内容流就变成"死图"，永远改不了。
  · 文本类批注必须用 /QuadPoints（四边形点集）而不是只给 /Rect 包围盒，
    否则多行高亮、断行、斜排会糊成一整块。

坐标约定：items 里的矩形都是 **PDF 用户空间**（原点左下、y 向上），
用 (x0, y0, x1, y1) 且 x0<x1、y0<y1；屏幕坐标的翻转由前端负责。
"""

import time

from pdfedit import Name, Ref

# 我们的标注类型 -> PDF 注释子类型
SUBTYPES = {
    "highlight": "Highlight",
    "underline": "Underline",
    "wavy": "Squiggly",
    "strike": "StrikeOut",
    "box": "Square",
    "note": "Text",
}

TEXT_TYPES = ("Highlight", "Underline", "Squiggly", "StrikeOut")

AUTHOR = "PDF Viewer"


def _rgb(color):
    """'#rrggbb' -> [r, g, b]（0..1）。解析不了就给琥珀色兜底。"""
    fallback = [1.0, 0.75, 0.1]
    if not isinstance(color, str):
        return fallback
    s = color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return fallback
    try:
        return [int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except ValueError:
        return fallback


def _norm_rects(rects):
    """规范化矩形：保证 x0<x1、y0<y1，丢掉零面积。"""
    out = []
    for r in rects or []:
        try:
            x0, y0, x1, y1 = (float(v) for v in r)
        except (TypeError, ValueError):
            continue
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if x1 - x0 < 0.2 or y1 - y0 < 0.2:
            continue
        out.append((x0, y0, x1, y1))
    return out


def _pdf_text(s):
    """
    PDF 文本串编码。

    ★ 非 ASCII 必须写成 **UTF-16BE + BOM 的十六进制串**。
    直接按 latin-1 编码（pdfedit.serialize_value 对 str 的默认行为）
    会把中文全变成 '?' —— 实测 pdf.js 读回来就是 "?????"。
    """
    if not s:
        return b"()"
    s = str(s)
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        raw = b"\xfe\xff" + s.encode("utf-16-be")
        return b"<" + raw.hex().upper().encode("ascii") + b">"
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):        # ( ) \
            out += b"\\" + bytes([byte])
        else:
            out += bytes([byte])
    out += b")"
    return bytes(out)


def _r(v):
    return round(float(v), 2)


def _annot_dict(sub, rects, color, note, stamp):
    """造一个注释字典。rects 已规范化。"""
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)

    d = {
        "Type": Name("Annot"),
        "Subtype": Name(sub),
        "Rect": [_r(x0), _r(y0), _r(x1), _r(y1)],
        # /F 4 = Print。少了这一位，很多阅读器打印时会把注释直接丢掉，
        # 而且"看不出来"—— 属于最难排查的那类问题。
        "F": 4,
        "C": [round(c, 3) for c in _rgb(color)],
        "T": _pdf_text(AUTHOR),
        "M": stamp,
    }
    if note:
        d["Contents"] = _pdf_text(note)

    if sub in TEXT_TYPES:
        # 每个矩形给 4 个点：左上、右上、左下、右下（规范顺序不能乱）
        qp = []
        for (rx0, ry0, rx1, ry1) in rects:
            qp += [_r(rx0), _r(ry1), _r(rx1), _r(ry1),
                   _r(rx0), _r(ry0), _r(rx1), _r(ry0)]
        d["QuadPoints"] = qp
        if sub == "Highlight":
            # 一条高亮整体的不透明度；太浓会盖住文字
            d["CA"] = 0.45

    elif sub == "Square":
        # 区域框：内部色 + 1pt 实线边框
        d["IC"] = [round(c, 3) for c in _rgb(color)]
        d["BS"] = {"W": 1, "S": Name("S")}

    elif sub == "Text":
        # 便签：图标要固定大小，不能跟着文字框一起缩成一条缝
        size = 18.0
        d["Name"] = Name("Comment")
        d["Rect"] = [_r(x0), _r(max(y0, y1 - size)), _r(x0 + size), _r(max(y0, y1 - size) + size)]
        d["Contents"] = _pdf_text(note or "笔记")

    return d


def apply_annotations(doc, page_refs, items):
    """
    把批注写进页面 /Annots。

    items: [{"page": 1(1 起), "type": "highlight", "rects": [(x0,y0,x1,y1)...],
             "color": "#f59e0b", "note": "..."}]

    返回 {"written": n, "pages": k, "skipped": n2}
    """
    stamp = time.strftime("D:%Y%m%d%H%M%S+08'00'")
    per_page = {}
    skipped = 0

    for it in items or []:
        if not isinstance(it, dict):
            skipped += 1
            continue
        sub = SUBTYPES.get(str(it.get("type") or "").lower())
        if not sub:
            skipped += 1
            continue
        try:
            pno = int(it.get("page", 1))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (1 <= pno <= len(page_refs)):
            skipped += 1
            continue

        rects = _norm_rects(it.get("rects"))
        if not rects:
            skipped += 1
            continue

        d = _annot_dict(sub, rects, it.get("color"), it.get("note"), stamp)
        per_page.setdefault(pno - 1, []).append(d)

    written = 0
    for pidx, dicts in per_page.items():
        pref = page_refs[pidx]
        page = doc.resolve(pref)
        if not isinstance(page, dict):
            continue
        annots = page.get("Annots")
        if isinstance(annots, Ref):
            annots = doc.resolve(annots)
        if not isinstance(annots, list):
            annots = []
        else:
            annots = list(annots)          # 别改动原数组（可能被别的页共享）
        for d in dicts:
            annots.append(doc.new_obj(d))
            written += 1
        page["Annots"] = annots

    return {"written": written, "pages": len(per_page), "skipped": skipped}
