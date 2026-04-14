#!/usr/bin/env python3
"""
BMW Repair Manual PDF extractor.

Commands:
  decode-db    One-time: XOR-decode XML_01.Dat → SQLite (required before all other commands)
  models       List all available models with codes and names
  extract      Extract all repair procedures for a model and build a PDF
  list-paths   List DB record paths for a model/subdirectory

Examples:
  python main.py decode-db
  python main.py models
  python main.py extract --model 0458
  python main.py extract --model 0507 --out ./output/
  python main.py list-paths --model 0458 --subdir POS
"""

import os
import re
import subprocess
import sys
import tempfile

import click

sys.path.insert(0, os.path.dirname(__file__))

import config
from gdb_reader import GdbReader, decode_db
from model_registry import list_models, get_model_info
from pdf_builder import build_final_pdf, make_title_page_html, html_to_pdf
from html_exporter import export_model_html

# Batch size for subprocess rendering: keeps each worker process small enough
# to avoid the WeasyPrint/Pango/fontconfig crash on macOS ARM64.
_BATCH_SIZE = 40


@click.group()
def cli():
    """BMW Repair Manual PDF extractor.\n\nRun 'decode-db' once first, then use 'models' to list available models."""
    pass


@cli.command('decode-db')
def cmd_decode_db():
    """One-time setup: XOR-decode XML_01.Dat into a plain SQLite database.

    This must be run once before any other command. The decoded database is
    stored at /tmp/grips_decoded.db (about 405 MB).
    """
    decode_db(config.XML_DAT, config.DECODED_DB)


@cli.command('models')
def cmd_models():
    """List all models available in the database with their codes and names."""
    _ensure_db()
    click.echo('Loading model list from database...')
    models = list_models(config.DECODED_DB)
    click.echo(f'\n{"Code":<8} {"Model Name":<40} {"Image"}')
    click.echo('-' * 70)
    for m in models:
        img = os.path.basename(m.image_path) if m.image_path else '(no image)'
        click.echo(f'{m.code:<8} {m.name:<40} {img}')
    click.echo(f'\n{len(models)} models found.')
    click.echo('\nTo extract a model, run:')
    click.echo('  python main.py extract --model <CODE>')


@cli.command('list-paths')
@click.option('--model', required=True, help='4-digit model code (e.g. 0458). Run "models" to see all codes.')
@click.option('--subdir', default='POS', show_default=True, help='DB subdirectory to list (POS, AD, PRUE, etc.)')
def cmd_list_paths(model, subdir):
    """List all DB record paths for a model in a given subdirectory."""
    _ensure_db()
    reader = GdbReader(config.DECODED_DB)
    paths = reader.list_paths(model, subdir)
    for p in paths:
        click.echo(p)
    click.echo(f'\n{len(paths)} paths in {subdir} for model {model}', err=True)
    reader.close()


@cli.command('extract')
@click.option('--model', required=True, help='4-digit model code (e.g. 0458). Run "models" to see all codes.')
@click.option('--out', default=None, help=f'Output directory (default: {config.OUTPUT_DIR})')
@click.option('--subdir', default=config.DEFAULT_SUBDIR, show_default=True,
              help='DB subdirectory to render (POS = main repair steps)')
@click.option('--limit', default=0, help='Max procedures to render (0 = all; use small values for testing)')
def cmd_extract(model, out, subdir, limit):
    """Extract all repair procedures for a model and produce a merged PDF.

    The output PDF contains a title page followed by all procedure pages,
    one procedure per page, in alphabetical order by procedure name.

    \b
    Examples:
      python main.py extract --model 0458
      python main.py extract --model 0507 --out ~/Desktop/
      python main.py extract --model 0458 --limit 10   # quick test
    """
    _ensure_db()
    out_dir = out or config.OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    model_info = get_model_info(config.DECODED_DB, model)
    click.echo(f'Model {model}: {model_info.name}')
    if model_info.image_path:
        click.echo(f'Cover image: {os.path.basename(model_info.image_path)}')

    reader = GdbReader(config.DECODED_DB)
    paths = reader.list_paths(model, subdir)
    reader.close()
    if limit:
        paths = paths[:limit]

    click.echo(f'Found {len(paths)} records in {subdir}')

    data_parent = os.path.dirname(config.DATA_DIR)
    tmp_dir = tempfile.mkdtemp(prefix=f'bmw_{model}_')

    # Title page (rendered in-process; it's just one page, safe to do directly)
    title_html = make_title_page_html(model_info.name, model, model_info.image_path)
    title_pdf = os.path.join(tmp_dir, '000_title.pdf')
    html_to_pdf(title_html, title_pdf)

    # Render procedures in subprocess batches to avoid WeasyPrint/Pango crash
    # on macOS ARM64 (FcConfigDestroy SIGSEGV when GC finalizes font objects
    # while Pango font worker threads are still running).
    worker = os.path.join(os.path.dirname(__file__), 'render_worker.py')
    fail_count = 0
    total = len(paths)

    # all_proc_pairs: ordered list of (db_path, pdf_path) for rendered procedures
    all_proc_pairs: list[tuple[str, str]] = []

    for batch_start in range(0, total, _BATCH_SIZE):
        batch = paths[batch_start:batch_start + _BATCH_SIZE]
        batch_end = batch_start + len(batch)
        click.echo(f'  Rendering [{batch_start+1}–{batch_end}/{total}] in subprocess...')

        confirmed, new_pairs = _run_worker_batch(worker, batch, tmp_dir)
        all_proc_pairs.extend(new_pairs)

        if len(confirmed) < len(batch):
            missing = [p for p in batch if p not in confirmed]
            click.echo(f'  Worker crashed (SIGSEGV); retrying {len(missing)} path(s) one-by-one...', err=True)
            for path in missing:
                _, retry_pairs = _run_worker_batch(worker, [path], tmp_dir)
                all_proc_pairs.extend(retry_pairs)
                if retry_pairs:
                    click.echo(f'    RETRY OK: {os.path.basename(path)}')
                else:
                    fail_count += 1
                    click.echo(f'    RETRY FAIL: {path}', err=True)

    # ── Look up English titles for TOC ────────────────────────────────────────
    click.echo(f'\nLooking up procedure titles for table of contents...')
    reader = GdbReader(config.DECODED_DB)
    toc_proc_pairs: list[tuple[str, str, bool]] = []  # (title, pdf_path, is_main)
    for db_path, pdf_path in all_proc_pairs:
        basename = db_path.replace('\\', '/').rsplit('/', 1)[-1].upper()
        is_main  = basename.endswith('_POS.XML')

        xml = reader.get_xml_exact(db_path) or ''
        m = re.search(r'<EMPH[^>]*BOLD="1"[^>]*>([^<]+)', xml)
        if m:
            title = m.group(1).strip()
            if not is_main:
                # Strip leading "NN NN NNN " procedure-number prefix from sub-docs
                # so "11 11 120 Tightening Torques" → "Tightening Torques"
                title = re.sub(r'^\d[\d\s]{3,10}\s+', '', title).strip()
                # Strip trailing " NNNN - ModelName" model suffix
                # so "Tightening torques 0458 - HP2 Sport" → "Tightening torques"
                title = re.sub(r'\s+\d{4}\s*[-\u2013].*$', '', title).strip()
        else:
            # Fallback: derive from path
            nm = re.search(r'\d{4}_\d{2}_\d+_(.+?)_(\w+)\.XML$', basename, re.IGNORECASE)
            if nm:
                title = nm.group(1).replace('_', ' ').title()
            else:
                title = re.sub(r'\.XML$', '', basename, flags=re.IGNORECASE)

        toc_proc_pairs.append((title, pdf_path, is_main))
    reader.close()

    # ── Build final PDF with TOC, bookmarks and clickable links ───────────────
    safe_name = model_info.name.replace(' ', '_').replace('/', '-')
    merged_path = os.path.join(out_dir, f'BMW_{safe_name}_{model}_Repair_Manual.pdf')
    click.echo(f'Building final PDF with table of contents → {merged_path}')
    build_final_pdf(title_pdf, toc_proc_pairs, merged_path)

    import pypdf
    page_count = len(pypdf.PdfReader(merged_path).pages)
    size_mb = os.path.getsize(merged_path) / (1024 * 1024)
    click.echo(f'Done. {page_count} pages, {size_mb:.1f} MB')
    if fail_count:
        click.echo(f'({fail_count} procedures failed to render)', err=True)


@cli.command('export-html')
@click.option('--model', required=True, help='4-digit model code (e.g. 0458). Run "models" to see all codes.')
@click.option('--out', default=None, help=f'Output directory (default: output/<model_name>_<code>/)')
@click.option('--subdir', default=config.DEFAULT_SUBDIR, show_default=True,
              help='DB subdirectory to render (POS = main repair steps)')
@click.option('--limit', default=0, help='Max procedures to export (0 = all; use small values for testing)')
def cmd_export_html(model, out, subdir, limit):
    """Export all repair procedures for a model as a self-contained HTML directory.

    The output directory contains an index.html listing all procedures, plus
    a procedures/ folder with one HTML file per procedure, and an images/
    folder with all referenced images copied locally.

    \b
    Examples:
      python main.py export-html --model 0458
      python main.py export-html --model 0458 --out ~/Desktop/HP2_Sport/
      python main.py export-html --model 0458 --limit 5   # quick test
    """
    _ensure_db()

    model_info = get_model_info(config.DECODED_DB, model)
    click.echo(f'Model {model}: {model_info.name}')

    reader = GdbReader(config.DECODED_DB)
    paths = reader.list_paths(model, subdir)
    reader.close()
    if limit:
        paths = paths[:limit]
    click.echo(f'Found {len(paths)} records in {subdir}')

    if out is None:
        safe_name = model_info.name.replace(' ', '_').replace('/', '-')
        out = os.path.join(config.OUTPUT_DIR, f'BMW_{safe_name}_{model}')
    os.makedirs(out, exist_ok=True)

    data_parent = os.path.dirname(config.DATA_DIR)
    total = len(paths)

    def _progress(current, total_, slug):
        click.echo(f'  [{current}/{total_}] {slug}')

    export_model_html(
        model_info=model_info,
        paths=paths,
        out_dir=out,
        xsl_path=config.XSL_PATH,
        data_parent=data_parent,
        on_progress=_progress,
    )

    click.echo(f'\nDone. {total} procedures → {out}')
    click.echo(f'Open: file://{os.path.join(out, "index.html")}')


@cli.command('serve')
@click.option('--port', default=5000, show_default=True, help='Port to listen on')
@click.option('--host', default='127.0.0.1', show_default=True, help='Host to bind')
@click.option('--debug', is_flag=True, default=False, help='Enable Flask debug mode')
def cmd_serve(port, host, debug):
    """Start a local web server for browsing all models and repair procedures.

    Renders procedures on demand — no pre-rendering required.

    \b
    Examples:
      python main.py serve
      python main.py serve --port 8080
    """
    _ensure_db()
    from server import create_app
    app = create_app()
    click.echo(f'Starting BMW Repair Manual server at http://{host}:{port}/')
    click.echo('Press Ctrl+C to stop.')
    app.run(host=host, port=port, debug=debug)


def _run_worker_batch(
    worker: str, batch: list[str], tmp_dir: str
) -> tuple[set[str], list[tuple[str, str]]]:
    """Run render_worker.py for a batch of DB paths.

    Returns (confirmed_paths, proc_pairs) where:
      - confirmed_paths: set of DB paths that produced an OK or SKIP response
        (i.e. were fully processed before any crash)
      - proc_pairs: ordered list of (db_path, pdf_path) for successfully
        rendered procedures (OK status only, in output order)
    """
    proc = subprocess.Popen(
        [sys.executable, worker,
         config.DECODED_DB, config.XSL_PATH,
         os.path.dirname(config.DATA_DIR), tmp_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdin_data = '\n'.join(batch) + '\n'
    stdout, stderr = proc.communicate(input=stdin_data)

    if stderr.strip():
        for line in stderr.strip().split('\n'):
            click.echo(f'    WARN: {line}', err=True)

    confirmed: set[str] = set()
    proc_pairs: list[tuple[str, str]] = []   # (db_path, pdf_path)

    for line in stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t', 2)
        status   = parts[0] if parts else '?'
        pdf_path = parts[1] if len(parts) > 1 else ''
        db_path  = parts[2] if len(parts) > 2 else ''
        if status == 'OK':
            confirmed.add(db_path)
            proc_pairs.append((db_path, pdf_path))
            if len(batch) > 1:
                click.echo(f'    OK: {os.path.basename(db_path)}')
        elif status == 'SKIP':
            confirmed.add(db_path)
            click.echo(f'    SKIP: {db_path}', err=True)
        else:
            click.echo(f'    FAIL: {db_path}', err=True)

    return confirmed, proc_pairs


def _ensure_db():
    if not os.path.exists(config.DECODED_DB):
        click.echo(f'ERROR: Decoded database not found at {config.DECODED_DB}')
        click.echo('Run first:  python main.py decode-db')
        sys.exit(1)


if __name__ == '__main__':
    cli()
