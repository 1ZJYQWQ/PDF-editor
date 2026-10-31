import statistics


def eff_size(g):
    return g.y1 - g.y0


def build_rows(glyphs, tol=2.0):
    rows = []
    for g in sorted(glyphs, key=lambda x: (-x.y0, x.x0)):
        for r in rows:
            if abs(r["y"] - g.y0) <= tol:
                r["g"].append(g)
                break
        else:
            rows.append({"y": g.y0, "g": [g]})
    for r in rows:
        r["g"].sort(key=lambda x: x.x0)
        r["x0"] = r["g"][0].x0
        r["x1"] = r["g"][-1].x1
        r["size"] = statistics.median(eff_size(g) for g in r["g"])
        r["fonts"] = sorted({g.font_alias for g in r["g"]})
    rows.sort(key=lambda r: -r["y"])
    return rows


def build_paragraphs(rows, indent_tol=3.0, gap_tol=0.25):
    if not rows:
        return []

    x0s = [round(r["x0"], 1) for r in rows]
    left = max(set(x0s), key=x0s.count)

    gaps = [rows[i]["y"] - rows[i + 1]["y"] for i in range(len(rows) - 1)]
    body_gap = statistics.median(gaps) if gaps else 0.0

    paras = []
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i else None
        new_para = True
        if prev is not None and paras:
            gap = prev["y"] - r["y"]
            indented = r["x0"] > left + indent_tol          
            same_gap = body_gap > 0 and abs(gap - body_gap) <= max(1.2, body_gap * gap_tol)
            same_x = abs(r["x0"] - left) <= indent_tol      
            same_size = abs(r["size"] - prev["size"]) <= 0.6
            if not indented and same_gap and (same_x or True) and same_size:
                new_para = False
        if new_para:
            paras.append({"rows": [r], "left": r["x0"], "right": r["x1"],
                          "size": r["size"], "fonts": list(r["fonts"])})
        else:
            p = paras[-1]
            p["rows"].append(r)
            p["left"] = min(p["left"], r["x0"])
            p["right"] = max(p["right"], r["x1"])
            p["fonts"] = sorted(set(p["fonts"]) | set(r["fonts"]))

    for p in paras:
        ys = [r["y"] for r in p["rows"]]
        p["top"] = max(ys)
        p["bottom"] = min(ys)
        p["gap"] = ((p["top"] - p["bottom"]) / (len(p["rows"]) - 1)
                    if len(p["rows"]) > 1 else body_gap)
    return paras


def row_text(row, text_of):
    return "".join(text_of.get(id(g), "") for g in row["g"])


def _hex_code(g, code_bytes):
    n = code_bytes.get(g.font_alias, 1)
    return ("%0*X" % (n * 2, g.code & ((1 << (8 * n)) - 1)))


def rebuild_ops(rows, code_bytes, positions=None):
    out = [b"q", b"BT"]
    for r in rows:
        for g in r["g"]:
            pos = None
            if positions is not None:
                pos = positions.get((g.op_index, g.char_index))
            x = pos[0] if pos else g.x0
            y = pos[1] if pos else g.y0
            size = eff_size(g)
            out.append(b"/%s %s Tf" % (g.font_alias.encode("latin-1"),
                                       ("%.3f" % size).rstrip("0").rstrip(".").encode()))
            out.append(b"1 0 0 1 %s %s Tm"
                       % (("%.3f" % x).rstrip("0").rstrip(".").encode(),
                          ("%.3f" % y).rstrip("0").rstrip(".").encode()))
            out.append(b"<" + _hex_code(g, code_bytes).encode() + b"> Tj")
    out.append(b"ET")
    out.append(b"Q")
    return b"\n".join(out) + b"\n"
