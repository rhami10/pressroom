# Pressroom: PDF compressor design notes

Goal: get any PDF under a size target (default **2 MB**) while keeping it looking the same.
That means the same fonts, the same colours and selectable text, with as little visible
change to images as the target allows.

## The key idea: only touch what's heavy

The text, vector graphics and fonts in a PDF are already compact and lossless. When a PDF
is big, almost all of the weight is in one of three places:

| Where the weight is | Typical example | What fixes it | Visible change |
|---|---|---|---|
| Embedded photos and scans | Scanned contracts, portfolios | Resample images to the size they actually print at, then re-encode | None at 300 dpi; slight softness only when zoomed in at lower dpi |
| Bloat | Full fonts embedded, page thumbnails, edit history, duplicate images | Subset fonts, remove duplicates and leftovers, pack objects | None (lossless) |
| Vector line work | CAD drawings, maps | Can't be shrunk much without rasterising | Avoided. The app tells you instead |

Common "compress PDF" tools (Ghostscript `/ebook`, online sites) re-render the **whole**
document. That's how fonts get swapped, colours shift (CMYK→RGB, lost ICC profiles) and text
can come out blurry. Pressroom never rewrites text, fonts or vectors. It works on the
existing file and only replaces image streams.

## Pipeline (per file)

1. **Analyse.** Measure how many bytes go to images, fonts, vectors/text and metadata. For each
   image, work out its *effective dpi* from its pixel size and its printed size on the page.
2. **Lossless pass, always first.**
   - Subset embedded fonts to the glyphs actually used (same outlines, fewer of them)
   - Merge duplicate images and fonts (for example a logo repeated on every page)
   - Drop page thumbnails, XMP edit history and unused objects
   - Recompress streams at maximum Flate, and use object streams and cross-reference streams
   - **If this is already under the target, stop.** The result looks identical.
3. **Image pass, only if needed.**
   - Process the largest images first. Images below the dpi ceiling are never upscaled or touched
   - Photos: downsample to the chosen dpi and re-encode as JPEG (quality 80–92, 4:4:4 chroma at the top end)
   - Line art, screenshots, signatures and images with transparency: keep lossless (Flate/PNG predictors), and keep soft masks separate
   - Scans that are really grey stay greyscale, and colour pages stay colour. Each page is detected separately
   - Keep the original colour space and ICC profile (CMYK stays CMYK)
4. **Search for the best quality that fits.** Binary-search a single quality dial (dpi + JPEG
   quality together) for the *highest* setting under target − 2.5 % headroom. Usually takes 4–6 trial encodes.
5. **Quality floor.** Never go below the user's floor (default 200 dpi). If the target can't be
   met above the floor, say so and offer options: a larger target, split into parts, or
   rasterise (with a preview first).
6. **Verify.** Re-open the output and confirm that the page count, text extraction, fonts, links, bookmarks and
   form fields all match the original. Compare image SSIM and flag anything under 0.97.

Sizes use **decimal MB** (2 MB = 2,000,000 bytes), the stricter of the two conventions,
plus 2.5 % headroom. That lets a "2 MB" upload portal accept the file whichever convention it uses.

## What is always kept

Text as real text (searchable and selectable), every embedded font, colour profiles, links,
bookmarks, form fields, annotations, page size and orientation. The original file is never overwritten.
Output goes next to it as `name (2 MB).pdf`.

## Proposed stack

- **Engine:** Python 3.12 + `pikepdf` (qpdf: lossless rewrite, object streams, dedupe) +
  `PyMuPDF` (analysis, font subsetting, image placement and dpi) + `Pillow` (image re-encoding).
- **UI:** the HTML prototype becomes the real front end, shown in a native window with
  `pywebview`. Everything runs locally, and no files are uploaded.
- **Packaging:** PyInstaller produces a single `Pressroom.exe`.
- Setup on this machine (approved: install whatever's needed): `winget install Python.Python.3.12`
  (only the Microsoft Store shortcut is there now), then `pip install pikepdf pymupdf pillow pywebview pyinstaller`.

Alternative if you'd rather avoid Python: Node + Electron + `qpdf` WASM + `sharp`. This works,
but font subsetting is much weaker there, and font subsetting is often the biggest lossless saving.
