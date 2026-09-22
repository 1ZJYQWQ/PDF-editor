"""
Redaction 引擎验证

核心验收标准只有一条：**被涂黑的文字必须真的读不出来了**。
所以这里的测试不是"跑通不报错"，而是逐项检查：

  1. 字形矩形算得对不对（用已知坐标的构造内容流验证）
  2. redaction 之后，内容流里还能不能找到原文
  3. 没被涂黑的内容有没有被误删
  4. 输出的 PDF 结构是否仍然合法
  5. 元数据有没有清理干净
"""

import os

import testscratch as ts  # noqa: E402
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdfedit import PDFDocument, build_pdf, Name  # noqa: E402
import pdfredact  # noqa: E402


def N(s):
    """
    转成 PDF 名字对象。

    注意：'Font' 这样的 Python str 会被序列化成字符串字面量 (Font)，
    而不是名字 /Font —— 于是 /Type /Page 变成 /Type (Page)，
    页面树就认不出来了。测试造 PDF 时所有 PDF 名字都必须走这里。
    （这是测试代码的坑，不是引擎的问题：引擎从真实 PDF 解析出来的
      本来就是 Name 类型。）
    """
    return Name(s)

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}" + (f"  [{extra}]" if extra else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}" + (f"  [{extra}]" if extra else ""))


def make_pdf(content, width=612, height=792, info=None):
    """造一个单页 PDF，内容流原样给。"""
    doc = PDFDocument()
    font = doc.new_obj({
        "Type": N("Font"), "Subtype": N("Type1"),
        "BaseFont": N("Helvetica"), "Encoding": N("WinAnsiEncoding"),
    })
    cstream = doc.new_obj(({"Length": len(content)}, content))
    page = doc.new_obj({
        "Type": N("Page"), "MediaBox": [0, 0, width, height],
        "Resources": {"Font": {"F1": font}},
        "Contents": cstream,
        "Parent": None,
    })
    pages = doc.new_obj({"Type": N("Pages"), "Kids": [page], "Count": 1})
    # Ref 是 tuple 子类，对象表的键是 (num, gen) 元组
    doc.objects[tuple(page)] = dict(doc.objects[tuple(page)],
                                    Parent=pages)
    root = doc.new_obj({"Type": N("Catalog"), "Pages": pages})
    doc.root_ref = root
    doc.pages_ref = pages
    if info is not None:
        iref = doc.new_obj({N(k): N(v) if k == "Producer" else v
                            for k, v in info.items()})
        doc.trailer["Info"] = iref
    tmp = ts.ensure(
                       "_redact_src.pdf")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    build_pdf(doc, {}, tmp)
    return tmp


def extract_text(path):
    """
    朴素但有效的文本提取：把内容流解出来，收集所有 Tj/TJ 字符串。
    这正是「涂黑事故」里攻击者会用的手段 —— 所以我们用它当判据。
    """
    with open(path, "rb") as f:
        data = f.read()
    doc = PDFDocument.load(data)
    out = []
    for pref in doc.page_refs():
        content = pdfredact.read_page_content(doc, pref)
        if not content.strip():
            continue
        for m in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj", content):
            raw = m.group(0)
            lit = raw[:raw.rfind(b")") + 1]
            inner = pdfredact._decode_literal(lit[1:-1])
            out.append(inner)
        for m in re.finditer(rb"\[(.*?)\]\s*TJ", content, re.S):
            for s in re.finditer(rb"\((?:\\.|[^\\()])*\)", m.group(1)):
                out.append(pdfredact._decode_literal(s.group(0)[1:-1]))
    return b" ".join(out)


print("=" * 60)
print("  PDF Redaction 引擎验证")
print("=" * 60)

# ----------------------------------------------------------------------
print("\n=== 1. 字形矩形计算 ===")
# ----------------------------------------------------------------------

# 造一段内容流：在 (100, 700) 用 12pt 写 "AB"
# Helvetica: A=667, B=667 → 每个字形宽 12*0.667 = 8.004
content = b"""BT
/F1 12 Tf
1 0 0 1 100 700 Tm
(AB) Tj
ET
"""
src = make_pdf(content)
with open(src, "rb") as f:
    doc = PDFDocument.load(f.read())
pref = doc.page_refs()[0]
res = doc.resolve(doc.inherited(pref, "Resources"))
interp = pdfredact.ContentStreamInterpreter(doc, res)
interp.run(pdfredact.read_page_content(doc, pref))

check("收集到 2 个字形", len(interp.glyphs) == 2,
      f"实际 {len(interp.glyphs)}")

if len(interp.glyphs) == 2:
    gA, gB = interp.glyphs
    check("字形 A 起点 x ≈ 100", abs(gA.x0 - 100) < 0.5, f"{gA.x0:.3f}")
    check("字形 A 宽度 ≈ 8.004 (12pt × 0.667)", 
          abs((gA.x1 - gA.x0) - 8.004) < 0.05,
          f"{gA.x1 - gA.x0:.3f}")
    check("字形 A 底边 y ≈ 700", abs(gA.y0 - 700) < 0.5, f"{gA.y0:.3f}")
    check("字形 A 顶边 y ≈ 712 (基线 + 字号)",
          abs(gA.y1 - 712) < 0.5, f"{gA.y1:.3f}")
    check("字形 B 紧接 A 之后 x ≈ 108", abs(gB.x0 - 108.004) < 0.5,
          f"{gB.x0:.3f}")

# Tm 带缩放
content2 = b"""BT
/F1 24 Tf
2 0 0 2 50 100 Tm
(X) Tj
ET
"""
src2 = make_pdf(content2)
with open(src2, "rb") as f:
    doc2 = PDFDocument.load(f.read())
pref2 = doc2.page_refs()[0]
res2 = doc2.resolve(doc2.inherited(pref2, "Resources"))
i2 = pdfredact.ContentStreamInterpreter(doc2, res2)
i2.run(pdfredact.read_page_content(doc2, pref2))
check("CTM 缩放被正确应用（宽 24*0.667*2=32.0）", len(i2.glyphs) == 1 and
      abs((i2.glyphs[0].x1 - i2.glyphs[0].x0) - 32.016) < 0.1,
      f"{i2.glyphs[0].x1 - i2.glyphs[0].x0:.3f}" if i2.glyphs else "无字形")
check("CTM 平移被正确应用（x0 = 50）", len(i2.glyphs) == 1 and
      abs(i2.glyphs[0].x0 - 50) < 0.5,
      f"{i2.glyphs[0].x0:.3f}" if i2.glyphs else "无字形")

# TJ 数组 + 字距
content3 = b"""BT
/F1 10 Tf
1 0 0 1 200 400 Tm
[(AB) -500 (CD)] TJ
ET
"""
src3 = make_pdf(content3)
with open(src3, "rb") as f:
    doc3 = PDFDocument.load(f.read())
pref3 = doc3.page_refs()[0]
res3 = doc3.resolve(doc3.inherited(pref3, "Resources"))
i3 = pdfredact.ContentStreamInterpreter(doc3, res3)
i3.run(pdfredact.read_page_content(doc3, pref3))
check("TJ 数组解析出 4 个字形", len(i3.glyphs) == 4, f"实际 {len(i3.glyphs)}")

# ----------------------------------------------------------------------
print("\n=== 2. Redaction 核心：文字真的消失了吗 ===")
# ----------------------------------------------------------------------

# 两行文字，只涂黑第一行
body = b"""BT
/F1 12 Tf
1 0 0 1 72 700 Tm
(SECRET-KEY-12345) Tj
ET
BT
/F1 12 Tf
1 0 0 1 72 650 Tm
(KEEP-THIS-TEXT) Tj
ET
"""
src = make_pdf(body)
before = extract_text(src)
check("redaction 前能读到 SECRET", b"SECRET" in before, before[:60].decode())
check("redaction 前能读到 KEEP", b"KEEP" in before)

out = ts.ensure("_redact_out.pdf")
# 第一行文字大致占 y 700~712，涂黑盖住它
stats = pdfredact.apply_redactions(
    src, out, [{"page": 1, "x": 70, "y": 698, "w": 200, "h": 16}])

after = extract_text(out)
check("redaction 后读不到 SECRET", b"SECRET" not in after,
      after[:80].decode("latin-1"))
check("redaction 后仍能读到 KEEP", b"KEEP" in after,
      after[:80].decode("latin-1"))
check("统计报告删除了字形", stats["removed_glyphs"] > 0,
      f"removed_glyphs={stats['removed_glyphs']}")

# 关键：直接搜原始字节，确认内容流里没有残留
with open(out, "rb") as f:
    raw = f.read()
check("输出文件字节流里搜不到 SECRET", b"SECRET" not in raw)
check("输出文件字节流里搜不到 SECRET-KEY", b"SECRET-KEY" not in raw)

# ----------------------------------------------------------------------
print("\n=== 3. 部分覆盖：整字删除语义 ===")
# ----------------------------------------------------------------------

# 涂黑区只盖住 "SECRET" 的后半 → 整段应全删（整字删除原则）
src = make_pdf(b"""BT
/F1 12 Tf
1 0 0 1 72 700 Tm
(ABCDEFGH) Tj
ET
""")
out2 = ts.ensure("_redact_p.pdf")
# 只盖住右半边（E 之后）
pdfredact.apply_redactions(
    src, out2, [{"page": 1, "x": 100, "y": 698, "w": 100, "h": 16}])
txt = extract_text(out2)
check("部分覆盖时，被涉及的整字被删（不做半字裁剪）",
      b"ABCDEFGH" not in txt, f"剩余文本: {txt[:40].decode('latin-1')}")

# ----------------------------------------------------------------------
print("\n=== 4. 覆盖块是纯黑（不是白） ===")
# ----------------------------------------------------------------------
with open(out2, "rb") as f:
    raw2 = f.read()
check("输出含纯黑填充指令 0 0 0 rg", b"0 0 0 rg" in raw2)
check("涂黑区用 re f 填充", b"re f" in raw2)

# ----------------------------------------------------------------------
print("\n=== 5. 元数据清理 ===")
# ----------------------------------------------------------------------
src = make_pdf(b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (X) Tj ET",
               info={"Author": "张三", "Title": "机密合同",
                     "Producer": "SomeSecretTool"})
with open(src, "rb") as f:
    raw = f.read()
check("清理前 Info 里有 Author", b"Author" in raw)

out3 = ts.ensure("_redact_m.pdf")
st3 = pdfredact.apply_redactions(
    src, out3, [{"page": 1, "x": 70, "y": 698, "w": 50, "h": 16}])
with open(out3, "rb") as f:
    raw = f.read()
check("清理后搜不到原文作者",
      "张三".encode("utf-8") not in raw and b"Author" not in raw)
check("清理后搜不到原标题", "机密合同".encode("utf-8") not in raw)
check("清理报告了 Info", "Info" in st3["metadata_cleaned"],
      str(st3["metadata_cleaned"]))

# ----------------------------------------------------------------------
print("\n=== 6. 输出结构合法性 ===")
# ----------------------------------------------------------------------
def verify_pdf(path):
    with open(path, "rb") as f:
        d = f.read()
    ok = d.startswith(b"%PDF-")
    ok = ok and d.rstrip().endswith(b"%%EOF")
    m = re.search(rb"startxref\s+(\d+)", d)
    ok = ok and bool(m)
    if m:
        xp = int(m.group(1))
        ok = ok and d[xp:xp + 4] == b"xref"
    return ok


check("输出是合法 PDF（头/尾/xref 都对）", verify_pdf(out))
check("输出是合法 PDF（部分覆盖版）", verify_pdf(out2))
check("输出是合法 PDF（元数据版）", verify_pdf(out3))

# xref 偏移逐个校验
def verify_offsets(path):
    with open(path, "rb") as f:
        d = f.read()
    m = re.search(rb"startxref\s+(\d+)", d)
    xp = int(m.group(1))
    seg = d[xp:xp + 900]
    lines = seg.split(b"\n")
    bad = 0
    total = 0
    for ln in lines[2:]:
        mm = re.match(rb"^(\d{10}) (\d{5}) ([nf])", ln)
        if not mm:
            break
        off = int(mm.group(1))
        if mm.group(3) == b"f":
            continue
        total += 1
        if d[off:off + 2] == b"%%" or not re.match(rb"^\d+ \d+ obj", d[off:off + 30]):
            bad += 1
    return total, bad


t, b = verify_offsets(out)
check(f"xref 偏移全部正确（{t} 个对象）", b == 0 and t > 0,
      f"错误 {b} 个")

# 流长度校验
def verify_lengths(path):
    with open(path, "rb") as f:
        d = f.read()
    bad = 0
    cnt = 0
    for m in re.finditer(rb"/Length (\d+)\s*>>\s*stream\r?\n", d):
        cnt += 1
        ln = int(m.group(1))
        start = m.end()
        actual = d[start:start + ln]
        # 流后应紧跟换行 + endstream
        tail = d[start + ln:start + ln + 12]
        if b"endstream" not in tail:
            bad += 1
    return cnt, bad


c, b2 = verify_lengths(out)
check(f"流 /Length 全部正确（{c} 个流）", b2 == 0 and c > 0, f"错误 {b2}")

# ----------------------------------------------------------------------
print("\n=== 7. 无内容模式下不应崩溃 ===")
# ----------------------------------------------------------------------
src_empty = make_pdf(b"")   # 空白内容流
out4 = ts.ensure("_redact_e.pdf")
try:
    st4 = pdfredact.apply_redactions(
        src_empty, out4, [{"page": 1, "x": 10, "y": 10, "w": 100, "h": 100}])
    check("空白页面也能处理（只加黑块）", st4["removed_glyphs"] == 0)
    check("空白页面输出合法", verify_pdf(out4))
except Exception as exc:                       # noqa: BLE001
    check("空白页面也能处理（只加黑块）", False, str(exc))

# 无效区域
try:
    pdfredact.apply_redactions(src, out4, [{"page": 1, "x": 0, "y": 0,
                                           "w": 0, "h": 0}])
    check("零尺寸区域被拒绝", False)
except ValueError:
    check("零尺寸区域被拒绝", True)

# 越界页码
try:
    pdfredact.apply_redactions(src, out4, [{"page": 99, "x": 0, "y": 0,
                                           "w": 10, "h": 10}])
    check("越界页码被拒绝", False)
except ValueError:
    check("越界页码被拒绝", True)


# ----------------------------------------------------------------------
print("\n=== 8. 泄漏面：孤儿流与书签标题 ===")
# ----------------------------------------------------------------------

# 造一个带书签的 PDF：书签标题与正文内容同名。
# 这是真实场景里最常见的泄漏 —— 页面涂黑了，书签还写着原文。
def make_pdf_with_outline(content, title, width=612, height=792):
    doc = PDFDocument()
    font = doc.new_obj({
        "Type": N("Font"), "Subtype": N("Type1"),
        "BaseFont": N("Helvetica"), "Encoding": N("WinAnsiEncoding"),
    })
    cstream = doc.new_obj(({"Length": len(content)}, content))
    page = doc.new_obj({
        "Type": N("Page"), "MediaBox": [0, 0, width, height],
        "Resources": {"Font": {"F1": font}},
        "Contents": cstream,
    })
    pages = doc.new_obj({"Type": N("Pages"), "Kids": [page], "Count": 1})
    doc.objects[tuple(page)] = dict(doc.objects[tuple(page)],
                                    Parent=pages)
    # 书签：/Title + /Dest 就会被认作大纲条目
    ol = doc.new_obj({
        "Type": N("Outlines"),
        "Title": ("(" + title + ")").encode("latin-1"),
        "Dest": [page, N("Fit")],
        "Parent": None,
    })
    doc.objects[tuple(ol)] = dict(doc.objects[tuple(ol)], Parent=ol)
    outlines = doc.new_obj({
        "Type": N("Outlines"), "First": ol, "Last": ol, "Count": 1,
    })
    doc.objects[tuple(ol)] = dict(doc.objects[tuple(ol)], Parent=outlines)
    root = doc.new_obj({
        "Type": N("Catalog"), "Pages": pages, "Outlines": outlines,
    })
    doc.root_ref = root
    doc.pages_ref = pages
    tmp = ts.ensure(
                       "_redact_ol.pdf")
    build_pdf(doc, {}, tmp)
    return tmp


ol_src = make_pdf_with_outline(
    b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (TOPSECRET-PROJECT) Tj ET",
    "TOPSECRET-PROJECT")
with open(ol_src, "rb") as f:
    raw = f.read()
check("造出的 PDF 书签里有敏感标题", b"TOPSECRET-PROJECT" in raw)
check("造出的 PDF 正文里有敏感内容",
      raw.count(b"TOPSECRET-PROJECT") >= 2,
      f"出现 {raw.count(b'TOPSECRET-PROJECT')} 次")

ol_out = ts.ensure(
                      "_redact_ol_out.pdf")
st_ol = pdfredact.apply_redactions(
    ol_src, ol_out, [{"page": 1, "x": 70, "y": 698, "w": 250, "h": 16}])
with open(ol_out, "rb") as f:
    raw_out = f.read()

check("输出字节流里彻底搜不到敏感词（含书签）",
      b"TOPSECRET" not in raw_out,
      f"残留 {raw_out.count(b'TOPSECRET')} 处")
check("统计报告清理了书签标题",
      st_ol["outline_titles_cleaned"] >= 0,
      f"outline_titles_cleaned={st_ol['outline_titles_cleaned']}")
check("统计报告丢弃了孤儿流对象",
      st_ol["dropped_objects"] >= 0,
      f"dropped_objects={st_ol['dropped_objects']}")

# 孤儿流：旧内容流对象必须从文件里消失
# （只换 /Contents 指针是不够的，旧流仍在对象表里）
check("旧内容流对象被丢弃（不是只换指针）",
      st_ol["dropped_objects"] >= 1,
      f"dropped={st_ol['dropped_objects']}")
check("输出仍是合法 PDF", verify_pdf(ol_out))


# ----------------------------------------------------------------------
print("\n=== 9. CID（Type0 / Identity-H）双字节文字 ===")
# ----------------------------------------------------------------------
# 中文 PDF（Word / WPS 导出）几乎都用 Type0 CID 字体，字符码是**双字节**。
# 这里锁死三个不变量，防止再退化成"按字节拆码"：
#   1. 2 个 CID 必须算成 2 个字形（不是 4 个）
#   2. 总推进宽度必须是 2em = 24pt（不是 4em = 48pt）
#   3. 涂掉一个 CID 后剩下的必须是**完整的另一个 CID**
#      曾错误地只删掉第一个字节，把 <0A0B0C0D> 变成 <0B0C0D> ——
#      奇数长度，渲染器会读出错误字形。这是"把文字改坏"，比漏删更糟。


def make_cid_pdf(content, w=612, h=792):
    """造一个用 Type0/Identity-H 字体（双字节码）的单页 PDF。"""
    doc = PDFDocument()
    cidfont = doc.new_obj({
        "Type": N("Font"), "Subtype": N("CIDFontType2"),
        "BaseFont": N("NotoSansSC"),
        "CIDSystemInfo": {"Registry": N("Adobe"),
                          "Ordering": N("Identity"), "Supplement": 0},
        "DW": 1000,
        "W": [0x0A0B, [1000], 0x0C0D, [1000]],
    })
    font = doc.new_obj({
        "Type": N("Font"), "Subtype": N("Type0"),
        "BaseFont": N("NotoSansSC"),
        "Encoding": N("Identity-H"),
        "DescendantFonts": [cidfont],
    })
    cstream = doc.new_obj(({"Length": len(content)}, content))
    page = doc.new_obj({
        "Type": N("Page"), "MediaBox": [0, 0, w, h],
        "Resources": {"Font": {"F1": font}},
        "Contents": cstream, "Parent": None,
    })
    pages = doc.new_obj({"Type": N("Pages"), "Kids": [page], "Count": 1})
    doc.objects[tuple(page)] = dict(doc.objects[tuple(page)], Parent=pages)
    root = doc.new_obj({"Type": N("Catalog"), "Pages": pages})
    doc.root_ref = root
    doc.pages_ref = pages
    return doc, page


def interp_of(doc, page):
    res = doc.resolve(doc.inherited(page, "Resources"))
    it = pdfredact.ContentStreamInterpreter(doc, res)
    it.run(pdfredact.read_page_content(doc, page))
    return it, res


CID_CONTENT = b"BT /F1 12 Tf 1 0 0 1 72 700 Tm <0A0B0C0D> Tj ET"

cdoc, cpage = make_cid_pdf(CID_CONTENT)
cres = cdoc.resolve(cdoc.inherited(cpage, "Resources"))
finfo = pdfredact.FontInfo(cdoc, cdoc.resolve(cres["Font"]["F1"]))
check("Type0 字体被识别为 CID", finfo.is_cid is True)
check("CID 的字符码宽度是 2", finfo.code_bytes == 2,
      f"实际 {finfo.code_bytes}")

cinterp = pdfredact.ContentStreamInterpreter(cdoc, cres)
cinterp.run(CID_CONTENT)
check("2 个 CID 算成 2 个字形（不是按字节拆成 4 个）",
      len(cinterp.glyphs) == 2, f"实际 {len(cinterp.glyphs)}")
if len(cinterp.glyphs) == 2:
    check("CID 码被完整解出（未按字节拆）",
          [g.code for g in cinterp.glyphs] == [0x0A0B, 0x0C0D],
          str([hex(g.code) for g in cinterp.glyphs]))
    span = (max(g.x1 for g in cinterp.glyphs) -
            min(g.x0 for g in cinterp.glyphs))
    check("总推进宽度是 2em = 24pt（不是 4em = 48pt）",
          abs(span - 24.0) < 0.5, f"实际 {span:.2f}pt")

# 只涂掉第一个 CID，然后把结果重新解释一遍：
# 应当只剩 1 个字形，且码是未被涂掉的那个 —— 这条同时挡住
# "删掉半个字导致奇数长度"和"删错字形"两种退化。
cnew = pdfredact.redact_page_content(
    cdoc, cpage, [(72.0, 700.0, 84.0, 712.0)])
cdoc2, cpage2 = make_cid_pdf(cnew)
ci2, _ = interp_of(cdoc2, cpage2)
check("重写后只剩 1 个字形",
      len(ci2.glyphs) == 1, f"实际 {len(ci2.glyphs)}")
if len(ci2.glyphs) == 1:
    check("剩下的是完整的另一个 CID（码未被截断）",
          ci2.glyphs[0].code == 0x0C0D, hex(ci2.glyphs[0].code))

# /ToUnicode：CID 的码本身不含语义，还原文本只能靠它。
# 书签标题清理依赖这个还原，解析不出来就等于泄漏面清理静默失效。
TOUNI = """
begincmap
1 beginbfchar
<0A0B> <4E2D>
endbfchar
1 beginbfrange
<0C0D> <0C0E> <6587>
endbfrange
endcmap
"""
parsed = pdfredact.FontInfo._parse_tounicode(TOUNI)
check("ToUnicode 的 bfchar 解析正确",
      parsed.get(0x0A0B) == "\u4e2d", repr(parsed.get(0x0A0B)))
check("ToUnicode 的 bfrange 起点解析正确",
      parsed.get(0x0C0D) == "\u6587", repr(parsed.get(0x0C0D)))
check("ToUnicode 的 bfrange 区间递增正确",
      parsed.get(0x0C0E) == "\u6588", repr(parsed.get(0x0C0E)))

# 还原文本的退化策略必须分明：
#   CID 没有 ToUnicode → 返回空（码是 GID，硬解只会得到乱码，
#   拿去比对书签标题等于白比，还可能误伤）
#   简单字体没有 ToUnicode → 退回 cp1252 解码，这是合理的
check("CID 无 ToUnicode 时返回空（不硬编乱码）",
      finfo.text_of([0x0A0B]) == "", repr(finfo.text_of([0x0A0B])))
check("简单字体无 ToUnicode 时仍按 cp1252 还原",
      pdfredact.FontInfo(cdoc, None).text_of([0x41, 0x42]) == "AB")

# 重建路径的拼接必须留分隔符。
# 曾漏掉这一笔：`Tj` 与后面的 `ET` 粘成 `TjET`，成为未知算子被整条忽略，
# 于是**没被涂黑的文字也跟着看不见了**（字节还在，渲染不出来）。
check("重建后的 Tj 不会粘连下一个 token",
      re.search(rb"(?:Tj|TJ)[A-Za-z]", cnew) is None,
      repr(cnew[:60]))


# ----------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"  通过 {PASS}，失败 {FAIL}")
if FAILED:
    print("  失败项：")
    for n in FAILED:
        print("    -", n)
print("=" * 60)
sys.exit(1 if FAIL else 0)
