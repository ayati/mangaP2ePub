#!/usr/bin/env python3
"""
manga_p2epub.py — Convert a scanned-book PDF into a fixed-layout (manga) EPUB3.

Input:  one PDF per book, each page holds a single embedded scan image.
Output: EBPAJ 1.1.2 style pre-paginated EPUB with 1103x1600 viewport.

v0.1 — minimum working pipeline.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image
from pypdf import PdfReader


# ---------- Constants ----------

VIEWPORT_W = 1103
VIEWPORT_H = 1600
JPEG_QUALITY = 88


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


# ---------- Image extraction & normalization ----------

def extract_page_images(pdf_path: Path):
    """
    Yield (page_index, raw_bytes, ext_hint) for every PDF page.

    - If the page contains exactly one embedded JPEG, yield its raw bytes
      untouched (lossless pass-through; the normalizer may still re-encode
      to hit the target viewport).
    - Otherwise rasterize the page via Pillow through pypdf's decoded image.
    """
    reader = PdfReader(str(pdf_path))
    for i, page in enumerate(reader.pages):
        images = list(page.images)
        if len(images) == 1:
            img = images[0]
            data = img.data
            if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                yield i, data, "jpeg"
                continue
            # Non-JPEG single image: hand off decoded PIL for re-encode.
            buf = BytesIO()
            img.image.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            yield i, buf.getvalue(), "jpeg"
            continue
        # 0 or >=2 images on a page — fall back to a rasterized PIL render.
        # pypdf can't rasterize vector content; as a v0.1 compromise, we
        # composite any images we do find and warn the user.
        print(f"[warn] page {i + 1}: {len(images)} images found, "
              f"falling back to first image only", file=sys.stderr)
        if images:
            buf = BytesIO()
            images[0].image.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            yield i, buf.getvalue(), "jpeg"
        else:
            raise RuntimeError(
                f"page {i + 1} contains no extractable image; "
                f"this tool only supports scanned PDFs where each page is an image."
            )


def normalize_to_viewport(img_bytes: bytes,
                          target_w: int = VIEWPORT_W,
                          target_h: int = VIEWPORT_H,
                          quality: int = JPEG_QUALITY) -> bytes:
    """Fit-resize into target_w x target_h, letterboxing with white."""
    im = Image.open(BytesIO(img_bytes))
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
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
               quality: int = JPEG_QUALITY) -> None:
    uid = str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Gather all page images into memory, normalized.
    print(f"[info] extracting & normalizing pages from {pdf_path.name}", file=sys.stderr)
    page_blobs: list[tuple[str, str, bytes]] = []
    # each entry: (image_id, image_filename, jpeg_bytes)
    for i, raw, _ext in extract_page_images(pdf_path):
        jpg = normalize_to_viewport(raw, VIEWPORT_W, VIEWPORT_H, quality)
        if i == 0:
            image_id = "cover"
            image_name = "cover.jpg"
        else:
            image_id = f"i-{i:03d}"
            image_name = f"i-{i:03d}.jpg"
        page_blobs.append((image_id, image_name, jpg))
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  page {i + 1} ... {len(jpg) / 1024:.1f} KB", file=sys.stderr)

    total = len(page_blobs)
    if total == 0:
        raise RuntimeError("no pages extracted from PDF")

    last_body_idx = total - 1

    # 2. Compose OPF parts.
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

    # 3. Compose nav + ncx (3 TOC entries: 表紙 / 本編 / 奥付).
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

    # 4. Compose per-page XHTML.
    page_xhtmls: list[tuple[str, str]] = []
    for idx, (_image_id, image_name, _blob) in enumerate(page_blobs):
        page_id = "p-cover" if idx == 0 else f"p-{idx:03d}"
        xhtml = PAGE_XHTML_TMPL.format(
            title=xml_escape(title),
            vw=VIEWPORT_W, vh=VIEWPORT_H,
            image_name=image_name,
        )
        page_xhtmls.append((page_id, xhtml))

    # 5. Write the ZIP (mimetype STORED first, everything else DEFLATED).
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
                    help=f"JPEG quality 1-95 (default: {JPEG_QUALITY})")
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
                   direction=args.direction, quality=args.quality)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    print(f"[done] {out_path} ({out_path.stat().st_size / 1024 / 1024:.1f} MB)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
