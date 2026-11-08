
import os

from pdfedit import (PDFDocument, Ref, build_pdf, reorder_pages)
from pdfredact import (ContentStreamInterpreter, read_page_content)
from pdfpara import build_page_model, table_cells
from pdfreflow import (apply_paragraph_edit, apply_paragraph_flow,
                       char_of, page_rules)

MAX_EDITS_DEFAULT = 300

def open_doc(path):
    with open(path, "rb") as fp:
        return PDFDocument.load(fp.read())

def page_model(doc, page_no, refs=None):
    if refs is None:
        refs = doc.page_refs()
    if not (1 <= page_no <= len(refs)):
        raise ValueError("页码超出范围：%d（共 %d 页）" % (page_no, len(refs)))
    pref = refs[page_no - 1]
    content = read_page_content(doc, pref)
    resources = doc.resolve(doc.inherited(pref, "Resources"))
    ip = ContentStreamInterpreter(doc, resources)
    ip.run(content)
    rows, paras = build_page_model(ip.glyphs, rules=page_rules(content))
    return pref, content, ip, rows, paras

def _segments(paras, ch):
    out = []
    for pi, p in enumerate(paras):
        if p.get("table_like"):
            cells, _cols = table_cells(p)
            for cl in cells:
                gs = cl["glyphs"]
                out.append({
                    "para": pi, "cell": (cl["row"], cl["col"]),
                    "lines": ["".join(ch(g) for g in gs)],
                    "glyph_rows": [gs],
                    "x0": cl["x0"], "y0": cl["y0"],
                    "x1": cl["x1"], "y1": cl["y1"],
                    "table_like": True,
                })
        else:
            rows = p["rows"]
            out.append({
                "para": pi, "cell": None,
                "lines": ["".join(ch(g) for g in r["g"]) for r in rows],
                "glyph_rows": [r["g"] for r in rows],
                "x0": min(r["x0"] for r in rows),
                "y0": p["bottom"],
                "x1": max(r["x1"] for r in rows),
                "y1": rows[0]["y"] + p["size"],
                "table_like": False,
            })
    return out

def _flat(lines):
    s = "".join(lines)
    breaks = []
    acc = 0
    for t in lines[:-1]:
        acc += len(t)
        breaks.append(acc)
    return s, breaks

def _spans(text, term, ignore_case):
    hay = text
    needle = term
    if ignore_case:
        low_s, low_t = text.lower(), term.lower()
        if len(low_s) == len(text) and len(low_t) == len(term):
            hay, needle = low_s, low_t
    out = []
    at = hay.find(needle)
    while at != -1:
        out.append((at, at + len(needle)))
        at = hay.find(needle, at + len(needle))
    return out

def _rebuild(lines, spans, repl):
    s, breaks = _flat(lines)
    parts = []
    last = 0
    for a, b in spans:
        parts.append(s[last:a])
        parts.append(repl)
        last = b
    parts.append(s[last:])
    new_s = "".join(parts)

    def shift(bp):
        delta = 0
        for a, b in spans:
            if b <= bp:
                delta += len(repl) - (b - a)
            elif a < bp < b:
                return a + delta + len(repl)
        return bp + delta

    pos = [shift(b) for b in breaks]
    if not pos:
        return [new_s]
    out = []
    prev = 0
    for p in pos:
        p = max(prev, min(p, len(new_s)))
        out.append(new_s[prev:p])
        prev = p
    out.append(new_s[prev:])
    return out

def _char_map(glyph_rows, ch, breaks):
    out = []
    row_start = 0
    for i, gs in enumerate(glyph_rows):
        pos = row_start
        for g in gs:
            n = len(ch(g))
            if n:
                out.append((pos, pos + n, g))
            pos += n
        row_start = breaks[i] if i < len(breaks) else pos
    return out

def _merge_boxes(glyphs, tol=2.0):
    items = sorted(glyphs, key=lambda g: (-round(g.y0, 1), g.x0))
    out = []
    for g in items:
        if out:
            cur = out[-1]
            same_row = abs(cur["y0"] - g.y0) <= 2.0
            if same_row and g.x0 - cur["x1"] <= tol:
                cur["x1"] = max(cur["x1"], g.x1)
                cur["y0"] = min(cur["y0"], g.y0)
                cur["y1"] = max(cur["y1"], g.y1)
                continue
        out.append({"x0": g.x0, "y0": g.y0, "x1": g.x1, "y1": g.y1})
    return out

def find_text(src_path, term, ignore_case=False, first_page=1,
              last_page=None, max_hits=2000):
    if not isinstance(term, str) or not term:
        raise ValueError("查找内容不能为空")
    if len(term) > 500:
        raise ValueError("查找内容太长了（上限 500 字）")

    doc = open_doc(src_path)
    refs = doc.page_refs()
    total_pages = len(refs)
    if last_page is None or last_page > total_pages:
        last_page = total_pages
    first_page = max(1, int(first_page))
    if first_page > last_page:
        return {"hits": [], "pages": total_pages, "truncated": False}

    hits = []
    seq = 0
    for pg in range(first_page, last_page + 1):
        _pref, _c, ip, _rows, paras = page_model(doc, pg, refs)
        ch = char_of(ip)
        for seg in _segments(paras, ch):
            seg["seq"] = seq
            seq += 1
            text, breaks = _flat(seg["lines"])
            spans = _spans(text, term, ignore_case)
            if not spans:
                continue
            cmap = _char_map(seg["glyph_rows"], ch, breaks)
            boxes = []
            for a, b in spans:
                gs = [g for (s, e, g) in cmap if e > a and s < b]
                boxes.extend(_merge_boxes(gs) if gs else [])
            first = spans[0]
            lo = max(0, first[0] - 18)
            hi = min(len(text), first[1] + 18)
            hits.append({
                "page": pg, "para": seg["para"],
                "cell": list(seg["cell"]) if seg["cell"] else None,
                "seq": seg["seq"],
                "text": "\n".join(seg["lines"]),
                "count": len(spans),
                "boxes": [{"x0": round(b["x0"], 2), "y0": round(b["y0"], 2),
                           "x1": round(b["x1"], 2), "y1": round(b["y1"], 2)}
                          for b in boxes],
                "preview": ("…" if lo > 0 else "") + text[lo:hi] +
                           ("…" if hi < len(text) else ""),
                "table_like": seg["table_like"],
            })
            if len(hits) >= max_hits:
                return {"hits": hits, "pages": total_pages, "truncated": True}
    return {"hits": hits, "pages": total_pages, "truncated": False}

def _apply_one(src, out, page, para, text, cell, orig_text):
    try:
        return apply_paragraph_edit(src, out, page, para, text,
                                   orig_text=orig_text, cell=cell)
    except ValueError as exc:
        if cell is not None or "放不下" not in str(exc):
            raise
        return apply_paragraph_flow(src, out, page, para, text,
                                    orig_text=orig_text)

def replace_all(src_path, out_path, term, repl, ignore_case=False,
                targets=None, max_edits=MAX_EDITS_DEFAULT):
    if not isinstance(term, str) or not term:
        raise ValueError("查找内容不能为空")
    if not isinstance(repl, str):
        raise ValueError("替换内容无效")

    found = find_text(src_path, term, ignore_case)
    hits = found["hits"]
    if targets is not None:
        allow = set()
        for t in targets:
            if not isinstance(t, (list, tuple)) or len(t) != 3:
                continue
            cell = t[2]
            allow.add((int(t[0]), int(t[1]),
                       tuple(cell) if cell else None))
        hits = [h for h in hits if (h["page"], h["para"],
                                    tuple(h["cell"]) if h["cell"] else None)
                in allow]
    if not hits:
        raise ValueError("没有匹配到要替换的文字")

    hits.sort(key=lambda h: (h["page"], h["para"],
                             h["cell"] or [-1, -1]), reverse=True)

    cur = src_path
    tmp = out_path + ".part"
    edited = 0
    occurrences = 0
    skipped = []
    for h in hits:
        if edited >= max_edits:
            skipped.append("达到单次上限 %d 段，剩下的请再点一次" % max_edits)
            break
        cell = tuple(h["cell"]) if h["cell"] else None
        lines = h["text"].split("\n")
        spans = _spans("".join(lines), term, ignore_case)
        if not spans:
            continue
        new_lines = _rebuild(lines, spans, repl)
        new_text = "\n".join(new_lines)
        if new_text == h["text"]:
            continue
        try:
            _apply_one(cur, tmp, h["page"], h["para"], new_text, cell,
                       h["text"])
            os.replace(tmp, out_path)
        except Exception as exc:
            skipped.append("第 %d 页第 %d 段：%s" %
                           (h["page"], h["para"] + 1, exc))
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            continue
        cur = out_path
        edited += 1
        occurrences += h["count"]

    if edited == 0:
        raise ValueError("没有成功替换任何文字" +
                         ("：" + "；".join(skipped[:3]) if skipped else ""))
    return {
        "edits": edited,
        "occurrences": occurrences,
        "skipped": skipped,
        "pages": found["pages"],
        "truncated": found["truncated"],
    }

def _locate_cell(cur_path, page, orig_text):
    _pref, _c, ip, rows, paras = page_model(open_doc(cur_path), page)
    ch = char_of(ip)
    for pi, p in enumerate(paras):
        if not p.get("table_like"):
            continue
        cells, _cols = table_cells(p)
        for cl in cells:
            if "".join(ch(g) for g in cl["glyphs"]) == orig_text:
                return pi, (cl["row"], cl["col"])
    return None, None

def scan_terms(src_path, terms, ignore_case=False, max_hits=4000):
    words = []
    for t in terms or []:
        if isinstance(t, str) and t.strip():
            w = t.strip()
            if len(w) > 200:
                raise ValueError("单个关键词太长（上限 200 字）")
            if w not in words:
                words.append(w)
    if not words:
        raise ValueError("没有有效的关键词")

    doc = open_doc(src_path)
    refs = doc.page_refs()
    per_term = {w: 0 for w in words}
    segs = {}

    for pg in range(1, len(refs) + 1):
        _pref, _c, ip, _rows, paras = page_model(doc, pg, refs)
        ch = char_of(ip)
        for seg in _segments(paras, ch):
            text, _breaks = _flat(seg["lines"])
            if not text:
                continue
            hit_words = []
            for w in words:
                sp = _spans(text, w, ignore_case)
                if sp:
                    per_term[w] += len(sp)
                    hit_words.append(w)
            if not hit_words:
                continue
            cell = tuple(seg["cell"]) if seg["cell"] else None
            key = (pg, seg["para"], cell)
            slot = segs.get(key)
            if slot is None:
                segs[key] = {
                    "page": pg,
                    "para": seg["para"],
                    "cell": cell,
                    "text": "\n".join(seg["lines"]),
                    "count": len(_spans(text, hit_words[0], ignore_case)),
                    "words": set(hit_words),
                }
            else:
                slot["words"].update(hit_words)
            if len(segs) >= max_hits:
                return segs, per_term, len(refs)

    return segs, per_term, len(refs)

def delete_terms(src_path, out_path, terms, ignore_case=False,
                 max_edits=40, targets=None):
    segs, per_term, total_pages = scan_terms(src_path, terms, ignore_case)
    if targets is not None:
        allow = set()
        for t in targets:
            if not isinstance(t, (list, tuple)) or len(t) != 3:
                continue
            cell = t[2]
            allow.add((int(t[0]), int(t[1]),
                       tuple(cell) if cell else None))
        segs = {k: v for k, v in segs.items() if k in allow}
    if not segs:
        raise ValueError("没有匹配到要删除的文字")

    order = sorted(segs.items(),
                   key=lambda kv: (kv[0][0], kv[0][1],
                                   kv[0][2] or (-1, -1)),
                   reverse=True)

    cur = src_path
    tmp = out_path + ".part"
    edited = 0
    removed = 0
    skipped = []

    for key, slot in order:
        if edited >= max_edits:
            skipped.append("达到单次上限 %d 段，剩下的请再点一次" % max_edits)
            break
        lines = slot["text"].split("\n")
        flat, _breaks = _flat(lines)
        spans = []
        for w in slot["words"]:
            spans.extend(_spans(flat, w, ignore_case))
        if not spans:
            continue
        spans.sort()
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        new_text = "\n".join(_rebuild(lines, merged, ""))
        if new_text == slot["text"]:
            continue
        para_now, cell_now = slot["para"], slot["cell"]
        if cell_now is not None:
            para_now, cell_now = _locate_cell(cur, slot["page"], slot["text"])
            if cell_now is None:
                skipped.append("第 %d 页：找不到要改的那一格了（表格结构变了）"
                               % slot["page"])
                continue
        try:
            _apply_one(cur, tmp, slot["page"], para_now, new_text,
                       cell_now, slot["text"])
            os.replace(tmp, out_path)
        except Exception as exc:
            skipped.append("第 %d 页第 %d 段：%s" %
                           (slot["page"], slot["para"] + 1, exc))
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            continue
        cur = out_path
        edited += 1
        removed += len(merged)

    if edited == 0:
        raise ValueError("没有成功删除任何文字" +
                         ("：" + "；".join(skipped[:3]) if skipped else ""))

    residue = {}
    for w in per_term:
        try:
            residue[w] = len(find_text(out_path, w, ignore_case)["hits"])
        except Exception:
            residue[w] = -1

    return {
        "edits": edited,
        "removed": removed,
        "total": len(order),
        "remaining": max(0, len(order) - edited - len(skipped)),
        "per_term": per_term,
        "residue": residue,
        "missing": [w for w, n in per_term.items() if n == 0],
        "skipped": skipped,
        "pages": total_pages,
    }

def term_regions(src_path, terms, ignore_case=True, mode="term", inset=0.02,
                 max_regions=4000):
    if not isinstance(terms, (list, tuple)):
        raise ValueError("terms 必须是数组")
    if mode not in ("term", "line", "para"):
        raise ValueError("mode 只能是 term / line / para")

    words = []
    for t in terms:
        if isinstance(t, str) and t.strip():
            w = t.strip()
            if len(w) > 200:
                raise ValueError("单个关键词太长（上限 200 字）")
            if w not in words:
                words.append(w)
    if not words:
        raise ValueError("没有有效的关键词")

    doc = open_doc(src_path)
    refs = doc.page_refs()
    regions = []
    seen = set()
    per_term = {w: 0 for w in words}

    for pg in range(1, len(refs) + 1):
        _pref, _c, ip, _rows, paras = page_model(doc, pg, refs)
        ch = char_of(ip)
        for seg in _segments(paras, ch):
            text, breaks = _flat(seg["lines"])
            if not text:
                continue

            hit_glyphs = set()
            cmap = None
            for w in words:
                spans = _spans(text, w, ignore_case)
                if not spans:
                    continue
                if cmap is None:
                    cmap = _char_map(seg["glyph_rows"], ch, breaks)
                for a, b in spans:
                    gs = [g for (s, e, g) in cmap if e > a and s < b]
                    if not gs:
                        continue
                    per_term[w] += 1
                    hit_glyphs.update(id(g) for g in gs)
            if not hit_glyphs:
                continue

            if mode == "para":
                gs = [g for row in seg["glyph_rows"] for g in row]
            elif mode == "line":
                gs = []
                for row in seg["glyph_rows"]:
                    if any(id(g) in hit_glyphs for g in row):
                        gs.extend(row)
            else:
                gs = [g for row in seg["glyph_rows"] for g in row
                      if id(g) in hit_glyphs]

            for box in _merge_boxes(gs):
                x0 = box["x0"] + inset
                y0 = box["y0"] + inset
                wd = (box["x1"] - box["x0"]) - inset * 2
                ht = (box["y1"] - box["y0"]) - inset * 2
                if wd <= 0.05 or ht <= 0.05:
                    continue
                key = (pg, round(x0, 2), round(y0, 2),
                       round(wd, 2), round(ht, 2))
                if key in seen:
                    continue
                seen.add(key)
                regions.append({
                    "page": pg,
                    "x": round(x0, 3),
                    "y": round(y0, 3),
                    "w": round(wd, 3),
                    "h": round(ht, 3),
                })
                if len(regions) >= max_regions:
                    return regions, per_term
    return regions, per_term

def parse_pages(spec, total):
    if spec is None:
        return list(range(1, total + 1))
    s = str(spec).strip()
    if not s:
        return list(range(1, total + 1))
    out = []
    for part in s.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a = a.strip()
            b = b.strip()
            try:
                start = int(a) if a else 1
                end = int(b) if b else total
            except ValueError:
                raise ValueError("页码格式看不懂：%s" % part)
            if start > end:
                start, end = end, start
            for p in range(start, end + 1):
                if 1 <= p <= total:
                    out.append(p)
        else:
            try:
                p = int(part)
            except ValueError:
                raise ValueError("页码格式看不懂：%s" % part)
            if 1 <= p <= total:
                out.append(p)
    if not out:
        raise ValueError("页码范围里没有有效页（本文档共 %d 页）" % total)
    return out

def export_pages(src_path, out_path, pages):
    doc = open_doc(src_path)
    refs = doc.page_refs()
    total = len(refs)
    order = []
    for p in pages:
        if not isinstance(p, int):
            try:
                p = int(p)
            except (TypeError, ValueError):
                continue
        if 1 <= p <= total:
            order.append(p - 1)
    if not order:
        raise ValueError("没有有效的页码（本文档共 %d 页）" % total)
    reorder_pages(doc, order)
    build_pdf(doc, {}, out_path)
    return {"pages": len(order), "total": total}

def split_document(src_path, out_dir, per=1, base_name=None):
    try:
        per = int(per)
    except (TypeError, ValueError):
        raise ValueError("拆分份数无效")
    if per < 1:
        raise ValueError("每份至少 1 页")
    doc = open_doc(src_path)
    total = len(doc.page_refs())
    if total == 0:
        raise ValueError("这份文档没有页面")
    if per >= total:
        raise ValueError("每份 %d 页 ≥ 总页数 %d，不需要拆分" % (per, total))

    stem = base_name or os.path.splitext(os.path.basename(src_path))[0]
    stem = "".join(c for c in stem if c not in '\\/:*?"<>|')[:60] or "文档"
    os.makedirs(out_dir, exist_ok=True)

    made = []
    idx = 0
    for start in range(1, total + 1, per):
        idx += 1
        pages = list(range(start, min(start + per, total + 1)))
        out = os.path.join(out_dir, "%s-第%d部分（第%d-%d页）.pdf" %
                           (stem, idx, pages[0], pages[-1]))
        n = 1
        while os.path.exists(out):
            n += 1
            out = os.path.join(out_dir, "%s-第%d部分（第%d-%d页）-%d.pdf" %
                               (stem, idx, pages[0], pages[-1], n))
        export_pages(src_path, out, pages)
        made.append({"path": out, "name": os.path.basename(out),
                     "pages": len(pages)})
    return made

def _rewrite_refs(val, offset):
    if isinstance(val, Ref):
        return Ref(val[0] + offset, val[1])
    if isinstance(val, list):
        return [_rewrite_refs(v, offset) for v in val]
    if isinstance(val, dict):
        return {k: _rewrite_refs(v, offset) for k, v in val.items()}
    if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], dict):
        return (_rewrite_refs(val[0], offset), val[1])
    return val

def merge_documents(paths, out_path):
    if not isinstance(paths, (list, tuple)) or len(paths) < 2:
        raise ValueError("合并至少需要两份文档")

    base = open_doc(paths[0])
    if not base.pages_ref:
        raise ValueError("第一份文档没有页面树")
    pages_node = base.resolve(base.pages_ref)
    if not isinstance(pages_node, dict):
        raise ValueError("第一份文档的页面树无法解析")
    kids = list(pages_node.get("Kids") or [])
    count = len(base.page_refs())

    for path in paths[1:]:
        other = open_doc(path)
        refs = other.page_refs()
        if not refs:
            continue
        offset = base.max_obj

        for ref in refs:
            page = other.resolve(ref)
            if not isinstance(page, dict):
                continue
            for key in ("MediaBox", "CropBox", "Resources", "Rotate"):
                if key not in page:
                    val = other.inherited(ref, key)
                    if val is not None:
                        page[key] = val

        for (num, gen), val in list(other.objects.items()):
            base.objects[(num + offset, gen)] = _rewrite_refs(val, offset)
        base.max_obj = max(base.max_obj, other.max_obj + offset)

        for ref in refs:
            new_ref = Ref(ref[0] + offset, ref[1])
            page = base.resolve(new_ref)
            if isinstance(page, dict):
                page["Parent"] = base.pages_ref
            kids.append(new_ref)
            count += 1

    pages_node["Kids"] = kids
    pages_node["Count"] = count
    build_pdf(base, {}, out_path)
    return {"pages": count, "sources": len(paths)}
