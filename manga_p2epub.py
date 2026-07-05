#!/usr/bin/env python3
"""
manga_p2epub.py — Convert a scanned-book PDF into a fixed-layout (manga) EPUB3.

Input:  one PDF per book, each page holds a single embedded scan image.
Output: EBPAJ 1.1.2 style pre-paginated EPUB with 1103x1600 viewport.

v0.1 — minimum working pipeline.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageFilter, ImageStat
from pypdf import PdfReader


# ---------- Constants ----------

VIEWPORT_W = 1103
VIEWPORT_H = 1600
JPEG_QUALITY = 78
GRAYSCALE_DOWNSCALE_LONG_SIDE = 400

# Color-vs-grayscale classification (see _is_effectively_grayscale)
COLOR_SAT_FLOOR = 30              # px counts as "colorful" when max(RGB)-min(RGB) > this
COLOR_MIN_COLORFUL_FRAC = 0.01    # below this colorful-px fraction -> plain B&W scan
TAN_HUE_CONC_MIN = 0.90           # circular concentration (0..1) of colorful-px hues;
                                  # aged paper measures >= 0.99, full-color art 0.1-0.85
TAN_WARM_BAND_LO = 15.0           # degrees; hue band that holds aged-paper tints
TAN_WARM_BAND_HI = 75.0
TAN_WARM_MIN_FRAC = 0.90          # colorful px that must fall inside the warm band
TAN_MEAN_HUE_LO = 20.0            # mean-hue window for aged paper (measured 28-44 deg;
TAN_MEAN_HUE_HI = 75.0            # red spot-color prints measure ~4-23 deg)
TAN_SAT_MEAN_MAX = 65.0           # colorful-px mean saturation ceiling; aged paper
                                  # measured 42-55, vivid spot-color prints >= 72
SANDWICH_MAX_RUN = 2              # a run of up to this many gray-judged RGB pages
                                  # bracketed by color pages on both sides is recolored
                                  # (color sections usually come in contiguous blocks)

# Auto-crop tuning (see _setup_auto_crop)
CROP_THRESHOLD_OFFSET = 25
CROP_THRESHOLD_FLOOR = 180
CROP_DARK_PCT = 0.01
CROP_DOWNSCALE_LONG_SIDE = 800
CROP_SAMPLE_COUNT = 10
CROP_SAMPLE_SKIP_PCT = 0.05
CROP_OUTLIER_MAX_FRAC = 0.30
CROP_OUTLIER_MIN_SUM = 0.01
CROP_PERCENTILE = 25
CROP_MIN_VALID_SAMPLES = 3
CROP_MIN_MARGIN_FRAC = 0.03
CROP_PER_PAGE_SAFETY = 0.015      # buffer between page's detected content edge and crop
CROP_PER_PAGE_TRUST_MIN = 0.01    # below this, treat per-page detection as unreliable
                                  # (likely a header / footer / scan artifact at the edge)
CROP_ARTIFACT_ZERO_FRAC = 0.40    # if at least this fraction of sampled pages return 0%
                                  # on a given side, treat the 0% as a systematic scanner
                                  # artifact (e.g., 1-3 row dark band at the page edge from
                                  # binding shadow) and exclude those samples when picking
                                  # the percentile. Manga has occasional full-bleed pages
                                  # that yield 0% legitimately; those stay below the
                                  # threshold and the book gets correctly skipped.
CROP_ARTIFACT_MIN_NONZERO = 2     # require at least this many non-zero samples to apply
                                  # the artifact filter; otherwise fall back to all samples
CROP_BOOK_LR_MAX = 0.05           # hard ceiling on the book-level L/R crop fraction.
                                  # Smartphone reading screens are typically portrait, so the
                                  # cropped image is displayed close to its native width;
                                  # over-aggressive horizontal cropping leaves the first/last
                                  # text column visually grazing the page edge. Capping at 5%
                                  # keeps a comfortable margin even on books whose detection
                                  # reports very wide L/R, at the cost of leaving a bit of
                                  # paper on each side. Vertical (T/B) crop is not capped.
CROP_BOOK_LR_BACKOFF = 0.03       # subtract from book-level L/R after percentile.
                                  # Detection only finds the body-text edge; running titles,
                                  # page numbers and binding shift can extend closer to the
                                  # actual paper edge than the body, so a uniform backoff
                                  # protects those elements on pages whose detected margin
                                  # exceeds the percentile (no per-page cap is triggered).


# ---------- Templates ----------

MIMETYPE = b"application/epub+zip"

CONTAINER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="item/standard.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

FIXED_LAYOUT_CSS = """\
@charset "UTF-8";
html, body { margin: 0; padding: 0; font-size: 0; }
svg { margin: 0; padding: 0; }
"""

PAGE_XHTML_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
<head>
<meta charset="UTF-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style/fixed-layout-jp.css"/>
<meta name="viewport" content="width={vw}, height={vh}"/>
</head>
<body>
<div class="main">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 {vw} {vh}">
<image width="{vw}" height="{vh}" xlink:href="../image/{image_name}"/>
</svg>
</div>
</body>
</html>
"""

NAV_XHTML_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
<head>
<meta charset="UTF-8"/>
<title>Navigation</title>
</head>
<body>
<nav epub:type="toc" id="toc">
<h1>Navigation</h1>
<ol>
{toc_items}
</ol>
</nav>
<nav epub:type="landmarks" id="guide">
<h1>Guide</h1>
<ol>
<li><a epub:type="cover" href="xhtml/p-cover.xhtml">表紙</a></li>
<li><a epub:type="bodymatter" href="xhtml/p-001.xhtml">本編</a></li>
</ol>
</nav>
</body>
</html>
"""

NCX_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ja">
<head>
<meta name="dtb:uid" content="{uid}"/>
<meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/>
<meta name="dtb:maxPageNumber" content="0"/>
</head>
<docTitle><text>{title}</text></docTitle>
<navMap>
{nav_points}
</navMap>
</ncx>
"""

OPF_TMPL = """\
<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xml:lang="ja" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/# ebpaj: http://www.ebpaj.jp/ fixed-layout-jp: http://www.digital-comic.jp/">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>{title}</dc:title>
<dc:creator>{author}</dc:creator>
<dc:language>ja</dc:language>
<dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier>
<meta property="dcterms:modified">{modified}</meta>
<meta property="rendition:layout">pre-paginated</meta>
<meta property="rendition:spread">landscape</meta>
<meta property="rendition:orientation">auto</meta>
<meta property="ebpaj:guide-version">1.1.2</meta>
<meta name="book-type" content="comic"/>
<meta name="original-resolution" content="{vw}x{vh}"/>
<meta property="fixed-layout-jp:viewport">width={vw}, height={vh}</meta>
</metadata>
<manifest>
<item id="toc" href="navigation-documents.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="fixed-layout-jp" href="style/fixed-layout-jp.css" media-type="text/css"/>
{image_items}
{page_items}
</manifest>
<spine page-progression-direction="{direction}">
{spine_items}
</spine>
</package>
"""


# ---------- Filename / metadata parsing ----------

_FS_BAD = re.compile(r'[\\/:*?"<>|]')


def parse_meta_from_filename(pdf_path: Path) -> tuple[str, Optional[str]]:
    """
    Derive (title, author) from a PDF filename like "作品名_作者名.pdf"
    or "作品名-作者名.pdf". `_` takes precedence over `-`.
    """
    stem = pdf_path.stem
    for sep in ("_", "-"):
        if sep in stem:
            title, _, author = stem.partition(sep)
            title, author = title.strip(), author.strip()
            if title and author:
                return title, author
    return stem.strip(), None


def sanitize_filename(name: str) -> str:
    return _FS_BAD.sub("_", name).strip()


def default_output_path(pdf_path: Path, title: str, author: Optional[str]) -> Path:
    base = f"{title}_{author}" if author else title
    return pdf_path.with_name(sanitize_filename(base) + ".epub")


# ---------- Auto-rotate (misscanned landscape page → portrait) ----------
#
# ScanSnap and similar document scanners sometimes mis-detect a portrait page
# as landscape when the OCR layer mistakes the text orientation. The image is
# then stored 90° rotated. The functions below detect those pages by:
#   1. Pre-scanning every page's dimensions.
#   2. Treating a landscape page as a "misscan candidate" only when its
#      (w, h) roughly matches the portrait median rotated 90° — this skips
#      genuine landscape spreads / covers (which are typically 2x wider).
#   3. Learning whether portraits in this book have wider top or bottom
#      margins, then matching the wider side of the landscape page (left or
#      right) to decide which way to rotate.
#
# Misjudged direction is acceptable per spec — the user can rotate in the
# viewer. The hard requirement is to never leave a misscanned page sideways.

DEFAULT_LANDSCAPE_RATIO = 1.05      # w/h ratio above which a page is "landscape"
DEFAULT_DIM_TOL = 0.10              # ±10% match against portrait median (rotated)
EDGE_BAND_PCT = 0.10                # measure mean brightness over this edge fraction
EDGE_DOWNSCALE_LONG_SIDE = 600      # downscale long side before measurement
ROTATION_CONFIDENCE_THRESHOLD = 2.0    # |brighter - dimmer| (0..255) below this -> uncertain
CONVENTION_SAMPLE_TARGET = 5        # collect this many portrait samples
CONVENTION_SKIP_FIRST_PAGES = 2     # skip cover/colophon pages when sampling
NEAR_BLANK_BRIGHTNESS = 250         # if both edges are above this, the page is nearly blank


def _page_rotation(page) -> int:
    """Return the page's /Rotate value normalized to {0, 90, 180, 270}."""
    rot = page.get("/Rotate", 0) or 0
    try:
        rot = int(rot)
    except (TypeError, ValueError):
        rot = 0
    return rot % 360


def _apply_pdf_rotation(im: Image.Image, rotate_deg: int) -> Image.Image:
    """Apply a PDF /Rotate value (clockwise degrees) to a PIL image."""
    if rotate_deg == 90:
        return im.transpose(Image.Transpose.ROTATE_270)  # 90° CW
    if rotate_deg == 180:
        return im.transpose(Image.Transpose.ROTATE_180)
    if rotate_deg == 270:
        return im.transpose(Image.Transpose.ROTATE_90)   # 90° CCW
    return im


def _prescan_dims(pdf_path: Path) -> list[tuple[int, int, int]]:
    """Return [(page_idx, display_w, display_h), ...] for every page.

    Dimensions reflect the page's /Rotate value so downstream landscape
    detection sees the same orientation a PDF reader would render.
    """
    reader = PdfReader(str(pdf_path))
    out: list[tuple[int, int, int]] = []
    for i, page in enumerate(reader.pages):
        try:
            imgs = list(page.images)
            if not imgs:
                out.append((i, 0, 0))
                continue
            im = imgs[0].image  # PIL header parse, no full decode
            w, h = im.width, im.height
            if _page_rotation(page) in (90, 270):
                w, h = h, w
            out.append((i, w, h))
        except Exception:
            out.append((i, 0, 0))
    return out


def _is_landscape(w: int, h: int, ratio: float = DEFAULT_LANDSCAPE_RATIO) -> bool:
    return w > 0 and h > 0 and w > h * ratio


def _is_misscan_candidate(w: int, h: int,
                          pw_med: int, ph_med: int,
                          tol: float = DEFAULT_DIM_TOL) -> bool:
    """True if a landscape page's dimensions match a portrait page rotated 90°."""
    if not _is_landscape(w, h):
        return False
    return ((1 - tol) * ph_med <= w <= (1 + tol) * ph_med
            and (1 - tol) * pw_med <= h <= (1 + tol) * pw_med)


def _edge_brightness(im: Image.Image, axis: str) -> tuple[float, float]:
    """
    axis="v" -> (top_band_mean, bottom_band_mean), each on a 0..255 scale.
    axis="h" -> (left_band_mean, right_band_mean).

    The "brighter" band has more whitespace relative to ink — i.e., it
    corresponds to the wider page margin in the original portrait. This holds
    even when the image has no clean white border (manga pages whose panel
    borders bleed to the edge), because the bottom edge often carries the page
    number while the top edge does not.
    """
    g = im.convert("L")
    long_side = max(g.size)
    if long_side > EDGE_DOWNSCALE_LONG_SIDE:
        scale = EDGE_DOWNSCALE_LONG_SIDE / long_side
        g = g.resize(
            (max(1, int(g.width * scale)), max(1, int(g.height * scale))),
            Image.LANCZOS,
        )
    w, h = g.size
    if axis == "v":
        bw = max(1, int(h * EDGE_BAND_PCT))
        a = g.crop((0, 0, w, bw))
        b = g.crop((0, h - bw, w, h))
    else:
        bw = max(1, int(w * EDGE_BAND_PCT))
        a = g.crop((0, 0, bw, h))
        b = g.crop((w - bw, 0, w, h))
    from PIL import ImageStat
    return ImageStat.Stat(a).mean[0], ImageStat.Stat(b).mean[0]


def _learn_convention(samples: list[tuple[float, float]]) -> Optional[str]:
    """Sum signed (top - bottom) brightness deltas. >0 -> 'top_wider'."""
    if not samples:
        return None
    diff = sum(t - b for t, b in samples)
    if diff == 0:
        return None
    return "top_wider" if diff > 0 else "bottom_wider"


def _decide_rotation(landscape_im: Image.Image,
                     convention: Optional[str]) -> tuple[Image.Transpose, str]:
    """
    Decide how to rotate a landscape page back to portrait.

    Returns (PIL transpose constant, human-readable label).
      ROTATE_90  = CCW (input's RIGHT side -> output TOP)
      ROTATE_270 = CW  (input's LEFT  side -> output TOP)
    """
    left, right = _edge_brightness(landscape_im, axis="h")
    diff = left - right

    if convention is None or abs(diff) < ROTATION_CONFIDENCE_THRESHOLD:
        return Image.Transpose.ROTATE_270, "CW (uncertain, defaulted)"

    landscape_brighter = "left" if diff > 0 else "right"
    if convention == "top_wider":
        original_top = landscape_brighter
    else:
        original_top = "right" if landscape_brighter == "left" else "left"

    if original_top == "left":
        return Image.Transpose.ROTATE_270, "CW"
    return Image.Transpose.ROTATE_90, "CCW"


def _setup_auto_rotate(pdf_path: Path) -> Optional[dict]:
    """Pre-scan dimensions and decide whether auto-rotate should be active."""
    dims = _prescan_dims(pdf_path)
    if not dims:
        return None
    portraits = [(w, h) for _, w, h in dims if 0 < w < h]
    n_total = sum(1 for _, w, h in dims if w > 0 and h > 0)
    if not portraits:
        print("[auto-rotate] no portrait pages found — disabled", file=sys.stderr)
        return None
    if len(portraits) / max(1, n_total) <= 0.5:
        print("[auto-rotate] PDF is not portrait-dominant — disabled", file=sys.stderr)
        return None
    pw_med = sorted(w for w, _ in portraits)[len(portraits) // 2]
    ph_med = sorted(h for _, h in portraits)[len(portraits) // 2]
    candidates = [
        idx for idx, w, h in dims
        if _is_misscan_candidate(w, h, pw_med, ph_med)
    ]
    if not candidates:
        print("[auto-rotate] no misscan-candidate pages found — nothing to rotate",
              file=sys.stderr)
        return None
    print(
        f"[auto-rotate] portrait median {pw_med}x{ph_med}; "
        f"{len(candidates)} candidate page(s): "
        f"{[c + 1 for c in candidates[:10]]}"
        f"{' ...' if len(candidates) > 10 else ''}",
        file=sys.stderr,
    )
    return {
        "dims": {idx: (w, h) for idx, w, h in dims},
        "pw_med": pw_med,
        "ph_med": ph_med,
        "convention_samples": [],
        "convention": None,
    }


def _maybe_rotate_page(raw_bytes: bytes, idx: int, ctx: dict,
                       quality: int = JPEG_QUALITY) -> bytes:
    """Rotate page `idx` if it's a misscan candidate; otherwise return raw_bytes."""
    w, h = ctx["dims"].get(idx, (0, 0))
    pw_med, ph_med = ctx["pw_med"], ctx["ph_med"]

    is_std_portrait = (
        0 < w < h
        and (1 - DEFAULT_DIM_TOL) * pw_med <= w <= (1 + DEFAULT_DIM_TOL) * pw_med
        and (1 - DEFAULT_DIM_TOL) * ph_med <= h <= (1 + DEFAULT_DIM_TOL) * ph_med
    )
    if (is_std_portrait
            and idx >= CONVENTION_SKIP_FIRST_PAGES
            and len(ctx["convention_samples"]) < CONVENTION_SAMPLE_TARGET):
        try:
            sample_im = Image.open(BytesIO(raw_bytes))
            t, b = _edge_brightness(sample_im, axis="v")
            if t < NEAR_BLANK_BRIGHTNESS or b < NEAR_BLANK_BRIGHTNESS:
                ctx["convention_samples"].append((t, b))
                ctx["convention"] = _learn_convention(ctx["convention_samples"])
        except Exception as e:
            print(f"[auto-rotate] page {idx + 1}: brightness sample failed ({e})",
                  file=sys.stderr)

    if not _is_misscan_candidate(w, h, pw_med, ph_med):
        return raw_bytes

    try:
        im = Image.open(BytesIO(raw_bytes))
    except Exception as e:
        print(f"[auto-rotate] page {idx + 1}: decode failed, skip rotate ({e})",
              file=sys.stderr)
        return raw_bytes

    rot, label = _decide_rotation(im, ctx["convention"])
    rotated = im.transpose(rot)
    if rotated.mode not in ("L", "RGB"):
        rotated = rotated.convert("RGB")
    buf = BytesIO()
    rotated.save(buf, "JPEG", quality=quality, optimize=True)
    print(
        f"[auto-rotate] page {idx + 1}: {w}x{h} -> "
        f"{rotated.width}x{rotated.height} ({label})",
        file=sys.stderr,
    )
    return buf.getvalue()


# ---------- Auto-crop (uniform fractional margin trim, default ON) ----------
#
# Scanned novels typically have 5-10% white margin on each side. Stripping
# them lets the content fill more of the 1103x1600 viewport so text reads
# larger. The detector:
#
#   1. Samples 10 pages from the middle 90% of the PDF.
#   2. Per page: estimates paper color from 4 corner regions, builds an
#      adaptive white threshold = max(180, paper - 25). This handles
#      yellowed / sepia paper that fixed thresholds (e.g. 240) would treat
#      as content.
#   3. Per page: finds the first row/column from each edge whose dark-pixel
#      fraction reaches 1% — this is the content edge. Margins are recorded
#      as fractions of page dimensions (so PDFs with mixed page resolutions
#      aggregate correctly).
#   4. Filters samples where any margin > 30% (chapter ends, full-bleed art)
#      or sum < 1% (dark pages, scan-frame pages).
#   5. Picks the 25th percentile of each side across remaining samples and
#      applies that uniform fractional crop to every page.
#
# Skipped (no cropping) when:
#   - Fewer than 3 valid samples remain.
#   - Any one of the 4 sides has < 3% margin at the 25th percentile.
#     This is the cheap "is this manga?" test: manga pages typically bleed
#     to at least one edge, so manga get this skip even if other sides have
#     margins.

def _detect_paper_color(im: Image.Image) -> float:
    """Median brightness across 4 corner squares — robust paper-color estimate."""
    g = im.convert("L")
    w, h = g.size
    sz = min(60, max(20, w // 30))
    corners = [
        g.crop((0, 0, sz, sz)),
        g.crop((w - sz, 0, w, sz)),
        g.crop((0, h - sz, sz, h)),
        g.crop((w - sz, h - sz, w, h)),
    ]
    return statistics.median(ImageStat.Stat(c).mean[0] for c in corners)


def _detect_content_margins(im: Image.Image) -> tuple[float, float, float, float]:
    """Return (top, bottom, left, right) margins as fractions of page dimensions."""
    paper = _detect_paper_color(im)
    g = im.convert("L")
    if max(g.size) > CROP_DOWNSCALE_LONG_SIDE:
        s = CROP_DOWNSCALE_LONG_SIDE / max(g.size)
        g = g.resize(
            (max(1, int(g.width * s)), max(1, int(g.height * s))),
            Image.LANCZOS,
        )
    g = g.filter(ImageFilter.MedianFilter(size=3))
    threshold = max(CROP_THRESHOLD_FLOOR, int(paper - CROP_THRESHOLD_OFFSET))
    w, h = g.size
    mask = g.point(lambda v: 1 if v <= threshold else 0, mode="L")
    data = mask.tobytes()
    max_dark_row = max(1, int(w * CROP_DARK_PCT))
    max_dark_col = max(1, int(h * CROP_DARK_PCT))

    top = 0
    while top < h and data[top * w:(top + 1) * w].count(1) < max_dark_row:
        top += 1
    bottom = 0
    while bottom < h:
        y = h - 1 - bottom
        if data[y * w:(y + 1) * w].count(1) >= max_dark_row:
            break
        bottom += 1

    g_t = mask.transpose(Image.Transpose.ROTATE_90)
    w_t, h_t = g_t.size
    data_t = g_t.tobytes()
    left = 0
    while left < h_t and data_t[left * w_t:(left + 1) * w_t].count(1) < max_dark_col:
        left += 1
    right = 0
    while right < h_t:
        y = h_t - 1 - right
        if data_t[y * w_t:(y + 1) * w_t].count(1) >= max_dark_col:
            break
        right += 1

    return (top / h, bottom / h, left / w, right / w)


def _setup_auto_crop(pdf_path: Path) -> Optional[tuple[float, float, float, float]]:
    """Return (T, B, L, R) fractional margins to crop, or None to skip."""
    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    if n < 5:
        print("[auto-crop] skipped: too few pages", file=sys.stderr)
        return None

    lo = int(n * CROP_SAMPLE_SKIP_PCT)
    hi = int(n * (1.0 - CROP_SAMPLE_SKIP_PCT))
    if hi - lo < CROP_SAMPLE_COUNT:
        idxs = list(range(lo, hi))
    else:
        step = (hi - lo) / CROP_SAMPLE_COUNT
        idxs = [int(lo + step * i) for i in range(CROP_SAMPLE_COUNT)]

    fracs: list[tuple[float, float, float, float]] = []
    for i in idxs:
        try:
            imgs = list(reader.pages[i].images)
            if not imgs:
                continue
            im = imgs[0].image
            fracs.append(_detect_content_margins(im))
        except Exception:
            continue

    clean = [
        f for f in fracs
        if max(f) < CROP_OUTLIER_MAX_FRAC and sum(f) >= CROP_OUTLIER_MIN_SUM
    ]
    if len(clean) < CROP_MIN_VALID_SAMPLES:
        print(
            f"[auto-crop] skipped: only {len(clean)} valid samples "
            f"(need {CROP_MIN_VALID_SAMPLES})",
            file=sys.stderr,
        )
        return None

    def _percentile(vals: list[float], p: int) -> float:
        s = sorted(vals)
        return s[int(p / 100 * (len(s) - 1))]

    def _percentile_artifact_aware(vals: list[float], p: int,
                                    label: str) -> tuple[float, bool]:
        """Percentile with systematic-artifact rejection.

        If a large fraction of samples report ~0% on this side, treat the 0%s
        as scanner artifacts (a 1-3 row dark band at the page edge from binding
        shadow or platen edge) and compute the percentile over the non-zero
        samples only. Manga has occasional full-bleed pages that legitimately
        return 0%; those stay below the threshold and the book correctly fails
        the CROP_MIN_MARGIN_FRAC check below.

        Returns (value, filtered_flag).
        """
        zeros = sum(1 for v in vals if v < 0.01)
        non_zero = [v for v in vals if v >= 0.01]
        if (zeros / len(vals) >= CROP_ARTIFACT_ZERO_FRAC
                and len(non_zero) >= CROP_ARTIFACT_MIN_NONZERO):
            print(
                f"[auto-crop] {label}: dropping {zeros}/{len(vals)} edge-artifact "
                f"sample(s) before percentile",
                file=sys.stderr,
            )
            return _percentile(non_zero, p), True
        return _percentile(vals, p), False

    T, _ = _percentile_artifact_aware([f[0] for f in clean], CROP_PERCENTILE, "T")
    B, _ = _percentile_artifact_aware([f[1] for f in clean], CROP_PERCENTILE, "B")
    L_raw, _ = _percentile_artifact_aware([f[2] for f in clean], CROP_PERCENTILE, "L")
    R_raw, _ = _percentile_artifact_aware([f[3] for f in clean], CROP_PERCENTILE, "R")

    if min(T, B, L_raw, R_raw) < CROP_MIN_MARGIN_FRAC:
        print(
            f"[auto-crop] skipped: T={T*100:.1f}% B={B*100:.1f}% "
            f"L={L_raw*100:.1f}% R={R_raw*100:.1f}% — a side <"
            f"{int(CROP_MIN_MARGIN_FRAC*100)}% (manga / edge content)",
            file=sys.stderr,
        )
        return None

    L = min(max(0.0, L_raw - CROP_BOOK_LR_BACKOFF), CROP_BOOK_LR_MAX)
    R = min(max(0.0, R_raw - CROP_BOOK_LR_BACKOFF), CROP_BOOK_LR_MAX)
    print(
        f"[auto-crop] enabled: T={T*100:.1f}% B={B*100:.1f}% "
        f"L={L*100:.1f}% R={R*100:.1f}% "
        f"(raw L={L_raw*100:.1f}% R={R_raw*100:.1f}%, "
        f"{len(clean)}/{len(fracs)} valid samples)",
        file=sys.stderr,
    )
    return (T, B, L, R)


# ---------- Image extraction & normalization ----------

def _encode_jpeg(im: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    buf = BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def extract_page_images(pdf_path: Path):
    """
    Yield (page_index, raw_bytes, ext_hint) for every PDF page.

    - If the page contains exactly one embedded JPEG and /Rotate is 0,
      yield its raw bytes untouched (lossless pass-through; the normalizer
      may still re-encode to hit the target viewport).
    - If /Rotate is 90/180/270 (set by a PDF editor to fix orientation),
      apply that rotation to the extracted image so the EPUB matches what
      a PDF reader would display.
    - Otherwise rasterize the page via Pillow through pypdf's decoded image.
    """
    reader = PdfReader(str(pdf_path))
    for i, page in enumerate(reader.pages):
        rot = _page_rotation(page)
        images = list(page.images)
        if len(images) == 1:
            img = images[0]
            data = img.data
            if rot == 0 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                yield i, data, "jpeg"
                continue
            im = img.image
            if rot:
                im = _apply_pdf_rotation(im, rot)
                print(f"[pdf-rotate] page {i + 1}: applied /Rotate={rot}",
                      file=sys.stderr)
            yield i, _encode_jpeg(im), "jpeg"
            continue
        # 0 or >=2 images on a page — fall back to a rasterized PIL render.
        # pypdf can't rasterize vector content; as a v0.1 compromise, we
        # composite any images we do find and warn the user.
        print(f"[warn] page {i + 1}: {len(images)} images found, "
              f"falling back to first image only", file=sys.stderr)
        if images:
            im = images[0].image
            if rot:
                im = _apply_pdf_rotation(im, rot)
                print(f"[pdf-rotate] page {i + 1}: applied /Rotate={rot}",
                      file=sys.stderr)
            yield i, _encode_jpeg(im), "jpeg"
        else:
            raise RuntimeError(
                f"page {i + 1} contains no extractable image; "
                f"this tool only supports scanned PDFs where each page is an image."
            )


def _is_effectively_grayscale(im: Image.Image) -> bool:
    """
    Decide whether an image is essentially monochrome — either a plain B&W
    scan or B&W content on age-tanned ("yaketa") paper whose tint should be
    stripped.

      1. Pages with almost no saturated pixels are plain B&W scans.
      2. Otherwise, look at the hue distribution of the saturated pixels.
         Age-tanning is physically always a warm yellow-orange tint: hue
         tightly concentrated inside 20-75 deg at moderate saturation.
         Anything else is real color and must be preserved:
           - cool / non-warm dominant hue (blue covers, pink section pages,
             green or red spot-color printing),
           - dispersed hues (full-color artwork, even when pale),
           - vivid saturation (solid-color endpapers, orange cover art).
    """
    if im.mode in ("L", "1"):
        return True
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > GRAYSCALE_DOWNSCALE_LONG_SIDE:
        s = GRAYSCALE_DOWNSCALE_LONG_SIDE / max(im.size)
        im = im.resize(
            (max(1, int(im.width * s)), max(1, int(im.height * s))),
            Image.NEAREST,
        )
    r, g, b = im.split()
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    sat = ImageChops.difference(hi, lo)
    colorful_mask = sat.point(
        lambda v: 255 if v > COLOR_SAT_FLOOR else 0, mode="L"
    )
    colorful_frac = ImageStat.Stat(colorful_mask).mean[0] / 255.0
    if colorful_frac < COLOR_MIN_COLORFUL_FRAC:
        return True

    # Circular hue statistics over the colorful pixels, computed from the
    # HSV hue-channel histogram (256 bins for 360 deg) so everything stays
    # in C-level PIL ops.
    hue_hist = im.convert("HSV").split()[0].histogram(mask=colorful_mask)
    total = sum(hue_hist)
    if total == 0:
        return True
    warm_lo = TAN_WARM_BAND_LO / 360.0 * 256.0
    warm_hi = TAN_WARM_BAND_HI / 360.0 * 256.0
    cos_sum = sin_sum = 0.0
    warm = 0
    for i, cnt in enumerate(hue_hist):
        if not cnt:
            continue
        ang = i * (2.0 * math.pi / 256.0)
        cos_sum += cnt * math.cos(ang)
        sin_sum += cnt * math.sin(ang)
        if warm_lo <= i <= warm_hi:
            warm += cnt
    conc = math.hypot(cos_sum / total, sin_sum / total)
    mean_hue = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
    warm_frac = warm / total
    sat_mean = ImageStat.Stat(sat, mask=colorful_mask).mean[0]
    return (conc >= TAN_HUE_CONC_MIN
            and warm_frac >= TAN_WARM_MIN_FRAC
            and TAN_MEAN_HUE_LO <= mean_hue <= TAN_MEAN_HUE_HI
            and sat_mean <= TAN_SAT_MEAN_MAX)


def normalize_to_viewport(img_bytes: bytes,
                          target_w: int = VIEWPORT_W,
                          target_h: int = VIEWPORT_H,
                          quality: int = JPEG_QUALITY,
                          crop_box: Optional[tuple[float, float, float, float]] = None,
                          crop_stats: Optional[dict] = None,
                          is_gray: Optional[bool] = None) -> bytes:
    """Fit-resize into target_w x target_h, letterboxing with white.

    RGB images that are essentially monochrome (B&W manga interiors, even
    with sepia paper tone) are auto-converted to L mode for smaller files
    with no perceptible quality loss.

    is_gray: pass the page's precomputed color class to override the
    per-image detection (build_epub decides book-wide: cover forced to
    color, sandwich rule). None = detect here.

    crop_box: optional (T, B, L, R) fractional margins (0..1) to strip from
    each side before fitting. Applied per-page using the page's own
    dimensions, so the same fractions work for PDFs with mixed page sizes.

    Per-page guard: a page is cropped only when (a) it is effectively
    monochrome (color illustrations stay full-frame) and (b) at most ONE
    of its four margins is below CROP_MIN_MARGIN_FRAC. A single tight side
    on an otherwise normal text page is usually a page-number/edge artifact
    and is fine to crop; two or more tight sides indicates a full-bleed
    B&W illustration that should stay full-frame. This preserves covers,
    frontispieces, and in-body illustrations while uniformly cropping the
    body text pages.

    Per-page cap: physical book trimming has per-page binding variation, so
    a uniform crop can shave text off the side that ended up tighter than
    the book median. Top, left, and right are capped at the page's own
    detected margin minus a small safety buffer. Bottom is left uncapped
    because page numbers typically pin per-page bottom detection at 0% even
    on otherwise normal text pages — capping there would suppress the crop
    everywhere. A per-page side measuring below CROP_PER_PAGE_TRUST_MIN is
    distrusted (likely a header line or scan artifact reaching the edge);
    those sides fall back to the book-level value rather than skipping the
    crop, since the tight_sides guard above already preserved any genuine
    edge-content pages.
    """
    im = Image.open(BytesIO(img_bytes))
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")

    if is_gray is None:
        is_gray = im.mode == "L" or _is_effectively_grayscale(im)

    apply_crop = False
    page_margins: Optional[tuple[float, float, float, float]] = None
    if crop_box is not None:
        if is_gray:
            page_margins = _detect_content_margins(im)
            tight_sides = sum(1 for x in page_margins if x < CROP_MIN_MARGIN_FRAC)
            if tight_sides <= 1:
                apply_crop = True
        if crop_stats is not None:
            if apply_crop:
                crop_stats["applied"] += 1
            elif not is_gray:
                crop_stats["skipped_color"] += 1
            else:
                crop_stats["skipped_tight"] += 1

    if apply_crop:
        W, H = im.size
        T_, B_, L_, R_ = page_margins  # type: ignore[misc]

        def _cap(book_val: float, page_val: float) -> float:
            if page_val < CROP_PER_PAGE_TRUST_MIN:
                return book_val
            return min(book_val, max(0.0, page_val - CROP_PER_PAGE_SAFETY))

        T = _cap(crop_box[0], T_)
        B = crop_box[1]  # see docstring: bottom is uncapped
        L = _cap(crop_box[2], L_)
        R = _cap(crop_box[3], R_)
        if crop_stats is not None:
            for book_v, page_v, side in (
                (crop_box[0], T_, "T"), (crop_box[2], L_, "L"), (crop_box[3], R_, "R"),
            ):
                if (page_v >= CROP_PER_PAGE_TRUST_MIN
                        and page_v - CROP_PER_PAGE_SAFETY < book_v):
                    crop_stats[f"capped_{side}"] = crop_stats.get(f"capped_{side}", 0) + 1
        top_px = int(T * H)
        bot_px = H - int(B * H)
        left_px = int(L * W)
        right_px = W - int(R * W)
        if right_px > left_px and bot_px > top_px:
            im = im.crop((left_px, top_px, right_px, bot_px))
    if im.mode == "RGB" and is_gray:
        im = im.convert("L")
    if (im.width, im.height) == (target_w, target_h):
        # Already correct size — re-emit with consistent encoder settings so
        # readers don't trip over unusual JPEG variants.
        buf = BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    ratio = min(target_w / im.width, target_h / im.height)
    nw, nh = max(1, int(round(im.width * ratio))), max(1, int(round(im.height * ratio)))
    resized = im.resize((nw, nh), Image.LANCZOS)
    bg = 255 if im.mode == "L" else (255, 255, 255)
    canvas = Image.new(im.mode, (target_w, target_h), bg)
    canvas.paste(resized, ((target_w - nw) // 2, (target_h - nh) // 2))
    buf = BytesIO()
    canvas.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ---------- XML helpers ----------

def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


# ---------- EPUB builder ----------

def build_epub(pdf_path: Path,
               out_path: Path,
               title: str,
               author: str,
               direction: str = "rtl",
               quality: int = JPEG_QUALITY,
               auto_rotate: bool = True,
               auto_crop: bool = True) -> None:
    uid = str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rotate_ctx = _setup_auto_rotate(pdf_path) if auto_rotate else None
    crop_box = _setup_auto_crop(pdf_path) if auto_crop else None
    crop_stats = (
        {"applied": 0, "skipped_color": 0, "skipped_tight": 0}
        if crop_box is not None else None
    )

    # 1a. Extract (and possibly rotate) every page, keeping raw JPEG bytes.
    print(f"[info] extracting pages from {pdf_path.name}", file=sys.stderr)
    raw_pages: list[bytes] = []
    for i, raw, _ext in extract_page_images(pdf_path):
        if rotate_ctx is not None:
            raw = _maybe_rotate_page(raw, i, rotate_ctx, quality=quality)
        raw_pages.append(raw)

    # 1b. Classify every page color/gray up front so the decision can use
    #     the whole book, not just the page itself:
    #       - the cover (page 1) is never grayscaled;
    #       - a short gray-judged run bracketed by color pages is recolored
    #         (color sections are printed in contiguous blocks, so a lone
    #         "gray" page between color pages is almost always a detection
    #         miss on a pale color page).
    #     draft() decodes JPEGs at reduced scale — the classifier downscales
    #     to GRAYSCALE_DOWNSCALE_LONG_SIDE anyway, so this pass stays cheap.
    gray_flags: list[bool] = []
    rgb_source: list[bool] = []
    for raw in raw_pages:
        try:
            im = Image.open(BytesIO(raw))
            im.draft(None, (GRAYSCALE_DOWNSCALE_LONG_SIDE,
                            GRAYSCALE_DOWNSCALE_LONG_SIDE))
            rgb_source.append(im.mode not in ("L", "1"))
            gray_flags.append(_is_effectively_grayscale(im))
        except Exception:
            rgb_source.append(False)
            gray_flags.append(True)

    cover_forced = False
    if gray_flags and gray_flags[0] and rgb_source[0]:
        gray_flags[0] = False
        cover_forced = True

    sandwich_pages: list[int] = []
    j = 1
    while j < len(gray_flags) - 1:
        if not gray_flags[j]:
            j += 1
            continue
        k = j
        while k < len(gray_flags) and gray_flags[k]:
            k += 1
        if (k - j <= SANDWICH_MAX_RUN and k < len(gray_flags)
                and not gray_flags[j - 1]
                and all(rgb_source[j:k])):
            for t in range(j, k):
                gray_flags[t] = False
                sandwich_pages.append(t)
        j = k

    n_color = sum(1 for f in gray_flags if not f)
    notes = []
    if cover_forced:
        notes.append("cover forced to color")
    if sandwich_pages:
        notes.append(f"sandwich rule recolored page(s) "
                     f"{[p + 1 for p in sandwich_pages]}")
    print(f"[color-detect] {n_color}/{len(gray_flags)} page(s) kept in color"
          + (f" ({'; '.join(notes)})" if notes else ""), file=sys.stderr)

    # 2. Normalize every page into the viewport.
    print("[info] normalizing pages", file=sys.stderr)
    page_blobs: list[tuple[str, str, bytes]] = []
    # each entry: (image_id, image_filename, jpeg_bytes)
    for i, raw in enumerate(raw_pages):
        jpg = normalize_to_viewport(raw, VIEWPORT_W, VIEWPORT_H, quality,
                                    crop_box=crop_box, crop_stats=crop_stats,
                                    is_gray=gray_flags[i])
        raw_pages[i] = b""  # free the raw page as we go
        if i == 0:
            image_id = "cover"
            image_name = "cover.jpg"
        else:
            image_id = f"i-{i:03d}"
            image_name = f"i-{i:03d}.jpg"
        page_blobs.append((image_id, image_name, jpg))
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  page {i + 1} ... {len(jpg) / 1024:.1f} KB", file=sys.stderr)

    if crop_stats is not None:
        print(
            f"[auto-crop] applied to {crop_stats['applied']} page(s); "
            f"preserved {crop_stats['skipped_color']} color and "
            f"{crop_stats['skipped_tight']} full-bleed illustration page(s)",
            file=sys.stderr,
        )
        capped = {s: crop_stats.get(f"capped_{s}", 0) for s in ("T", "L", "R")}
        if any(capped.values()):
            print(
                f"[auto-crop] per-page cap protected content on "
                f"T={capped['T']} L={capped['L']} R={capped['R']} page(s)",
                file=sys.stderr,
            )

    total = len(page_blobs)
    if total == 0:
        raise RuntimeError("no pages extracted from PDF")

    last_body_idx = total - 1

    # 3. Compose OPF parts.
    image_items_lines = []
    page_items_lines = []
    spine_lines = []

    for idx, (image_id, image_name, _blob) in enumerate(page_blobs):
        if idx == 0:
            item_props = ' properties="cover-image"'
            page_id = "p-cover"
            spread = "rendition:page-spread-center"
        else:
            item_props = ""
            page_id = f"p-{idx:03d}"
            # rtl: odd body page → right, even → left
            if direction == "rtl":
                spread = "page-spread-right" if (idx % 2 == 1) else "page-spread-left"
            else:
                spread = "page-spread-left" if (idx % 2 == 1) else "page-spread-right"

        image_items_lines.append(
            f'<item id="{image_id}" href="image/{image_name}" '
            f'media-type="image/jpeg"{item_props}/>'
        )
        page_items_lines.append(
            f'<item id="{page_id}" href="xhtml/{page_id}.xhtml" '
            f'media-type="application/xhtml+xml" properties="svg" '
            f'fallback="{image_id}"/>'
        )
        spine_lines.append(
            f'<itemref idref="{page_id}" linear="yes" properties="{spread}"/>'
        )

    opf_xml = OPF_TMPL.format(
        title=xml_escape(title),
        author=xml_escape(author),
        uid=uid,
        modified=modified,
        vw=VIEWPORT_W,
        vh=VIEWPORT_H,
        image_items="\n".join(image_items_lines),
        page_items="\n".join(page_items_lines),
        spine_items="\n".join(spine_lines),
        direction=direction,
    )

    # 4. Compose nav + ncx (3 TOC entries: 表紙 / 本編 / 奥付).
    if total >= 3:
        toc = [
            ("表紙", "xhtml/p-cover.xhtml"),
            ("本編", "xhtml/p-001.xhtml"),
            ("奥付", f"xhtml/p-{last_body_idx:03d}.xhtml"),
        ]
    elif total == 2:
        toc = [
            ("表紙", "xhtml/p-cover.xhtml"),
            ("本編", "xhtml/p-001.xhtml"),
        ]
    else:
        toc = [("表紙", "xhtml/p-cover.xhtml")]

    nav_items = "\n".join(
        f'<li><a href="{href}">{xml_escape(label)}</a></li>' for label, href in toc
    )
    nav_xml = NAV_XHTML_TMPL.format(toc_items=nav_items)

    nav_points = "\n".join(
        f'<navPoint id="navPoint-{i + 1}" playOrder="{i + 1}">'
        f'<navLabel><text>{xml_escape(label)}</text></navLabel>'
        f'<content src="{href}"/></navPoint>'
        for i, (label, href) in enumerate(toc)
    )
    ncx_xml = NCX_TMPL.format(uid=uid, title=xml_escape(title), nav_points=nav_points)

    # 5. Compose per-page XHTML.
    page_xhtmls: list[tuple[str, str]] = []
    for idx, (_image_id, image_name, _blob) in enumerate(page_blobs):
        page_id = "p-cover" if idx == 0 else f"p-{idx:03d}"
        xhtml = PAGE_XHTML_TMPL.format(
            title=xml_escape(title),
            vw=VIEWPORT_W, vh=VIEWPORT_H,
            image_name=image_name,
        )
        page_xhtmls.append((page_id, xhtml))

    # 6. Write the ZIP (mimetype STORED first, everything else DEFLATED).
    print(f"[info] writing {out_path}", file=sys.stderr)
    with zipfile.ZipFile(out_path, "w") as z:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, MIMETYPE)

        z.writestr("META-INF/container.xml", CONTAINER_XML,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("item/standard.opf", opf_xml,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("item/navigation-documents.xhtml", nav_xml,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("item/toc.ncx", ncx_xml,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("item/style/fixed-layout-jp.css", FIXED_LAYOUT_CSS,
                   compress_type=zipfile.ZIP_DEFLATED)

        for page_id, xhtml in page_xhtmls:
            z.writestr(f"item/xhtml/{page_id}.xhtml", xhtml,
                       compress_type=zipfile.ZIP_DEFLATED)

        for _image_id, image_name, blob in page_blobs:
            # JPEGs are already compressed — STORED keeps them as-is.
            z.writestr(f"item/image/{image_name}", blob,
                       compress_type=zipfile.ZIP_STORED)


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert a scanned-book PDF into a fixed-layout manga EPUB3."
    )
    ap.add_argument("pdf", type=Path, help="input PDF (each page = one scan image)")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="output EPUB path (default: <title>_<author>.epub next to input)")
    ap.add_argument("--title", default=None,
                    help="override title (default: parsed from filename)")
    ap.add_argument("--author", default=None,
                    help="override author (default: parsed from filename)")
    ap.add_argument("--direction", choices=("rtl", "ltr"), default="rtl",
                    help="page progression (default: rtl)")
    ap.add_argument("--quality", type=int, default=JPEG_QUALITY,
                    help=f"JPEG quality 1-95 (default: {JPEG_QUALITY}; "
                         f"raise for higher fidelity at the cost of file size)")
    ap.add_argument("--no-auto-rotate", action="store_true",
                    help="disable auto-rotation of misscanned landscape pages "
                         "(default: enabled — landscape pages whose dimensions "
                         "match a 90°-rotated portrait page are auto-rotated)")
    ap.add_argument("--no-auto-crop", action="store_true",
                    help="disable auto-cropping of page margins "
                         "(default: enabled — uniform fractional crop is "
                         "applied when all 4 sides have detected margin ≥ 3%; "
                         "manga and bleed pages are skipped automatically)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing output file")
    args = ap.parse_args(argv)

    pdf_path: Path = args.pdf
    if not pdf_path.is_file():
        print(f"[error] PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    parsed_title, parsed_author = parse_meta_from_filename(pdf_path)
    title = args.title or parsed_title
    author = args.author or parsed_author or "unknown"

    if not title:
        print("[error] could not determine title (pass --title)", file=sys.stderr)
        return 2

    t_src = "option" if args.title else "filename"
    a_src = "option" if args.author else ("filename" if parsed_author else "default")
    print(f"[info] title=\"{title}\" (from {t_src}), "
          f"author=\"{author}\" (from {a_src})", file=sys.stderr)

    out_path: Path = args.output or default_output_path(pdf_path, title, author if args.author or parsed_author else None)
    if out_path.exists() and not args.force:
        print(f"[error] output already exists: {out_path} (use --force)", file=sys.stderr)
        return 2
    print(f"[info] output -> {out_path}", file=sys.stderr)

    try:
        build_epub(pdf_path, out_path, title, author,
                   direction=args.direction, quality=args.quality,
                   auto_rotate=not args.no_auto_rotate,
                   auto_crop=not args.no_auto_crop)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    print(f"[done] {out_path} ({out_path.stat().st_size / 1024 / 1024:.1f} MB)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
