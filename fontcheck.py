"""字体骨架检查 —— 为「中文嵌入 + 子集化」做前置确认。

用法：
    python fontcheck.py <字体文件> [更多字体文件 ...]

打印表目录与关键结论：

  sfnt         TrueType(0x00010000) 还是 CFF 包装(OTTO)
  glyf/CFF     轮廓存储方式 —— **只有 glyf 才做子集化**。
               CFF 的 INDEX/charstring/subrs 是另一个量级的工程，
               本项目明确不做。
  fvar/gvar    是否可变字体。**PDF 阅读器不处理 gvar**，只渲染默认实例。
  numGlyphs    字形总数（决定子集化后的规模）
  cmap         覆盖的码位数，以及对样例汉字的映射结果
  字重         笔画面宽 + ★ 默认实例在字重轴上的位置

★ 为什么必须查「字重」
    可变字体的 glyf 里存的是**默认实例**的轮廓，gvar 才是其余字重的增量。
    PDF 阅读器不处理 gvar —— 所以默认实例是细体，嵌进去就是细体。
    实测：NotoSansSC-VF 的 wght 默认是 100，直接嵌进 PDF 会渲染成发丝字。

    判据是「默认实例在轴上的位置」（机械可判定），不是笔画粗细
    （后者是启发式，会被衬线字的顿笔带偏 —— 踩过）。

⚠ 笔画面宽只在**同风格族内**可比（衬线比衬线、无衬线比无衬线）。
    跨风格比会把设计差异当成字重差异。

cmap 解析共用 pdffont 的实现，避免两份代码漂移。
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdffont                                               # noqa: E402

# 样例字符：ASCII + 常用汉字
SAMPLES = "Aaz09中文测试你好世界涂层"

# CJK 统一表意文字基本区
CJK_LO, CJK_HI = 0x4E00, 0x9FFF


def read_tables(data, base=0):
    """读一个 sfnt 表目录，返回 (tag, {表名: (偏移, 长度)})。

    共用 pdffont 的实现 —— 两份解析必然漂移。
    """
    return data[base:base + 4], pdffont._read_table_dir(data, base)


def cmap_with_formats(data, tables):
    """cmap 映射 + 用到了哪些子表格式。

    映射本身走 pdffont.parse_cmap；这里额外报告格式，
    因为「用到 format 12」说明字体覆盖了 BMP 之外的码位。
    """
    out = pdffont.parse_cmap(data, tables)
    used = set()
    if "cmap" in tables:
        off = tables["cmap"][0]
        try:
            n = struct.unpack(">H", data[off + 2:off + 4])[0]
        except struct.error:
            n = 0
        for i in range(n):
            p = off + 4 + i * 8
            if p + 8 > len(data):
                break
            so = off + struct.unpack(">I", data[p + 4:p + 8])[0]
            if so + 2 > len(data):
                continue
            used.add(str(struct.unpack(">H", data[so:so + 2])[0]))
    return out, sorted(used)


def stroke_ratios(data, tables, cmap, upem):
    """量一笔画字的**笔画面宽**（占 em 的百分比）。

    「一」量包围盒高度、「丨」量包围盒宽度 —— 都是一笔画字的笔画厚度。
    注意这是启发式：**衬线字的「一」带顿笔**，量到的是最粗处而非笔画
    主体，会让衬线字显得比实际粗（我在这里误判过一次）。
    所以它只作提示，真正的判据是 weight_axes()。
    """
    if "glyf" not in tables or "loca" not in tables or "head" not in tables:
        return {}
    head = tables["head"][0]
    itl = struct.unpack(">h", data[head + 50:head + 52])[0]
    num = struct.unpack(">H", data[tables["maxp"][0] + 4:
                                   tables["maxp"][0] + 6])[0]
    n = num + 1
    lo = tables["loca"][0]
    if itl == 0:
        raw = data[lo:lo + 2 * n]
        if len(raw) < 2 * n:
            return {}
        loca = [2 * v for v in struct.unpack(">%dH" % n, raw)]
    else:
        raw = data[lo:lo + 4 * n]
        if len(raw) < 4 * n:
            return {}
        loca = list(struct.unpack(">%dI" % n, raw))
    go = tables["glyf"][0]
    out = {}
    for ch in ("一", "丨"):
        g = cmap.get(ord(ch))
        if g is None or g + 1 >= len(loca) or loca[g + 1] <= loca[g]:
            continue
        gd = data[go + loca[g]: go + loca[g + 1]]
        if len(gd) < 10:
            continue
        x0, y0, x1, y1 = struct.unpack(">hhhh", gd[2:10])
        t = (y1 - y0) if ch == "一" else (x1 - x0)
        out[ch] = t * 100.0 / (upem or 1000)
    return out


def weight_axes(data, tables):
    """读 fvar，返回 [(轴名, min, default, max)]。"""
    if "fvar" not in tables:
        return []
    off = tables["fvar"][0]
    try:
        hdr = struct.unpack(">HHHHHHHH", data[off:off + 16])
    except struct.error:
        return []
    axes_off, axis_count, axis_size = hdr[2], hdr[4], hdr[5]
    out = []
    for i in range(axis_count):
        p = off + axes_off + i * axis_size
        if p + 16 > len(data):
            break
        tag = data[p:p + 4].decode("latin-1")
        mn, df, mx = struct.unpack(">iii", data[p + 4:p + 16])
        out.append((tag, mn / 65536.0, df / 65536.0, mx / 65536.0))
    return out


def inspect(path):
    print("=" * 62)
    print("文件 : %s" % path)
    print("=" * 62)

    with open(path, "rb") as f:
        data = f.read()
    print("体积 : %.2f MB (%d 字节)" % (len(data) / 1048576, len(data)))

    if data[:4] == b"ttcf":
        nfaces = struct.unpack(">I", data[8:12])[0]
        offs = [struct.unpack(">I", data[12 + i * 4:16 + i * 4])[0]
                for i in range(nfaces)]
        print("类型 : TTC 字体集合，含 %d 个 face" % nfaces)
        print("       ★ 不能直接当 FontFile2（那个要单个 sfnt），")
        print("         要提取其中一个 face 并重建表目录")
        base = offs[0]
        print("       下面只检查第 0 个 face")
    else:
        base = 0

    _tag, tables = read_tables(data, base)
    sfnt = data[base:base + 4]
    print("sfnt : %s (%s)" % (sfnt.hex(), {
        "00010000": "TrueType",
        "4f54544f": "OTTO / CFF",
        "74727565": "true (旧 Mac)",
    }.get(sfnt.hex(), "未知")))

    has_glyf = "glyf" in tables
    has_cff = "CFF " in tables
    has_cff2 = "CFF2" in tables
    print("轮廓 : glyf=%s  CFF=%s  CFF2=%s" % (has_glyf, has_cff, has_cff2))
    if has_glyf:
        print("       ✓ glyf 轮廓 —— 可以做子集化")
    elif has_cff or has_cff2:
        print("       ✗ CFF/CFF2 轮廓 —— 本项目不做子集化，换字体")
    else:
        print("       ? 既无 glyf 也无 CFF，异常")

    var = [t for t in ("fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR", "STAT")
           if t in tables]
    print("可变 : %s" % (("是 → " + " ".join(var)) if var else "否（静态字体）"))
    if var:
        print("       ★ PDF 阅读器不处理可变轴，只会渲染默认实例；")
        print("         子集化时应把这些表剥掉（见下方字重体检）")

    if "maxp" in tables:
        mo = tables["maxp"][0]
        print("字形数: %d" % struct.unpack(">H", data[mo + 4:mo + 6])[0])

    cmap, used = cmap_with_formats(data, tables)
    print("cmap : %d 个码位（子表 format %s）"
          % (len(cmap), "/".join(used) or "无"))
    cjk = sum(1 for c in cmap if CJK_LO <= c <= CJK_HI)
    print("       其中 CJK 基本区(U+4E00-U+9FFF): %d / %d"
          % (cjk, CJK_HI - CJK_LO + 1))

    # ---- 字重体检 ----
    upem = 1000
    if "head" in tables:
        try:
            upem = struct.unpack(">H", data[tables["head"][0] + 18:
                                            tables["head"][0] + 20])[0] or 1000
        except struct.error:
            upem = 1000
    ratios = stroke_ratios(data, tables, cmap, upem) if cmap else {}
    axes = weight_axes(data, tables)
    waxis = next((a for a in axes if a[0] == "wght"), None)

    print("字重 : %s" % ("  ".join("%s=%.1f%%em" % (k, v)
                                  for k, v in ratios.items()) or "(量不到)"))
    bad = False
    if ratios:
        thin = min(ratios.values())
        if thin < 5.0:
            bad = True
            print("       ⚠ 笔画面宽只有 %.1f%%em，明显偏细"
                  "（常规无衬线中文约 8~10%%）" % thin)

    if waxis:
        _t, mn, df, mx = waxis
        rng = mx - mn
        pos = (df - mn) / rng if rng else 0.0
        if pos <= 0.15:
            bad = True
            print("       默认实例 wght=%g（轴范围 %g~%g，位于最轻端 %.0f%%）"
                  " ⚠" % (df, mn, mx, pos * 100))
            print("       ★ 可变字体的 glyf 存的是**默认实例**轮廓，而 PDF")
            print("         阅读器不处理 gvar —— 直接嵌入会渲染成细体。")
            print("         先用 makefont.py 实例化到常规字重（如 400）。")
        else:
            print("       默认实例 wght=%g（轴范围 %g~%g，位于 %.0f%%）✓"
                  % (df, mn, mx, pos * 100))
    elif ratios:
        print("       静态字体（无 wght 轴）—— 可直接用")

    if bad:
        print("       结论 : ✗ 这个字体直接嵌入会有问题，见上面的提示")
    elif ratios or waxis:
        print("       结论 : ✓ 字重看起来正常")

    if ratios:
        print("       注：笔画面宽只在**同风格族内**可比（衬线比衬线、")
        print("           无衬线比无衬线）；跨风格比会把设计差异当成字重差异。")

    if cmap:
        miss = [ch for ch in SAMPLES if ord(ch) not in cmap]
        print("样例 : %s" % " ".join(
            "%s=%s" % (ch, cmap.get(ord(ch), "缺")) for ch in SAMPLES))
        print("       缺失 %d 个%s"
              % (len(miss), ("：" + "".join(miss)) if miss else ""))

    print("表   : %s" % " ".join(sorted(tables)))
    print()
    # 返回 1 表示「这个字体直接嵌入会有问题」，供脚本/CI 判据用
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    rc = 0
    for path in argv[1:]:
        try:
            rc = rc or inspect(path)
        except Exception as exc:
            print("!! %s 检查失败: %s: %s" % (path, type(exc).__name__, exc))
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
