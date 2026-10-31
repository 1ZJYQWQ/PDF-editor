# -*- coding: utf-8 -*-
"""
段落改字与页内重排（Stage B）。

解决什么：
  原来是「原位替换」——只改字，不重排，字一变长就压到邻行。
  这里做的是：把目标段落的新文本**按原段宽度重新折行**，行数变多就把
  该段之后的内容整体下移，行数变少就上移。

边界（有意为之）：
  · 只在**本页内**推进；溢出页底就明确报错，不做跨页级联（那是 Stage C）。
  · 只处理横排文字（没有旋转字形）；竖排/表格不做。
  · 字体优先用**源字体**：源字体（嵌入子集）里有这个字，就直接写原字码，
    字形和宽度都和原文一致；没有再回退到项目自带 CJK 字体，并在结果里
    报告用了哪些回退字符 —— 静默换字体是最容易让人"看着不对却说不上来"的坑。
"""

import struct

from pdfedit import build_cjk_font, _attach_font_resource
from pdfredact import (PDFDocument, ContentStreamInterpreter,
                       read_page_content, _rewrite_text)
from pdfpara import build_rows, build_paragraphs

FALLBACK_ALIAS = "CJKfloor"      # 回退字体在页面 Resources 里的别名


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------

def load_page_model(path, page_no=1):
    """读一页并建好 行/段 模型。返回 (doc, pref, content, ip, rows, paras)。"""
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
    """字形 -> 字符（靠 /ToUnicode）。"""
    def f(g):
        fi = ip.fonts.get(g.font_alias)
        if not fi:
            return ""
        return fi.to_unicode.get(g.code, "")
    return f


def _rev_map(fi):
    """字体里 字符 -> 原字码（反过来查，用于"改字时优先复用源字体"）。"""
    rev = getattr(fi, "_rev", None)
    if rev is None:
        rev = {}
        for code, ch in (fi.to_unicode or {}).items():
            if ch and ch not in rev:      # 同字多码时取第一个
                rev[ch] = code
        fi._rev = rev
    return rev


def para_text(p, ch):
    """
    段落原文。**必须按行用 \n 连接**。

    逐字形拼成一整串的后果（实测）：
      · 段落编辑器里看到的是"一长串"，完全看不出原来有几行；
      · 行边界丢失后，即使原样提交也会被当成一整段重新折行，
        断行点跟原文不一样 ——"我什么都没改，排版却变了"。
    """
    return "\n".join("".join(ch(g) for g in r["g"]) for r in p["rows"])


def dominant_font(p):
    """段落用的主字体（按字形数投票）。"""
    n = {}
    for r in p["rows"]:
        for g in r["g"]:
            n[g.font_alias] = n.get(g.font_alias, 0) + 1
    return max(n, key=n.get) if n else ""


# --------------------------------------------------------------------------
# 折行
# --------------------------------------------------------------------------

def solid_glyphs(p):
    """
    段落里的字形序列（阅读顺序），剔除"极窄"的占位字形。

    ★ 阈值只取 0.15 字高：连字符(≈0.33)、引号(≈0.44) 都是**真字符**，
      按 0.35 判空格会把它们误删（实测：136 字被当成 135 个字形）。
      真正需要剔的只有零宽/亚像素级的占位。
    """
    size = p["size"] or 12.0
    out = []
    for r in p["rows"]:
        for g in r["g"]:
            if (g.x1 - g.x0) > 0.15 * size:
                out.append(g)
    return out


def build_aligned_map(p, orig_text):
    """
    用「前端解出来的段落原文」和「字形序列」对齐，反推 字符 -> (字体, 字码)。

    这是给**没有 /ToUnicode 的文档**补映射用的：
      pdf.js 在浏览器里已经能把文字解对（用户能选中、能复制），
      把这段文字按字对齐到字形上，就等价于得到了一张 ToUnicode 的反表。

    对齐策略（依次尝试，全部要**严格等长**才接受）：
      1. 字形数 == 原文去掉空白后的字数      （最常见：中文正文没有空格字形）
      2. 字形数 == 原文总字数（含空格）      （原文里的空格对应空格字形）
      3. 再试一次剔除极窄字形后的组合       （排版里有零宽占位的情况）

    都不成立就拒绝 —— 猜错一个字，整段就写歪了。
    """
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
                conflicts += 1      # 同一个字对应多个字码：保留第一个
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
    """
    把文本拆成 [(ch, alias, code, width, nbytes, is_fallback)]。

    extra: {字符: (字体别名, 字码)} —— 从对齐推出来的映射，优先级最高。
    fonts: {别名: FontInfo}，取宽度用。
    """
    rev = _rev_map(fi)
    out = []
    for ch in text:
        if ch == "\n":
            out.append((ch, None, None, 0.0, 0, False))
            continue
        # 1) 对齐推出来的映射（无 ToUnicode 时的主要来源）
        if extra:
            hit = extra.get(ch)
            if hit is not None:
                a, code = hit
                fi2 = (fonts or {}).get(a) or fi
                w = fi2.width_of(code) / 1000.0 * size
                out.append((ch, a, code, w, fi2.code_bytes, False))
                continue
        # 2) 源字体自带的 ToUnicode
        code = rev.get(ch)
        if code is not None:
            w = fi.width_of(code) / 1000.0 * size
            out.append((ch, alias, code, w, fi.code_bytes, False))
            continue
        # 3) 回退字体
        g = None
        if subset is not None:
            g = subset.gid_of.get(ord(ch))
        if g is not None:
            upem = float(subset.upem or 1000)
            w = subset.widths.get(g, upem) / upem * size
            out.append((ch, FALLBACK_ALIAS, g, w, 2, True))
            continue
        # 4) 谁都排不了（例如字体和回退字体都没有的字符）：占个宽度，别挤在一起
        if ch.isspace():
            out.append((ch, alias, 32, size * 0.5, fi.code_bytes, True))
        else:
            out.append((ch, None, None, size * 0.5, 0, True))
    return out


def wrap_runs(runs, first_x, cont_x, right, tol=0.5):
    """
    折行。返回 [{"runs": [...], "x": 该行起点}]。

    ★ 分成两级，顺序很重要：
      ① **先按显式 \\\\n 切成"硬行"** —— 原文的断行必须原样保留；
      ② 只有硬行自己**超出右边距**时才在行内软折（用户粘了一大段才会走到这里）。

    为什么不能边扫边折（旧实现）：某行宽度正好等于右边距时，
    浮点累加会超出 0.05 的容差 → 多折一次 → **原样提交行数却变了**，
    用户看到的就是"我什么都没改，排版却动了"。硬行优先能彻底消除这个抖动。

    · 首行用 first_x（可能缩进），续行用 cont_x（左边距）；
    · 软折时拉丁优先断在空格处，中文按字断。

    tol 是软折容差。原文行是按它自己的字距（Tc/Tz）排的，
    我们用"自然字宽"重算会差个 1~2pt —— 容差必须大于这个差，
    否则每行都会在行尾多折一个字出来（实测：5 行段变成 6 行）。
    """
    # ---- ① 切成硬行（\n 是硬边界，空行也要保留）----
    hard = [[]]
    for r in runs:
        if r[0] == "\n":
            hard.append([])
        else:
            hard[-1].append(r)

    lines = []

    def push(cur, x, cut=False):
        # cut=True 表示"这行是因为超出右界被切开的"，只有这种行才该两端对齐；
        # 原文自带的硬行要保留它自己的宽度（否则短行会被撑成"一 个 字 一 个 字"）。
        lines.append({"runs": cur, "x": x, "cut": cut})

    # ---- ② 每个硬行内部再软折 ----
    for hi, hr in enumerate(hard):
        start_x = first_x if hi == 0 else cont_x
        if not hr:
            # 空行：占一行（没有 runs），保留原文档的空行
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
                    # 断在空格处：空格留在上一行末尾
                    push(cur[:last_space + 1], cur_x, cut=True)
                    rest = cur[last_space + 1:]
                    cur = rest
                    cur_x = cont_x
                    x = cont_x + sum(r[3] for r in rest)
                    last_space = -1
                else:
                    # 中文没法断词，直接按字断
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
    """
    把一行的**字距**按比例压到 right 以内（字形本身不变）。

    为什么需要：折行判定留了半字容差（否则原文行会因 1~2pt 的字距差被误折），
    代价是行尾可能超出 right 一两磅。这里把它压回来 ——
    只动"下一个字的起点"，不动字形宽度，二十个字分摊 1~2pt 完全看不出来。
    """
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
    """
    两端对齐：把一行撑满到 right（末行不撑）。

    中文排版默认两端对齐，原文也是 —— 不撑的话编辑后行尾会参差，
    一眼就能看出"这段被人动过"。按字平均分配多出来的宽度即可。
    """
    runs = line["runs"]
    if not runs:
        return runs
    used = sum(r[3] for r in runs)
    room = right - (line["x"] + used)
    if room <= 0.2:
        return runs
    # 末尾是空格就不撑（避免把空格撑得很宽）
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


# --------------------------------------------------------------------------
# 写出
# --------------------------------------------------------------------------

def _hex(code, nbytes):
    return "%0*X" % (max(1, nbytes) * 2, int(code) & ((1 << (8 * max(1, nbytes))) - 1))


def runs_to_ops(items):
    """items: [(x, y, alias, size, hexstring)] -> 文本流指令。"""
    # ★ 必须显式设填充色。
    #   PDF 的 /Contents 数组是"拼接成一个流"来解释的，图形状态跨流延续；
    #   q 只保存状态、不重置颜色 —— 不写这一句，新文字会继承上一个流末尾的
    #   填充色（实测原页画灰线用的 0.6 0.6 0.6），整段字变成浅灰。
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
    """把既有行按原位置（可选整体位移）转成 items。"""
    items = []
    for r in rows:
        for g in r["g"]:
            items.append((g.x0, g.y0 + dy, g.font_alias,
                          g.y1 - g.y0, _hex(g.code, code_bytes.get(g.font_alias, 1))))
    return items


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _prepare_edit(src_path, page_no, para_index, new_text,
                  orig_text=None, page_text=None):
    """
    改字前的准备：读页 -> 建模型 -> 定字体/折行/字符映射/回退字体。

    抽出来的原因：页内重排（apply_paragraph_edit）和跨页级联（apply_paragraph_flow）
    前半段完全一样，只有"怎么摆"不同。重复一份迟早会改漏一边。
    """
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

    # ★ 源字体没有 /ToUnicode 时：不再直接放弃 ——
    #   如果调用方把「这段原文」（前端 pdf.js 解出来的）一起传进来，
    #   就按字对齐反推「字符 -> 字码」，等价于给这份文档补了一张 ToUnicode 反表。
    #   对不上就明确拒绝，绝不猜（猜错一个字，整段就写歪了）。
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

        # 段落级映射只覆盖"这一段出现过的字"。新文字里没出现过的字
        # （比如换个说法、加个新词）还会掉到回退字体。
        # 如果前端把**整页文字**也传上来了，就再做一次页级对齐当底表 ——
        # 覆盖范围扩到整页，而段落级结果仍然优先（它更精确）。
        if page_text and page_text.strip():
            try:
                psize = p["size"] or 12.0
                pmap, _pc = build_aligned_map(
                    {"rows": rows, "size": psize}, page_text)
            except ValueError:
                pmap = {}
            if pmap:
                merged = dict(pmap)
                merged.update(extra_map)       # 段落级覆盖页级
                extra_map = merged
                map_source = "aligned-page"

    size = p["size"]
    first_x = p["rows"][0]["x0"]                      # 首行位置（可能缩进）
    cont_x = min(r["x0"] for r in p["rows"])          # 续行回到左边距
    # 折行右界：多行段用它自己的最大行宽（= 右页边，最后一行的短尾不算）；
    # 单行段没有参考，退回全页最大行宽。
    if len(p["rows"]) > 1:
        right = max(r["x1"] for r in p["rows"])
    else:
        right = max((r["x1"] for r in rows), default=0.0)
    gap = p["gap"] or (rows[0]["y"] - rows[1]["y"] if len(rows) > 1 else size * 1.6)
    if gap <= 0:
        gap = size * 1.4

    # ---- 源字体缺哪些字？缺的用回退字体补（先建子集，再折行取宽度）----
    #   extra_map 里的字已经在源字体里（对齐推出来的），不算缺。
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
    # 容差 ≈ 半个字宽：吸收"原文行按自身字距排版"与"自然字宽"的差（实测 1~2pt），
    # 又不至于让用户明显加长的行赖着不折。超出的部分由 _fit() 压回，不越界。
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
    """
    把第 page_no 页的第 para_index 段（0 起）改成 new_text 并重排。

    返回统计：改了哪段、行数怎么变、下移多少、哪些字用了回退字体。
    溢出页底时抛 ValueError（明确说明还差多少），不静默截断。
    """
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
    delta = (new_n - old_n) * gap        # 正数 = 后面的内容下移

    # ---- 页底检查：宁可拒绝，也不要让文字掉出页面 ----
    # ★ 页脚（页码那行）**不算正文**：它固定在页面底部，不跟着内容下移。
    #   把它算进来会得到一个荒唐的结论 —— "页底只剩 0pt"，
    #   于是每次改段落都被判定"放不下"而升级成跨页，把页脚一起搬到下一页。
    body_rows = [r for r in rows if r["y"] >= bottom_limit - 1]
    below = [r for r in body_rows if r["y"] < p["bottom"] - 0.5]
    lowest = min((r["y"] for r in body_rows), default=bottom_limit)
    avail = lowest - bottom_limit          # 页底还剩多少空间（pt）
    if delta > avail + 1e-6:
        raise ValueError(
            "这一段放不下：重排后本页还需下移 %.1fpt，但页底只剩 %.1fpt。\n"
            "跨页回流（把溢出的内容续到下一页）尚未实现，"
            "请缩短这段文字，或先手动在末尾插入空白页再分段编辑。"
            % (delta, max(0.0, avail)))

    # ---- 组装整页指令：段前原样、段内重排、段后整体位移 ----
    own = {id(r) for r in p["rows"]}
    items = []
    for r in rows:
        if id(r) in own:
            continue                                   # 目标段稍后重画
        if r["y"] < bottom_limit - 1:
            dy = 0.0                                   # 页脚：原地不动
        elif r["y"] < p["bottom"] - 0.5:
            dy = -delta                                # 段落以下的正文：跟着平移
        else:
            dy = 0.0
        items += _row_items([r], code_bytes, dy=dy)

    # 目标段：从原首行基线开始，按 gap 往下排；除末行外两端对齐
    y = p["rows"][0]["y"]
    for li, ln in enumerate(lines):
        ln_runs = ln["runs"]
        # 只对齐"被软折切开"的行；原文硬行保持自身宽度
        if li < len(lines) - 1 and ln.get("cut"):
            ln_runs = justify(ln, right)
        # 折行留了半字容差，行尾可能超出一点 —— 压回来，保证不越过右界
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

    # ---- 删掉原页所有字形（图形/图片等非文字指令原样保留），再追加重建流 ----
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
        # 和跨页版本保持同一套字段，调用方不用分支
        "pages_affected": 1,
        "new_pages": 0,
        "last_page": page_no,
        "font": alias,
        # 字符映射是怎么来的：tounicode=源字体自带；aligned=按原文对齐反推
        "map_source": map_source,
        "mapped_chars": len(extra_map),
        "map_conflicts": conflicts,
        "fallback_chars": "".join(sorted(miss)),
    }


def list_paragraphs(src_path, page_no=1):
    """给前端用：列出某页的段落（含 PDF 坐标框与原文）。"""
    doc, pref, content, ip, rows, paras = load_page_model(src_path, page_no)
    ch = char_of(ip)
    out = []
    for i, p in enumerate(paras):
        _ptext = para_text(p, ch)
        out.append({
            "index": i,
            # 按行拼（行间 \n）。整段都解不出字时归一化成空串 ——
            # 否则会返回 "\n\n\n"（看起来"有文本"，其实一个字都没有），
            # 前端和 needs_text 的判定会互相打架。
            "text": _ptext if _ptext.strip() else "",
            "lines": len(p["rows"]),
            "size": round(p["size"], 2),
            "gap": round(p["gap"], 2),
            "font": dominant_font(p),
            # 没有 ToUnicode 时 text 是空的：前端要自己解出原文再传回来
            "needs_text": not bool(_ptext.strip()),
            # PDF 坐标（左下原点）：x0,y0 左下角；x1,y1 右上角
            "x0": round(min(r["x0"] for r in p["rows"]), 2),
            "y0": round(p["bottom"], 2),
            "x1": round(max(r["x1"] for r in p["rows"]), 2),
            "y1": round(p["rows"][0]["y"] + p["size"], 2),
        })
    return {"ok": True, "page": page_no, "count": len(out), "paragraphs": out}


# --------------------------------------------------------------------------
# 跨页级联（Stage C）
# --------------------------------------------------------------------------

def _font_resources(doc, pref):
    """取页面 Resources/Font 里的 {别名: 字体资源值}，跨页时要补挂字体。"""
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
    """
    跨页级联重排：段落变长放不下时，溢出的内容**续到下一页**，再溢出就再往后推。

    做法（把受影响的那几页当成一条竖直的"文字流"）：
      1. 从目标页开始，按原顺序收集每一行（目标段落换成新排的行）；
      2. 从上往下摆：摆到页底就换页，继续从下一页的顶部摆；
      3. 摆不下就新建空白页；
      4. 只重写**真正被触及**的页（下一页没被推动就停），图形/图片原样保留。

    局限（有意）：只搬文字，图片/图形留在原页不动；表格里的行会被当成普通行。
    """
    from pdfedit import build_pdf, insert_blank_page

    ctx = _prepare_edit(src_path, page_no, para_index, new_text,
                        orig_text=orig_text, page_text=page_text)
    doc, ip, rows, p = ctx["doc"], ctx["ip"], ctx["rows"], ctx["p"]
    code_bytes, size, gap, right = (ctx["code_bytes"], ctx["size"],
                                    ctx["gap"], ctx["right"])
    lines = ctx["lines"]

    refs = doc.page_refs()
    total = len(refs)
    total0 = total                     # 新建了几页 = 结束时 total - total0

    # 页底安全线：按页高比例（比"最低一行"更保守，避免压到页脚）
    page_h = 842.0
    mb = doc.inherited(refs[0], "MediaBox")
    if isinstance(mb, list) and len(mb) == 4:
        page_h = abs(float(mb[3]) - float(mb[1]))
    bottom = max(18.0, page_h * bottom_ratio)
    # 新建页沿用的页面尺寸
    page_w = 595.0
    if isinstance(mb, list) and len(mb) == 4:
        page_w = abs(float(mb[2]) - float(mb[0]))

    # 折行后除末行外两端对齐（先定稿，摆放阶段直接用）
    # ★ 只对齐"被软折切开"的行：原文硬行必须保留自己的宽度。
    #   不分青红皂白全撑满，会把 "PDF Viewer Test Document" 这种短行
    #   拉成 "P D F   V i e w e r"，整页看起来像字距坏掉。
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
    # 目标页的默认行距（插入段落时，段首与上一行的间距）
    gaps = [rows[i]["y"] - rows[i + 1]["y"] for i in range(len(rows) - 1)]
    default_gap = gap or (sorted(gaps)[len(gaps) // 2] if gaps else size * 1.6)

    def items_for(k):
        m = model_of(k)
        out = []
        fixed = []          # 页脚：固定在原位，不参与流动
        prev_y = None
        replaced = False
        for r in m["rows"]:
            # 页脚（页码那行）在内容区安全线以下 —— 它属于页面，不属于正文。
            # 不排除的话，内容一长就会被当成普通行"续"到下一页顶部。
            if r["y"] < bottom - 1:
                fixed.append(r)
                continue
            if k == page_no and id(r) in own:
                if not replaced:
                    # 目标段落所在位置：换成新排的行
                    for li, ln in enumerate(lines):
                        out.append({"line": ln,
                                    "gap": (prev_y - para_top_y) if (li == 0 and prev_y is not None)
                                           else gap})
                    replaced = True
                    # ★ 插入新段落后，"上一行"要更新成新段落的**最后一行**。
                    #   不更新的话，段落后面那一行会用"段落上方那行"来算间距，
                    #   凭空多出一个大间距 —— 后面的内容整体被推下去，
                    #   实测：只改几个字就把整页推爆，波及 4 页。
                    prev_y = para_top_y - (len(lines) - 1) * gap
                continue
            g = (prev_y - r["y"]) if prev_y is not None else default_gap
            out.append({"row": r, "gap": g})
            prev_y = r["y"]
        return out, fixed

    top_default = max((r["y"] for r in rows), default=page_h - bottom)

    # ---- 逐页摆放 ----
    page = page_no
    carry = []
    plan = []                     # [(page, [(item, y), ...])]
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

    # ---- 写出：只改真正被触及的页 ----
    new_line_aliases = set()
    for ln in lines:
        for (_c, a, _code, _w, _nb, _fb) in ln["runs"]:
            if a:
                new_line_aliases.add(a)
    font_src = _font_resources(doc, ctx["pref"])
    font_src.setdefault(FALLBACK_ALIAS, None)      # 回退字体已挂在目标页

    for (pg, placed, fixed) in plan:
        m = model_of(pg)
        ref = m["ref"]
        # 补挂字体：新排的字用的是目标页的字体资源，别的页不一定有
        need = {}
        for a in new_line_aliases:
            v = font_src.get(a)
            if v is not None:
                need[a] = v
        if pg == page_no:
            need[FALLBACK_ALIAS] = None            # 已在目标页资源里
        need = {k: v for k, v in need.items() if v is not None}
        if need:
            _attach_font_resource(doc, ref, need)

        cb = {a: fi.code_bytes for a, fi in m["ip"].fonts.items()}
        killed = {(g.op_index, g.char_index) for g in m["ip"].glyphs}
        del_content = _rewrite_text(m["content"], killed, cb) if m["content"].strip() else b""

        items = []
        # 固定行（页脚）原位重画 —— _rewrite_text 已经把整页文字删了，
        # 不画回去就等于把页码弄丢了。
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
