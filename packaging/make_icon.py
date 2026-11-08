
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 4
BASE = 256
CANVAS = BASE * S

BG_TOP = (59, 130, 246)
BG_BOTTOM = (29, 78, 216)
PAPER = (255, 255, 255)
FOLD = (226, 233, 243)
LINE = (168, 182, 200)
BAR = (15, 23, 42)

def sc(*v):
    return tuple(int(round(x * S)) for x in v)

def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(sc(*box), radius=radius * S, fill=fill)

def build_master():
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))

    grad = Image.new("RGB", (1, CANVAS))
    for y in range(CANVAS):
        t = y / (CANVAS - 1)
        grad.putpixel((0, y), tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((CANVAS, CANVAS))

    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        sc(4, 4, BASE - 4, BASE - 4), radius=46 * S, fill=255)
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        sc(66, 48, 198, 216), radius=12 * S, fill=(8, 20, 45, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6 * S)))

    rounded(draw, (62, 40, 194, 210), 12, PAPER)
    draw.polygon([(x * S, y * S) for (x, y) in
                  [(166, 40), (194, 40), (194, 68)]], fill=FOLD)

    rounded(draw, (80, 84, 166, 96), 6, LINE)
    rounded(draw, (80, 108, 150, 120), 6, LINE)
    rounded(draw, (72, 132, 174, 156), 6, BAR)
    rounded(draw, (80, 172, 158, 184), 6, LINE)

    return img

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    master = build_master()

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                 (128, 128), (256, 256)]
    frames = [master.resize(s, Image.LANCZOS) for s in ico_sizes]

    ico_path = os.path.join(here, "app.ico")
    frames[-1].save(ico_path, format="ICO", sizes=ico_sizes,
                    append_images=frames[:-1])
    print("写出", ico_path, os.path.getsize(ico_path), "字节")

    fav = os.path.join(root, "static", "favicon.ico")
    frames[0].save(fav, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print("写出", fav, os.path.getsize(fav), "字节")

    png = os.path.join(here, "app-256.png")
    master.resize((256, 256), Image.LANCZOS).save(png)
    print("写出", png, os.path.getsize(png), "字节")

if __name__ == "__main__":
    sys.exit(main())
