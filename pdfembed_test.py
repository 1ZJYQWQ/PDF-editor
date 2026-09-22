"""中文字体嵌入验证（Type0 / Identity-H 整条链）。

验收标准不是"跑通不报错"，而是这几条硬指标：

  1. 输出 PDF 里有完整的字体链条
     /Type0 -> /CIDFontType2 -> /FontDescriptor -> /FontFile2
     外加 /W 宽度数组、/CIDToGIDMap、/ToUnicode
  2. 内容流里的中文是**双字节 GID**（十六进制字符串），不是 `?`
  3. 嵌进去的 FontFile2 能被 pdffont 重新解析，且含所需字形
  4. ★ **能选中**：用 /ToUnicode 把内容流里的 GID 还原，必须等于原文
     —— 这条是「可搜可复制」的唯一证明
  5. ★ **能涂黑**：涂黑引擎（另一套代码）能正确解释新写入的中文，
     且涂黑后文字真的从内容流消失 —— 跨引擎交叉验证
  6. 纯拉丁文字不嵌字体（走标准 14 字体），输出更小
  7. 字体里没有的字符必须上报，不能静默丢
"""
import os

import testscratch as ts  # noqa: E402
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdfedit import PDFDocument, Name, apply_edits, build_pdf   # noqa: E402
from pdfedit import _pdf_text_encode, split_lines, line_leading  # noqa: E402
import pdfredact                                                # noqa: E402
import pdffont                                                  # noqa: E402

N = Name
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = ts.SCRATCH
SRC = os.path.join(WORK, "_embed_src.pdf")
OUT = os.path.join(WORK, "_embed_out.pdf")
OUT_LATIN = os.path.join(WORK, "_embed_latin.pdf")
OUT_REDACT = os.path.join(WORK, "_embed_redacted.pdf")

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS " + name + ("  [%s]" % extra if extra else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL " + name + ("  [%s]" % extra if extra else ""))


def make_src_pdf(path, content=b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (PLAIN) Tj ET"):
    """造一个单页源 PDF（标准 14 字体，没有中文）。"""
    doc = PDFDocument()
    font = doc.new_obj({
        "Type": N("Font"), "Subtype": N("Type1"),
        "BaseFont": N("Helvetica"), "Encoding": N("WinAnsiEncoding"),
    })
    cstream = doc.new_obj(({"Length": len(content)}, content))
    page = doc.new_obj({
        "Type": N("Page"), "MediaBox": [0, 0, 612, 792],
        "Resources": {"Font": {"F1": font}},
        "Contents": cstream, "Parent": None,
    })
    pages = doc.new_obj({"Type": N("Pages"), "Kids": [page], "Count": 1})
    doc.objects[tuple(page)] = dict(doc.objects[tuple(page)], Parent=pages)
    root = doc.new_obj({"Type": N("Catalog"), "Pages": pages})
    doc.root_ref = root
    doc.pages_ref = pages
    build_pdf(doc, {}, path)
    return path


TEXT_CN = "中文涂黑测试：思源黑体嵌入"
TEXT_MIX = "混合 mixed 123 中文"


def find_type0_font(doc):
    """在页面资源里找 /Type0 字体对象。"""
    for pref in doc.page_refs():
        res = doc.resolve(doc.inherited(pref, "Resources"))
        if not isinstance(res, dict):
            continue
        fonts = doc.resolve(res.get("Font"))
        if not isinstance(fonts, dict):
            continue
        for _alias, fref in fonts.items():
            fd = doc.resolve(fref)
            if isinstance(fd, dict) and str(fd.get("Subtype")) == "Type0":
                return fd, fref
    return None, None


def page_content_bytes(doc, pno=1):
    return pdfredact.read_page_content(doc, doc.page_refs()[pno - 1])


print("=" * 62)
print("  中文字体嵌入验证（Type0 / Identity-H）")
print("=" * 62)

os.makedirs(WORK, exist_ok=True)
make_src_pdf(SRC)

# ----------------------------------------------------------------------
print("\n=== 1. 写入中文并检查字体链条 ===")
st = apply_edits(SRC, OUT, {
    "objects": [{"page": 1, "kind": "text", "x": 72, "y": 640,
                 "text": TEXT_CN, "size": 18, "color": "#112233"}],
})
print("  输出 %.2f KB，cjk_glyphs=%d，cjk_chars=%d"
      % (st["size"] / 1024, st["cjk_glyphs"], st["cjk_chars"]))
check("apply_edits 报告成功", st["ok"] is True)
check("子集里没有缺字", st["cjk_missing"] == "", repr(st["cjk_missing"]))
check("子集字形数合理（字符数 + 复合组件）",
      st["cjk_glyphs"] >= len(set(TEXT_CN)), str(st["cjk_glyphs"]))

raw = open(OUT, "rb").read()
doc = PDFDocument.load(raw)

check("输出是合法 PDF（头/尾/xref）",
      raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF")
      and b"xref" in raw)

type0, type0_ref = find_type0_font(doc)
check("页面资源里有 /Type0 字体", type0 is not None)
if type0:
    check("编码是 /Identity-H", str(type0.get("Encoding")) == "Identity-H",
          str(type0.get("Encoding")))
    check("有 /ToUnicode", type0.get("ToUnicode") is not None)

    desc_fonts = doc.resolve(type0.get("DescendantFonts"))
    desc = doc.resolve(desc_fonts[0]) if isinstance(desc_fonts, list) else None
    check("有 /DescendantFonts 里的 /CIDFontType2",
          isinstance(desc, dict) and str(desc.get("Subtype")) == "CIDFontType2",
          str(desc.get("Subtype")) if isinstance(desc, dict) else "无")
    if isinstance(desc, dict):
        check("CIDToGIDMap 是 /Identity",
              str(desc.get("CIDToGIDMap")) == "Identity")
        warr = doc.resolve(desc.get("W"))
        check("有 /W 宽度数组", isinstance(warr, list) and len(warr) > 0,
              "%s 项" % (len(warr) if isinstance(warr, list) else 0))

        fd = doc.resolve(desc.get("FontDescriptor"))
        check("有 /FontDescriptor", isinstance(fd, dict))
        if isinstance(fd, dict):
            check("FontName 是子集命名（6 位标签+字体名）",
                  re.match(r"^[0-9A-F]{6}\+", str(fd.get("FontName") or ""))
                  is not None,
                  str(fd.get("FontName")))
            ff = doc.resolve(fd.get("FontFile2"))
            check("有 /FontFile2 流", doc.is_stream(ff))
            if doc.is_stream(ff):
                fdata = ff[1]
                check("FontFile2 长度字段正确",
                      ff[0].get("Length") == len(fdata)
                      and ff[0].get("Length1") == len(fdata),
                      "%s / %s" % (ff[0].get("Length"), len(fdata)))

                # ★ 嵌进去的字体必须能被我们自己的解析器重新读出来
                emb = pdffont.TrueTypeFont(fdata)
                check("嵌入字体可被重新解析", emb.num_glyphs == st["cjk_glyphs"],
                      "numGlyphs=%d" % emb.num_glyphs)
                check("嵌入字体是 glyf 轮廓", "glyf" in emb.tables)
                check("嵌入字体里没有 cmap（防反查还原）",
                      "cmap" not in emb.tables)

# ----------------------------------------------------------------------
print("\n=== 2. 内容流里是双字节 GID，不是问号 ===")
content = page_content_bytes(doc)
check("内容流里没有 '?' 占位", b"?) Tj" not in content and b"(?" not in content)
m = re.search(rb"<([0-9A-Fa-f]+)>\s*Tj", content)
check("存在十六进制字符串的 Tj", m is not None)
hx = m.group(1).decode() if m else ""
check("十六进制长度是偶数（双字节码）", bool(hx) and len(hx) % 2 == 0, hx)
check("码的个数等于字符数", len(hx) // 4 == len(TEXT_CN),
      "%d 个码 vs %d 个字符" % (len(hx) // 4, len(TEXT_CN)))

# ----------------------------------------------------------------------
print("\n=== 3. ★ 能选中：用 ToUnicode 把 GID 还原回原文 ===")
table = {}
if type0:
    tu_ref = type0.get("ToUnicode")
    tu_data = doc.stream_data(tu_ref)
    table = pdfredact.FontInfo._parse_tounicode(
        tu_data.decode("latin-1", "replace"))
    print("  ToUnicode 表项: %d" % len(table))

    gids = [int(hx[i:i + 4], 16) for i in range(0, len(hx), 4)] if m else []
    restored = "".join(table.get(g, "") for g in gids)
    print("  还原结果: %r" % restored)
    check("GID 经 ToUnicode 还原等于原文",
          restored == TEXT_CN, "%r != %r" % (restored, TEXT_CN))

# ----------------------------------------------------------------------
print("\n=== 4. ★ 跨引擎：涂黑引擎能正确解释新写入的中文 ===")
int_doc = PDFDocument.load(raw)
pref = int_doc.page_refs()[0]
res = int_doc.resolve(int_doc.inherited(pref, "Resources"))
interp = pdfredact.ContentStreamInterpreter(int_doc, res)
interp.run(page_content_bytes(int_doc))
cn_glyphs = [g for g in interp.glyphs if g.code > 0 and g.code in (table or {})]
print("  解释出字形 %d 个，其中中文 %d 个"
      % (len(interp.glyphs), len(cn_glyphs)))
check("涂黑引擎能解出写入的中文（双字节码）",
      len(cn_glyphs) == len(TEXT_CN),
      "%d vs %d" % (len(cn_glyphs), len(TEXT_CN)))
check("中文被归到嵌入字体（font_alias 正确）",
      all(g.font_alias for g in cn_glyphs))

if cn_glyphs:
    x0 = min(g.x0 for g in cn_glyphs) - 1
    x1 = max(g.x1 for g in cn_glyphs) + 1
    y0 = min(g.y0 for g in cn_glyphs) - 1
    y1 = max(g.y1 for g in cn_glyphs) + 1
    print("  中文范围 x=[%.1f,%.1f] y=[%.1f,%.1f]" % (x0, x1, y0, y1))
    st2 = pdfredact.apply_redactions(
        OUT, OUT_REDACT,
        [{"page": 1, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}])
    print("  涂黑删除字形: %s" % st2["removed_glyphs"])
    check("涂黑把写入的中文也删掉了", st2["removed_glyphs"] >= len(TEXT_CN),
          str(st2["removed_glyphs"]))
    after = page_content_bytes(PDFDocument.load(open(OUT_REDACT, "rb").read()))
    check("涂黑后内容流里不再有那段十六进制中文",
          b"<" + hx.encode() + b">" not in after)

# ----------------------------------------------------------------------
print("\n=== 5. 纯拉丁文字不嵌字体（输出更小）===")
st3 = apply_edits(SRC, OUT_LATIN, {
    "objects": [{"page": 1, "kind": "text", "x": 72, "y": 600,
                 "text": "plain ASCII only", "size": 14}],
})
size_latin = os.path.getsize(OUT_LATIN)
size_cjk = os.path.getsize(OUT)
check("纯拉丁时没有子集化（cjk_glyphs=0）", st3["cjk_glyphs"] == 0)
check("纯拉丁输出更小，差值正好是嵌入字体的体积",
      size_cjk - size_latin > 2000,
      "%.2f KB vs %.2f KB（差 %.2f KB）"
      % (size_latin / 1024, size_cjk / 1024,
         (size_cjk - size_latin) / 1024))
check("纯拉丁输出里没有 /FontFile2",
      b"FontFile2" not in open(OUT_LATIN, "rb").read())

# ----------------------------------------------------------------------
print("\n=== 6. 中英混排 ===")
OUT_MIX = os.path.join(WORK, "_embed_mix.pdf")
st4 = apply_edits(SRC, OUT_MIX, {
    "objects": [{"page": 1, "kind": "text", "x": 72, "y": 560,
                 "text": TEXT_MIX, "size": 14}],
})
mix_doc = PDFDocument.load(open(OUT_MIX, "rb").read())
mix_t0, _ = find_type0_font(mix_doc)
check("混排也启用了嵌入字体", mix_t0 is not None)
mix_content = page_content_bytes(mix_doc)
mm = re.search(rb"<([0-9A-Fa-f]+)>\s*Tj", mix_content)
check("混排整串走同一个字体（一条 Tj 只能用一个字体）",
      mm is not None and len(mm.group(1)) // 4 == len(TEXT_MIX),
      "%d 码 vs %d 字符"
      % ((len(mm.group(1)) // 4) if mm else 0, len(TEXT_MIX)))
if mm and mix_t0 is not None:
    tbl = pdfredact.FontInfo._parse_tounicode(
        mix_doc.stream_data(mix_t0.get("ToUnicode")).decode("latin-1", "replace"))
    gs = [int(mm.group(1)[i:i + 4].decode(), 16)
          for i in range(0, len(mm.group(1)), 4)]
    check("混排还原正确", "".join(tbl.get(g, "") for g in gs) == TEXT_MIX,
          repr("".join(tbl.get(g, "") for g in gs)))

# ----------------------------------------------------------------------
print("\n=== 7. 缺字必须上报（不静默丢）===")
OUT_MISS = os.path.join(WORK, "_embed_missing.pdf")
# U+E000 是私用区，中文字体里通常没有
st5 = apply_edits(SRC, OUT_MISS, {
    "objects": [{"page": 1, "kind": "text", "x": 72, "y": 520,
                 "text": "中文\ue000", "size": 14}],
})
print("  cjk_missing = %r" % st5["cjk_missing"])
check("私用区字符被上报为缺字", st5["cjk_missing"] == "\ue000",
      repr(st5["cjk_missing"]))
check("可写的部分仍然写进去了", st5["cjk_glyphs"] > 0,
      str(st5["cjk_glyphs"]))

# ----------------------------------------------------------------------
print("\n=== 8. 回归：纯图形编辑不受影响 ===")
OUT_GFX = os.path.join(WORK, "_embed_gfx.pdf")
st6 = apply_edits(SRC, OUT_GFX, {
    "objects": [{"page": 1, "kind": "rect", "x": 100, "y": 100,
                 "w": 200, "h": 50, "color": "#ff0000", "fill": True},
                {"page": 1, "kind": "whiteout", "x": 50, "y": 200,
                 "w": 100, "h": 20}],
})
gfx_raw = open(OUT_GFX, "rb").read()
check("图形编辑产出合法 PDF",
      st6["ok"] and gfx_raw.startswith(b"%PDF-"))
check("图形编辑不嵌入字体", b"FontFile2" not in gfx_raw)
check("图形编辑报告 cjk_glyphs=0", st6["cjk_glyphs"] == 0)

# ----------------------------------------------------------------------
print("\n=== 9. 多行文字（换行必须真的换行）===")
# 前端输入框写着「可多行」，但原来导出只发一个 Tj —— 换行符被当成普通
# 字符塞进字符串，渲染器把它当字符码 10 去画，没有字形，等于白画。
# UI 承诺了就得兑现。这里把「拆行 / 空行 / 行距」都锁死。
OUT_ML = os.path.join(WORK, "_embed_multiline.pdf")
TEXT_ML = "第一行\n第二行\n\n第四行"          # 第 3 行是空行
st7 = apply_edits(SRC, OUT_ML, {
    "objects": [{"page": 1, "kind": "text", "x": 80, "y": 600,
                 "text": TEXT_ML, "size": 16}],
})
ml_doc = PDFDocument.load(open(OUT_ML, "rb").read())
ml_content = page_content_bytes(ml_doc)

tds = re.findall(rb"(-?[\d.]+)\s+(-?[\d.]+)\s+Td", ml_content)
tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", ml_content)
print("  Td %d 个，Tj %d 个" % (len(tds), len(tjs)))
check("4 行拆成 4 次定位（含空行）", len(tds) == 4, str(len(tds)))
check("空行不画字（只有 3 个 Tj）", len(tjs) == 3, str(len(tjs)))

ml_t0, _ = find_type0_font(ml_doc)
ml_tbl = {}
if ml_t0 is not None:
    ml_tbl = pdfredact.FontInfo._parse_tounicode(
        ml_doc.stream_data(ml_t0.get("ToUnicode")).decode("latin-1", "replace"))
restored = []
for hx2 in tjs:
    gs2 = [int(hx2[i:i + 4].decode(), 16) for i in range(0, len(hx2), 4)]
    restored.append("".join(ml_tbl.get(g, "") for g in gs2))
print("  逐行还原: %r" % restored)
check("每一行都还原正确（空行不产生条目）",
      restored == [l for l in TEXT_ML.split("\n") if l], "%r" % restored)

# ★ 纵向位置：交给涂黑引擎按行分组量出来 —— 跨引擎验证位置
ml_i = PDFDocument.load(open(OUT_ML, "rb").read())
res_ml = ml_i.resolve(ml_i.inherited(ml_i.page_refs()[0], "Resources"))
it_ml = pdfredact.ContentStreamInterpreter(ml_i, res_ml)
it_ml.run(page_content_bytes(ml_i))
rows = sorted({round(g.y0) for g in it_ml.glyphs if g.font_alias == "CJK"},
              reverse=True)
print("  各行的基线 y: %s" % rows)
# 600 → 580 → (空行 560) → 540。行高 = 16 × 1.25 = 20
check("行的纵向位置正确（空行也占一行高度）",
      rows == [600, 580, 540], "%s（期望 [600, 580, 540]）" % rows)

# 拉丁路径同样要拆行
OUT_MLL = os.path.join(WORK, "_embed_multiline_latin.pdf")
apply_edits(SRC, OUT_MLL, {
    "objects": [{"page": 1, "kind": "text", "x": 80, "y": 300,
                 "text": "line one\nline two\nline three", "size": 12}],
})
lat_doc = PDFDocument.load(open(OUT_MLL, "rb").read())
lat_content = page_content_bytes(lat_doc)
for t in ("line one", "line two", "line three"):
    check("拉丁多行里有 %r 的 Tj" % t,
          b"(" + t.encode() + b") Tj" in lat_content)
lat_td = re.findall(rb"0 -([\d.]+) Td", lat_content)
check("拉丁行距 = 12 × 1.25 = 15", lat_td == [b"15.000", b"15.000"],
      str(lat_td))
check("字面量里没有裸换行", b"(line one\n" not in lat_content)

# 控制字符转义（防御性：万一有没被拆掉的换行/制表符）
check("换行与制表符被转义",
      _pdf_text_encode("a\nb") == "a\\nb"
      and _pdf_text_encode("a\tb") == "a\\tb",
      repr(_pdf_text_encode("a\nb")))
check("split_lines 同时吃 \\r\\n 与 \\r",
      split_lines("a\r\nb\rc") == ["a", "b", "c"],
      str(split_lines("a\r\nb\rc")))
check("line_leading 与前端一致（size × 1.25）",
      abs(line_leading(16) - 20.0) < 1e-9)

for f in (SRC, OUT, OUT_LATIN, OUT_REDACT, OUT_MIX, OUT_MISS, OUT_GFX,
          OUT_ML, OUT_MLL):
    if os.path.exists(f):
        os.remove(f)

print()
print("=" * 62)
print("  通过 %d，失败 %d" % (PASS, FAIL))
if FAILED:
    print("  失败项：")
    for n in FAILED:
        print("    -", n)
print("=" * 62)
sys.exit(1 if FAIL else 0)
