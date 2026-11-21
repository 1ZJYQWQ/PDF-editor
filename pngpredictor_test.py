import os
import sys
import zlib

import testscratch as ts

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfedit

fails = 0

def check(label, ok, extra=""):
    global fails
    print(("  OK   " if ok else "  FAIL ") + label + (("   " + str(extra)) if extra else ""))
    if not ok:
        fails += 1

def make_pdf_with_image(w=8, h=8):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes([(x * 31) % 256, (y * 37) % 256, 128])
    com = zlib.compress(bytes(raw))

    content = b"q 50 0 0 50 25 25 cm /Im1 Do Q"
    objs = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources <</XObject <</Im1 5 0 R>>>> /Contents 4 0 R>>",
        b"<</Length %d>>\nstream\n" % len(content) + content + b"\nendstream",
        b"<</Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        b"/DecodeParms <</Predictor 15 /Colors 3 /BitsPerComponent 8 "
        b"/Columns %d>> /Length %d>>\nstream\n" % (w, h, w, len(com)) + com + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out), com, bytes(raw), (w, h)

print("=" * 60)
print("  1. 解析：带预测器的图片流不能被解压")
print("=" * 60)

src_pdf, com, pixels, (W, H) = make_pdf_with_image()
doc = pdfedit.PDFDocument.load(src_pdf)
key = (5, 0)
val = doc.objects.get(key)
ok_tuple = isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], dict)
check("图片对象存在且是流", ok_tuple)

if ok_tuple:
    dic, sdata = val
    check("保留 /Filter", str(dic.get("Filter")).lstrip("/") == "FlateDecode",
          dic.get("Filter"))
    parms = dic.get("DecodeParms")
    check("保留 /DecodeParms", isinstance(parms, dict) and parms.get("Predictor") == 15,
          parms)
    check("数据仍是压缩态（没被解压）", sdata == com,
          "%d 字节 vs 原始压缩 %d 字节" % (len(sdata), len(com)))
    check("/Length 等于压缩长度", dic.get("Length") == len(com), dic.get("Length"))
    check("压缩流解开后是预测器数据（每行多 1 字节）",
          zlib.decompress(sdata) == pixels, "%d 字节" % len(pixels))

print()
print("=" * 60)
print("  2. 写回：重排/导出之后图片声明必须还在")
print("=" * 60)

out_path = ts.ensure("_pngpredictor_test.pdf")
pdfedit.build_pdf(doc, {}, out_path)
raw_out = open(out_path, "rb").read()
check("写出的文件含 /Filter /FlateDecode", b"/Filter /FlateDecode" in raw_out)
check("写出的文件含 /Predictor 15", b"/Predictor 15" in raw_out)
check("写出的文件体积接近原始（没被解压膨胀）",
      len(raw_out) < len(src_pdf) * 3, "%d 字节（原始 %d）" % (len(raw_out), len(src_pdf)))

doc2 = pdfedit.PDFDocument.load(raw_out)
val2 = doc2.objects.get(key)
if isinstance(val2, tuple) and len(val2) == 2:
    dic2, sdata2 = val2
    check("二次解析仍有 /DecodeParms", isinstance(dic2.get("DecodeParms"), dict))
    check("二次解析的像素数据与原始一致",
          zlib.decompress(sdata2) == pixels, "%d 字节" % len(zlib.decompress(sdata2)))
else:
    check("二次解析能拿到图片流", False, val2)

print()
print("=" * 60)
print("  3. 对照：普通 Flate 流仍要解压并去掉 /Filter")
print("=" * 60)

plain = zlib.compress(b"BT /F1 12 Tf 10 10 Td (hello) Tj ET")
objs_plain = [
    b"<</Type /Catalog /Pages 2 0 R>>",
    b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
    b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R>>",
    b"<</Length %d /Filter /FlateDecode>>\nstream\n" % len(plain) + plain + b"\nendstream",
]
out = bytearray(b"%PDF-1.7\n")
offs = []
for i, o in enumerate(objs_plain, 1):
    offs.append(len(out))
    out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
xref = len(out)
out += b"xref\n0 %d\n" % (len(objs_plain) + 1) + b"0000000000 65535 f \n"
for off in offs:
    out += b"%010d 00000 n \n" % off
out += (b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objs_plain) + 1, xref))

docp = pdfedit.PDFDocument.load(bytes(out))
valp = docp.objects.get((4, 0))
if isinstance(valp, tuple) and len(valp) == 2:
    dicp, sdatap = valp
    check("普通流被解压成明文", sdatap == b"BT /F1 12 Tf 10 10 Td (hello) Tj ET",
          sdatap[:40])
    check("普通流去掉了 /Filter", dicp.get("Filter") is None, dicp.get("Filter"))
else:
    check("普通内容流可读", False, valp)

print()
print("=" * 60)
print("  4. JPEG(DCTDecode) 流：没解码它，/Filter 与字节都必须原样留着")
print("=" * 60)

jpeg_bytes = (b"\xff\xd8"
              b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
              b"\xff\xd9")
content_j = b"q 50 0 0 50 25 25 cm /Im1 Do Q"
objs_j = [
    b"<</Type /Catalog /Pages 2 0 R>>",
    b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
    b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
    b"/Resources <</XObject <</Im1 5 0 R>>>> /Contents 4 0 R>>",
    b"<</Length %d>>\nstream\n" % len(content_j) + content_j + b"\nendstream",
    b"<</Type /XObject /Subtype /Image /Width 8 /Height 8 "
    b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
    b"/Length %d>>\nstream\n" % len(jpeg_bytes) + jpeg_bytes + b"\nendstream",
]
outj = bytearray(b"%PDF-1.7\n")
offs_j = []
for i, o in enumerate(objs_j, 1):
    offs_j.append(len(outj))
    outj += b"%d 0 obj\n" % i + o + b"\nendobj\n"
xref_j = len(outj)
outj += b"xref\n0 %d\n" % (len(objs_j) + 1) + b"0000000000 65535 f \n"
for off in offs_j:
    outj += b"%010d 00000 n \n" % off
outj += (b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
         % (len(objs_j) + 1, xref_j))
src_j = bytes(outj)

docj = pdfedit.PDFDocument.load(src_j)
valj = docj.objects.get((5, 0))
if isinstance(valj, tuple) and len(valj) == 2:
    dicj, dataj = valj
    check("JPEG 流的 /Filter 保住了",
          str(dicj.get("Filter")).lstrip("/") == "DCTDecode", dicj.get("Filter"))
    check("JPEG 字节没被改动", dataj == jpeg_bytes, "%d 字节" % len(dataj))
else:
    check("JPEG 图片流可读", False, valj)

out_j = ts.ensure("_jpeg_stream_test.pdf")
pdfedit.build_pdf(docj, {}, out_j)
raw_j = open(out_j, "rb").read()
check("写出的文件仍声明 /DCTDecode", b"/DCTDecode" in raw_j)
check("写出的文件里能找到 JPEG 头", jpeg_bytes in raw_j)
docj2 = pdfedit.PDFDocument.load(raw_j)
valj2 = docj2.objects.get((5, 0))
if isinstance(valj2, tuple) and len(valj2) == 2:
    check("二次解析字节仍一致", valj2[1] == jpeg_bytes, "%d 字节" % len(valj2[1]))
    check("二次解析声明仍一致",
          str(valj2[0].get("Filter")).lstrip("/") == "DCTDecode",
          valj2[0].get("Filter"))
else:
    check("二次解析能拿到 JPEG 流", False, valj2)

import pdfcrypt
enc_path = ts.ensure("_jpeg_stream_enc.pdf")
enc = pdfcrypt.SecurityEncoder(b"jpgpw", b"",
                               perms=pdfcrypt.make_permissions(True, True, False),
                               algorithm="aes256")
pdfedit.build_pdf(pdfedit.PDFDocument.load(src_j), {}, enc_path, security=enc)
dje0 = pdfedit.PDFDocument.load(open(enc_path, "rb").read())
check("加密的 JPEG 文档确实需要口令", dje0.encrypted and not dje0.password_ok,
      "encrypted=%s pw_ok=%s" % (dje0.encrypted, dje0.password_ok))
dje = pdfedit.PDFDocument.load(open(enc_path, "rb").read(), b"jpgpw")
plain_j = dje.stream_data(pdfedit.Ref(5, 0))
dicje = dje.objects[(5, 0)][0]
check("加密+延迟解密后字节仍是 JPEG", plain_j == jpeg_bytes, "%d 字节" % len(plain_j))
check("加密+延迟解密后 /Filter 仍保留",
      str(dicje.get("Filter")).lstrip("/") == "DCTDecode", dicje.get("Filter"))

print()
print("=" * 60)
print("  失败 %d 项" % fails)
print("=" * 60)
sys.exit(1 if fails else 0)
