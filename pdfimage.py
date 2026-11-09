
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"

MAX_PIXELS = 8 * 1000 * 1000

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

def _png_chunks(data):
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise ValueError("PNG 数据被截断")
        yield ctype, body
        if ctype == b"IEND":
            return
        pos += 12 + length

def _png_header(data):
    for ctype, body in _png_chunks(data):
        if ctype == b"IHDR":
            if len(body) < 13:
                raise ValueError("PNG 头不完整")
            w, h = struct.unpack(">II", body[:8])
            return w, h, body[8], body[9], body[12]
    raise ValueError("PNG 缺少 IHDR")

def _jpeg_size(data):
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 10 > n:
                break
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            comps = data[i + 9]
            return w, h, comps
        if marker == 0xDA:
            break
        i += 2 + length
    raise ValueError("这个 JPEG 里找不到图像尺寸（可能不是标准 JPEG）")

def inspect(data):
    if not data:
        raise ValueError("空图片")
    if data.startswith(PNG_SIG):
        w, h, _bit, ctype, _il = _png_header(data)
        return {"format": "png", "width": w, "height": h,
                "alpha": ctype in (4, 6)}
    if data[:2] == b"\xff\xd8":
        w, h, comps = _jpeg_size(data)
        return {"format": "jpeg", "width": w, "height": h,
                "alpha": False, "components": comps}
    raise ValueError("只支持 PNG 和 JPEG")

def _unfilter(raw, width, height, bpp):
    stride = width * bpp
    need = stride * height
    if len(raw) < need + height:
        raise ValueError("PNG 像素数据不完整")
    out = bytearray(need)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa = b - c
                if pa < 0:
                    pa = -pa
                pb = a - c
                if pb < 0:
                    pb = -pb
                pc = a + b - 2 * c
                if pc < 0:
                    pc = -pc
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError("PNG 行滤波类型不认识：%d" % ft)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out

def _split_pixels(rows, width, height, ctype, plte, trns):
    n = width * height

    if ctype == 0:
        color = bytes(rows[:n])
        alpha = None
        if trns and len(trns) >= 2:
            key = trns[1]
            a = bytearray(n)
            for i in range(n):
                a[i] = 0 if color[i] == key else 255
            alpha = bytes(a)
        return color, alpha, "DeviceGray"

    if ctype == 2:
        color = bytes(rows[:n * 3])
        alpha = None
        if trns and len(trns) >= 6:
            kr, kg, kb = trns[1], trns[3], trns[5]
            a = bytearray(n)
            for i in range(n):
                j = i * 3
                a[i] = 0 if (color[j] == kr and color[j + 1] == kg
                             and color[j + 2] == kb) else 255
            alpha = bytes(a)
        return color, alpha, "DeviceRGB"

    if ctype == 3:
        if not plte:
            raise ValueError("PNG 有调色板索引但没有 PLTE")
        count = len(plte) // 3
        palette = [bytes(plte[i * 3:i * 3 + 3]) for i in range(count)]
        rgb = bytearray(n * 3)
        a = None
        if trns:
            a = bytearray(n)
        for i in range(n):
            idx = rows[i]
            if idx >= count:
                idx = 0
            j = i * 3
            rgb[j:j + 3] = palette[idx]
            if a is not None:
                a[i] = trns[idx] if idx < len(trns) else 255
        return bytes(rgb), (bytes(a) if a is not None else None), "DeviceRGB"

    if ctype == 4:
        color = bytearray(n)
        a = bytearray(n)
        for i in range(n):
            color[i] = rows[i * 2]
            a[i] = rows[i * 2 + 1]
        return bytes(color), bytes(a), "DeviceGray"

    if ctype == 6:
        rgb = bytearray(n * 3)
        a = bytearray(n)
        for i in range(n):
            s = i * 4
            d = i * 3
            rgb[d] = rows[s]
            rgb[d + 1] = rows[s + 1]
            rgb[d + 2] = rows[s + 2]
            a[i] = rows[s + 3]
        return bytes(rgb), bytes(a), "DeviceRGB"

    raise ValueError("不支持的 PNG 颜色类型：%d" % ctype)

def decode(data):
    if not data:
        raise ValueError("空图片")

    if data[:2] == b"\xff\xd8":
        w, h, comps = _jpeg_size(data)
        if w <= 0 or h <= 0:
            raise ValueError("JPEG 尺寸无效")
        if comps == 4:
            raise ValueError("暂不支持 CMYK 的 JPEG（请先转成 RGB）")
        return {
            "format": "jpeg", "width": w, "height": h,
            "colorSpace": "DeviceGray" if comps == 1 else "DeviceRGB",
            "filter": "DCTDecode", "stream": data, "parms": None,
            "alpha": None,
        }

    if not data.startswith(PNG_SIG):
        raise ValueError("只支持 PNG 和 JPEG")

    w, h, bit, ctype, interlace = _png_header(data)
    if w <= 0 or h <= 0:
        raise ValueError("PNG 尺寸无效")
    if w * h > MAX_PIXELS:
        raise ValueError("图片太大（%d 万像素），请先缩小再插入"
                         % (w * h // 10000))
    if interlace != 0:
        raise ValueError("不支持隔行扫描的 PNG，请另存为普通 PNG")
    if bit != 8:
        raise ValueError("只支持 8 位深的 PNG（这张是 %d 位）" % bit)
    if ctype not in _CHANNELS:
        raise ValueError("不支持的 PNG 颜色类型：%d" % ctype)

    idat = []
    plte = None
    trns = None
    for chunk, body in _png_chunks(data):
        if chunk == b"IDAT":
            idat.append(body)
        elif chunk == b"PLTE":
            plte = body
        elif chunk == b"tRNS":
            trns = body
    if not idat:
        raise ValueError("PNG 里没有像素数据")
    packed = b"".join(idat)

    if ctype in (0, 2) and not trns:
        colors = _CHANNELS[ctype]
        return {
            "format": "png", "width": w, "height": h,
            "colorSpace": "DeviceGray" if ctype == 0 else "DeviceRGB",
            "filter": "FlateDecode", "stream": packed,
            "parms": {"Predictor": 15, "Colors": colors,
                      "BitsPerComponent": 8, "Columns": w},
            "alpha": None,
        }

    try:
        raw = zlib.decompress(packed)
    except zlib.error as exc:
        raise ValueError("PNG 数据解压失败：%s" % exc)

    rows = _unfilter(raw, w, h, _CHANNELS[ctype])
    color, alpha, color_space = _split_pixels(rows, w, h, ctype, plte, trns)

    out = {
        "format": "png", "width": w, "height": h,
        "colorSpace": color_space,
        "filter": "FlateDecode",
        "stream": zlib.compress(color, 6),
        "parms": None,
        "alpha": None,
    }
    if alpha is not None:
        out["alpha"] = {
            "colorSpace": "DeviceGray",
            "filter": "FlateDecode",
            "stream": zlib.compress(alpha, 6),
            "parms": None,
        }
    return out
