"""
DYN-W-PLAN PAK file reader.

Files like 00_0458_01_SERVICE_W-PLAN.DAT use PAK+zlib format:
  - 4-byte magic 'PAK\x00'
  - header metadata (offsets, sizes)
  - zlib compressed payload at the first 0x78 0x9C marker
  - payload is UTF-16 LE XML with <PARA><REF LINK='...'> elements
"""

import os
import re
import zlib


def _find_zlib_offset(data: bytes) -> int:
    """Return offset of first zlib stream (0x78 followed by 0x9C/0x01/0xDA)."""
    for i in range(len(data) - 1):
        if data[i] == 0x78 and data[i + 1] in (0x9C, 0x01, 0xDA, 0x5E):
            return i
    raise ValueError('No zlib marker found in PAK file')


def decompress_pak(path: str) -> str:
    """Decompress a PAK file and return the UTF-16 LE text payload."""
    with open(path, 'rb') as f:
        data = f.read()
    if not data[:3] == b'PAK':
        raise ValueError(f'Not a PAK file: {path}')
    offset = _find_zlib_offset(data)
    raw = zlib.decompress(data[offset:])
    return raw.decode('utf-16-le', errors='replace')


def get_procedure_links(w_plan_path: str) -> list[str]:
    """
    Parse a SERVICE_W-PLAN.DAT file and return the list of DB path strings
    referenced by REF LINK attributes (normalised to upper-case backslash).
    """
    text = decompress_pak(w_plan_path)
    refs = re.findall(r"LINK='([^']+)'", text)
    # Normalise path separators and case to match DB keys
    normalised = []
    for ref in refs:
        p = ref.replace('/', '\\').upper()
        normalised.append(p)
    return normalised


def get_all_links_for_model(dyn_w_plan_dir: str, model_code: str) -> list[str]:
    """Collect all procedure links from all W-PLAN DAT files for a model code."""
    links: list[str] = []
    seen: set[str] = set()
    for fname in os.listdir(dyn_w_plan_dir):
        if model_code not in fname:
            continue
        if not fname.endswith('.DAT'):
            continue
        fpath = os.path.join(dyn_w_plan_dir, fname)
        try:
            for link in get_procedure_links(fpath):
                if link not in seen:
                    seen.add(link)
                    links.append(link)
        except Exception:
            pass
    return links
