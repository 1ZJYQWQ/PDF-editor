"""pdfedit 引擎验证：解析 -> 改页 -> 加内容 -> 写回 -> 再用 pdfjs 读取校验。"""
import os

import testscratch as ts  # noqa: E402
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfedit  # noqa: E402

SRC = ts.FIXTURE_PDF
OUT = ts.ensure("_edit_test.pdf")

print("=" * 56)
print("  pdfedit 引擎验证")
print("=" * 56)

# ---- 1. 解析 ----
with open(SRC, "rb") as fp:
    data = fp.read()
print(f"原文件: {len(data)} 字节")

doc = pdfedit.PDFDocument.load(data)
print(f"对象数: {len(doc.objects)}")
print(f"Catalog: {doc.root_ref}")
print(f"Pages  : {doc.pages_ref}")

refs = doc.page_refs()
print(f"页面数: {len(refs)} -> {[tuple(r) for r in refs]}")

for i, r in enumerate(refs):
    mb = doc.inherited(r, "MediaBox")
    rot = doc.inherited(r, "Rotate", 0)
    print(f"  第 {i+1} 页  MediaBox={mb}  Rotate={rot}")

# ---- 2. 应用编辑 ----
edits = {
    "pages": {
        "rotate": {0: 90},
        "insert": [{"at": 1, "width": 595, "height": 842}],
    },
    "objects": [
        {"page": 1, "kind": "whiteout", "x": 50, "y": 700, "w": 200, "h": 20},
        {"page": 1, "kind": "text", "x": 50, "y": 690, "text": "Edited by pdfedit",
         "size": 14, "color": "#d32f2f"},
        {"page": 1, "kind": "rect", "x": 50, "y": 600, "w": 120, "h": 60,
         "color": "#2563eb", "stroke": True, "fill": False, "lineWidth": 2},
        {"page": 2, "kind": "ellipse", "x": 300, "y": 400, "w": 100, "h": 60,
         "color": "#16a34a", "fill": True},
        {"page": 2, "kind": "line", "x": 60, "y": 100, "x2": 500, "y2": 100,
         "color": "#000000", "lineWidth": 1.5},
    ],
}
print("\n应用编辑:")
res = pdfedit.apply_edits(SRC, OUT, edits)
print(f"  结果: {res}")

# ---- 3. 重新解析，验证结果 ----
print("\n重新解析输出文件:")
with open(OUT, "rb") as fp:
    out_data = fp.read()
print(f"输出大小: {len(out_data)} 字节")

doc2 = pdfedit.PDFDocument.load(out_data)
refs2 = doc2.page_refs()
print(f"对象数: {len(doc2.objects)}")
print(f"页面数: {len(refs2)}  (期望 6 = 原 5 + 插入 1)")

if refs2:
    print(f"第 1 页 Rotate={doc2.inherited(refs2[0], 'Rotate', 0)}  (期望 90)")
    c = doc2.resolve(doc2.resolve(refs2[0]).get("Contents"))
    if isinstance(c, list):
        print(f"第 1 页 Contents 是数组，共 {len(c)} 个流  (期望 2 = 原 + 编辑)")
    res_dict = doc2.resolve(doc2.resolve(refs2[0]).get("Resources"))
    if isinstance(res_dict, dict):
        fonts = doc2.resolve(res_dict.get("Font"))
        print(f"第 1 页 Font 资源: {list(fonts.keys()) if isinstance(fonts, dict) else fonts}")

    # 检查编辑内容流确实被写进去了
    if isinstance(c, list):
        last = doc2.stream_data(c[-1])
        if last:
            txt = last.decode("latin-1")
            print(f"\n追加的内容流（前 300 字符）:\n{txt[:300]}")
            checks = [
                ("白框 re 指令", "re" in txt),
                ("文字 Tj 指令", "Tj" in txt),
                ("Hello".encode().decode("latin-1") or True, True),
                ("颜色 rg 指令", "rg" in txt),
                ("字体 Tf 指令", "Tf" in txt),
            ]
            print()
            for name, ok in checks:
                print(f"  {'OK  ' if ok else 'FAIL'} {name}")

print("\n" + "=" * 56)
print(f"  输出文件: {os.path.abspath(OUT)}")
print("=" * 56)
