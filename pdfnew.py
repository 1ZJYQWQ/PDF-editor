import sys

SIZES = {
    "a4": (595.28, 841.89),
    "a3": (841.89, 1190.55),
    "a5": (419.53, 595.28),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}

MAX_PAGES = 200

def page_size(size="a4", landscape=False):
    key = (size or "a4").lower()
    if key not in SIZES:
        raise ValueError("不支持的纸张：%s" % size)
    w, h = SIZES[key]
    return (h, w) if landscape else (w, h)

def build_blank_pdf(pages=1, size="a4", landscape=False):
    w, h = page_size(size, landscape)
    try:
        n = int(pages)
    except (TypeError, ValueError):
        raise ValueError("页数必须是整数")
    if n < 1:
        raise ValueError("至少要有 1 页")
    if n > MAX_PAGES:
        raise ValueError("页数太多了（上限 %d 页）" % MAX_PAGES)

    kids = " ".join("%d 0 R" % (3 + i * 2) for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)).encode(),
    ]
    for i in range(n):
        cnum = 4 + i * 2
        objs.append((
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.4f %.4f] "
            "/Resources << >> /Contents %d 0 R >>" % (w, h, cnum)
        ).encode())
        objs.append(b"<< /Length 0 >>\nstream\n\nendstream")

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"

    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)

def main(argv):
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    size, pages, land = "a4", 1, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--size" and i + 1 < len(argv):
            size = argv[i + 1]
            i += 2
        elif a == "--pages" and i + 1 < len(argv):
            pages = int(argv[i + 1])
            i += 2
        elif a == "--landscape":
            land = True
            i += 1
        else:
            i += 1
    data = build_blank_pdf(pages, size, land)
    with open(path, "wb") as fp:
        fp.write(data)
    print("已生成 %s（%d 页，%s%s，%d 字节）"
          % (path, pages, size, " 横版" if land else "", len(data)))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
