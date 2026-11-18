"""Generate test PDFs that stress different parts of the engine."""
import io
import os
import random
import sys

import pymupdf
from PIL import Image, ImageDraw, ImageFilter

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)
rnd = random.Random(7)
A4 = pymupdf.paper_rect("a4")


def photo(w, h, hue_shift=0, mode="RGB"):
    """Natural-looking image: smooth gradients, shapes, blur and grain (compresses like a photo)."""
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(40 + 150 * t + hue_shift) % 256, int(70 + 120 * t), int(150 - 60 * t)))
    for _ in range(40):
        x, y, r = rnd.randrange(w), rnd.randrange(h), rnd.randrange(w // 30, w // 6)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    im = im.filter(ImageFilter.GaussianBlur(w / 300))
    noise = Image.effect_noise((w, h), 22).convert("RGB")
    im = Image.blend(im, noise, 0.08)
    return im.convert(mode)


def jpeg(im, q=95):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q, subsampling=0)
    return b.getvalue()


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def body(page, title, y=72):
    page.insert_text((72, y), title, fontsize=22, fontname="helv")
    page.insert_textbox(pymupdf.Rect(72, y + 14, A4.width - 72, y + 90),
                        "The quick brown fox jumps over the lazy dog. Text must stay selectable and identical "
                        "after compression; fonts and colours must not change.", fontsize=10, fontname="tiro")


# 1. Scanned contract: 12 full-page 600 dpi greyish scans stored lossless (huge), plus a colour signature page.
doc = pymupdf.open()
for i in range(12):
    p = doc.new_page(width=A4.width, height=A4.height)
    scan = photo(2480 // 2 * 2, 3508 // 2 * 2).convert("L").convert("RGB")  # neutral RGB scan, ~300dpi*2
    scan = scan.resize((int(A4.width / 72 * 600) // 2, int(A4.height / 72 * 600) // 2))
    p.insert_image(A4, stream=png(scan))
    if i == 11:
        sig = Image.new("RGB", (900, 300), "white")
        ImageDraw.Draw(sig).line([(40, 200), (300, 80), (520, 220), (860, 90)], fill=(20, 40, 160), width=9)
        p.insert_image(pymupdf.Rect(300, 650, 520, 723), stream=png(sig))
doc.save(os.path.join(OUT, "scanned-contract.pdf"), deflate=True)

# 2. Portfolio: high-res JPEG photos at ~500 dpi, a repeated logo, real text.
doc = pymupdf.open()
logo = Image.new("RGB", (600, 200), "white")
ImageDraw.Draw(logo).rectangle([20, 20, 180, 180], fill=(220, 60, 90))
logo_png = png(logo)
for i in range(10):
    p = doc.new_page(width=A4.width, height=A4.height)
    body(p, f"Project {i + 1}: Light, Glass & Stone")
    r = pymupdf.Rect(72, 200, A4.width - 72, 200 + (A4.width - 144) * 0.66)
    p.insert_image(r, stream=jpeg(photo(int(r.width / 72 * 500), int(r.height / 72 * 500), i * 20)))
    p.insert_image(pymupdf.Rect(A4.width - 160, 20, A4.width - 40, 60), stream=logo_png)
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 760, 200, 775), "uri": "https://example.com"})
doc.set_toc([[1, f"Project {i + 1}", i + 1] for i in range(10)])
doc.save(os.path.join(OUT, "portfolio.pdf"), deflate=True)

# 3. Invoice with a whole TrueType font embedded (bloat) and a small logo.
doc = pymupdf.open()
p = doc.new_page(width=A4.width, height=A4.height)
font = next((f for f in [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf"] if os.path.exists(f)), None)
p.insert_font(fontname="F0", fontfile=font)
p.insert_text((72, 90), "Invoice INV-0457", fontsize=24, fontname="F0")
for k in range(12):
    p.insert_text((72, 140 + k * 18), f"Line item {k + 1} . . . . . . . . . . . . £{(k + 1) * 37}.00", fontsize=11, fontname="F0")
p.insert_image(pymupdf.Rect(420, 50, 520, 100), stream=logo_png)
doc.save(os.path.join(OUT, "invoice-full-font.pdf"), deflate=True)

# 4. CMYK print artwork: CMYK JPEG photos at 450 dpi.
doc = pymupdf.open()
for i in range(4):
    p = doc.new_page(width=A4.width, height=A4.height)
    body(p, f"Autumn programme, page {i + 1}")
    r = pymupdf.Rect(72, 200, A4.width - 72, 700)
    p.insert_image(r, stream=jpeg(photo(int(r.width / 72 * 450), int(r.height / 72 * 450), 90 + i * 30, "CMYK"), 95))
doc.save(os.path.join(OUT, "cmyk-brochure.pdf"), deflate=True)

# 5. Vector-heavy drawing: lots of paths, no images.
doc = pymupdf.open()
p = doc.new_page(width=1684, height=2384)  # A1 portrait
sh = p.new_shape()
for _ in range(60000):
    x, y = rnd.uniform(0, 1684), rnd.uniform(0, 2384)
    sh.draw_line((x, y), (x + rnd.uniform(-40, 40), y + rnd.uniform(-40, 40)))
sh.finish(color=(0.1, 0.2, 0.4), width=0.3)
sh.commit()
p.insert_text((60, 80), "Site plan, ground floor", fontsize=40, fontname="helv")
doc.save(os.path.join(OUT, "site-plan-vector.pdf"), deflate=True)

for f in sorted(os.listdir(OUT)):
    print(f"{os.path.getsize(os.path.join(OUT, f)) / 1e6:8.2f} MB  {f}")
