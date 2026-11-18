"""Pressroom compression engine.

Strategy: never rewrite text, fonts or vector content. Lossless clean-up first; then,
only if needed, re-encode embedded photos at the highest dpi/quality that fits the target.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
import zlib
from dataclasses import dataclass, field
from typing import Callable

import pymupdf
from PIL import Image, ImageChops, ImageStat

try:
    from . import precision
except ImportError:  # run as a loose script
    import precision  # type: ignore

pymupdf.TOOLS.mupdf_display_errors(False)

MB = 1_000_000
HEADROOM = 0.975  # aim 2.5% under the target

# Quality ladder, best first: (dpi, jpeg quality, chroma subsampling 0=4:4:4 2=4:2:0).
# The search picks the first level that fits.
LADDER = [
    (300, 94, 0), (300, 90, 0), (300, 90, 2), (300, 86, 2), (300, 82, 2), (270, 82, 2),
    (240, 80, 2), (220, 80, 2), (200, 78, 2), (180, 76, 2), (165, 75, 2), (150, 74, 2),
    (130, 72, 2), (110, 70, 2), (96, 68, 2), (72, 65, 2),
]
RESAMPLE_SLACK = 1.15      # only downsample if the image is >15% above the target dpi
REENCODE_MIN_GAIN = 0.85   # only replace an image if the new stream is <=85% of the old one
SMALL_IMAGE = 32_000       # images smaller than this (logos, icons) are left exactly as they are
UNSAFE_COLORSPACES = ("/Indexed", "/Separation", "/DeviceN", "/Lab", "/Pattern")

Progress = Callable[[str, int], None]


class EngineError(Exception):
    """A problem the user can act on; the message is shown as-is."""


# ---------------------------------------------------------------- analysis

@dataclass
class ImageInfo:
    xref: int
    width: int
    height: int
    max_w_pt: float = 0.0   # largest placed size on any page, in points
    max_h_pt: float = 0.0
    pages: set = field(default_factory=set)
    raw_size: int = 0

    @property
    def dpi(self) -> float:
        if self.max_w_pt <= 0 or self.max_h_pt <= 0:
            return 0.0
        return min(self.width / (self.max_w_pt / 72), self.height / (self.max_h_pt / 72))


def _key(doc, xref, k):
    t, v = doc.xref_get_key(xref, k)
    return t, v


def _ref(v: str) -> int | None:
    m = re.match(r"(\d+) 0 R", v or "")
    return int(m.group(1)) if m else None


def breakdown(doc: pymupdf.Document, file_size: int) -> dict:
    """Bytes by kind: img, font, vec (text, vectors and structure), meta (metadata & extras)."""
    fontfiles, thumbs = set(), set()
    for xref in range(1, doc.xref_length()):
        try:
            if _key(doc, xref, "Type")[1] == "/FontDescriptor":
                for k in ("FontFile", "FontFile2", "FontFile3"):
                    r = _ref(_key(doc, xref, k)[1])
                    if r:
                        fontfiles.add(r)
        except Exception:
            continue
    for page in doc:
        r = _ref(_key(doc, page.xref, "Thumb")[1])
        if r:
            thumbs.add(r)

    out = {"img": 0, "font": 0, "vec": 0, "meta": 0}
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref):
                continue
            n = len(doc.xref_stream_raw(xref) or b"")
        except Exception:
            continue
        sub = _key(doc, xref, "Subtype")[1]
        typ = _key(doc, xref, "Type")[1]
        if xref in thumbs or typ in ("/Metadata", "/EmbeddedFile") or sub == "/XML":
            out["meta"] += n
        elif sub == "/Image":
            out["img"] += n
        elif xref in fontfiles:
            out["font"] += n
        else:
            out["vec"] += n
    # Object dictionaries and xref tables count as structure.
    out["vec"] += max(0, file_size - sum(out.values()))
    return out


def collect_images(doc: pymupdf.Document) -> dict[int, ImageInfo]:
    imgs: dict[int, ImageInfo] = {}
    for page in doc:
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            continue
        for info in infos:
            xref = info.get("xref") or 0
            if xref <= 0:
                continue  # inline image: part of the content stream, left alone
            a, b, c, d, _, _ = info["transform"]
            w_pt, h_pt = math.hypot(a, b), math.hypot(c, d)
            ii = imgs.get(xref)
            if ii is None:
                ii = imgs[xref] = ImageInfo(xref, info["width"], info["height"])
                try:
                    ii.raw_size = len(doc.xref_stream_raw(xref) or b"")
                except Exception:
                    pass
            ii.max_w_pt = max(ii.max_w_pt, w_pt)
            ii.max_h_pt = max(ii.max_h_pt, h_pt)
            ii.pages.add(page.number)
    return imgs


def analyze(path: str) -> dict:
    size = os.path.getsize(path)
    doc = _open(path)
    try:
        imgs = collect_images(doc)
        fonts = len({f[0] for p in doc for f in p.get_fonts(full=False)})
        return {
            "path": path,
            "name": os.path.basename(path),
            "pages": doc.page_count,
            "size": size,
            "breakdown": breakdown(doc, size),
            "images": len(imgs),
            "fonts": fonts,
            "signed": _is_signed(doc),
        }
    finally:
        doc.close()


def _open(path_or_bytes) -> pymupdf.Document:
    if isinstance(path_or_bytes, str) and not os.path.exists(path_or_bytes):
        raise EngineError("This file can't be found. It may have been moved or deleted.")
    try:
        doc = (pymupdf.open(stream=path_or_bytes, filetype="pdf")
               if isinstance(path_or_bytes, (bytes, bytearray)) else pymupdf.open(path_or_bytes))
    except Exception as e:
        raise EngineError("This file couldn't be opened as a PDF. It may be damaged or not a PDF.") from e
    if not doc.is_pdf:
        raise EngineError("This file isn't a PDF.")
    if doc.needs_pass:
        raise EngineError("This PDF is password-protected. Remove the password in a PDF reader, then add it again.")
    return doc


def _is_signed(doc) -> bool:
    try:
        return doc.get_sigflags() > 0
    except Exception:
        return False


# ---------------------------------------------------------------- fingerprint for verification

def fingerprint(doc: pymupdf.Document) -> dict:
    pages = []
    for p in doc:
        try:
            fonts = sorted({f[3].split("+")[-1] for f in p.get_fonts(full=False)})
        except Exception:
            fonts = []
        pages.append({
            "text": p.get_text("text"),
            "links": len(p.get_links()),
            "widgets": sum(1 for _ in p.widgets()),
            "rect": tuple(round(v, 1) for v in p.rect),
            "rot": p.rotation,
            "fonts": fonts,
        })
    return {"pages": pages, "toc": doc.get_toc(simple=True)}


def compare(a: dict, b: dict) -> dict:
    pa, pb = a["pages"], b["pages"]
    same_count = len(pa) == len(pb)
    z = list(zip(pa, pb)) if same_count else []
    return {
        "pages": same_count and all(x["rect"] == y["rect"] and x["rot"] == y["rot"] for x, y in z),
        "text": same_count and all(x["text"] == y["text"] for x, y in z),
        "fonts": same_count and all(x["fonts"] == y["fonts"] for x, y in z),
        "links": same_count and all(x["links"] == y["links"] for x, y in z),
        "forms": same_count and all(x["widgets"] == y["widgets"] for x, y in z),
        "bookmarks": a["toc"] == b["toc"],
    }


# ---------------------------------------------------------------- lossless pass

def _save(doc, garbage=3) -> bytes:
    return doc.tobytes(garbage=garbage, deflate=True, deflate_images=True, deflate_fonts=True,
                       use_objstms=1, compression_effort=100)


def lossless_pass(src: bytes, ref_print: dict) -> tuple[bytes, dict]:
    """Returns (bytes, notes). Never changes how a page looks."""
    notes = {"thumbs": 0, "xmp": False, "subset": False, "dupes": 0}
    doc = _open(src)

    # Duplicate images (same pixels stored twice) are merged by garbage=4; count them for the report.
    hashes = {}
    for x in range(1, doc.xref_length()):
        try:
            if _key(doc, x, "Subtype")[1] == "/Image":
                h = hashlib.sha1(doc.xref_stream_raw(x) or b"").digest()
                hashes[h] = hashes.get(h, 0) + 1
        except Exception:
            pass
    notes["dupes"] = sum(n - 1 for n in hashes.values())

    for page in doc:
        if _key(doc, page.xref, "Thumb")[0] != "null":
            doc.xref_set_key(page.xref, "Thumb", "null")
            notes["thumbs"] += 1
        if _key(doc, page.xref, "PieceInfo")[0] != "null":
            doc.xref_set_key(page.xref, "PieceInfo", "null")
    cat = doc.pdf_catalog()
    if _key(doc, cat, "PieceInfo")[0] != "null":
        doc.xref_set_key(cat, "PieceInfo", "null")

    # XMP holds edit history, but also PDF/A identification: keep it when the file claims PDF/A.
    xmp = doc.get_xml_metadata() or ""
    if xmp and "pdfaid" not in xmp:
        doc.del_xml_metadata()
        notes["xmp"] = True

    base = _save(doc, garbage=4)

    # Font subsetting: same glyph outlines, fewer glyphs. Keep it only if the text still matches exactly.
    try:
        doc.subset_fonts(fallback=True)
        subset = _save(doc, garbage=4)
        if len(subset) < len(base):
            check = _open(subset)
            ok = all(compare(ref_print, fingerprint(check))[k] for k in ("pages", "text", "links", "forms"))
            check.close()
            if ok:
                base = subset
                notes["subset"] = True
    except Exception:
        pass
    doc.close()

    # Drawing precision: design tools write 6 decimals per coordinate. Round paths to 0.01 pt and
    # colours to 3 decimals, then keep the result only if every checked page renders the same.
    notes["trim_saved"] = 0
    try:
        trimmed = _trim_precision(base)
        if trimmed and len(trimmed) < len(base) * 0.98 and _renders_match(base, trimmed, ref_print):
            notes["trim_saved"] = len(base) - len(trimmed)
            base = trimmed
    except Exception:
        pass
    return (base if len(base) < len(src) else src), notes


def _content_xrefs(doc) -> set[int]:
    xs = {x for pg in doc for x in pg.get_contents()}
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(x) and (_key(doc, x, "Subtype")[1] == "/Form" or _key(doc, x, "Type")[1] == "/Pattern"):
                xs.add(x)
        except Exception:
            pass
    return xs


def _trim_precision(src: bytes) -> bytes | None:
    doc = _open(src)
    changed = 0
    for x in _content_xrefs(doc):
        try:
            s = doc.xref_stream(x)
            if not s or len(s) < 2000:
                continue
            r = precision.trim(s)
            if len(r) < len(s) * 0.97:
                doc.update_stream(x, r)
                changed += 1
        except Exception:
            continue  # unparseable stream: leave it exactly as it was
    out = _save(doc, garbage=3) if changed else None
    doc.close()
    return out


def _renders_match(a_bytes: bytes, b_bytes: bytes, ref_print: dict, max_pages: int = 24) -> bool:
    """Render pages of both versions at 150 dpi and require them to look the same."""
    a, b = _open(a_bytes), _open(b_bytes)
    try:
        if not all(compare(ref_print, fingerprint(b))[k] for k in ("pages", "text", "links", "forms")):
            return False
        n = a.page_count
        pages = range(n) if n <= max_pages else sorted({round(i * (n - 1) / (max_pages - 1)) for i in range(max_pages)})
        for i in pages:
            pa, pb = a[i].get_pixmap(dpi=150, alpha=False), b[i].get_pixmap(dpi=150, alpha=False)
            ia = Image.frombytes("RGB", (pa.width, pa.height), pa.samples)
            ib = Image.frombytes("RGB", (pb.width, pb.height), pb.samples)
            d = ImageChops.difference(ia, ib).convert("L")
            hist = d.histogram()
            if ImageStat.Stat(d).mean[0] > 0.3 or sum(hist[48:]) > 0.001 * ia.width * ia.height:
                return False
        return True
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- image pass

def _colorspace_ok(doc, xref) -> bool:
    t, v = _key(doc, xref, "ColorSpace")
    s = v or ""
    r = _ref(s)
    if t == "xref" and r:
        s = doc.xref_object(r, compressed=True)
    return not any(u in s for u in UNSAFE_COLORSPACES)


def _filters(doc, xref) -> str:
    return _key(doc, xref, "Filter")[1] or ""


def _to_pil(pix: pymupdf.Pixmap) -> Image.Image | None:
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(pix.n)
    if not mode:
        return None
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _is_graphic(im: Image.Image) -> bool:
    """Flat colour => logo, chart, screenshot or line art: keep lossless.
    Judged by how much of the image the 16 most common colours cover, so greyscale
    photos and scans (which only ever have 256 levels) still count as photos."""
    t = im.convert("RGB")
    t.thumbnail((200, 200), Image.Resampling.NEAREST)
    colors = t.getcolors(maxcolors=8192)
    if colors is None:
        return False
    colors.sort(reverse=True)
    return sum(c for c, _ in colors[:16]) >= 0.9 * t.width * t.height


def _is_neutral(im: Image.Image) -> bool:
    if im.mode != "RGB":
        return False
    t = im.copy()
    t.thumbnail((256, 256))
    r, g, b = t.split()
    d = ImageChops.lighter(ImageChops.difference(r, g), ImageChops.difference(g, b))
    hist = d.histogram()
    total = sum(hist)
    return sum(hist[7:]) <= total * 0.001


def _diff(a: Image.Image, b: Image.Image) -> float:
    a = a.convert("RGB").resize((48, 48), Image.Resampling.BOX)
    b = b.convert("RGB").resize((48, 48), Image.Resampling.BOX)
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


@dataclass
class ImageStats:
    reencoded: int = 0
    resampled: int = 0
    lossless_kept: int = 0
    untouched: int = 0
    skipped_colour: int = 0
    gray: int = 0
    masks: int = 0


def _replace_image(doc, x, work: Image.Image, data: bytes, filt: str, to_gray=False) -> bool:
    """Swap in a new image stream; verify it decodes to what we encoded, else restore the original."""
    orig_obj = doc.xref_object(x, compressed=False)
    orig_raw = doc.xref_stream_raw(x)
    doc.update_stream(x, data, compress=False)
    doc.xref_set_key(x, "Filter", filt)
    doc.xref_set_key(x, "DecodeParms", "null")
    doc.xref_set_key(x, "Decode", "null")
    doc.xref_set_key(x, "Width", str(work.width))
    doc.xref_set_key(x, "Height", str(work.height))
    doc.xref_set_key(x, "BitsPerComponent", "8")
    if to_gray:
        doc.xref_set_key(x, "ColorSpace", "/DeviceGray")
    ok = _verify_image(doc, x, work)
    if not ok and work.mode == "CMYK":
        doc.xref_set_key(x, "Decode", "[1 0 1 0 1 0 1 0]")  # Adobe-style inverted CMYK
        ok = _verify_image(doc, x, work)
    if not ok:
        doc.update_stream(x, orig_raw, compress=False)
        doc.update_object(x, orig_obj)
    return ok


def _process_image(doc, ii: ImageInfo, dpi: int, q: int, sub: int, st: ImageStats) -> float:
    """Re-encode one image if worthwhile. Returns the scale applied (1.0 = size unchanged)."""
    x = ii.xref
    if _key(doc, x, "ImageMask")[1] == "true" or _key(doc, x, "BitsPerComponent")[1] == "1":
        st.untouched += 1
        return 1.0
    filt = _filters(doc, x)
    if any(f in filt for f in ("JBIG2", "CCITT")):
        st.untouched += 1
        return 1.0
    if not _colorspace_ok(doc, x):
        st.skipped_colour += 1
        return 1.0
    # Small images (logos, icons) cost almost nothing and are the most likely to be inspected
    # up close, so they are never resampled or re-encoded.
    if ii.raw_size < SMALL_IMAGE:
        st.untouched += 1
        return 1.0
    needs_resample = ii.dpi > dpi * RESAMPLE_SLACK
    lossy_src = "DCT" in filt or "JPX" in filt
    if not needs_resample and not lossy_src and ii.raw_size < 400_000:
        st.untouched += 1
        return 1.0

    im = _to_pil(pymupdf.Pixmap(doc, x))
    if im is None:
        st.untouched += 1
        return 1.0
    graphic = not lossy_src and _is_graphic(im)
    if graphic and not needs_resample:
        st.lossless_kept += 1
        return 1.0

    k, work = 1.0, im
    if needs_resample:
        k = dpi / ii.dpi
        work = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.Resampling.LANCZOS, reducing_gap=3.0)
    to_gray = not graphic and work.mode == "RGB" and _key(doc, x, "ColorSpace")[1] == "/DeviceRGB" and _is_neutral(work)
    if to_gray:
        work = work.convert("L")

    if graphic:
        data, f = zlib.compress(work.tobytes(), 6), "/FlateDecode"
    else:
        buf = io.BytesIO()
        work.save(buf, "JPEG", quality=q, optimize=True, subsampling=sub)
        data, f = buf.getvalue(), "/DCTDecode"
        if len(data) > ii.raw_size * REENCODE_MIN_GAIN:
            st.untouched += 1
            return 1.0
    if not _replace_image(doc, x, work, data, f, to_gray):
        st.untouched += 1
        return 1.0
    st.reencoded += 1
    st.resampled += needs_resample
    st.gray += to_gray
    return k


def _process_mask(doc, sm: int, k: float, q: int, st: ImageStats):
    """Soft transparency masks (fades, feathered edges) are often stored lossless and can be as big as
    the photo itself. Encode soft ones as high-quality greyscale JPEG; keep hard-edged ones lossless."""
    filt = _filters(doc, sm)
    if "DCT" in filt or "JPX" in filt or _key(doc, sm, "BitsPerComponent")[1] != "8":
        return
    raw = doc.xref_stream_raw(sm) or b""
    if len(raw) < 16_000:
        return
    im = _to_pil(pymupdf.Pixmap(doc, sm))
    if im is None or im.mode != "L":
        return
    if k < 1 / RESAMPLE_SLACK:
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.Resampling.LANCZOS, reducing_gap=3.0)
    elif len(im.getcolors(256) or []) < 32:
        return  # hard-edged mask at full size: already efficient lossless
    if len(im.getcolors(256) or []) < 32:
        data, f = zlib.compress(im.tobytes(), 6), "/FlateDecode"
    else:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=min(95, q + 4), optimize=True)
        data, f = buf.getvalue(), "/DCTDecode"
    if len(data) > len(raw) * REENCODE_MIN_GAIN:
        return
    if _replace_image(doc, sm, im, data, f):
        st.masks += 1


def image_pass(src: bytes, imgs: dict[int, ImageInfo], dpi: int, q: int, sub: int = 2) -> tuple[bytes, ImageStats]:
    doc = _open(src)
    st = ImageStats()
    masks_done: set[int] = set()
    for ii in sorted(imgs.values(), key=lambda i: -i.raw_size):
        try:
            k = _process_image(doc, ii, dpi, q, sub, st)
        except Exception:
            st.untouched += 1
            k = 1.0
        sm = _ref(_key(doc, ii.xref, "SMask")[1])
        if sm and sm not in masks_done:
            masks_done.add(sm)
            try:
                _process_mask(doc, sm, k, q, st)
            except Exception:
                pass
    out = _save(doc, garbage=3)
    doc.close()
    return out, st


def _verify_image(doc, xref, expected: Image.Image) -> bool:
    try:
        got = _to_pil(pymupdf.Pixmap(doc, xref))
        if got is None or got.size != expected.size:
            return False
        return _diff(got, expected) < 6.0
    except Exception:
        return False


# ---------------------------------------------------------------- orchestration

def compress(path: str, target_mb: float, floor_dpi: int = 200, progress: Progress | None = None,
             out_dir: str | None = None) -> dict:
    say = progress or (lambda s, p: None)
    target = int(target_mb * MB)
    budget = int(target * HEADROOM)

    say("Reading", 5)
    _open(path).close()  # friendly errors for missing / locked / non-PDF files
    src = open(path, "rb").read()
    orig = _open(src)
    ref = fingerprint(orig)
    bd_before = breakdown(orig, len(src))
    imgs_orig = collect_images(orig)
    signed = _is_signed(orig)
    producer = (orig.metadata or {}).get("producer") or (orig.metadata or {}).get("creator") or ""
    orig.close()

    say("Lossless pass", 20)
    clean, notes = lossless_pass(src, ref)
    cdoc = _open(clean)
    imgs = collect_images(cdoc)
    bd_clean = breakdown(cdoc, len(clean))
    cdoc.close()

    best, level, stats = clean, None, None
    status = "lossless"
    if len(clean) > budget and imgs:
        ladder = [l for l in LADDER if l[0] >= floor_dpi] or [LADDER[0]]
        trials: dict[int, tuple[bytes, ImageStats]] = {}

        def trial(i):
            if i not in trials:
                dpi, q, sub = ladder[i]
                say(f"Trying {dpi} dpi · Q{q}", 30 + int(55 * len(trials) / max(1, math.ceil(math.log2(len(ladder))) + 1)))
                trials[i] = image_pass(clean, imgs, dpi, q, sub)
            return trials[i]

        last = len(ladder) - 1
        if len(trial(last)[0]) > budget:
            best, stats = trials[last]
            level, status = ladder[last], "nofit"
        else:
            lo, hi = 0, last          # find the first (best-quality) level that fits
            while lo < hi:
                mid = (lo + hi) // 2
                if len(trial(mid)[0]) <= budget:
                    hi = mid
                else:
                    lo = mid + 1
            best, stats = trial(lo)
            level = ladder[lo]
            # "fit-low" only when images really were downsampled below print resolution.
            status = "fit" if level[0] >= 300 or not stats.resampled else "fit-low"
    elif len(clean) > budget:
        status = "nofit"

    say("Checking output", 92)
    out_doc = _open(best)
    checks = compare(ref, fingerprint(out_doc))
    bd_after = breakdown(out_doc, len(best))
    out_doc.close()

    changes = _changes(notes, bd_before, bd_clean, bd_after, imgs_orig, stats, level)
    preview_page = _preview_page(imgs_orig)

    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "Pressroom")
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, hashlib.sha1(f"{path}|{target}|{floor_dpi}".encode()).hexdigest()[:16] + ".pdf")
    with open(tmp, "wb") as f:
        f.write(best)

    say("Done", 100)
    return {
        "status": status,
        "size": len(best),
        "original_size": len(src),
        "target": target,
        "dpi": level[0] if level else None,
        "quality": level[1] if level else None,
        "breakdown_before": bd_before,
        "breakdown_after": bd_after,
        "changes": changes,
        "checks": checks,
        "signed": signed,
        "temp_output": tmp,
        "preview_page": preview_page,
        "vector_heavy": bd_after["vec"] > 0.6 * len(best),
        "floor_dpi": floor_dpi,
        "images_resampled": stats.resampled if stats else 0,
        "producer": producer,
    }


def _changes(notes, before, clean, after, imgs, stats: ImageStats | None, level) -> list[dict]:
    rows = []
    font_saved = before["font"] - clean["font"]
    if notes["subset"] and font_saved > 0:
        rows.append({"kind": "lossless", "text": "Subset embedded fonts to the characters used (same glyph shapes)", "saved": font_saved})
    if notes["dupes"]:
        rows.append({"kind": "lossless", "text": f"Merged {notes['dupes']} duplicate image{'s' * (notes['dupes'] > 1)}", "saved": max(0, before["img"] - clean["img"])})
    meta_saved = before["meta"] - clean["meta"]
    if (notes["thumbs"] or notes["xmp"]) and meta_saved > 1000:
        bits = []
        if notes["thumbs"]:
            bits.append(f"{notes['thumbs']} page thumbnail{'s' * (notes['thumbs'] > 1)}")
        if notes["xmp"]:
            bits.append("edit history (XMP)")
        rows.append({"kind": "lossless", "text": "Removed " + " and ".join(bits), "saved": meta_saved})
    trim = notes.get("trim_saved", 0)
    if trim:
        rows.append({"kind": "vector", "text": "Rounded drawing coordinates from 6 decimals to 0.01 pt, finer than any printer dot (pages verified to render the same)", "saved": trim})
    vec_saved = before["vec"] - clean["vec"] - trim
    if vec_saved > 1000:
        rows.append({"kind": "lossless", "text": "Recompressed page streams and packed the file structure", "saved": vec_saved})
    n = len(imgs)
    if stats and level:
        dpi, q, _ = level
        img_saved = max(0, clean["img"] - after["img"])
        masks = f"{stats.masks} soft transparency mask{'s' * (stats.masks > 1)}" if stats.masks else ""
        if stats.reencoded:
            extra = f", {stats.gray} neutral scan{'s' * (stats.gray > 1)} stored as greyscale" if stats.gray else ""
            where = f"at up to {dpi} dpi" if stats.resampled else "at their original resolution"
            rows.append({"kind": "image", "text": f"Re-encoded {stats.reencoded} of {n} images {where}, JPEG quality {q}{extra}" + (f", plus {masks}" if masks else ""),
                         "saved": img_saved})
        elif masks:
            rows.append({"kind": "image", "text": f"Stored {masks} as JPEG quality {min(95, q + 4)}; the images themselves are untouched", "saved": img_saved})
        if stats.lossless_kept:
            rows.append({"kind": "kept", "text": f"{stats.lossless_kept} graphic{'s' * (stats.lossless_kept > 1)} or screenshot{'s' * (stats.lossless_kept > 1)} kept lossless", "saved": 0})
        if stats.untouched:
            rows.append({"kind": "kept", "text": f"{stats.untouched} image{'s' * (stats.untouched > 1)} already efficient, left untouched", "saved": 0})
        if stats.skipped_colour:
            rows.append({"kind": "kept", "text": f"{stats.skipped_colour} image{'s' * (stats.skipped_colour > 1)} in spot or indexed colour left exactly as is", "saved": 0})
    elif n:
        rows.append({"kind": "kept", "text": f"All {n} image{'s' * (n > 1)} left untouched", "saved": 0})
    return rows


def _preview_page(imgs: dict[int, ImageInfo]) -> int:
    weight: dict[int, int] = {}
    for ii in imgs.values():
        for p in ii.pages:
            weight[p] = weight.get(p, 0) + ii.raw_size
    return max(weight, key=weight.get) if weight else 0


def render_page(path: str, page: int, width: int = 900) -> bytes:
    doc = _open(path)
    try:
        p = doc[max(0, min(page, doc.page_count - 1))]
        z = width / p.rect.width
        return p.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")
    finally:
        doc.close()


def output_name(original: str, target_mb: float) -> str:
    folder, base = os.path.split(original)
    stem = os.path.splitext(base)[0]
    label = f"{target_mb:g} MB"
    cand = os.path.join(folder, f"{stem} ({label}).pdf")
    n = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{stem} ({label}) {n}.pdf")
        n += 1
    return cand
