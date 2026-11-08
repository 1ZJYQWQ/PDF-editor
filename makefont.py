import os
import sys

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("PDFVIEWER_CJK_VF") or \
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf"
OUT_DIR = os.path.join(HERE, "assets", "fonts")
OUT = os.path.join(OUT_DIR, "NotoSansSC-Regular.ttf")

KEEP = {"head", "hhea", "maxp", "hmtx", "loca", "glyf",
        "cmap", "OS/2", "post", "name"}

def main(argv):
    wght = argv[1] if len(argv) > 1 else "400"
    if not os.path.exists(SRC):
        print("找不到可变字体源：%s" % SRC)
        print("用环境变量 PDFVIEWER_CJK_VF 指定。")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    print("载入 %s（%.2f MB）…" % (SRC, os.path.getsize(SRC) / 1048576))
    font = TTFont(SRC)

    axes = []
    if "fvar" in font:
        for a in font["fvar"].axes:
            axes.append("%s[%g,%g,%g]"
                        % (a.axisTag, a.minValue, a.defaultValue,
                           a.maxValue))
    print("  可变轴: %s" % (", ".join(axes) or "(无)"))
    print("  字形数: %d" % font["maxp"].numGlyphs)
    if "glyf" not in font:
        print("  ✗ 源字体没有 glyf 表（是 CFF 轮廓）—— 本流程只处理 TrueType。")
        print("    对 .otf/CFF 源请先用 cu2qu 做轮廓转换，或改用 glyf 版本。")
        return 1

    print("实例化 wght=%s …（这一步对 CJK 字体较慢，请耐心）" % wght)
    instancer.instantiateVariableFont(
        font, {"wght": float(wght)}, inplace=True, updateFontNames=True)

    dropped = sorted(set(font.keys()) - KEEP)
    for t in dropped:
        del font[t]
    print("  丢弃的表: %s" % (" ".join(dropped) or "(无)"))

    font.save(OUT)
    print()
    print("写出: %s" % OUT)
    print("体积: %.2f MB" % (os.path.getsize(OUT) / 1048576))
    print()
    print("下一步：用 fontcheck.py 体检（轮廓类型 / 字重 / 覆盖），")
    print("确认无误后把 pdffont.load_default() 指向它。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
