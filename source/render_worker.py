#!/usr/bin/env python3
"""
Subprocess worker: render a batch of DB paths to individual PDF files.
Called by main.py via subprocess; reads paths from stdin (one per line),
writes a PDF for each to the given output directory.

Usage:
    python render_worker.py <db_path> <xsl_path> <data_parent> <out_dir>
    (paths piped to stdin, one per line)
"""

import logging
import os
import sys
import warnings

logging.getLogger('pypdf').setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(__file__))

import config
from gdb_reader import GdbReader
from render import xml_to_html, strip_pdf_hrefs
from pdf_builder import html_to_pdf


def main():
    if len(sys.argv) != 5:
        print('Usage: render_worker.py <db_path> <xsl_path> <data_parent> <out_dir>', file=sys.stderr)
        sys.exit(1)

    db_path, xsl_path, data_parent, out_dir = sys.argv[1:]
    base_url = 'file://' + data_parent + '/'

    reader = GdbReader(db_path)
    warnings.filterwarnings('ignore')

    for line in sys.stdin:
        db_record_path = line.rstrip('\n')
        if not db_record_path:
            continue
        xml = reader.get_xml_exact(db_record_path)
        safe = db_record_path.replace('\\', '_').replace('/', '_').strip('_')
        out_pdf = os.path.join(out_dir, safe + '.pdf')

        if not xml:
            print(f'SKIP\t{db_record_path}', flush=True)
            continue

        html = xml_to_html(xml, xsl_path, data_parent)
        html = strip_pdf_hrefs(html)
        ok = html_to_pdf(html, out_pdf, base_url=base_url)
        print(f'{"OK" if ok else "FAIL"}\t{out_pdf}\t{db_record_path}', flush=True)

    reader.close()


if __name__ == '__main__':
    main()
