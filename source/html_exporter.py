"""
HTML export: render all procedures for a model into a self-contained directory.

Output layout:
  <out_dir>/
    index.html            ← procedure list with cover image
    images/               ← all referenced images, flat namespace
    procedures/           ← one HTML file per procedure
"""

import os
import re
import shutil
import urllib.parse

import config
from gdb_reader import GdbReader
from model_registry import ModelInfo
from render import xml_to_html

# Injected into every exported procedure's <head>
_SCREEN_CSS = """
<style>
  body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt;
         max-width: 960px; margin: 0 auto; padding: 16px; }
  img  { max-width: 100% !important; height: auto !important; }
  table { max-width: 100% !important; word-break: break-word; }
  td, th { overflow-wrap: break-word; word-break: break-word; }
  td[style*="padding-left:8mm"] { padding-left: 2mm !important; }
  table[border="0"] > tbody > tr > td[style*="padding-left"] { padding-left: 2mm !important; }
  input, button, script, .noPrint { display: none !important; }
  .back-link { display: block; margin-bottom: 12px; color: #003399; font-size: 12pt; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }
</style>
"""


def _proc_slug(db_path: str) -> str:
    """Derive a filesystem-safe slug from a DB path."""
    basename = db_path.replace('\\', '/').rsplit('/', 1)[-1]
    # Strip .XML suffix
    return re.sub(r'\.XML$', '', basename, flags=re.IGNORECASE)


def _proc_display_name(db_path: str, xml: str | None = None) -> str:
    """Human-readable procedure name.

    Prefers the real title from the XML (<EMPH BOLD="1">); falls back to the
    path-derived German name if the XML is unavailable or has no such element.
    """
    if xml:
        m = re.search(r'<EMPH[^>]*BOLD="1"[^>]*>([^<]+)', xml)
        if m:
            return m.group(1).strip()
    # Fallback: derive from path
    basename = db_path.replace('\\', '/').rsplit('/', 1)[-1]
    m = re.search(r'\d{4}_\d{2}_\d+_(.+)_(?:POS|AD|BS|SW|TD|WAU|REPSCH)\.XML$',
                  basename, re.IGNORECASE)
    if m:
        return m.group(1).replace('_', ' ').title()
    return re.sub(r'\.XML$', '', basename, flags=re.IGNORECASE).replace('_', ' ')


def _collect_and_copy_images(html: str, images_dir: str) -> tuple[str, set[str]]:
    """
    Find all file:// image URLs in html, copy each image into images_dir,
    and rewrite the src/href to ../images/<basename>.

    Returns (rewritten_html, set_of_copied_basenames).
    """
    copied: set[str] = set()
    collision_map: dict[str, str] = {}   # basename → abs source path (first seen)

    def _rewrite(m: re.Match) -> str:
        attr = m.group(1)   # 'src=' or 'href='
        url  = m.group(2)   # file:// URL
        # Decode URL encoding and strip file:// prefix
        abs_path = urllib.parse.unquote(url.removeprefix('file://'))
        basename = os.path.basename(abs_path)

        if basename not in collision_map:
            collision_map[basename] = abs_path
            if os.path.exists(abs_path) and basename not in copied:
                shutil.copy2(abs_path, os.path.join(images_dir, basename))
                copied.add(basename)
        return f'{attr}"../images/{basename}"'

    rewritten = re.sub(
        r'(src=|href=)"(file://[^"]+\.(jpg|gif|png|JPG|GIF|PNG))"',
        _rewrite,
        html,
    )
    return rewritten, copied


def _inject_css(html: str, css: str) -> str:
    """Insert css block before </head> (or at top of body if no </head>)."""
    if '</head>' in html:
        return html.replace('</head>', css + '</head>', 1)
    return css + html


def export_model_html(
    model_info: ModelInfo,
    paths: list[str],
    out_dir: str,
    xsl_path: str,
    data_parent: str,
    on_progress=None,
) -> None:
    """
    Render all procedures in `paths` to a self-contained HTML directory.

    Args:
        model_info:   ModelInfo with .code, .name, .image_path
        paths:        List of DB record paths (already filtered, e.g. POS only)
        out_dir:      Root output directory (will be created)
        xsl_path:     Path to RSD.XSL
        data_parent:  Parent of BMW-Motorrad/ (the DATAS/ directory)
        on_progress:  Optional callable(current: int, total: int, slug: str)
    """
    images_dir    = os.path.join(out_dir, 'images')
    procedures_dir = os.path.join(out_dir, 'procedures')
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(procedures_dir, exist_ok=True)

    # Copy model cover image
    cover_filename: str | None = None
    if model_info.image_path and os.path.exists(model_info.image_path):
        cover_filename = os.path.basename(model_info.image_path)
        shutil.copy2(model_info.image_path, os.path.join(images_dir, cover_filename))

    reader = GdbReader(config.DECODED_DB)
    procedures: list[dict] = []   # {slug, name, filename}
    total = len(paths)

    for i, db_path in enumerate(paths):
        slug = _proc_slug(db_path)
        name = _proc_display_name(db_path)
        html_filename = slug + '.html'

        if on_progress:
            on_progress(i + 1, total, slug)

        xml = reader.get_xml_exact(db_path)
        if not xml:
            continue

        name = _proc_display_name(db_path, xml)  # use real title now we have the XML
        html = xml_to_html(xml, xsl_path, data_parent)
        html, _ = _collect_and_copy_images(html, images_dir)
        html = _inject_css(html, _SCREEN_CSS)

        # Add back-navigation link
        back = (f'<a class="back-link" href="../index.html">'
                f'&larr; {model_info.name} ({model_info.code})</a>')
        if '<body>' in html:
            html = html.replace('<body>', '<body>' + back, 1)
        else:
            html = back + html

        out_path = os.path.join(procedures_dir, html_filename)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html)

        procedures.append({'slug': slug, 'name': name, 'filename': html_filename})

    reader.close()

    # Generate index.html
    cover_img_tag = ''
    if cover_filename:
        cover_img_tag = (f'<img src="images/{cover_filename}" '
                         f'style="max-height:140px;margin-bottom:16px;display:block">')

    proc_items = '\n'.join(
        f'    <li><a href="procedures/{p["filename"]}">{p["name"]}</a></li>'
        for p in procedures
    )

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>BMW {model_info.name} ({model_info.code}) — Repair Manual</title>
  <style>
    body {{ font-family: Helvetica, Arial, sans-serif; font-size: 10pt;
             max-width: 800px; margin: 0 auto; padding: 24px; }}
    h1   {{ color: #003399; margin-bottom: 4px; }}
    .subtitle {{ color: #666; margin-top: 0; font-size: 11pt; }}
    ul   {{ list-style: none; padding: 0; margin-top: 16px; }}
    li a {{ display: block; padding: 7px 10px; border-bottom: 1px solid #eee;
             text-decoration: none; color: #222; }}
    li a:hover {{ background: #f0f4ff; color: #003399; }}
  </style>
</head>
<body>
  {cover_img_tag}
  <h1>BMW {model_info.name}</h1>
  <p class="subtitle">Model {model_info.code} &mdash; {len(procedures)} repair procedures</p>
  <ul>
{proc_items}
  </ul>
</body>
</html>"""

    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(index_html)
