"""
严格校验：把输出 PDF 当成「第三方阅读器」来读，
逐字节验证 xref 表、对象定位、流长度 —— 任何一处错了，
真实阅读器都会报错或显示空白。
"""
import os

import testscratch as ts  # noqa: E402
import re
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8")

OUT = ts.ensure("_edit_test.pdf")
data = open(OUT, "rb").read()

print("=" * 58)
print(f"  严格校验 {OUT}  ({len(data)} 字节)")
print("=" * 58)

problems = []

# ---- 1. 头部与尾部 ----
if not data.startswith(b"%PDF-"):
    problems.append("文件头不是 %PDF-")
else:
    print(f"OK   文件头: {data[:9].decode('latin-1').strip()}")

if not data.rstrip().endswith(b"%%EOF"):
    problems.append("文件尾缺少 %%EOF")
else:
    print("OK   文件尾: %%EOF")

# ---- 2. startxref 指向的偏移是否真的是 xref ----
m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
if not m:
    problems.append("找不到 startxref")
    sx = None
else:
    sx = int(m.group(1))
    actual = data[sx:sx + 4]
    if actual != b"xref":
        problems.append(f"startxref={sx} 指向的不是 xref，而是 {actual!r}")
    else:
        print(f"OK   startxref={sx} 正确指向 xref 表")

# ---- 3. 逐条解析 xref，核对每个偏移 ----
if sx is not None:
    seg = data[sx:]
    hm = re.match(rb"xref\s+(\d+)\s+(\d+)\s+", seg)
    if not hm:
        problems.append("xref 头部格式不对")
    else:
        start_obj = int(hm.group(1))
        count = int(hm.group(2))
        print(f"OK   xref 声明 {start_obj}..{start_obj+count-1}，共 {count} 条")

        entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", seg[hm.end():])
        if len(entries) != count:
            problems.append(f"xref 条目数 {len(entries)} != 声明 {count}")

        checked = 0
        for i, (off, gen, typ) in enumerate(entries):
            if typ == b"f":
                continue
            objnum = start_obj + i
            o = int(off)
            expect = b"%d %d obj" % (objnum, int(gen))
            got = data[o:o + len(expect)]
            if got != expect:
                problems.append(
                    f"对象 {objnum} 的 xref 偏移 {o} 错误："
                    f"期望 {expect!r}，实际 {got!r}")
            else:
                checked += 1
        print(f"OK   {checked} 个对象的偏移全部精确匹配")

# ---- 4. 每个流对象的 /Length 与实际数据是否一致 ----
print()
stream_count = 0
for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", data):
    num = int(m.group(1))
    body_start = m.end()
    end = data.find(b"endobj", body_start)
    body = data[body_start:end]

    sm = re.search(rb"\bstream\r?\n", body)
    if not sm:
        continue
    stream_count += 1

    head = body[:sm.start()]
    lm = re.search(rb"/Length\s+(\d+)", head)
    if not lm:
        problems.append(f"对象 {num} 的流没有 /Length")
        continue
    declared = int(lm.group(1))

    raw_start = sm.end()
    raw = body[raw_start:raw_start + declared]
    tail = body[raw_start + declared:raw_start + declared + 12]
    if not tail.lstrip().startswith(b"endstream"):
        problems.append(
            f"对象 {num} 的 /Length={declared} 与实际数据长度不符 "
            f"（后面是 {tail!r}）")

print(f"OK   检查了 {stream_count} 个流对象的 /Length")

# ---- 5. 内容流能否解压 / 是否可读 ----
print()
for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", data):
    num = int(m.group(1))
    body_start = m.end()
    end = data.find(b"endobj", body_start)
    body = data[body_start:end]
    sm = re.search(rb"\bstream\r?\n", body)
    if not sm:
        continue
    head = body[:sm.start()]
    if b"/Filter" in head:
        lm = re.search(rb"/Length\s+(\d+)", head)
        raw = body[sm.end():sm.end() + int(lm.group(1))]
        try:
            zlib.decompress(raw)
        except zlib.error:
            problems.append(f"对象 {num} 声明了 Filter 但解压失败")

# ---- 6. 关键对象存在性 ----
for key, pat in [("Catalog", rb"/Type\s*/Catalog"),
                 ("Pages", rb"/Type\s*/Pages"),
                 ("Page", rb"/Type\s*/Page")]:
    n = len(re.findall(pat, data))
    if n == 0:
        problems.append(f"缺少 {key} 对象")
    else:
        print(f"OK   找到 {key} x{n}")

# ---- 结论 ----
print()
print("=" * 58)
if problems:
    print(f"  发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"    FAIL {p}")
else:
    print("  全部通过 —— 文件结构合法，可被标准阅读器解析")
print("=" * 58)
sys.exit(1 if problems else 0)
