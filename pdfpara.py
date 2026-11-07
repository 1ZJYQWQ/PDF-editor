import statistics


def eff_size(g):
    return g.y1 - g.y0


def row_columns(row, gap_ratio=2.0, min_gap=6.0):
    gs = row["g"]
    if len(gs) < 2:
        return 1, 0.0
    limit = max(row["size"] * gap_ratio, min_gap)
    cols = 1
    max_gap = 0.0
    for a, b in zip(gs, gs[1:]):
        d = b.x0 - a.x1
        if d > max_gap:
            max_gap = d
        if d > limit:
            cols += 1
    return cols, max_gap


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
        r["cols"], r["maxgap"] = row_columns(r)
    rows.sort(key=lambda r: -r["y"])
    return rows


def split_columns(glyphs, gap_min=12.0):
    if not glyphs:
        return []

    ev = []
    for g in glyphs:
        ev.append((g.x0, 1))
        ev.append((g.x1, -1))
    ev.sort(key=lambda t: (t[0], -t[1]))

    depth = 0
    start = None
    spans = []
    for x, d in ev:
        if depth == 0 and d > 0:
            start = x
        depth += d
        if depth == 0 and start is not None:
            spans.append([start, x])
            start = None
    if not spans:
        return [list(glyphs)]

    cols = [spans[0]]
    for s, e in spans[1:]:
        if s - cols[-1][1] > gap_min:
            cols.append([s, e])
        else:
            cols[-1][1] = max(cols[-1][1], e)

    bands = [[] for _ in cols]
    for g in glyphs:
        for i, (a, b) in enumerate(cols):
            if g.x0 <= b + 1.0:
                bands[i].append(g)
                break
        else:
            bands[-1].append(g)
    return bands


def _band_extent(rows):
    return (min(r["x0"] for r in rows), max(r["x1"] for r in rows))


def table_cells(p, gap_ratio=2.0, min_gap=6.0, tol=5.0):
    rows = p.get("rows") or []
    if len(rows) < 2:
        return [], []

    row_starts = []
    for r in rows:
        if not r["g"]:
            continue
        limit = max(r["size"] * gap_ratio, min_gap)
        xs = [r["g"][0].x0]
        for a, b in zip(r["g"], r["g"][1:]):
            if b.x0 - a.x1 > limit:
                xs.append(b.x0)
        row_starts.append(xs)

    buckets = []          
    for xs in row_starts:
        for x in xs:
            for b in buckets:
                if abs(b[0] - x) <= tol:
                    b[1] += 1
                    break
            else:
                buckets.append([x, 1])
    cols = sorted(b[0] for b in buckets if b[1] >= 2)
    if len(cols) < 2:
        return [], cols

    cells = []
    for ri, r in enumerate(rows):
        buckets = [[] for _ in cols]
        for g in r["g"]:
            ci = 0
            for i, cx in enumerate(cols):
                if g.x0 >= cx - tol:
                    ci = i
            buckets[ci].append(g)
        for ci, gs in enumerate(buckets):
            if not gs:
                continue
            gs = sorted(gs, key=lambda g: g.x0)
            right = cols[ci + 1] if ci + 1 < len(cols) else p["right"]
            cells.append({
                "row": ri, "col": ci,
                "x0": gs[0].x0, "x1": gs[-1].x1,
                "y0": r["y"], "y1": r["y"] + r["size"],
                "right": right,
                "size": r["size"],
                "rows": [r],
                "glyphs": gs,
            })
    return cells, cols


def build_page_model(glyphs, gap_min=12.0, indent_tol=3.0, gap_tol=0.25):
    bands = split_columns(glyphs, gap_min=gap_min)
    band_rows = [build_rows(b) for b in bands]
    band_rows = [rs for rs in band_rows if rs]

    if len(band_rows) > 1:
        merged = []
        for i, rs in enumerate(band_rows):
            if len(rs) >= 2:
                merged.append(rs)
                continue
            if not merged and i + 1 < len(band_rows):
                band_rows[i + 1] = band_rows[i + 1] + rs
            elif merged:
                merged[-1] = merged[-1] + rs
            else:
                merged.append(rs)
        band_rows = merged

    rows = []
    paras = []
    for ci, rs in enumerate(band_rows):
        rs = sorted(rs, key=lambda r: -r["y"])
        for r in rs:
            r["col"] = ci
        rows.extend(rs)
        for p in _paragraphs_of_rows(rs, indent_tol, gap_tol):
            p["col"] = ci
            paras.append(p)

    gaps = [rs[i]["y"] - rs[i + 1]["y"] for rs in band_rows for i in range(len(rs) - 1)]
    body_gap = statistics.median(gaps) if gaps else 0.0
    return rows, _finalize_paras(paras, body_gap)


def _finalize_paras(paras, body_gap):
    for p in paras:
        ys = [r["y"] for r in p["rows"]]
        p["top"] = max(ys)
        p["bottom"] = min(ys)
        p["gap"] = ((p["top"] - p["bottom"]) / (len(p["rows"]) - 1)
                    if len(p["rows"]) > 1 else body_gap)
        ncol_rows = sum(1 for r in p["rows"] if r.get("cols", 1) > 1)
        p["cols"] = max((r.get("cols", 1) for r in p["rows"]), default=1)
        p["table_like"] = ncol_rows >= 2 and ncol_rows * 2 >= len(p["rows"])
    return paras


def _paragraphs_of_rows(rows, indent_tol=3.0, gap_tol=0.25):
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
            if not indented and same_gap and same_x and same_size:
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
