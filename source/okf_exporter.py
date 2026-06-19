"""
OKF (Open Knowledge Format) export.

Render all procedures for a model into a portable Open Knowledge Format bundle:
a directory of Markdown files with YAML frontmatter, cross-linked with relative
Markdown links, with referenced images copied in. Designed to be handed to an
LLM/agent (Claude Code, Codex, …) as a navigable knowledge base for inference.

Output layout (per the OKF v0.1 spec — see
https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing):

  <out_dir>/
    index.md              ← model overview + grouped procedure links (entry point)
    log.md                ← generation provenance (reserved OKF filename)
    images/               ← all referenced procedure diagrams, flat namespace
    procedures/
      index.md            ← full concept list (progressive disclosure)
      <SLUG>.md           ← one concept per document (POS + sub-docs + linked docs)

Each <SLUG>.md is one OKF "concept": YAML frontmatter (the only required field is
`type`; standard optional fields `title`, `description`, `resource`, `tags`,
`timestamp`) followed by a GFM Markdown body.

This mirrors html_exporter.export_model_html (same record loop, same BFS over
link:: cross-references, same image copying) — only the per-document
serialization (HTML page → Markdown concept) and the index/log generation differ.
"""

import os
import re
import shutil
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

import config
from gdb_reader import GdbReader
from model_registry import ModelInfo
from render import xml_to_html

# Reuse the pure path / slug / image / link helpers from the HTML exporter so the
# OKF and HTML outputs share one source of truth (html_exporter is left untouched).
from html_exporter import (
    _TYPE_LABELS,
    _proc_slug,
    _proc_num_from_path,
    _doc_type_from_path,
    _proc_display_name,
    _clean_subdoc_label,
    _extract_link_targets,
    _link_to_slug,
    _collect_and_copy_images,
)


# ── OKF `type` taxonomy ───────────────────────────────────────────────────────

# Maps a document-type suffix (POS, AD, AUS, …) to an OKF `type` string.
# Builds on _TYPE_LABELS (AD/SW/BS/TD/WAU/REPSCH) from html_exporter and adds the
# main procedure type plus the cross-referenced sub-step document types.
_OKF_TYPES: dict[str, str] = {
    'POS':     'Repair Procedure',
    'AUS':     'Removal',
    'EIN':     'Installation',
    'TEILAUS': 'Partial Removal',
    'TEILEIN': 'Partial Installation',
    'SPEZW':   'Special Work',
    'PRUE':    'Test',
    'EINST':   'Adjustment',
    'FUELL':   'Filling',
    'INB':     'Commissioning',
    **_TYPE_LABELS,
}


def okf_type(doc_type: str) -> str:
    """Map a document-type suffix to an OKF `type` string (case-insensitive)."""
    return _OKF_TYPES.get((doc_type or '').upper(), 'Repair Document')


# ── YAML frontmatter ──────────────────────────────────────────────────────────

# Standard OKF frontmatter fields, in canonical order (type is required & first).
_FRONTMATTER_ORDER = ('type', 'title', 'description', 'resource', 'tags', 'timestamp')


def build_frontmatter(meta: dict) -> str:
    """Build a YAML frontmatter block ('---' … '---') from a metadata dict.

    `type` is required (OKF's only mandatory field) and is emitted first; the
    remaining standard fields follow in canonical order. Only present, non-empty
    keys are written. The body is produced with yaml.safe_dump so any colons,
    quotes, or non-ASCII characters are escaped correctly.
    """
    import yaml  # local import: only needed when serializing

    if 'type' not in meta or not meta['type']:
        raise ValueError('OKF frontmatter requires a non-empty "type" field')

    ordered: dict = {}
    for key in _FRONTMATTER_ORDER:
        value = meta.get(key)
        if value is None or value == '' or value == []:
            continue
        ordered[key] = value

    body = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=10_000)
    return f'---\n{body}---\n'


# ── HTML → Markdown ───────────────────────────────────────────────────────────

# Elements that are UI chrome or non-content and must never reach the Markdown.
_DROP_TAGS = ('script', 'style', 'noscript', 'input', 'button')

# A table is a real data table (→ GFM) only if it has border="1"; RSD.XSL uses
# borderless tables purely for layout/indentation, which would otherwise become
# unreadable empty pipe-tables.
_DATA_TABLE_BORDERS = ('1',)


def _is_layout_table(table) -> bool:
    return str(table.get('border')) not in _DATA_TABLE_BORDERS


def _figure_stem(src: str) -> str | None:
    """Return the shared stem of a BILD figure's _preview/_small variants."""
    m = re.search(r'(.+?)_(?:preview|small)\.(?:jpg|jpeg|png|gif)$', src, re.I)
    return m.group(1) if m else None


def _count(n: int, singular: str) -> str:
    """'1 procedure' / '3 procedures' — naive English pluralization."""
    return f'{n} {singular}' if n == 1 else f'{n} {singular}s'


def _clean_content_html(html: str) -> str:
    """Reduce rendered RSD.XSL HTML to clean, content-only HTML for Markdown.

    RSD.XSL output is built for an interactive viewer: hundreds of nested
    borderless layout tables, UI-icon <img>s (bullets, arrows, expand/collapse),
    and JavaScript callbacks. This:
      - drops <script>/<style>/form controls and .noPrint chrome;
      - removes UI-icon images (anything under imgs/), keeping BILD/ diagrams;
      - strips on* event handlers (which embed icon paths);
      - flattens borderless layout tables to <div> blocks, leaving border="1"
        data tables intact so markdownify renders them as GFM tables.
    """
    if not html or not html.strip():
        return ''
    soup = BeautifulSoup(html, 'html.parser')

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.select('.noPrint'):
        tag.decompose()

    # Drop UI-icon images; keep real procedure diagrams (BILD/).
    for img in soup.find_all('img'):
        if '/imgs/' in (img.get('src') or ''):
            img.decompose()

    # Drop redundant _small thumbnails when the _preview of the same figure is
    # present (RSD pairs an inline thumbnail with a larger preview).
    preview_stems = {stem for img in soup.find_all('img')
                     if (stem := _figure_stem(img.get('src') or '')) and '_preview' in (img.get('src') or '').lower()}
    for img in soup.find_all('img'):
        src = img.get('src') or ''
        if '_small' in src.lower() and _figure_stem(src) in preview_stems:
            img.decompose()

    # Blank out path-like alt text so images render as ![](…), not ![long/path](…).
    for img in soup.find_all('img'):
        alt = img.get('alt') or ''
        if 'BMW-Motorrad' in alt or '/' in alt or alt.lower().endswith(('.jpg', '.png', '.gif')):
            img['alt'] = ''

    # Strip inline event handlers (they carry hardcoded icon paths).
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.startswith('on')]:
            del tag.attrs[attr]

    # Flatten layout tables → divs; keep data tables (border="1") as tables.
    for cell in soup.find_all(['tr', 'td', 'th', 'tbody', 'thead', 'col', 'colgroup']):
        parent_table = cell.find_parent('table')
        if parent_table is None or _is_layout_table(parent_table):
            cell.name = 'div'
    for table in soup.find_all('table'):
        if _is_layout_table(table):
            table.name = 'div'

    root = soup.body if soup.body is not None else soup
    return str(root)


def html_to_markdown(html: str) -> str:
    """Convert rendered procedure HTML to clean GFM Markdown.

    Cleans the HTML (see _clean_content_html), then converts with markdownify
    (ATX headings, '-' bullets, GFM data tables). Runs of blank lines and stray
    leftover empty table pipes are collapsed.
    """
    clean = _clean_content_html(html)
    if not clean:
        return ''
    md = _markdownify(clean, heading_style='ATX', bullets='-')
    # Drop empty markdown table rows left by any residual single-column layout.
    md = re.sub(r'(?m)^\s*\|[\s|]*\|\s*$\n?', '', md)
    # Drop standalone expand/collapse chrome words left by the viewer controls.
    md = re.sub(r'(?mi)^\s*(?:close|open)\s*$\n?', '', md)
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


# ── link rewriting ────────────────────────────────────────────────────────────

def rewrite_links_okf(html: str) -> str:
    """Rewrite cross-reference hrefs for the OKF bundle.

    - href="link::BMW-Motorrad/…"  → href="./<SLUG>.md"   (relative concept link)
    - <a href="image::…">…</a>      → <span>…</span>        (zoom callout, keep text)
    - <a href="javascript:…">…</a>  → <span>…</span>        (UI callback, no target)
    - href="#anchor"                → drop the href          (same-page anchor)

    The javascript/anchor handling mirrors render.strip_pdf_hrefs.
    """
    def _replace_link(m: re.Match) -> str:
        slug = _link_to_slug(m.group(1))
        return f'href="./{slug}.md"'

    html = re.sub(r'href="link::(BMW-Motorrad[^"]+)"', _replace_link, html)
    html = re.sub(
        r'<a\b([^>]*)\bhref="(?:image::|javascript:)[^"]*"([^>]*)>(.*?)</a>',
        r'<span\1\2>\3</span>',
        html, flags=re.S | re.I,
    )
    html = re.sub(r'\bhref="#[^"]*"', '', html)
    return html


# ── description ───────────────────────────────────────────────────────────────

def derive_description(md_body: str, limit: int = 200) -> str:
    """First prose sentence of a Markdown body, truncated for the frontmatter.

    Skips headings (#…), table rows (|…), list items (-…), images (![…]) and
    short single-word UI-chrome fragments. Returns '' if no prose line is found.
    """
    for raw_line in md_body.splitlines():
        line = raw_line.strip()
        if not line or line[0] in '#|-(' or line.startswith('!['):
            continue  # skip headings, tables, lists, ![imgs], and (+)/(-)/(1) markers
        line = re.sub(r'\s+', ' ', line)
        plain = re.sub(r'[*_`>]', '', line).strip()
        if len(plain) < 12 or ' ' not in plain:        # skip chrome like "close"
            continue
        if plain.lower() in _SECTION_CHROME:           # skip structural labels
            continue
        if len(plain) > limit:
            return plain[:limit].rstrip() + '…'
        return plain
    return ''


# Structural section labels that are not useful as a concept description.
_SECTION_CHROME = {'core activity', 'preparatory work', 'follow-up work'}


# ── per-document rendering ────────────────────────────────────────────────────

def _render_doc_to_md(
    db_path: str,
    xml: str,
    xsl_path: str,
    data_parent: str,
    images_dir: str,
    procedures_dir: str,
    timestamp: str,
    model_info: ModelInfo,
    *,
    is_main: bool,
) -> tuple[str, str, str, list[str]]:
    """Render one DB record to a procedures/<SLUG>.md concept file.

    Returns (slug, display_name, okf_type_label, linked_db_paths).
    """
    html = xml_to_html(xml, xsl_path, data_parent)

    # Collect link targets BEFORE rewriting them (the originals carry the path).
    linked = _extract_link_targets(html)

    html = rewrite_links_okf(html)
    html = _clean_content_html(html)              # drop icons/chrome before copying images
    html, _ = _collect_and_copy_images(html, images_dir, data_parent)
    md_body = html_to_markdown(html)

    raw_name = _proc_display_name(db_path, xml)
    doc_type = _doc_type_from_path(db_path)
    if is_main:
        display_name = raw_name
    else:
        display_name = _clean_subdoc_label(raw_name) or _TYPE_LABELS.get(doc_type, raw_name)

    type_label = okf_type(doc_type)
    section = db_path.replace('\\', '/').rsplit('/', 1)[-1].split('_', 1)[0]
    frontmatter = build_frontmatter({
        'type':        type_label,
        'title':       display_name,
        'description': derive_description(md_body),
        'resource':    db_path,
        'tags':        ['bmw', 'motorcycle', model_info.code, model_info.name,
                        doc_type, f'section-{section}'],
        'timestamp':   timestamp,
    })

    slug = _proc_slug(db_path)
    content = f'{frontmatter}\n# {display_name}\n\n{md_body}\n'
    with open(os.path.join(procedures_dir, slug + '.md'), 'w', encoding='utf-8') as f:
        f.write(content)

    return slug, display_name, type_label, linked


# ── public API ────────────────────────────────────────────────────────────────

def export_model_okf(
    model_info: ModelInfo,
    paths: list[str],
    out_dir: str,
    xsl_path: str,
    data_parent: str,
    on_progress=None,
    timestamp: str | None = None,
) -> None:
    """
    Render all procedures in `paths` to a self-contained OKF bundle.

    After rendering the primary POS procedures, performs a BFS over all link::
    targets (removal/installation/special-work docs, …) so every cross-procedure
    link in the bundle resolves to a local concept file — the same traversal as
    export_model_html.

    Args:
        model_info:   ModelInfo with .code, .name, .image_path
        paths:        List of DB record paths (POS procedures, numerically sorted)
        out_dir:      Root output directory (will be created)
        xsl_path:     Path to RSD.XSL
        data_parent:  Parent of BMW-Motorrad/ (the DATAS/ directory)
        on_progress:  Optional callable(current: int, total: int, label: str)
        timestamp:    ISO-8601 string for frontmatter/log (default: now, UTC)
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    images_dir     = os.path.join(out_dir, 'images')
    procedures_dir = os.path.join(out_dir, 'procedures')
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(procedures_dir, exist_ok=True)

    # Copy model cover image (referenced from the root index.md)
    cover_filename: str | None = None
    if model_info.image_path and os.path.exists(model_info.image_path):
        cover_filename = os.path.basename(model_info.image_path)
        shutil.copy2(model_info.image_path, os.path.join(images_dir, cover_filename))

    reader = GdbReader(config.DECODED_DB)

    groups: list[dict] = []            # one entry per POS procedure (+ its sub-docs)
    current_group: dict | None = None
    current_proc_num: str = ''
    linked_docs: list[dict] = []       # BFS-discovered cross-referenced documents
    rendered: set[str] = set()         # normalised DB paths already written
    pending: list[str] = []            # link:: targets queued for BFS
    total = len(paths)

    # ── Phase 1: POS procedures + their sub-docs ─────────────────────────────
    for i, db_path in enumerate(paths):
        norm     = db_path.upper()
        proc_num = _proc_num_from_path(db_path)
        is_main  = (_doc_type_from_path(db_path) == 'POS')
        rendered.add(norm)

        if on_progress:
            on_progress(i + 1, total, _proc_slug(db_path))

        xml = reader.get_xml_exact(db_path)
        if not xml:
            continue

        slug, name, type_label, linked = _render_doc_to_md(
            db_path, xml, xsl_path, data_parent, images_dir, procedures_dir,
            timestamp, model_info, is_main=is_main,
        )

        if is_main:
            current_group = {'name': name, 'slug': slug, 'sub_docs': []}
            groups.append(current_group)
            current_proc_num = proc_num
        elif current_group and proc_num == current_proc_num:
            current_group['sub_docs'].append({'label': name, 'slug': slug})

        for lp in linked:
            if lp not in rendered:
                pending.append(lp)

    # ── Phase 2: BFS over linked documents ───────────────────────────────────
    while pending:
        next_pending: list[str] = []
        for db_path in pending:
            norm = db_path.upper()
            if norm in rendered:
                continue
            rendered.add(norm)

            xml = reader.get_xml_exact(db_path)
            if not xml:
                continue

            slug, name, type_label, linked = _render_doc_to_md(
                db_path, xml, xsl_path, data_parent, images_dir, procedures_dir,
                timestamp, model_info, is_main=False,
            )
            linked_docs.append({'label': name, 'slug': slug, 'type': type_label})
            for lp in linked:
                if lp not in rendered:
                    next_pending.append(lp)

        pending = list(set(next_pending) - rendered)

    reader.close()

    if linked_docs and on_progress:
        on_progress(total, total, f'(+ {len(linked_docs)} linked documents rendered)')

    image_count = len([n for n in os.listdir(images_dir)]) if os.path.isdir(images_dir) else 0

    _write_procedures_index(procedures_dir, model_info, groups, linked_docs, timestamp)
    _write_root_index(out_dir, model_info, groups, cover_filename, timestamp,
                      len(groups), len(linked_docs))
    _write_log(out_dir, model_info, len(groups), len(linked_docs), image_count, timestamp)


# ── index / log generation ────────────────────────────────────────────────────

def _write_root_index(
    out_dir: str, model_info: ModelInfo, groups: list[dict],
    cover_filename: str | None, timestamp: str,
    n_procedures: int, n_linked: int,
) -> None:
    """Write the bundle entry point index.md (overview + grouped procedure links)."""
    title = f'BMW {model_info.name} ({model_info.code}) — Repair Manual'
    frontmatter = build_frontmatter({
        'type':        'Repair Manual',
        'title':       title,
        'description': (f'Open Knowledge Format bundle of {_count(n_procedures, "repair procedure")} '
                        f'for the BMW {model_info.name} ({model_info.code}).'),
        'tags':        ['bmw', 'motorcycle', model_info.code, model_info.name],
        'timestamp':   timestamp,
    })

    lines: list[str] = [frontmatter]
    if cover_filename:
        lines.append(f'![BMW {model_info.name}](./images/{cover_filename})\n')
    lines.append(f'# BMW {model_info.name} — Repair Manual\n')
    lines.append(
        f'Model **{model_info.code}** — {_count(n_procedures, "repair procedure")}'
        + (f' (+ {_count(n_linked, "cross-referenced document")}).' if n_linked else '.')
    )
    lines.append(
        '\nThis bundle is in Open Knowledge Format: each procedure is a Markdown '
        'file under `procedures/` with YAML frontmatter and relative cross-links. '
        'See [the full document list](./procedures/index.md).\n'
    )
    lines.append('## Procedures\n')
    for g in groups:
        line = f'- [{g["name"]}](./procedures/{g["slug"]}.md)'
        if g['sub_docs']:
            subs = ', '.join(f'[{s["label"]}](./procedures/{s["slug"]}.md)'
                             for s in g['sub_docs'])
            line += f' — {subs}'
        lines.append(line)

    with open(os.path.join(out_dir, 'index.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def _write_procedures_index(
    procedures_dir: str, model_info: ModelInfo, groups: list[dict],
    linked_docs: list[dict], timestamp: str,
) -> None:
    """Write procedures/index.md — the full concept list (progressive disclosure)."""
    total = len(groups) + len(linked_docs)
    frontmatter = build_frontmatter({
        'type':        'Index',
        'title':       f'Procedures — BMW {model_info.name} ({model_info.code})',
        'description': f'All {total} concept documents in this OKF bundle.',
        'timestamp':   timestamp,
    })

    lines: list[str] = [frontmatter, '# Procedures\n']
    lines.append(_count(len(groups), 'repair procedure')
                 + (f' + {_count(len(linked_docs), "cross-referenced document")}' if linked_docs else '')
                 + '.\n')
    lines.append('## Repair procedures\n')
    for g in groups:
        lines.append(f'- [{g["name"]}](./{g["slug"]}.md) (`Repair Procedure`)')
        for s in g['sub_docs']:
            lines.append(f'  - [{s["label"]}](./{s["slug"]}.md)')

    if linked_docs:
        lines.append('\n## Cross-referenced documents\n')
        for d in sorted(linked_docs, key=lambda x: x['slug']):
            lines.append(f'- [{d["label"]}](./{d["slug"]}.md) (`{d["type"]}`)')

    with open(os.path.join(procedures_dir, 'index.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def _write_log(
    out_dir: str, model_info: ModelInfo,
    n_procedures: int, n_linked: int, n_images: int, timestamp: str,
) -> None:
    """Write log.md — the reserved OKF chronological-history file (one entry)."""
    entry = (
        f'- {timestamp} — Generated OKF bundle for BMW {model_info.name} '
        f'({model_info.code}): {_count(n_procedures, "procedure")}'
        + (f', +{_count(n_linked, "cross-referenced document")}' if n_linked else '')
        + f', {_count(n_images, "image")}. '
        f'Source: BMW GRIPS repair-manual database. '
        f'Tool: `python source/main.py export --model {model_info.code} --format okf`.'
    )
    content = f'# Change log\n\n{entry}\n'
    with open(os.path.join(out_dir, 'log.md'), 'w', encoding='utf-8') as f:
        f.write(content)
