# -*- coding: utf-8 -*-
"""
段落模型（完整编辑的地基）。

分层：
    字形 GlyphBox  ->  行 Row（同一基线）  ->  段 Para（首行缩进 / 行距一致）

本模块只做两件事：
  1. 建模型：把一页的字形聚成行、段；
  2. 重建：把行里的字形按**原位置**写成文本流指令（逐个字形绝对定位）。

为什么逐个字形定位而不是一行一个 Tj：
  原文件可能带 Tc/Tw/Tz（字距/词距/水平缩放）和 cm 缩放，复现这些参数很容易
  差一点点；而逐字形给绝对位置，位置是**精确**的，与这些参数无关。

为什么按 code 而不是 Unicode 写：
  文字是嵌进 PDF 的原字体子集，同一个字只有在这个字体里有字形。
  直接写原 code 就一定对（字形、宽度都不变）；把 Unicode 反过来查 code
  留给"改字"阶段，查不到时再回退到项目自带 CJK 字体。
"""

import statistics


def eff_size(g):
    """有效字号 = 字身高（不是 Tf 原始值；整页可能套了 cm 缩放）。"""
    return g.y1 - g.y0


def build_rows(glyphs, tol=2.0):
    """按基线把字形聚成行。tol 是 y 容差（pt）。"""
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
    """
    聚段：中文文档的通用规律 ——
      · 首行比左边距**缩进**（常见两字）；
      · 段内行距一致；
      · 字号/字体变化一般意味着换段（标题）。

    返回 [{rows:[...], left, right, size, gap, fonts}]
    """
    if not rows:
        return []

    # 左边距 = 行起点里最常见的那个（续行的位置）
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
            indented = r["x0"] > left + indent_tol          # 首行缩进 -> 必然新段
            same_gap = body_gap > 0 and abs(gap - body_gap) <= max(1.2, body_gap * gap_tol)
            same_x = abs(r["x0"] - left) <= indent_tol      # 续行贴左边距
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
    """
    把行里的字形按位置写成文本流指令。

    positions: {(op_index, char_index): (x, y)} —— 不传就用字形原位。
    返回 bytes（可直接追加到页面内容流后面）。
    """
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
