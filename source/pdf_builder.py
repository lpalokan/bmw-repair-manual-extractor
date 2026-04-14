"""
HTML → PDF generator and merger.
"""

import logging
import math
import os
import re
import tempfile
import warnings
from datetime import date

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject
from weasyprint import HTML, CSS

# Suppress pypdf annotation merge warnings (harmless cross-doc link issues)
logging.getLogger('pypdf').setLevel(logging.ERROR)


_PRINT_CSS = CSS(string="""
@page {
    size: A4 portrait;
    margin: 15mm 12mm 15mm 12mm;
}

/* Box model: padding/border do not add to declared width */
*, *::before, *::after {
    box-sizing: border-box;
}

body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9pt;
    max-width: 100%;
    overflow-x: hidden;
    margin: 0;
    padding: 0;
}

/* Tables must never exceed the page content area */
table {
    max-width: 100% !important;
    word-break: break-word;
}

/* The XSL sets <col width="1%"> for variant code columns and <col width="96%">
   for description columns. 1% of a full-width table is ~7px — far too narrow
   to render even short codes like "0524". Override all col widths and let the
   browser use automatic layout so columns size to their content instead. */
col {
    width: auto !important;
}

/* Images: scale down to fit, never overflow */
img {
    max-width: 100% !important;
    height: auto !important;
}

/* Prevent td padding from pushing content outside the page */
td, th {
    max-width: 100%;
    overflow-wrap: break-word;
    word-break: break-word;
}

/* The XSL wraps all content in a table with padding-left:8mm.
   Collapse that to a small indent so images and text use the full width. */
table[border="0"] > tbody > tr > td[style*="padding-left"] {
    padding-left: 2mm !important;
}

/* Also target the outer table that holds the numbered step column + content column:
   collapse the left (number) column so the content column gets more room. */
td[style*="padding-left:8mm"] {
    padding-left: 2mm !important;
}

/* Hide interactive/navigation elements irrelevant in print */
input, button, script, .noPrint { display: none !important; }
""")


# ── TOC layout constants (must match _TOC_CSS exactly) ───────────────────────
# All values in PDF points (1 pt = 1/72 inch).  A4 = 595.28 × 841.89 pt.
_PAGE_W_PT   = 595.28
_PAGE_H_PT   = 841.89
_MARGIN_T_PT = 42.52   # 15 mm
_MARGIN_B_PT = 42.52   # 15 mm
_MARGIN_L_PT = 34.02   # 12 mm
_MARGIN_R_PT = 34.02   # 12 mm
_CONTENT_H_PT = _PAGE_H_PT - _MARGIN_T_PT - _MARGIN_B_PT   # 756.85

_TOC_LINE_H_PT = 14.0   # height of each TOC entry row (must match .toc-entry height in CSS)
_TOC_HEAD_H_PT = 30.0   # height of the heading block on page 1 (must match .toc-heading CSS)

_TOC_ENTRIES_PAGE1 = int((_CONTENT_H_PT - _TOC_HEAD_H_PT) / _TOC_LINE_H_PT)   # 51
_TOC_ENTRIES_PAGEN = int(_CONTENT_H_PT / _TOC_LINE_H_PT)                        # 54


def _toc_page_count(n_entries: int) -> int:
    """Return the number of TOC pages needed for n_entries procedures."""
    if n_entries == 0:
        return 0
    if n_entries <= _TOC_ENTRIES_PAGE1:
        return 1
    remaining = n_entries - _TOC_ENTRIES_PAGE1
    return 1 + math.ceil(remaining / _TOC_ENTRIES_PAGEN)


def _toc_entry_rect(global_idx: int) -> tuple[float, float, float, float, int]:
    """Return the PDF-coordinate bounding box and TOC-local page index for entry i.

    Returns (x1, y1, x2, y2, toc_page_idx) where:
      - (x1, y1) is the bottom-left corner (PDF origin = page bottom-left)
      - (x2, y2) is the top-right corner
      - toc_page_idx is 0-based index within the TOC pages
    """
    if global_idx < _TOC_ENTRIES_PAGE1:
        toc_page = 0
        local_idx = global_idx
        # Entry sits below the heading block
        y_top_from_top = _MARGIN_T_PT + _TOC_HEAD_H_PT + local_idx * _TOC_LINE_H_PT
    else:
        remaining   = global_idx - _TOC_ENTRIES_PAGE1
        toc_page    = 1 + remaining // _TOC_ENTRIES_PAGEN
        local_idx   = remaining % _TOC_ENTRIES_PAGEN
        y_top_from_top = _MARGIN_T_PT + local_idx * _TOC_LINE_H_PT

    # Convert CSS top-of-entry → PDF coordinates (y=0 at bottom)
    y2 = _PAGE_H_PT - y_top_from_top           # top edge of entry box
    y1 = y2 - _TOC_LINE_H_PT                   # bottom edge of entry box
    x1 = _MARGIN_L_PT
    x2 = _PAGE_W_PT - _MARGIN_R_PT
    return x1, y1, x2, y2, toc_page


def make_toc_html(entries: list[tuple[str, int, bool]]) -> str:
    """Generate TOC page HTML.

    Args:
        entries: list of (title, display_page_number, is_main) where is_main=True
                 for main POS procedures (rendered bold, no indent) and False for
                 supplementary documents like AD (tightening torques), SW (special
                 tools), BS (lubricants) etc. (rendered muted and indented).
                 Page numbers are 1-indexed as they appear in the merged PDF.
    """
    rows = []
    for name, page_num, is_main in entries:
        safe = name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        cls = 'toc-main' if is_main else 'toc-sub'
        rows.append(
            f'<div class="toc-entry {cls}">'
            f'<span class="toc-title">{safe}</span>'
            f'<span class="toc-page">{page_num}</span>'
            f'</div>'
        )
    rows_html = '\n'.join(rows)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
@page {{ size: A4 portrait; margin: 15mm 12mm 15mm 12mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 9pt; }}
.toc-heading {{
    height: {_TOC_HEAD_H_PT}pt;
    line-height: {_TOC_HEAD_H_PT}pt;
    font-size: 16pt;
    font-weight: bold;
    color: #003399;
    overflow: hidden;
}}
.toc-entry {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: {_TOC_LINE_H_PT}pt;
    line-height: {_TOC_LINE_H_PT}pt;
    overflow: hidden;
}}
/* Main procedure: bold, full width, separator line */
.toc-main {{
    font-weight: bold;
    border-bottom: 0.3pt solid #c0c0c0;
}}
/* Sub-document (tightening torques, special tools, etc.):
   indented, smaller, muted colour — no border so it visually
   attaches to the procedure above it */
.toc-sub {{
    padding-left: 14pt;
    font-size: 8pt;
    color: #555;
}}
.toc-title {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    padding-right: 8pt;
}}
.toc-page {{
    flex-shrink: 0;
    text-align: right;
    min-width: 24pt;
}}
</style>
</head>
<body>
<div class="toc-heading">Table of Contents</div>
{rows_html}
</body>
</html>"""


def build_final_pdf(
    title_pdf: str,
    proc_pairs: list[tuple[str, str, bool]],
    out_path: str,
    linked_pdfs: list[tuple[str, str]] | None = None,
    slug_to_page: dict[str, int] | None = None,
) -> None:
    """Build the final merged PDF with title page, TOC, procedures, bookmarks and links.

    Args:
        title_pdf:    Path to the already-rendered title-page PDF.
        proc_pairs:   Ordered list of (title, pdf_path, is_main) where is_main=True
                      for main POS procedures and False for sub-documents.
        out_path:     Destination path for the merged PDF.
        linked_pdfs:  Optional list of (slug, pdf_path) for BFS-rendered linked
                      documents (long PDF only).  These are appended after the
                      main procedures without TOC entries.
        slug_to_page: Optional pre-computed mapping of SLUG → 0-based page index
                      in the final merged PDF, used by patch_goto_links().
                      Computed here if not supplied (only needed for long PDF).
    """
    from pypdf.annotations import Link

    proc_titles  = [t for t, _, _is in proc_pairs]
    proc_pdfs    = [p for _, p, _is in proc_pairs]
    proc_is_main = [m for _, _, m in proc_pairs]

    # ── 1. Count pages in each procedure PDF ─────────────────────────────────
    page_counts: list[int] = []
    for pdf_path in proc_pdfs:
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            page_counts.append(len(PdfReader(pdf_path).pages))
        else:
            page_counts.append(0)

    n_procs = len(proc_pdfs)

    # Cumulative page offsets within the procedure block (0-based within procs)
    cumulative = [0] * n_procs
    for i in range(1, n_procs):
        cumulative[i] = cumulative[i - 1] + page_counts[i - 1]

    # ── 2. Compute TOC pages (may need two passes if count differs) ──────────
    n_title_pages = 1 if (os.path.exists(title_pdf) and os.path.getsize(title_pdf) > 0) else 0
    n_toc_pages   = _toc_page_count(n_procs)

    def _proc_page_offsets(n_toc: int) -> list[int]:
        """0-based page index in the final PDF for the first page of each procedure."""
        return [n_title_pages + n_toc + cumulative[i] for i in range(n_procs)]

    def _display_pages(offsets: list[int]) -> list[int]:
        return [off + 1 for off in offsets]  # 1-indexed for display

    offsets      = _proc_page_offsets(n_toc_pages)
    display_nums = _display_pages(offsets)

    # ── 3. Render TOC to a temporary PDF ─────────────────────────────────────
    toc_tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    toc_tmp.close()
    toc_pdf_path = toc_tmp.name

    def _render_toc(titles: list[str], pages: list[int], is_mains: list[bool], dest: str) -> int:
        toc_html_str = make_toc_html(list(zip(titles, pages, is_mains)))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            HTML(string=toc_html_str).write_pdf(dest, stylesheets=[])
        return len(PdfReader(dest).pages)

    actual_toc_pages = _render_toc(proc_titles, display_nums, proc_is_main, toc_pdf_path)

    # If WeasyPrint paginated differently than predicted, recompute and re-render once
    if actual_toc_pages != n_toc_pages:
        n_toc_pages  = actual_toc_pages
        offsets      = _proc_page_offsets(n_toc_pages)
        display_nums = _display_pages(offsets)
        actual_toc_pages = _render_toc(proc_titles, display_nums, proc_is_main, toc_pdf_path)

    # ── 4. Merge: title + TOC + procedures (+ linked docs for long PDF) ──────
    writer = PdfWriter()

    if n_title_pages:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            writer.append(title_pdf)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        writer.append(toc_pdf_path)

    for pdf_path in proc_pdfs:
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                writer.append(pdf_path)

    # Append linked documents (long PDF only) — no TOC entries, just reachable pages
    if linked_pdfs:
        for _slug, pdf_path in linked_pdfs:
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    writer.append(pdf_path)

    # ── 5. Sidebar bookmarks (outline) ───────────────────────────────────────
    # Main procedures get top-level bookmarks; sub-docs (AD, SW, BS…) are
    # nested under the most recent main entry.
    current_parent = None
    for title, page_off, is_main in zip(proc_titles, offsets, proc_is_main):
        if is_main:
            current_parent = writer.add_outline_item(title, page_off)
        else:
            writer.add_outline_item(title, page_off, parent=current_parent)

    # ── 6. Clickable link annotations on TOC pages ───────────────────────────
    toc_first_page_in_pdf = n_title_pages   # 0-based index in final PDF

    for i, (title, proc_page_off) in enumerate(zip(proc_titles, offsets)):
        x1, y1, x2, y2, toc_local = _toc_entry_rect(i)
        abs_page = toc_first_page_in_pdf + toc_local   # 0-based in final PDF

        annotation = Link(
            rect=[x1, y1, x2, y2],
            border=[0, 0, 0],
            target_page_index=proc_page_off,
        )
        writer.add_annotation(page_number=abs_page, annotation=annotation)

    # ── 7. Patch bmwlink:// sentinels → GoTo page actions (long PDF only) ────
    if linked_pdfs:
        # Build slug → 0-based page index map from proc_pdfs + linked_pdfs
        if slug_to_page is None:
            # _slug_from_pdf: strips the 'BMW-MOTORRAD_<SUBDIR>_' directory
            # prefix that render_worker adds when building the safe filename,
            # leaving just the basename portion that sentinel_pdf_hrefs uses.
            _SUBDIR_PAT = re.compile(
                r'BMW-MOTORRAD_(?:POS|AUS|EIN|TEILAUS|TEILEIN|SPEZW|WSATZ|'
                r'PRUE|EINST|FUELL|BS|WAU|REPSCH|GRUPPE-BS|REIN|INB|MES|'
                r'ZERL|ZUBAU|LEER|TD|SW|AD)_', re.IGNORECASE)

            def _slug_from_pdf(pdf_path: str) -> str:
                stem = re.sub(r'\.pdf$', '', os.path.basename(pdf_path),
                              flags=re.IGNORECASE)
                m = _SUBDIR_PAT.search(stem)
                raw = stem[m.end():] if m else stem
                return re.sub(r'\.XML$', '', raw, flags=re.IGNORECASE).upper()

            slug_to_page = {}
            for (_, pdf_path, _is), page_off in zip(proc_pairs, offsets):
                slug_to_page[_slug_from_pdf(pdf_path)] = page_off

            # Linked PDFs: append sequentially after all proc pages
            total_proc_pages = sum(page_counts)
            link_page_cursor = n_title_pages + actual_toc_pages + total_proc_pages
            for slug, pdf_path in linked_pdfs:
                if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                    slug_to_page[slug.upper()] = link_page_cursor
                    link_page_cursor += len(PdfReader(pdf_path).pages)

        n_patched = patch_goto_links(writer, slug_to_page)
        if n_patched:
            import logging as _log
            _log.getLogger(__name__).debug('Patched %d GoTo link annotations', n_patched)

    # ── 8. Write output ───────────────────────────────────────────────────────
    with open(out_path, 'wb') as f:
        writer.write(f)
    writer.close()

    try:
        os.unlink(toc_pdf_path)
    except OSError:
        pass


def patch_goto_links(writer: PdfWriter, slug_to_page: dict[str, int]) -> int:
    """Replace bmwlink://SLUG URI annotations with GoTo page-number actions.

    WeasyPrint turns href="bmwlink://SLUG" into PDF URI actions.  After merging,
    this function iterates every page's link annotations and replaces any
    bmwlink:// URI action with a GoTo action that jumps to the correct page.

    Args:
        writer:        The PdfWriter containing the fully merged document.
        slug_to_page:  Mapping of SLUG (upper-case filename without .XML) →
                       0-based page index in the merged PDF.

    Returns the number of annotations patched.
    """
    patched = 0
    for page_num, page in enumerate(writer.pages):
        annots = page.get('/Annots')
        if not annots:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            if annot.get('/Subtype') != '/Link':
                continue
            action = annot.get('/A')
            if action is None:
                continue
            try:
                action_obj = action.get_object()
            except Exception:
                action_obj = action
            if action_obj.get('/S') != '/URI':
                continue
            uri = str(action_obj.get('/URI', ''))
            if not uri.startswith('bmwlink://'):
                continue
            slug = uri[len('bmwlink://'):].upper()
            target_page = slug_to_page.get(slug)
            if target_page is None:
                continue
            # Replace URI action with a GoTo action pointing to target page
            target_page_ref = writer.pages[target_page].indirect_reference
            new_dest = ArrayObject([target_page_ref, NameObject('/Fit')])
            new_action = DictionaryObject({
                NameObject('/S'): NameObject('/GoTo'),
                NameObject('/D'): new_dest,
            })
            annot[NameObject('/A')] = new_action
            patched += 1
    return patched


def html_to_pdf(html_str: str, out_path: str, base_url: str | None = None) -> bool:
    """Render a single HTML page to PDF. Returns True on success."""
    try:
        doc = HTML(string=html_str, base_url=base_url)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            doc.write_pdf(out_path, stylesheets=[_PRINT_CSS])
        return True
    except Exception as e:
        print(f'  WARNING: PDF render failed for {out_path}: {e}')
        return False


def merge_pdfs(pdf_paths: list[str], out_path: str) -> None:
    """Merge a list of PDF files into a single output PDF."""
    writer = PdfWriter()
    for p in pdf_paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                writer.append(p)
    with open(out_path, 'wb') as f:
        writer.write(f)
    writer.close()


def make_title_page_html(model_name: str, model_code: str, image_path: str | None) -> str:
    today = date.today().strftime('%d %B %Y')

    if image_path and os.path.exists(image_path):
        img_tag = f'<img src="file://{image_path}" style="max-width:120mm; max-height:80mm; margin: 8mm 0;"/>'
    else:
        img_tag = ''

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
  @page {{ size: A4 portrait; margin: 20mm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; margin: 0; padding: 20mm 0 0 0; }}
  h1 {{ font-size: 26pt; margin: 0 0 2mm 0; color: #003399; }}
  h2 {{ font-size: 14pt; color: #333; font-weight: normal; margin: 0 0 2mm 0; }}
  p  {{ font-size: 9pt; color: #888; margin-top: 15mm; }}
</style>
</head><body>
  <h1>BMW {model_name}</h1>
  <h2>Repair Manual &mdash; Model {model_code}</h2>
  {img_tag}
  <p>Generated {today} from BMW KSD repair manual application data.</p>
</body></html>"""
