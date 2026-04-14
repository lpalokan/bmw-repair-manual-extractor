"""
HTML → PDF generator and merger.
"""

import logging
import os
import warnings
from datetime import date

from pypdf import PdfWriter
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
