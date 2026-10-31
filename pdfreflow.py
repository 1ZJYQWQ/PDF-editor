import struct

from pdfedit import build_cjk_font, _attach_font_resource
from pdfredact import (PDFDocument, ContentStreamInterpreter,
                       read_page_content, _rewrite_text)
from pdfpara import build_rows, build_paragraphs

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
    rows = build_rows(ip.glyphs)
    paras = build_paragraphs(rows)
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


def char_runs(text, fi, alias, size, subset, extra=None, fonts=None):
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
            out.append((ch, FALLBACK_ALIAS, g, w, 2, True))
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
                if last_space > 0:
                    push(cur[:last_space + 1], cur_x, cut=True)
                    rest = cur[last_space + 1:]
                    cur = rest
                    cur_x = cont_x
                    x = cont_x + sum(r[3] for r in rest)
                    last_space = -1
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


def _row_items(rows, code_bytes, dy=0.0):
    items = []
    for r in rows:
        for g in r["g"]:
            items.append((g.x0, g.y0 + dy, g.font_alias,
                          g.y1 - g.y0, _hex(g.code, code_bytes.get(g.font_alias, 1))))
    return items



def _prepare_edit(src_path, page_no, para_index, new_text,
                  orig_text=None, page_text=None):
    doc, pref, content, ip, rows, paras = load_page_model(src_path, page_no)
    if not (0 <= para_index < len(paras)):
        raise ValueError("段落下标超出范围：%d（本页共 %d 段）"
                         % (para_index, len(paras)))

    p = paras[para_index]
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
    if len(p["rows"]) > 1:
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
    if miss:
        cjk_ref, subset = build_cjk_font(doc, {ord(c) for c in miss})
        _attach_font_resource(doc, pref, {FALLBACK_ALIAS: cjk_ref})
        subset = subset

    runs = char_runs(new_text, fi, alias, size, subset,
                    extra=extra_map, fonts=ip.fonts)
    lines = wrap_runs(runs, first_x, cont_x, right, tol=max(0.5, size * 0.5))
    return {
        "doc": doc, "pref": pref, "content": content, "ip": ip,
        "rows": rows, "paras": paras, "p": p,
        "code_bytes": code_bytes, "alias": alias, "fi": fi,
        "size": size, "first_x": first_x, "cont_x": cont_x, "right": right,
        "gap": gap, "lines": lines, "subset": subset, "miss": miss,
        "extra_map": extra_map, "map_source": map_source,
        "conflicts": conflicts,
    }


def apply_paragraph_edit(src_path, out_path, page_no, para_index, new_text,
                         bottom_limit=24.0, orig_text=None, page_text=None):
    ctx = _prepare_edit(src_path, page_no, para_index, new_text,
                        orig_text=orig_text, page_text=page_text)
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

    body_rows = [r for r in rows if r["y"] >= bottom_limit - 1]
    below = [r for r in body_rows if r["y"] < p["bottom"] - 0.5]
    lowest = min((r["y"] for r in body_rows), default=bottom_limit)
    avail = lowest - bottom_limit          
    if delta > avail + 1e-6:
        raise ValueError(
            "这一段放不下：重排后本页还需下移 %.1fpt，但页底只剩 %.1fpt。\n"
            "跨页回流（把溢出的内容续到下一页）尚未实现，"
            "请缩短这段文字，或先手动在末尾插入空白页再分段编辑。"
            % (delta, max(0.0, avail)))

    own = {id(r) for r in p["rows"]}
    items = []
    for r in rows:
        if id(r) in own:
            continue                                   
        if r["y"] < bottom_limit - 1:
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
        out.append({
            "index": i,
            "text": _ptext if _ptext.strip() else "",
            "lines": len(p["rows"]),
            "size": round(p["size"], 2),
            "gap": round(p["gap"], 2),
            "font": dominant_font(p),
            "needs_text": not bool(_ptext.strip()),
            "x0": round(min(r["x0"] for r in p["rows"]), 2),
            "y0": round(p["bottom"], 2),
            "x1": round(max(r["x1"] for r in p["rows"]), 2),
            "y1": round(p["rows"][0]["y"] + p["size"], 2),
        })
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
                         bottom_ratio=0.05, max_new_pages=20):
    from pdfedit import build_pdf, insert_blank_page

    ctx = _prepare_edit(src_path, page_no, para_index, new_text,
                        orig_text=orig_text, page_text=page_text)
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
                    prev_y = para_top_y - (len(lines) - 1) * gap
                continue
            g = (prev_y - r["y"]) if prev_y is not None else default_gap
            out.append({"row": r, "gap": g})
            prev_y = r["y"]
        return out, fixed

    top_default = max((r["y"] for r in rows), default=page_h - bottom)

    page = page_no
    carry = []
    plan = []                     
    while True:
        m = model_of(page)
        items, fixed = items_for(page)
        if carry:
            items = [dict(it) for it in carry] + items

        top = max((r["y"] for r in m["rows"]), default=top_default)
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
    font_src.setdefault(FALLBACK_ALIAS, None)      

    for (pg, placed, fixed) in plan:
        m = model_of(pg)
        ref = m["ref"]
        need = {}
        for a in new_line_aliases:
            v = font_src.get(a)
            if v is not None:
                need[a] = v
        if pg == page_no:
            need[FALLBACK_ALIAS] = None            
        need = {k: v for k, v in need.items() if v is not None}
        if need:
            _attach_font_resource(doc, ref, need)

        cb = {a: fi.code_bytes for a, fi in m["ip"].fonts.items()}
        killed = {(g.op_index, g.char_index) for g in m["ip"].glyphs}
        del_content = _rewrite_text(m["content"], killed, cb) if m["content"].strip() else b""

        items = []
        for r in fixed:
            items += _row_items([r], cb, dy=0.0)
        for (it, yy) in placed:
            if "row" in it:
                items += _row_items([it["row"]], cb, dy=(yy - it["row"]["y"]))
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
        "new_pages": total - total0,
        "last_page": plan[-1][0],
        "map_source": ctx["map_source"],
        "mapped_chars": len(ctx["extra_map"]),
        "map_conflicts": ctx["conflicts"],
        "fallback_chars": "".join(sorted(ctx["miss"])),
        "font": ctx["alias"],
    }
