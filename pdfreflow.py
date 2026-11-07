import re
import statistics
import struct

from pdfedit import build_cjk_font, _attach_font_resource, Ref
from pdfredact import (PDFDocument, ContentStreamInterpreter,
                       read_page_content, _rewrite_text)
from pdfpara import build_rows, build_page_model, table_cells

FALLBACK_ALIAS = "CJKfloor"      



def load_page_model(path, page_no=1):
    with open(path, "rb") as fp:
        doc = PDFDocument.load(fp.read())
    refs = doc.page_refs()
    if not (1 <= page_no <= len(refs)):
        raise ValueError("页码超出范围：%d（共 %d 页）" % (page_no, len(refs)))
    pref = refs[page_no - 1]
    content = read_page_content(doc, pref)
    resources = doc.resolve(doc.inherited(pref, "Resources"))
    ip = ContentStreamInterpreter(doc, resources)
    ip.run(content)
    rows, paras = build_page_model(ip.glyphs)
    return doc, pref, content, ip, rows, paras


def char_of(ip):
    def f(g):
        fi = ip.fonts.get(g.font_alias)
        if not fi:
            return ""
        return fi.to_unicode.get(g.code, "")
    return f


def _rev_map(fi):
    rev = getattr(fi, "_rev", None)
    if rev is None:
        rev = {}
        for code, ch in (fi.to_unicode or {}).items():
            if ch and ch not in rev:      
                rev[ch] = code
        fi._rev = rev
    return rev


def para_text(p, ch):
    return "\n".join("".join(ch(g) for g in r["g"]) for r in p["rows"])


def dominant_font(p):
    n = {}
    for r in p["rows"]:
        for g in r["g"]:
            n[g.font_alias] = n.get(g.font_alias, 0) + 1
    return max(n, key=n.get) if n else ""



def solid_glyphs(p):
    size = p["size"] or 12.0
    out = []
    for r in p["rows"]:
        for g in r["g"]:
            if (g.x1 - g.x0) > 0.15 * size:
                out.append(g)
    return out


def build_aligned_map(p, orig_text):
    size = p["size"] or 12.0
    glyphs = [g for r in p["rows"] for g in r["g"]]
    text = (orig_text or "").replace("\r", "").replace("\n", "")
    if not text.strip():
        raise ValueError("没有提供段落原文，无法建立字符映射")

    chars_all = list(text)
    chars_ns = [c for c in chars_all if not c.isspace()]
    narrow = max(0.15 * size, 0.5)
    solid = [g for g in glyphs if (g.x1 - g.x0) > narrow]

    def pair(gs, cs):
        table = {}
        conflicts = 0
        for g, c in zip(gs, cs):
            key = (g.font_alias, g.code)
            if c in table and table[c] != key:
                conflicts += 1      
                continue
            table[c] = key
        return table, conflicts

    for gs in (glyphs, solid):
        if len(gs) == len(chars_ns):
            return pair(gs, chars_ns)
        if len(gs) == len(chars_all):
            return pair(gs, chars_all)

    raise ValueError(
        "段落原文与字形的字数对不上（原文 %d 字 / 去空白 %d 字，字形 %d 个 / 去窄字形 %d 个），"
        "为避免把字写错，已拒绝。\n"
        "常见原因：这段里含图片文字、或者原文少取/多取了几个字。"
        % (len(chars_all), len(chars_ns), len(glyphs), len(solid)))


def unique_font_alias(doc, page_ref, base):
    page = doc.resolve(page_ref)
    res = page.get("Resources") if isinstance(page, dict) else None
    if isinstance(res, Ref):
        res = doc.resolve(res)
    fonts = res.get("Font") if isinstance(res, dict) else None
    if isinstance(fonts, Ref):
        fonts = doc.resolve(fonts)
    used = set(fonts.keys()) if isinstance(fonts, dict) else set()

    name = base
    n = 1
    while name in used:
        n += 1
        name = "%s%d" % (base, n)
    return name


def char_runs(text, fi, alias, size, subset, extra=None, fonts=None,
              fallback_alias=None):
    floor = fallback_alias or FALLBACK_ALIAS
    rev = _rev_map(fi)
    out = []
    for ch in text:
        if ch == "\n":
            out.append((ch, None, None, 0.0, 0, False))
            continue
        if extra:
            hit = extra.get(ch)
            if hit is not None:
                a, code = hit
                fi2 = (fonts or {}).get(a) or fi
                w = fi2.width_of(code) / 1000.0 * size
                out.append((ch, a, code, w, fi2.code_bytes, False))
                continue
        code = rev.get(ch)
        if code is not None:
            w = fi.width_of(code) / 1000.0 * size
            out.append((ch, alias, code, w, fi.code_bytes, False))
            continue
        g = None
        if subset is not None:
            g = subset.gid_of.get(ord(ch))
        if g is not None:
            upem = float(subset.upem or 1000)
            w = subset.widths.get(g, upem) / upem * size
            out.append((ch, floor, g, w, 2, True))
            continue
        if ch.isspace():
            out.append((ch, alias, 32, size * 0.5, fi.code_bytes, True))
        else:
            out.append((ch, None, None, size * 0.5, 0, True))
    return out


def wrap_runs(runs, first_x, cont_x, right, tol=0.5):
    hard = [[]]
    for r in runs:
        if r[0] == "\n":
            hard.append([])
        else:
            hard[-1].append(r)

    lines = []

    def push(cur, x, cut=False):
        lines.append({"runs": cur, "x": x, "cut": cut})

    for hi, hr in enumerate(hard):
        start_x = first_x if hi == 0 else cont_x
        if not hr:
            push([], start_x)
            continue

        cur = []
        x = start_x
        cur_x = start_x
        last_space = -1

        for run in hr:
            w = run[3]
            if cur and x + w > right + tol:
                head = cur[:last_space + 1] if last_space > 0 else cur
                head_w = sum(r[3] for r in head)
                use_space = (last_space > 0 and
                             head_w >= (right - cur_x) * 0.5)
                if use_space:
                    push(head, cur_x, cut=True)
                    rest = cur[last_space + 1:]
                    cur = rest
                    cur_x = cont_x
                    x = cont_x + sum(r[3] for r in rest)
                else:
                    push(cur, cur_x, cut=True)
                    cur = []
                    cur_x = cont_x
                    x = cont_x
                last_space = -1
            if run[0] == " ":
                last_space = len(cur)
            cur.append(run)
            x += w

        push(cur, cur_x)

    return lines


def fit_line(line, right):
    runs = line["runs"]
    if not runs:
        return runs
    total = sum(r[3] for r in runs)
    end = line["x"] + total
    if end <= right + 0.01 or total <= 0:
        return runs
    k = max(0.1, (right - line["x"]) / total)
    return [(r[0], r[1], r[2], r[3] * k, r[4], r[5]) for r in runs]


def justify(line, right):
    runs = line["runs"]
    if not runs:
        return runs
    used = sum(r[3] for r in runs)
    room = right - (line["x"] + used)
    if room <= 0.2:
        return runs
    span = right - line["x"]
    if span > 0 and room > span * 0.4:
        return runs
    body = runs
    while body and body[-1][0] == " ":
        body = body[:-1]
    if not body:
        return runs
    extra = room / len(body)
    out = []
    for i, r in enumerate(runs):
        if i < len(body):
            out.append((r[0], r[1], r[2], r[3] + extra, r[4], r[5]))
        else:
            out.append(r)
    return out



def _hex(code, nbytes):
    return "%0*X" % (max(1, nbytes) * 2, int(code) & ((1 << (8 * max(1, nbytes))) - 1))


def runs_to_ops(items):
    out = [b"q", b"0 0 0 rg", b"BT"]
    for (x, y, alias, size, hx) in items:
        out.append(b"/%s %s Tf" % (alias.encode("latin-1"),
                                   ("%.3f" % size).rstrip("0").rstrip(".").encode()))
        out.append(b"1 0 0 1 %s %s Tm"
                   % (("%.3f" % x).rstrip("0").rstrip(".").encode(),
                      ("%.3f" % y).rstrip("0").rstrip(".").encode()))
        out.append(b"<" + hx.encode() + b"> Tj")
    out.append(b"ET")
    out.append(b"Q")
    return b"\n".join(out) + b"\n"


def _row_items(rows, code_bytes, dy=0.0, alias_map=None):
    items = []
    for r in rows:
        for g in r["g"]:
            a = alias_map.get(g.font_alias, g.font_alias) if alias_map else g.font_alias
            items.append((g.x0, g.y0 + dy, a,
                          g.y1 - g.y0, _hex(g.code, code_bytes.get(g.font_alias, 1))))
    return items



_NUM = r'[-+]?(?:\d+\.?\d*|\.\d+)'
_RE_RECT = re.compile(
    r'(%s)\s+(%s)\s+(%s)\s+(%s)\s+re(?![A-Za-z])' % (_NUM, _NUM, _NUM, _NUM))

_PATH_PT = re.compile(r'((?:%s\s+){2,6})(m|l|c|v|y)(?![A-Za-z])' % _NUM)
_PATH_END = re.compile(
    r'(?<![A-Za-z])(f\*|f|S|s|B\*|B|b\*|b|n|W\*|W)(?![A-Za-z])')
_PATH_Y_IDX = {"m": [1], "l": [1], "c": [1, 3, 5], "v": [1, 3], "y": [1, 3]}


def _fmt_num(v):
    s = ('%.6f' % v).rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s


def _next_op(text, pos):
    tail = text[pos:pos + 12].lstrip()
    sp = tail.split()
    return sp[0] if sp else ''


def h_lines(content, x0, x1, tol=2.0):
    text = content.decode('latin-1')
    ys = []
    for m in _RE_RECT.finditer(text):
        op = _next_op(text, m.end())
        if op in ('W*', 'n', ''):
            continue
        a, b, c, d = (float(m.group(i)) for i in range(1, 5))
        if d > 1.5:
            continue                                  
        if a + c < x0 - tol or a > x1 + tol:
            continue
        ys.append(round(b + d / 2.0, 2))
    return sorted(set(ys), reverse=True)


def _mask_strings(text):
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '(':
            depth, j = 1, i + 1
            while j < n and depth > 0:
                c = text[j]
                if c == '\\':
                    out[j] = ' '
                    if j + 1 < n:
                        out[j + 1] = ' '
                    j += 2
                    continue
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                if depth > 0:
                    out[j] = ' '
                j += 1
            i = j
        elif ch == '<' and text[i:i + 2] != '<<':
            j = text.find('>', i)
            j = n if j < 0 else j + 1
            for k in range(i + 1, j - 1):
                out[k] = ' '
            i = j
        else:
            i += 1
    return ''.join(out)


_CTM_RE = re.compile(
    r'(%s)\s+(%s)\s+(%s)\s+(%s)\s+(%s)\s+(%s)\s+cm(?![A-Za-z])' % ((_NUM,) * 6))
_QS_RE = re.compile(r'(?<![A-Za-z0-9])([qQ])(?![A-Za-z0-9])')


def _mat_mul(m, c):
    a, b, cc, d, e, f = m
    ca, cb, ccc, cd, ce, cf = c
    return (a * ca + cc * cb,
            b * ca + d * cb,
            a * ccc + cc * cd,
            b * ccc + d * cd,
            a * ce + cc * cf + e,
            b * ce + d * cf + f)


def _map_pt(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def shift_vectors(content, y_bot, delta, x0, x1, tol=2.0, y_min=0.0,
                  stretch=True):
    if abs(delta) < 1e-6:
        return content, {"moved": 0, "stretched": 0, "ctm_skipped": 0, "paths": 0}

    text = content.decode('latin-1')
    masked = _mask_strings(text)
    stat = {"moved": 0, "stretched": 0, "ctm_skipped": 0, "paths": 0}

    events = []
    for m in _QS_RE.finditer(masked):
        events.append((m.start(), "q" if m.group(1) == "q" else "Q", m))
    for m in _CTM_RE.finditer(masked):
        events.append((m.start(), "cm",
                       tuple(float(m.group(i)) for i in range(1, 7))))
    for m in _RE_RECT.finditer(masked):
        events.append((m.start(), "re", m))
    for m in _PATH_PT.finditer(masked):
        events.append((m.start(), "pt", m))
    for m in _PATH_END.finditer(masked):
        events.append((m.start(), "end", m))
    events.sort(key=lambda e: e[0])

    edits = []
    stack = []
    path = {"xs": [], "ys": [], "spans": [], "dy": 0.0}

    def flush_path():
        if path["spans"] and path["xs"]:
            bx0, bx1 = min(path["xs"]), max(path["xs"])
            by0, by1 = min(path["ys"]), max(path["ys"])
            ok = (not (bx1 < x0 - tol or bx0 > x1 + tol)
                  and by1 > y_min + 1.0
                  and (by0 + by1) / 2.0 <= y_bot + tol
                  and path["dy"] is not None)
            if ok:
                for (s, e, v) in path["spans"]:
                    edits.append((s, e, _fmt_num(v + path["dy"])))
                stat["paths"] += 1
        path["xs"].clear()
        path["ys"].clear()
        path["spans"].clear()
        path["dy"] = 0.0

    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    for _pos, kind, payload in events:
        if kind == "q":
            stack.append(ctm)
            continue
        if kind == "Q":
            if stack:
                ctm = stack.pop()
            continue
        if kind == "cm":
            ctm = _mat_mul(ctm, payload)
            continue

        if kind == "pt":
            op2 = payload.group(2)
            base = payload.start(1)
            nums = [(base + mm.start(), base + mm.end(), float(mm.group(0)))
                    for mm in re.finditer(_NUM, payload.group(1))]
            for idx in _PATH_Y_IDX.get(op2, []):
                if idx < len(nums):
                    path["spans"].append(nums[idx])
            for i in range(0, len(nums) - 1, 2):
                X, Y = _map_pt(ctm, nums[i][2], nums[i + 1][2])
                path["xs"].append(X)
                path["ys"].append(Y)
            if path["dy"] is None:
                pass                                   
            elif abs(ctm[1]) > 1e-9 or abs(ctm[2]) > 1e-9 or abs(ctm[3]) < 1e-9:
                path["dy"] = None                      
                stat["ctm_skipped"] += 1
            else:
                path["dy"] = -delta / ctm[3]
            continue

        if kind == "end":
            if payload.group(1) in ("n", "W", "W*"):
                path["xs"].clear()
                path["ys"].clear()
                path["spans"].clear()
                path["dy"] = 0.0
            else:
                flush_path()
            continue

        m = payload
        op = _next_op(text, m.end())
        if op in ("W", "W*", "n", ""):
            continue                                  
        a, b, c, d = (float(m.group(i)) for i in range(1, 5))
        if abs(ctm[1]) > 1e-9 or abs(ctm[2]) > 1e-9 or abs(ctm[3]) < 1e-9:
            stat["ctm_skipped"] += 1
            continue
        (px0, py0) = _map_pt(ctm, a, b)
        (px1, py1) = _map_pt(ctm, a + c, b + d)
        bx0, bx1 = min(px0, px1), max(px0, px1)
        by0, by1 = min(py0, py1), max(py0, py1)
        bw, bh = bx1 - bx0, by1 - by0
        if bx1 < x0 - tol or bx0 > x1 + tol:
            continue                                  
        if by1 <= y_min + 1.0:
            continue                                  

        local_dy = -delta / ctm[3]
        local_h = delta / abs(ctm[3]) if stretch else 0.0

        vertical = bw < 1.5 and bh > 1.5
        if stretch and vertical and by0 <= y_bot + tol and by1 > y_bot + tol:
            stat["stretched"] += 1
            edits.append((m.start(), m.end(),
                          '%s %s %s %s re' % (_fmt_num(a), _fmt_num(b + local_dy),
                                              _fmt_num(c), _fmt_num(d + local_h))))
            continue
        if (by0 + by1) / 2.0 <= y_bot + tol:
            stat["moved"] += 1
            edits.append((m.start(), m.end(),
                          '%s %s %s %s re' % (_fmt_num(a), _fmt_num(b + local_dy),
                                              _fmt_num(c), _fmt_num(d))))

    if not edits:
        return content, stat
    out, last = [], 0
    for s, e, rep in edits:
        out.append(text[last:s])
        out.append(rep)
        last = e
    out.append(text[last:])
    return ''.join(out).encode('latin-1'), stat


def take_vectors(content, y_cut, x0, x1, tol=2.0, y_min=0.0):
    text = content.decode("latin-1")
    masked = _mask_strings(text)
    picked = []

    events = []
    for m in _QS_RE.finditer(masked):
        events.append((m.start(), "q" if m.group(1) == "q" else "Q", m))
    for m in _CTM_RE.finditer(masked):
        events.append((m.start(), "cm",
                       tuple(float(m.group(i)) for i in range(1, 7))))
    for m in _RE_RECT.finditer(masked):
        events.append((m.start(), "re", m))
    events.sort(key=lambda e: e[0])

    edits = []
    stack = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for _pos, kind, payload in events:
        if kind == "q":
            stack.append(ctm)
            continue
        if kind == "Q":
            if stack:
                ctm = stack.pop()
            continue
        if kind == "cm":
            ctm = _mat_mul(ctm, payload)
            continue

        m = payload
        op = _next_op(text, m.end())
        if op in ("W", "W*", "n", ""):
            continue                                  
        if abs(ctm[1]) > 1e-9 or abs(ctm[2]) > 1e-9:
            continue                                  
        a, b, c, d = (float(m.group(i)) for i in range(1, 5))
        (px0, py0) = _map_pt(ctm, a, b)
        (px1, py1) = _map_pt(ctm, a + c, b + d)
        bx0, bx1 = min(px0, px1), max(px0, px1)
        by0, by1 = min(py0, py1), max(py0, py1)
        if bx1 < x0 - tol or bx0 > x1 + tol:
            continue
        if by1 <= y_min + 1.0:
            continue                                  
        if (by0 + by1) / 2.0 >= y_cut - tol:
            continue                                  
        picked.append((bx0, by0, bx1 - bx0, by1 - by0))
        edits.append((m.start(), m.end(), m.group(0) + " n"))

    if not edits:
        return content, picked
    out, last = [], 0
    for s, e, rep in edits:
        out.append(text[last:s])
        out.append(rep)
        last = e
    out.append(text[last:])
    return "".join(out).encode("latin-1"), picked


def rect_ops(rects, dy, gray=0.0):
    if not rects:
        return b""
    g = _fmt_num(gray)
    parts = ["q", "%s g" % g, "0 G"]
    for (a, b, c, d) in rects:
        parts.append("%s %s %s %s re" % (_fmt_num(a), _fmt_num(b + dy),
                                         _fmt_num(c), _fmt_num(d)))
        parts.append("f*")
    parts.append("Q")
    return ("\n".join(parts) + "\n").encode("latin-1")


def shift_table_borders(content, p, delta, y_min=24.0):
    ty0 = p.get("_table_y0", p["bottom"])
    ty1 = p.get("_table_y1", p["top"])
    span = None
    for m in _RE_RECT.finditer(content.decode("latin-1")):
        if _next_op(content.decode("latin-1"), m.end()) in ("W", "W*", "n", ""):
            continue
        a, b, c, d = (float(m.group(i)) for i in range(1, 5))
        if d > 1.5 or c < 5:
            continue                                   
        if b + d / 2.0 < ty0 - 3 or b + d / 2.0 > ty1 + 3:
            continue
        lo, hi = a, a + c
        span = (lo, hi) if span is None else (min(span[0], lo), max(span[1], hi))

    if span is None:
        x0 = p.get("_table_x0", p["left"]) - 2.0
        x1 = p.get("_table_x1", p["right"]) + 2.0
    else:
        x0, x1 = span[0] - 1.0, span[1] + 1.0

    y_bot = None
    for y in h_lines(content, x0, x1):
        if y <= p["bottom"] + 2.0:
            y_bot = y
            break
    if y_bot is None:
        return content, {"moved": 0, "stretched": 0, "y_bot": None}

    new_content, stat = shift_vectors(content, y_bot, delta, x0, x1, y_min=y_min)
    stat["y_bot"] = y_bot
    return new_content, stat



def _cell_as_para(p, cl):
    orig = p["rows"][cl["row"]]
    ids = {id(g) for g in cl["glyphs"]}

    sub = dict(orig)
    sub["g"] = list(cl["glyphs"])
    sub["x0"], sub["x1"] = cl["x0"], cl["x1"]
    sub["cols"], sub["maxgap"] = 1, 0.0

    rest = dict(orig)
    rest["g"] = [g for g in orig["g"] if id(g) not in ids]
    rest["x0"] = rest["g"][0].x0 if rest["g"] else orig["x0"]
    rest["x1"] = rest["g"][-1].x1 if rest["g"] else orig["x1"]

    pseudo = dict(p)
    pseudo.update({
        "rows": [sub],
        "left": cl["x0"],
        "right": cl["right"],
        "top": cl["y1"],
        "bottom": cl["y0"],
        "size": cl["size"],
        "gap": cl["size"] * 1.25,
        "table_like": False,
        "_cell": True,
        "_orig_row": orig,
        "_extra_rows": [rest] if rest["g"] else [],
        "_force_right": cl["right"],
        "_table_x0": p.get("left", cl["x0"]),
        "_table_x1": p.get("right", cl["right"]),
        "_table_y0": p.get("bottom", cl["y0"]),
        "_table_y1": p.get("top", cl["y1"]),
    })
    return pseudo


def _prepare_edit(src_path, page_no, para_index, new_text,
                  orig_text=None, page_text=None, cell=None):
    doc, pref, content, ip, rows, paras = load_page_model(src_path, page_no)
    if not (0 <= para_index < len(paras)):
        raise ValueError("段落下标超出范围：%d（本页共 %d 段）"
                         % (para_index, len(paras)))

    p = paras[para_index]
    if cell is not None:
        _ch = char_of(ip)
        by_text = []
        if orig_text:
            for q in paras:
                if not q.get("table_like"):
                    continue
                cells_q, _ = table_cells(q)
                for cl in cells_q:
                    if "".join(_ch(g) for g in cl["glyphs"]) == orig_text:
                        by_text.append((q, cl))
        if len(by_text) == 1:
            want = by_text
        else:
            want = []
            for q in paras:
                if not q.get("table_like"):
                    continue
                cells_q, _ = table_cells(q)
                hit = [cl for cl in cells_q
                       if cl["row"] == cell[0] and cl["col"] == cell[1]]
                if hit:
                    got = "".join(_ch(g) for g in hit[0]["glyphs"])
                    if not orig_text or got == orig_text:
                        want = [(q, hit[0])]
                    break
            if not want and by_text:
                by_text.sort(key=lambda z: (abs(z[1]["row"] - cell[0]) +
                                            abs(z[1]["col"] - cell[1])))
                want = by_text[:1]
        if not want:
            raise ValueError("没找到这个单元格（表格结构可能已变化）。")
        p = _cell_as_para(want[0][0], want[0][1])
    elif p.get("table_like"):
        raise ValueError(
            "这块是多列/表格区域（最多 %d 列）。段落重排会按段宽重新折行、"
            "把各列拼成一列，表格结构会被破坏，所以这里不提供整段改写。\n"
            "请点选具体的**单元格**来改，或者用「替换」工具逐处处理。"
            % int(p.get("cols") or 1))
    ch = char_of(ip)
    code_bytes = {a: fi.code_bytes for a, fi in ip.fonts.items()}

    alias = dominant_font(p)
    fi = ip.fonts.get(alias)
    if fi is None:
        raise ValueError("该段没有可用的源字体，无法编辑")

    own_text = para_text(p, ch)
    extra_map = {}
    map_source = "tounicode"
    conflicts = 0
    if not own_text.strip():
        if not (orig_text and orig_text.strip()):
            raise ValueError(
                "这份文档的字体里没有 ToUnicode 映射。\n"
                "可以把段落原文一起提交（orig 参数），我们会按字对齐反推字符映射；"
                "否则请改用「替换」工具逐段处理。")
        extra_map, conflicts = build_aligned_map(p, orig_text)
        map_source = "aligned"

        if page_text and page_text.strip():
            try:
                psize = p["size"] or 12.0
                pmap, _pc = build_aligned_map(
                    {"rows": rows, "size": psize}, page_text)
            except ValueError:
                pmap = {}
            if pmap:
                merged = dict(pmap)
                merged.update(extra_map)       
                extra_map = merged
                map_source = "aligned-page"

    size = p["size"]
    first_x = p["rows"][0]["x0"]                      
    cont_x = min(r["x0"] for r in p["rows"])          
    if p.get("_force_right"):
        right = p["_force_right"]
    elif len(p["rows"]) > 1:
        right = max(r["x1"] for r in p["rows"])
    else:
        right = max((r["x1"] for r in rows), default=0.0)
    gap = p["gap"] or (rows[0]["y"] - rows[1]["y"] if len(rows) > 1 else size * 1.6)
    if gap <= 0:
        gap = size * 1.4

    rev = _rev_map(fi)
    miss = {c for c in new_text
            if c != "\n" and not c.isspace()
            and c not in rev and c not in extra_map}
    subset = None
    floor_alias = FALLBACK_ALIAS
    if miss:
        floor_alias = unique_font_alias(doc, pref, FALLBACK_ALIAS)
        cjk_ref, subset = build_cjk_font(doc, {ord(c) for c in miss})
        _attach_font_resource(doc, pref, {floor_alias: cjk_ref})
        subset = subset

    runs = char_runs(new_text, fi, alias, size, subset,
                    extra=extra_map, fonts=ip.fonts,
                    fallback_alias=floor_alias)
    lines = wrap_runs(runs, first_x, cont_x, right, tol=max(0.5, size * 0.5))

    cols = {q.get("col", 0) for q in paras}
    if len(cols) > 1:
        col = p.get("col", 0)
        same = [q for q in paras if q.get("col", 0) == col]
        shift_x0 = min(q["left"] for q in same) - 2.0
        shift_x1 = max(q["right"] for q in same) + 2.0
    else:
        mb = doc.inherited(pref, "MediaBox")
        pw = (abs(float(mb[2]) - float(mb[0]))
              if isinstance(mb, list) and len(mb) == 4 else 595.32)
        shift_x0, shift_x1 = 6.0, pw - 6.0

    return {
        "doc": doc, "pref": pref, "content": content, "ip": ip,
        "rows": rows, "paras": paras, "p": p,
        "code_bytes": code_bytes, "alias": alias, "fi": fi,
        "size": size, "first_x": first_x, "cont_x": cont_x, "right": right,
        "gap": gap, "lines": lines, "subset": subset, "miss": miss,
        "extra_map": extra_map, "map_source": map_source,
        "conflicts": conflicts, "floor_alias": floor_alias,
        "shift_x0": shift_x0, "shift_x1": shift_x1,
    }


def apply_paragraph_edit(src_path, out_path, page_no, para_index, new_text,
                         bottom_limit=24.0, orig_text=None, page_text=None,
                         cell=None):
    ctx = _prepare_edit(src_path, page_no, para_index, new_text,
                        orig_text=orig_text, page_text=page_text, cell=cell)
    doc, pref, content, ip = ctx["doc"], ctx["pref"], ctx["content"], ctx["ip"]
    rows, p, code_bytes = ctx["rows"], ctx["p"], ctx["code_bytes"]
    size, lines, gap = ctx["size"], ctx["lines"], ctx["gap"]
    right = ctx["right"]
    alias, miss, subset = ctx["alias"], ctx["miss"], ctx["subset"]
    extra_map = ctx["extra_map"]
    map_source, conflicts = ctx["map_source"], ctx["conflicts"]


    old_n = len(p["rows"])
    new_n = len(lines)
    delta = (new_n - old_n) * gap        

    pcol = p.get("col", 0)
    body_rows = [r for r in rows
                 if r["y"] >= bottom_limit - 1 and r.get("col", 0) == pcol]

    footer_y = None
    if len(body_rows) >= 2:
        ys = sorted(r["y"] for r in body_rows)
        diffs = [b - a for a, b in zip(ys, ys[1:])]
        typical = statistics.median(diffs) if diffs else 0.0
        if typical > 0 and (ys[1] - ys[0]) > typical * 3:
            footer_y = ys[0]
            body_rows = [r for r in body_rows if r["y"] > footer_y + 0.5]

    below = [r for r in body_rows if r["y"] < p["bottom"] - 0.5]
    lowest = min((r["y"] for r in body_rows), default=bottom_limit)
    avail = lowest - bottom_limit          
    if delta > avail + 1e-6:
        if p.get("_cell"):
            raise ValueError(
                "这一格放不下：文字变成 %d 行后，整行要加高 %.1fpt，"
                "但页底只剩 %.1fpt。\n"
                "请缩短文字，或先把表格下面的内容往下挪一点。"
                % (new_n, delta, max(0.0, avail)))
        raise ValueError(
            "这一段放不下：重排后本页还需下移 %.1fpt，但页底只剩 %.1fpt。\n"
            "请缩短这段文字，或先手动在末尾插入空白页再分段编辑。"
            % (delta, max(0.0, avail)))

    own = {id(r) for r in p["rows"]}
    skip = {id(p["_orig_row"])} if p.get("_orig_row") is not None else set()

    items = []
    extra_dy = -delta / 2.0 if p.get("_cell") else 0.0
    for r in (p.get("_extra_rows") or []):
        items += _row_items([r], code_bytes, dy=extra_dy)
    for r in rows:
        if id(r) in own or id(r) in skip:
            continue                                   
        if r.get("col", 0) != pcol:
            dy = 0.0                                   
        elif r["y"] < bottom_limit - 1 or (
                footer_y is not None and abs(r["y"] - footer_y) < 1.0):
            dy = 0.0                                   
        elif r["y"] < p["bottom"] - 0.5:
            dy = -delta                                
        else:
            dy = 0.0
        items += _row_items([r], code_bytes, dy=dy)

    y = p["rows"][0]["y"]
    for li, ln in enumerate(lines):
        ln_runs = ln["runs"]
        if li < len(lines) - 1 and ln.get("cut"):
            ln_runs = justify(ln, right)
        ln_runs = fit_line({"x": ln["x"], "runs": ln_runs}, right)
        x = ln["x"]
        for (chr_, a, code, w, nb, _fb) in ln_runs:
            if a is None or code is None:
                x += w
                continue
            items.append((x, y, a, size, _hex(code, nb)))
            x += w
        y -= gap

    ops = runs_to_ops(items)

    border = {"moved": 0, "stretched": 0, "y_bot": None}
    if abs(delta) > 1e-6:
        y_min = (footer_y + 1.0) if footer_y is not None else bottom_limit
        if p.get("_cell"):
            content, border = shift_table_borders(content, p, delta, y_min=y_min)
        else:
            content, border = shift_vectors(
                content, p["bottom"], delta,
                ctx.get("shift_x0", 0.0), ctx.get("shift_x1", 1e9),
                y_min=y_min, stretch=False)

    killed = {(g.op_index, g.char_index) for g in ip.glyphs}
    del_content = _rewrite_text(content, killed, code_bytes)
    page = doc.resolve(pref)
    page["Contents"] = [
        doc.new_obj(({"Length": len(del_content)}, del_content)),
        doc.new_obj(({"Length": len(ops)}, ops)),
    ]

    from pdfedit import build_pdf
    size_bytes = build_pdf(doc, {}, out_path)

    return {
        "ok": True,
        "size": size_bytes,
        "page": page_no,
        "para": para_index,
        "chars": len([c for c in new_text if c != "\n"]),
        "lines_before": old_n,
        "lines_after": new_n,
        "shift": round(delta, 2),
        "shifted_rows": len(below),
        "border_shift": border,
        "pages_affected": 1,
        "new_pages": 0,
        "last_page": page_no,
        "font": alias,
        "map_source": map_source,
        "mapped_chars": len(extra_map),
        "map_conflicts": conflicts,
        "fallback_chars": "".join(sorted(miss)),
    }


def list_paragraphs(src_path, page_no=1):
    doc, pref, content, ip, rows, paras = load_page_model(src_path, page_no)
    ch = char_of(ip)
    out = []
    for i, p in enumerate(paras):
        _ptext = para_text(p, ch)
        item = {
            "index": i,
            "text": _ptext if _ptext.strip() else "",
            "lines": len(p["rows"]),
            "size": round(p["size"], 2),
            "gap": round(p["gap"], 2),
            "font": dominant_font(p),
            "table_like": bool(p.get("table_like")),
            "cols": int(p.get("cols") or 1),
            "col": int(p.get("col", 0)),
            "needs_text": not bool(_ptext.strip()),
            "x0": round(min(r["x0"] for r in p["rows"]), 2),
            "y0": round(p["bottom"], 2),
            "x1": round(max(r["x1"] for r in p["rows"]), 2),
            "y1": round(p["rows"][0]["y"] + p["size"], 2),
        }
        if p.get("table_like"):
            cs, _cstarts = table_cells(p)
            item["cells"] = [{
                "row": cl["row"], "col": cl["col"],
                "x0": round(cl["x0"], 2), "y0": round(cl["y0"], 2),
                "x1": round(cl["right"], 2), "y1": round(cl["y1"], 2),
                "text": "".join(ch(g) for g in cl["glyphs"]),
            } for cl in cs]
        out.append(item)
    return {"ok": True, "page": page_no, "count": len(out), "paragraphs": out}



def _font_resources(doc, pref):
    page = doc.resolve(pref)
    res = doc.resolve(page.get("Resources")) if isinstance(page, dict) else None
    if not isinstance(res, dict):
        return {}
    fonts = doc.resolve(res.get("Font"))
    if not isinstance(fonts, dict):
        return {}
    return {str(k): v for k, v in fonts.items()}


def apply_paragraph_flow(src_path, out_path, page_no, para_index, new_text,
                         orig_text=None, page_text=None,
                         bottom_ratio=0.05, max_new_pages=20, cell=None,
                         max_cascade=20):
    from pdfedit import build_pdf, insert_blank_page

    if cell is not None:
        raise ValueError(
            "单元格不支持跨页重排：表格的行高是固定的，把它撑成两行会毁掉整张表。\n"
            "请把文字缩短到当前列宽能放下的长度。")

    ctx = _prepare_edit(src_path, page_no, para_index, new_text,
                        orig_text=orig_text, page_text=page_text, cell=cell)
    doc, ip, rows, p = ctx["doc"], ctx["ip"], ctx["rows"], ctx["p"]
    code_bytes, size, gap, right = (ctx["code_bytes"], ctx["size"],
                                    ctx["gap"], ctx["right"])
    lines = ctx["lines"]

    refs = doc.page_refs()
    total = len(refs)
    total0 = total                     

    page_h = 842.0
    mb = doc.inherited(refs[0], "MediaBox")
    if isinstance(mb, list) and len(mb) == 4:
        page_h = abs(float(mb[3]) - float(mb[1]))
    bottom = max(18.0, page_h * bottom_ratio)
    page_w = 595.0
    if isinstance(mb, list) and len(mb) == 4:
        page_w = abs(float(mb[2]) - float(mb[0]))

    for li, ln in enumerate(lines):
        if li < len(lines) - 1 and ln.get("cut"):
            ln["runs"] = justify(ln, right)

    cache = {}

    def model_of(k):
        if k in cache:
            return cache[k]
        if k == page_no:
            m = {"ref": ctx["pref"], "content": ctx["content"],
                 "ip": ip, "rows": rows}
        else:
            pref_k = refs[k - 1]
            c = read_page_content(doc, pref_k)
            ipk = ContentStreamInterpreter(
                doc, doc.resolve(doc.inherited(pref_k, "Resources")))
            ipk.run(c)
            m = {"ref": pref_k, "content": c, "ip": ipk,
                 "rows": build_rows(ipk.glyphs)}
        cache[k] = m
        return m

    own = {id(r) for r in p["rows"]}
    para_top_y = p["rows"][0]["y"]
    gaps = [rows[i]["y"] - rows[i + 1]["y"] for i in range(len(rows) - 1)]
    default_gap = gap or (sorted(gaps)[len(gaps) // 2] if gaps else size * 1.6)

    def items_for(k):
        m = model_of(k)
        out = []
        fixed = []          
        prev_y = None
        replaced = False
        after_y = None      
        for r in m["rows"]:
            if r["y"] < bottom - 1:
                fixed.append(r)
                continue
            if k == page_no and id(r) in own:
                if not replaced:
                    for li, ln in enumerate(lines):
                        out.append({"line": ln,
                                    "gap": (prev_y - para_top_y) if (li == 0 and prev_y is not None)
                                           else gap})
                    replaced = True
                    after_y = p["rows"][-1]["y"]
                    prev_y = para_top_y - (len(lines) - 1) * gap
                continue
            if after_y is not None:
                g = after_y - r["y"]
                after_y = None
            else:
                g = (prev_y - r["y"]) if prev_y is not None else default_gap
            out.append({"row": r, "gap": g, "src": k})
            prev_y = r["y"]
        return out, fixed

    top_default = max((r["y"] for r in rows), default=page_h - bottom)

    page = page_no
    carry = []
    plan = []                     
    moved = []                    
    carry_cut, carry_from = None, None
    while True:
        m = model_of(page)
        items, fixed = items_for(page)
        if carry:
            items = [dict(it) for it in carry] + items

        top = max((r["y"] for r in m["rows"]), default=top_default)
        if carry_cut is not None:
            moved.append((carry_from, carry_cut, page, top - carry_cut))
            carry_cut = None
        placed, spill = [], []
        y = top
        first = True
        for it in items:
            if spill:
                spill.append(it)
                continue
            yy = y if first else y - it["gap"]
            if yy < bottom:
                spill.append(it)
                continue
            placed.append((it, yy))
            y = yy
            first = False

        plan.append((page, placed, fixed))
        if not spill:
            break

        page += 1
        first_row = next((it for it in spill if "row" in it), None)
        if first_row is not None:
            carry_cut, carry_from = first_row["row"]["y"], page - 1
        else:
            carry_cut = None
        if page - page_no > max_cascade:
            raise ValueError(
                "这一段太长：要推动后面 %d 页以上才能放下（上限 %d 页）。\n"
                "请缩短文字；确实需要这么长，可以手动在末尾插入一页再改。"
                % (max_cascade + 1, max_cascade))
        if page > total:
            if page - total > max_new_pages:
                raise ValueError("内容太长：需要新增超过 %d 页，已中止" % max_new_pages)
            insert_blank_page(doc, total, page_w, page_h)
            total += 1
            refs = doc.page_refs()
            cache.pop(page, None)
        carry = spill

    new_line_aliases = set()
    for ln in lines:
        for (_c, a, _code, _w, _nb, _fb) in ln["runs"]:
            if a:
                new_line_aliases.add(a)
    font_src = _font_resources(doc, ctx["pref"])
    floor_alias = ctx.get("floor_alias") or FALLBACK_ALIAS
    font_src.setdefault(floor_alias, None)         

    taken = {}      
    give = {}       
    for (src, cut, dst, dy) in moved:
        if src in taken:
            continue
        ms = model_of(src)
        base = _rewrite_text(ms["content"],
                             {(g.op_index, g.char_index) for g in ms["ip"].glyphs},
                             {a: fi.code_bytes for a, fi in ms["ip"].fonts.items()})
        new_raw, picked = take_vectors(
            base, cut,
            ctx.get("shift_x0", 0.0), ctx.get("shift_x1", 1e9),
            y_min=bottom)
        taken[src] = (new_raw, picked)
        give[dst] = (picked, dy)

    cache_amap = {}     

    def _carry_font(doc_, dst_ref, dst_pg, src_pg, alias, model_of_, cache_):
        key = (dst_pg, src_pg, alias)
        if key in cache_:
            return cache_[key]
        ms = model_of_(src_pg)
        res = doc_.resolve(doc_.inherited(ms["ref"], "Resources")) or {}
        fonts = doc_.resolve(res.get("Font")) if isinstance(res, dict) else None
        fref = fonts.get(alias) if isinstance(fonts, dict) else None
        new_name = unique_font_alias(doc_, dst_ref, "%s_p%d" % (alias, src_pg))
        if fref is not None:
            _attach_font_resource(doc_, dst_ref, {new_name: fref})
        cache_[key] = new_name
        return new_name

    for (pg, placed, fixed) in plan:
        m = model_of(pg)
        ref = m["ref"]
        need = {}
        for a in new_line_aliases:
            v = font_src.get(a)
            if v is not None:
                need[a] = v
        if pg == page_no:
            need[floor_alias] = None               
        need = {k: v for k, v in need.items() if v is not None}
        if need:
            _attach_font_resource(doc, ref, need)

        cb = {a: fi.code_bytes for a, fi in m["ip"].fonts.items()}
        killed = {(g.op_index, g.char_index) for g in m["ip"].glyphs}

        dys = [yy - it["row"]["y"] for (it, yy) in placed if "row" in it]
        page_dy = dys[-1] if dys else 0.0
        pre_cleaned = pg in taken
        raw, _picked = taken.get(pg, (m["content"], []))
        if abs(page_dy) > 0.01 and raw.strip():
            y_cut = p["bottom"] if pg == page_no else page_h + 1.0
            raw, _sstat = shift_vectors(
                raw, y_cut, -page_dy,
                ctx.get("shift_x0", 0.0), ctx.get("shift_x1", 1e9),
                y_min=bottom, stretch=False)
        del_content = (raw if pre_cleaned
                       else _rewrite_text(raw, killed, cb)) if raw.strip() else b""

        items = []
        for r in fixed:
            items += _row_items([r], cb, dy=0.0)
        for (it, yy) in placed:
            if "row" in it:
                src = it.get("src", pg)
                if src == pg:
                    items += _row_items([it["row"]], cb, dy=(yy - it["row"]["y"]))
                else:
                    ms = model_of(src)
                    cbs = {a: fi.code_bytes for a, fi in ms["ip"].fonts.items()}
                    amap = {}
                    for g in it["row"]["g"]:
                        if g.font_alias not in amap:
                            amap[g.font_alias] = _carry_font(
                                doc, ref, pg, src, g.font_alias, model_of, cache_amap)
                    items += _row_items([it["row"]], cbs,
                                        dy=(yy - it["row"]["y"]), alias_map=amap)
            else:
                ln = it["line"]
                x = ln["x"]
                for (chr_, a, code, w, nb, _fb) in ln["runs"]:
                    if a is None or code is None:
                        x += w
                        continue
                    items.append((x, yy, a, size, _hex(code, nb)))
                    x += w
        ops = runs_to_ops(items)

        page_dict = doc.resolve(ref)
        streams = []
        if del_content:
            streams.append(doc.new_obj(({"Length": len(del_content)}, del_content)))
        streams.append(doc.new_obj(({"Length": len(ops)}, ops)))
        if pg in give:
            rects, gdy = give[pg]
            moved_ops = rect_ops(rects, gdy)
            if moved_ops:
                streams.append(doc.new_obj(
                    ({"Length": len(moved_ops)}, moved_ops)))
        page_dict["Contents"] = streams

    size_bytes = build_pdf(doc, {}, out_path)
    return {
        "ok": True,
        "size": size_bytes,
        "page": page_no,
        "para": para_index,
        "chars": len([c for c in new_text if c != "\n"]),
        "lines_before": len(p["rows"]),
        "lines_after": len(lines),
        "shift": 0.0,
        "pages_affected": len(plan),
        "cascade_pages": plan[-1][0] - page_no,
        "new_pages": total - total0,
        "last_page": plan[-1][0],
        "map_source": ctx["map_source"],
        "mapped_chars": len(ctx["extra_map"]),
        "map_conflicts": ctx["conflicts"],
        "fallback_chars": "".join(sorted(ctx["miss"])),
        "font": ctx["alias"],
    }
