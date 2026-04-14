"""
HTML export: render all procedures for a model into a self-contained directory.

Output layout:
  <out_dir>/
    index.html            ← procedure list with cover image
    images/               ← all referenced images, flat namespace
    procedures/           ← one HTML file per procedure (POS + all linked docs)
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
  col { width: auto !important; }
  td, th { overflow-wrap: break-word; word-break: break-word; }
  td[style*="padding-left:8mm"] { padding-left: 2mm !important; }
  table[border="0"] > tbody > tr > td[style*="padding-left"] { padding-left: 2mm !important; }
  input, button, script, .noPrint { display: none !important; }
  .back-link { display: block; margin-bottom: 12px; color: #003399; font-size: 12pt; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }
</style>
"""


# ── path / slug helpers ───────────────────────────────────────────────────────

def _proc_slug(db_path: str) -> str:
    """Derive a filesystem-safe slug from a DB path (uppercase, no extension)."""
    basename = db_path.replace('\\', '/').rsplit('/', 1)[-1]
    return re.sub(r'\.XML$', '', basename, flags=re.IGNORECASE).upper()


def _link_to_db_path(link_path: str) -> str:
    """Convert a link:: path to a normalised DB path (upper-case, backslash).

    e.g. 'BMW-Motorrad/AUS/1111_0458_01_Motorhalter_ausbauen_AUS.xml'
      →  'BMW-MOTORRAD\\AUS\\1111_0458_01_MOTORHALTER_AUSBAUEN_AUS.XML'
    """
    return link_path.replace('/', '\\').upper()


def _link_to_slug(link_path: str) -> str:
    """Derive the slug for a link:: target path.

    The slug is the uppercased basename without extension — identical to what
    _proc_slug() returns for the corresponding DB path.
    """
    basename = link_path.rsplit('/', 1)[-1]
    return re.sub(r'\.xml$', '', basename, flags=re.IGNORECASE).upper()


def _proc_display_name(db_path: str, xml: str | None = None) -> str:
    """Human-readable procedure name.

    Prefers the real title from the XML (<EMPH BOLD="1">); falls back to the
    path-derived name if the XML is unavailable or has no such element.
    """
    if xml:
        m = re.search(r'<EMPH[^>]*BOLD="1"[^>]*>([^<]+)', xml)
        if m:
            return m.group(1).strip()
    basename = db_path.replace('\\', '/').rsplit('/', 1)[-1]
    m = re.search(r'\d{4}_\d{2}_\d+_(.+)_(?:POS|AD|BS|SW|TD|WAU|REPSCH)\.XML$',
                  basename, re.IGNORECASE)
    if m:
        return m.group(1).replace('_', ' ').title()
    return re.sub(r'\.XML$', '', basename, flags=re.IGNORECASE).replace('_', ' ')


# ── HTML post-processing helpers ──────────────────────────────────────────────

def _collect_and_copy_images(html: str, images_dir: str) -> tuple[str, set[str]]:
    """Find all file:// image URLs, copy each into images_dir, rewrite to ../images/."""
    copied: set[str] = set()
    seen: dict[str, str] = {}   # basename → abs source path (first seen wins)

    def _rewrite(m: re.Match) -> str:
        attr = m.group(1)
        url  = m.group(2)
        abs_path = urllib.parse.unquote(url.removeprefix('file://'))
        basename = os.path.basename(abs_path)
        if basename not in seen:
            seen[basename] = abs_path
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


def _extract_link_targets(html: str) -> list[str]:
    """Return all link:: targets as normalised DB paths (before link rewriting)."""
    raw = re.findall(r'href="link::(BMW-Motorrad[^"]+)"', html)
    return [_link_to_db_path(p) for p in raw]


def _rewrite_link_hrefs_local(html: str) -> str:
    """Rewrite link:: hrefs to relative ../procedures/<slug>.html paths."""
    def _replace(m: re.Match) -> str:
        slug = _link_to_slug(m.group(1))
        return f'href="../procedures/{slug}.html"'

    return re.sub(
        r'href="link::(BMW-Motorrad[^"]+)"',
        _replace,
        html,
    )


def _inject_css(html: str, css: str) -> str:
    if '</head>' in html:
        return html.replace('</head>', css + '</head>', 1)
    return css + html


def _add_back_link(html: str, back: str) -> str:
    if '<body>' in html:
        return html.replace('<body>', '<body>' + back, 1)
    return back + html


# ── core render helper ────────────────────────────────────────────────────────

def _render_and_write(
    db_path: str,
    reader: GdbReader,
    xsl_path: str,
    data_parent: str,
    images_dir: str,
    procedures_dir: str,
    back_html: str,
) -> tuple[str | None, list[str]]:
    """Render one DB record to an HTML file.

    Returns (slug, linked_db_paths) on success, (None, []) if the record
    has no content.  linked_db_paths is the list of link:: targets found
    in the rendered HTML (as normalised DB paths).
    """
    xml = reader.get_xml_exact(db_path)
    if not xml:
        return None, []

    html = xml_to_html(xml, xsl_path, data_parent)
    html, _ = _collect_and_copy_images(html, images_dir)

    # Collect link targets BEFORE rewriting them so the originals are readable
    linked = _extract_link_targets(html)

    html = _rewrite_link_hrefs_local(html)
    html = _inject_css(html, _SCREEN_CSS)
    html = _add_back_link(html, back_html)

    slug = _proc_slug(db_path)
    out_path = os.path.join(procedures_dir, slug + '.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return slug, linked


# ── public API ────────────────────────────────────────────────────────────────

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

    After rendering the primary POS procedures, performs a BFS over all
    link:: targets (preceding steps, disassembly docs, etc.) so that every
    cross-procedure link in the exported HTML resolves to a local file.

    Args:
        model_info:   ModelInfo with .code, .name, .image_path
        paths:        List of DB record paths (POS procedures, numerically sorted)
        out_dir:      Root output directory (will be created)
        xsl_path:     Path to RSD.XSL
        data_parent:  Parent of BMW-Motorrad/ (the DATAS/ directory)
        on_progress:  Optional callable(current: int, total: int, label: str)
    """
    images_dir     = os.path.join(out_dir, 'images')
    procedures_dir = os.path.join(out_dir, 'procedures')
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(procedures_dir, exist_ok=True)

    # Copy model cover image
    cover_filename: str | None = None
    if model_info.image_path and os.path.exists(model_info.image_path):
        cover_filename = os.path.basename(model_info.image_path)
        shutil.copy2(model_info.image_path, os.path.join(images_dir, cover_filename))

    back_html = (f'<a class="back-link" href="../index.html">'
                 f'&larr; {model_info.name} ({model_info.code})</a>')

    reader = GdbReader(config.DECODED_DB)
    procedures: list[dict] = []   # index entries (POS procedures only)
    rendered: set[str] = set()    # normalised DB paths already written to disk
    pending: list[str] = []       # linked docs queued for BFS rendering
    total = len(paths)

    # ── Phase 1: render primary (POS) procedures ──────────────────────────────
    for i, db_path in enumerate(paths):
        norm = db_path.upper()
        rendered.add(norm)

        if on_progress:
            on_progress(i + 1, total, _proc_slug(db_path))

        xml = reader.get_xml_exact(db_path)
        if not xml:
            continue

        name = _proc_display_name(db_path, xml)
        slug, linked = _render_and_write(
            db_path, reader, xsl_path, data_parent,
            images_dir, procedures_dir, back_html,
        )
        if slug:
            procedures.append({'slug': slug, 'name': name,
                                'filename': slug + '.html'})
            for lp in linked:
                if lp not in rendered:
                    pending.append(lp)

    # ── Phase 2: BFS over linked documents ────────────────────────────────────
    linked_count = 0
    while pending:
        next_pending: list[str] = []
        for db_path in pending:
            norm = db_path.upper()
            if norm in rendered:
                continue
            rendered.add(norm)

            _, linked = _render_and_write(
                db_path, reader, xsl_path, data_parent,
                images_dir, procedures_dir, back_html,
            )
            linked_count += 1
            for lp in linked:
                if lp not in rendered:
                    next_pending.append(lp)

        pending = list(set(next_pending) - rendered)

    reader.close()

    if linked_count and on_progress:
        on_progress(total, total, f'(+ {linked_count} linked documents rendered)')

    # ── Generate index.html ───────────────────────────────────────────────────
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
