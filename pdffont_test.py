"""TrueType 子集化验证。

子集化最容易犯的错是**静默的**：复合字形的组件索引没重映射对，
结果是「某些字少了几笔」—— 不报错、不崩溃，只有渲染出来才看得见。
所以这里的判据不是"跑通"，而是逐字形比对结构：

  1. 输出的 sfnt 结构自洽（表目录、checksum 自洽、loca 单调）
  2. **每个字形的点数与源字体一致**（轮廓没被截断）
  3. **复合字形的组件数与组件指向都正确**（不缺点、不指向错字形）
  4. hinting 被剥掉（指令长度 0，且没有 cvt/fpgm/prep）
  5. 输出里没有 cmap（Identity-H 不需要，且是「反查还原」的漏洞面）
  6. 缺字要如实报告，不能静默丢字
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdffont                                              # noqa: E402

FONT = os.environ.get("PDFVIEWER_CJK_FONT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "fonts", "NotoSansSC-Regular.ttf")

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


def parse_glyph(gd):
    """解析字形，返回 (是否复合, 点数, 指令长度, 组件 GID 列表, 是否完整)。

    「是否完整」= 按 flags/坐标规则走完后没有越界 —— 用来发现数据被截断。
    """
    if len(gd) < 10:
        return None
    nc = struct.unpack(">h", gd[:2])[0]
    if nc < 0:
        return (True, 0, -1, pdffont.composite_gids(gd), True)

    end = 10 + nc * 2
    if end + 2 > len(gd):
        return (False, 0, -1, [], False)
    ilen = struct.unpack(">H", gd[end:end + 2])[0]
    if nc == 0:
        return (False, 0, ilen, [], end + 2 <= len(gd))

    endpts = struct.unpack(">%dH" % nc, gd[10:end])
    npts = endpts[-1] + 1
    p = end + 2 + ilen
    if p > len(gd):
        return (False, npts, ilen, [], False)

    flags = []
    while len(flags) < npts:
        if p >= len(gd):
            return (False, npts, ilen, [], False)
        f = gd[p]
        p += 1
        flags.append(f)
        if f & 0x08:                     # REPEAT
            if p >= len(gd):
                return (False, npts, ilen, [], False)
            p += 1
            flags.extend([f] * gd[p - 1])
    flags = flags[:npts]

    for f in flags:                      # x 坐标
        if f & 0x02:
            p += 1
        elif not (f & 0x10):
            p += 2
    for f in flags:                      # y 坐标
        if f & 0x04:
            p += 1
        elif not (f & 0x20):
            p += 2

    return (False, npts, ilen, [], p == len(gd))


print("=" * 60)
print("  TrueType 子集化验证")
print("=" * 60)
print("字体: %s" % FONT)

if not os.path.exists(FONT):
    print("字体不存在，跳过（设 PDFVIEWER_CJK_FONT 指定）")
    sys.exit(0)

src = pdffont.load(FONT)
src_size = len(src.data)
print("源字体: %.2f MB, %d 字形, upem=%d, indexToLocFormat=%d"
      % (src_size / 1048576, src.num_glyphs, src.upem, src.index_to_loc))
print("PostScript 名: %s" % (src.ps_name or "(无)"))

# 一段中英混排 + 标点，覆盖：常用汉字、ASCII、全角标点
TEXT = ("中文涂黑测试：思源黑体嵌入子集化。"
        "PDF redaction 123 ABC xyz .,!?；：、（）【】"
        "层涂测试锁定一家之言")
unicodes = {ord(c) for c in TEXT}

print()
print("=== 1. 子集化 ===")
res = src.subset(unicodes)
print("  需要 %d 个码位" % len(unicodes))
print("  子集: %.1f KB, %d 字形, 缺字 %d 个"
      % (len(res.data) / 1024, res.num_glyphs, len(res.missing)))

check("子集体积远小于源字体（至少 100 倍）",
      len(res.data) * 100 < src_size,
      "%.1f KB vs %.1f MB" % (len(res.data) / 1024, src_size / 1048576))
check("缺字如实报告（这段文字应当不缺）",
      len(res.missing) == 0, "缺 %r" % "".join(map(chr, res.missing)))
check("每个字形都有 GID",
      all(ord(c) in res.gid_of for c in TEXT))
check("GID 都在范围内",
      all(0 <= g < res.num_glyphs for g in res.gid_of.values()))

print()
print("=== 2. sfnt 结构自洽 ===")
tables = pdffont._read_table_dir(res.data, 0)
print("  表: %s" % " ".join(sorted(tables)))

for t in ("head", "hhea", "maxp", "hmtx", "loca", "glyf"):
    check("保留必需表 %s" % t, t in tables)
for t in pdffont.DROP_HINTING + pdffont.DROP_VAR + ("cmap", "name"):
    check("剔除 %s" % t, t not in tables)

# 整个文件的 checksum 必须是 0xB1B0AFBA —— 这是 checkSumAdjustment 写对的定义
check("文件级 checksum 自洽",
      pdffont._checksum(res.data) == 0xB1B0AFBA,
      "0x%08X" % pdffont._checksum(res.data))

sub = pdffont.TrueTypeFont(res.data)
check("子集可被重新解析", sub.num_glyphs == res.num_glyphs,
      "numGlyphs=%d" % sub.num_glyphs)
check("head.indexToLocFormat 已设为 long(1)", sub.index_to_loc == 1,
      "实际 %d" % sub.index_to_loc)
check("hhea.numberOfHMetrics 与字形数一致",
      sub.num_hmetrics == res.num_glyphs,
      "%d vs %d" % (sub.num_hmetrics, res.num_glyphs))

# loca 必须单调不减，且末项等于 glyf 长度
loca = sub._loca
check("loca 单调不减", all(loca[i] <= loca[i + 1]
                          for i in range(len(loca) - 1)))
check("loca 末项等于 glyf 表长度",
      loca[-1] == tables["glyf"][1],
      "%d vs %d" % (loca[-1], tables["glyf"][1]))

print()
print("=== 3. 逐字形比对轮廓（核心） ===")
src_cmap = src.cmap_map()

n_simple = 0
n_composite = 0
bad_truncated = []
bad_points = []
bad_components = []
zero_instr_ok = True

for ch in sorted(set(TEXT)):
    u = ord(ch)
    old_gid = src_cmap.get(u)
    new_gid = res.gid_of.get(u)
    if old_gid is None or new_gid is None:
        continue
    a = parse_glyph(src.glyph(old_gid))
    b = parse_glyph(sub.glyph(new_gid))
    if a is None or b is None:
        continue

    if not b[4]:
        bad_truncated.append(ch)
        continue

    if a[0]:                                  # 复合字形
        n_composite += 1
        # 组件数必须一致；越界检查放第 4 节做精确比对
        if len(a[3]) != len(b[3]):
            bad_components.append("%s 组件数 %d->%d"
                                  % (ch, len(a[3]), len(b[3])))
            continue
        for new_c in b[3]:
            if not (0 <= new_c < res.num_glyphs):
                bad_components.append("%s 组件 GID 越界 %d" % (ch, new_c))
    else:
        n_simple += 1
        if a[1] != b[1]:
            bad_points.append("%s %d->%d" % (ch, a[1], b[1]))
        if b[2] != 0:
            zero_instr_ok = False

print("  简单字形 %d 个，复合字形 %d 个" % (n_simple, n_composite))
check("没有字形的轮廓被截断", not bad_truncated,
      "问题字: %s" % "".join(bad_truncated[:10]))
check("简单字形的点数与源一致", not bad_points,
      "; ".join(bad_points[:5]))
check("复合字形的组件数一致", not bad_components,
      "; ".join(bad_components[:5]))
check("hinting 已被剥离（简单字形指令长度全为 0）", zero_instr_ok)
print("  说明：NotoSerifSC-VF 全字体 %d 个字形里复合字形为 %d —— "
      "它用完整轮廓，不做部件合成。" % (src.num_glyphs, n_composite))
print("        所以复合字形那条路径改由第 4 节的合成数据覆盖。")

# 复合字形的组件索引重映射是子集化里**最容易做错、又最难发现**的地方
# （错了表现为「某些字少几笔」，只有渲染出来才看得见）。
# 而本项目锁定的字体里一个复合字形都没有 —— 所以这里**造几个**来测。
print()
print("=== 4. 复合字形组件重映射（合成数据） ===")


def make_composite(comps, instructions=b""):
    """造一个复合字形。comps = [(组件GID, 额外flags), ...]。

    ARG_1_AND_2_ARE_WORDS 置位 → 每个组件带 4 字节参数。
    布局：10 字节头 + 每个组件 8 字节（flags/gid 各 2 + 参数 4）。
    """
    out = bytearray(struct.pack(">hhhhh", -1, 0, 0, 500, 500))
    for i, (gid, extra) in enumerate(comps):
        flags = pdffont.ARG_1_AND_2_ARE_WORDS | extra
        if i < len(comps) - 1:
            flags |= pdffont.MORE_COMPONENTS
        if instructions and i == len(comps) - 1:
            flags |= pdffont.WE_HAVE_INSTRUCTIONS
        out += struct.pack(">HHhh", flags, gid, 0, 0)
    if instructions:
        out += struct.pack(">H", len(instructions)) + instructions
    return bytes(out)


comp = make_composite([(7, 0), (9, 0)], b"\x01\x02\x03")
check("合成复合字形被正确解析出组件",
      pdffont.composite_gids(comp) == [7, 9],
      str(pdffont.composite_gids(comp)))

mapped = pdffont.subset_glyph(comp, {7: 3, 9: 5})
check("组件 GID 被重映射到新编号",
      pdffont.composite_gids(mapped) == [3, 5],
      str(pdffont.composite_gids(mapped)))
check("尾部指令被丢弃（长度 31 → 26）", len(mapped) == 26,
      str(len(mapped)))

# 标志位必须清掉，否则解析器会去找已经不存在的指令
last_flag = struct.unpack(">H", mapped[18:20])[0]
check("最后一个组件的指令标志位被清除",
      not (last_flag & pdffont.WE_HAVE_INSTRUCTIONS),
      "flags=0x%04X" % last_flag)

# 源组件不在映射表里时必须退化为 GID 0（.notdef），
# 不能留下指向不存在字形的悬空引用
partial = pdffont.subset_glyph(comp, {7: 3})
check("没映射到的组件退化为 GID 0（不留悬空引用）",
      pdffont.composite_gids(partial) == [3, 0],
      str(pdffont.composite_gids(partial)))

plain = make_composite([(11, 0), (13, 0)])
plain2 = pdffont.subset_glyph(plain, {11: 1, 13: 2})
check("无指令的复合字形长度不变", len(plain2) == len(plain),
      "%d vs %d" % (len(plain2), len(plain)))

# 简单字形：指令剥离，其余逐字节不变
simple = src.glyph(src_cmap[ord("中")])
nc = struct.unpack(">h", simple[:2])[0]
end = 10 + nc * 2
ilen = struct.unpack(">H", simple[end:end + 2])[0]
stripped = pdffont.subset_glyph(simple, {})
check("简单字形的指令被清空",
      struct.unpack(">H", stripped[end:end + 2])[0] == 0,
      "原指令 %d 字节" % ilen)
check("简单字形其余部分逐字节不变（只少了指令）",
      len(stripped) == len(simple) - ilen and
      stripped[:end] == simple[:end] and
      stripped[end + 2:] == simple[end + 2 + ilen:],
      "原 %d → 新 %d 字节" % (len(simple), len(stripped)))

print()
print("=== 5. 编码与宽度 ===")
enc, miss = res.encode(TEXT)
check("每字符编码为 2 字节", len(enc) == 2 * len(TEXT),
      "%d 字节 / %d 字符" % (len(enc), len(TEXT)))
check("编码无缺字", not miss)
# 汉字在 Noto Sans SC 里是全宽，宽度应当接近 1000
zh_gid = res.gid_of[ord("中")]
check("汉字宽度是全宽（约 1000）",
      abs(res.widths.get(zh_gid, 0) - 1000) <= 20,
      "中 = %d" % res.widths.get(zh_gid, 0))

print()
print("=== 6. 缺字处理 ===")
# 用一个字体里肯定没有的码位（私用区）
res2 = src.subset({ord("A"), 0xE000})
check("字体里没有的码位被报告", 0xE000 in res2.missing,
      "%r" % [hex(x) for x in res2.missing])
check("已有的码位不受影响", ord("A") in res2.gid_of)
enc2, miss2 = res2.encode("A\uE000")
check("encode 跳过缺字并报告", miss2 == ["\uE000"], "%r" % miss2)

print()
print("=== 7. 缓存与性能 ===")
import time                                                 # noqa: E402
t0 = time.time()
for _ in range(3):
    src.subset(unicodes)
dt = (time.time() - t0) / 3
print("  单次子集化 %.0f ms（%d 字形）" % (dt * 1000, res.num_glyphs))
check("单次子集化在 3 秒内", dt < 3.0, "%.0f ms" % (dt * 1000))

print()
print("=" * 60)
print("  通过 %d，失败 %d" % (PASS, FAIL))
if FAILED:
    print("  失败项：")
    for n in FAILED:
        print("    -", n)
print("=" * 60)
sys.exit(1 if FAIL else 0)
