
import time

from pdfedit import Name, Ref

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
        if byte in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([byte])
        else:
            out += bytes([byte])
    out += b")"
    return bytes(out)

def _r(v):
    return round(float(v), 2)

def _annot_dict(sub, rects, color, note, stamp):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)

    d = {
        "Type": Name("Annot"),
        "Subtype": Name(sub),
        "Rect": [_r(x0), _r(y0), _r(x1), _r(y1)],
        "F": 4,
        "C": [round(c, 3) for c in _rgb(color)],
        "T": _pdf_text(AUTHOR),
        "M": stamp,
    }
    if note:
        d["Contents"] = _pdf_text(note)

    if sub in TEXT_TYPES:
        qp = []
        for (rx0, ry0, rx1, ry1) in rects:
            qp += [_r(rx0), _r(ry1), _r(rx1), _r(ry1),
                   _r(rx0), _r(ry0), _r(rx1), _r(ry0)]
        d["QuadPoints"] = qp
        if sub == "Highlight":
            d["CA"] = 0.45

    elif sub == "Square":
        d["IC"] = [round(c, 3) for c in _rgb(color)]
        d["BS"] = {"W": 1, "S": Name("S")}

    elif sub == "Text":
        size = 18.0
        d["Name"] = Name("Comment")
        d["Rect"] = [_r(x0), _r(max(y0, y1 - size)), _r(x0 + size), _r(max(y0, y1 - size) + size)]
        d["Contents"] = _pdf_text(note or "笔记")

    return d

def apply_annotations(doc, page_refs, items):
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
            annots = list(annots)
        for d in dicts:
            annots.append(doc.new_obj(d))
            written += 1
        page["Annots"] = annots

    return {"written": written, "pages": len(per_page), "skipped": skipped}
