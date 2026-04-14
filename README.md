# BMW Repair Manual PDF Extractor

Extracts repair procedures from the BMW KSD (2013) Windows-only CD application and produces a printable PDF or a browsable web interface — on macOS, Linux, or any modern system.

The original application stores all content in a proprietary binary database (`XML_01.Dat`, ~405 MB). This tool decodes the database, extracts repair procedures for any model, renders them via the original XSLT stylesheet, and outputs a merged PDF with a cover page and table of contents, a self-contained HTML directory, or an on-demand local web server.

## Background

The BMW KSD repair manual application is a Windows-only CD from 2013 (RSD.EXE, DYNAPDF.DLL). All repair content is stored inside a single file using the **GRIPS GDB V1.00U** format — an undocumented proprietary database. The file is actually a SQLite 3 database XOR-encoded byte-by-byte with `0xAA`, with the standard SQLite header replaced by a GRIPS magic string. Records inside are delta-encoded and compressed with zlib.

This project reverse-engineered the format entirely in Python, with no dependency on the original application.

## Requirements

- Python 3.12+
- The original BMW KSD CD contents (not included) placed in `input/BMW Repair manual application/`

Install Python dependencies:

```bash
pip install -r source/requirements.txt
```

WeasyPrint also requires system libraries. On macOS:

```bash
brew install pango libffi
```

On Ubuntu/Debian:

```bash
apt install libpango-1.0-0 libpangoft2-1.0-0
```

## Input Data Layout

Place the CD contents so the directory structure looks like this:

```
input/
  BMW Repair manual application/
    DATAS/
      BMW-Motorrad/
        XML_01.Dat          ← main database (405 MB)
        XSL/
          RSD.XSL           ← original XSLT stylesheet
        BILD/               ← 73,000+ procedure images (JPG)
        DYN-W-PLAN/         ← model-to-procedure index files
      MODELLBILD/
        Modellbild.dat      ← model cover images index
        *.jpg               ← model cover photos
```

## Usage

**Step 1 — decode the database (one time only):**

```bash
python source/main.py decode-db
```

This decodes `XML_01.Dat` into a plain SQLite database at `/tmp/grips_decoded.db` (~405 MB). Only needed once.

**Step 2 — list available models:**

```bash
python source/main.py models
```

Outputs all 98 models with their 4-digit code and human-readable name, e.g.:

```
Code     Model Name                               Image
----------------------------------------------------------------------
0458     HP2 Sport                                K29_HP_RKG021a.jpg
0507     S 1000 RR                                K46_S1000RR_01.jpg
...
```

**Step 3 — pick an output format:**

**PDF** — a single merged document with table of contents:
```bash
python source/main.py extract --model 0458
python source/main.py extract --model 0507 --out ~/Desktop/
python source/main.py extract --model 0458 --limit 10   # quick test
```

The PDF contains a title page, a multi-page table of contents with clickable hyperlinks to each procedure, all procedures in numerical order, sidebar bookmarks for quick navigation, and all diagrams and images.

For the HP2 Sport (model 0458): ~2,050 pages, ~125 MB.

**HTML** — a self-contained directory, browsable offline:
```bash
python source/main.py export-html --model 0458
python source/main.py export-html --model 0458 --out ~/Desktop/HP2_Sport/
```

Produces `index.html` + `procedures/` + `images/` — open in any browser without the original data directory. The index groups each main procedure with its associated sub-documents (tightening torques, special tools, lubricants, etc.) as pill links, matching the layout of the web UI.

**Web UI** — browse all models and render procedures on demand:
```bash
python source/main.py serve
# → http://127.0.0.1:5000/
```

Shows all 98 models as a card grid with cover photos. Click a model to see its procedure list; click a procedure to render it instantly. No pre-rendering required.

**Other commands:**

```bash
# List all DB record paths for a model
python source/main.py list-paths --model 0458 --subdir POS
```

## Procedure Hierarchy

Each BMW repair procedure consists of a main document and several associated sub-documents:

| Type | Content |
|------|---------|
| POS | Main repair procedure (steps, images, instructions) |
| AD | Tightening torques |
| SW | Special tools required |
| BS | Lubricants and fluids |
| TD | Technical data |
| WAU | Workshop equipment |
| REPSCH | Repair scheme / diagram |

All three output formats (PDF table of contents, HTML export index, web UI model page) present the main procedure in bold, with its sub-documents shown as indented or pill-style links beneath it.

## Source Layout

```
source/
  main.py            # CLI entry point: decode-db, models, extract, export-html, serve, list-paths
  config.py          # paths to input data, decoded DB, output directory
  gdb_reader.py      # GRIPS GDB decoder: XOR + delta encoding + zlib decompression
  pak_reader.py      # PAK/+PAK format decompressor for DYN-W-PLAN index files
  model_registry.py  # discovers all models, names, and cover images from the DB
  render.py          # XML → HTML via lxml XSLT + image path resolution
  pdf_builder.py     # HTML → PDF (WeasyPrint) + TOC + merge (pypdf) + bookmarks + links
  render_worker.py   # subprocess worker for crash-safe batch rendering
  html_exporter.py   # exports procedures as a self-contained HTML directory
  server.py          # Flask web server with on-demand procedure rendering
  templates/
    home.html        # model grid home page
    model.html       # procedure list page
  requirements.txt
```

## Technical Notes

**GRIPS GDB format** — `XML_01.Dat` is SQLite 3 XOR-encoded with `0xAA` (first 16 bytes replaced with GRIPS magic). Records inside are delta-encoded blobs with either a `PAK` (offset 7) or `+PAK` (offset 8) header before the zlib stream. Decompressed records are UTF-16 LE XML with a `<GRIPS-OUT>` root element.

**Rendering** — the original `RSD.XSL` stylesheet (XSLT 1.0) is applied server-side via `lxml`. Image paths are resolved to absolute `file://` URLs pointing into the `BILD/` directory. CSS overrides correct the A4 layout (the XSL's `padding-left: 8mm` on a `width: 100%` table silently overflows the page margin in print). Equipment variant tables with `<col width="1%">` are corrected with `col { width: auto !important; }`.

**Procedure titles** — extracted directly from `<EMPH BOLD="1">` in the raw XML blob without running the XSLT transform, giving English titles like "11 11 120 Replacing all cylinders" used in the PDF table of contents, web UI, and HTML export index.

**PDF table of contents** — generated with fixed 14pt line height per entry, with main procedures in bold and sub-documents indented and muted. pypdf `Link` annotations with `target_page_index` make each TOC entry a clickable hyperlink. `add_outline_item()` with hierarchical nesting populates the PDF sidebar bookmarks panel.

**Crash recovery** — WeasyPrint triggers a Pango/fontconfig SIGSEGV on macOS ARM64 when the GC finalizes font objects while Pango worker threads are still running. The fix: render in subprocess batches (40 records per process). If a subprocess crashes mid-batch, the main process detects which paths were not confirmed and retries them individually. All retries succeed; zero records are lost.

**Icon images in the web UI and HTML export** — `RSD.XSL` hardcodes UI icon paths as bare relative strings inside JavaScript (e.g. `Change_Icon(escape('BMW-Motorrad/imgs/icon/open.gif'))`). These are rewritten to absolute `/image/` routes (serve mode) or `../images/` relative paths (HTML export) so icons continue to work after JavaScript fires.

**Non-breaking spaces** — the BMW XML uses U+00A0 (non-breaking space) extensively for spacing in headings and table cells. The XSLT preserves them as raw `\xa0` bytes; without normalisation these appear as `Â` in browsers that miss the UTF-8 charset hint. All `\xa0` characters are replaced with `&nbsp;` after rendering.

**Missing images** — a small number of cross-model comparison thumbnails (`*_MODELLE_00_*_preview.jpg`) are referenced in certain procedures but not present in the BILD directory of this CD version. These affect 5 of 236 procedures for model 0458 and are a data gap in the source, not a code issue.

## Output Example

A full extraction of the BMW HP2 Sport (model 0458) produces a ~2,050-page PDF containing:
- Cover page with model name, code, and photo
- Multi-page table of contents with clickable links and sidebar bookmarks
- 236 main repair procedures in numerical order, each with associated tightening torques, special tools, and lubricant sub-documents
- Diagrams, torque specs, step-by-step instructions with images

## License

This tool is for personal use with legally owned BMW KSD CD content. The BMW KSD application, its data files, stylesheets, and images remain the property of BMW AG.
