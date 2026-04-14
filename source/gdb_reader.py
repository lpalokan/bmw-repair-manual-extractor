"""
GRIPS GDB reader.

XML_01.dat is a SQLite 3 database XOR-encoded byte-by-byte with 0xAA,
with the first 16 bytes replaced by the GRIPS magic string instead of
the normal SQLite header.

Blob format: delta-encoded (+PAK header + zlib-compressed UTF-16 LE XML)
"""

import os
import sqlite3
import struct
import zlib


GRIPS_MAGIC = b'GRIPS GDB V1.00U'
SQLITE_MAGIC = b'SQLite format 3\x00'
XOR_KEY = 0xAA


def decode_db(src_path: str, dst_path: str) -> None:
    """XOR-decode XML_01.dat → plain SQLite file at dst_path."""
    print(f'Decoding {src_path} → {dst_path}')
    with open(src_path, 'rb') as fin, open(dst_path, 'wb') as fout:
        fout.write(SQLITE_MAGIC)  # replace GRIPS magic with SQLite magic
        fin.read(16)              # skip GRIPS magic in source
        while True:
            chunk = fin.read(65536)
            if not chunk:
                break
            fout.write(bytes(b ^ XOR_KEY for b in chunk))
    print('Done.')


def _delta_decode(data: bytes) -> bytes | None:
    """
    Decode GRIPS blob delta encoding.

    Format:
      byte[0]  = base value B
      byte[1]  = first delta d (then continue reading deltas from byte[2]...)
      output[i] = (B + d) & 0xFF for each delta d
      Terminator: delta == 0
      Escape: delta == 1, followed by:
        1 → actual delta 0
        2 → actual delta 1
        3 → actual delta 0x27
    """
    if len(data) < 2:
        return None
    B = data[0]
    cl = data[1]
    if cl == 0:
        return b''
    output = []
    pos = 2
    while True:
        if cl == 1:
            if pos >= len(data):
                return None
            nb = data[pos]; pos += 1
            if nb == 1:
                cl = 0
            elif nb == 2:
                cl = 1
            elif nb == 3:
                cl = 0x27
            else:
                return None  # invalid escape
        output.append((B + cl) & 0xFF)
        if pos >= len(data):
            break
        cl = data[pos]; pos += 1
        if cl == 0:
            break
    return bytes(output)


def _decompress_blob(blob: bytes) -> str | None:
    """Delta-decode a raw SQLite blob and return the XML string."""
    decoded = _delta_decode(blob)
    if not decoded:
        return None
    if decoded[:4] == b'+PAK':
        # +PAK: 4-byte magic + 4-byte LE uncompressed size + zlib at byte 8
        xml_bytes = zlib.decompress(decoded[8:])
        return xml_bytes.decode('utf-16-le')
    elif decoded[:3] == b'PAK':
        # PAK: 3-byte magic + 4-byte LE uncompressed size + zlib at byte 7
        xml_bytes = zlib.decompress(decoded[7:])
        return xml_bytes.decode('utf-16-le')
    # Fallback: treat as raw UTF-16 LE
    return decoded.decode('utf-16-le', errors='replace')


class GdbReader:
    """Read repair procedure XML records from the decoded SQLite database."""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.text_factory = bytes

    def get_xml(self, path: str) -> str | None:
        """Return XML string for the given DB path (case-insensitive LIKE match)."""
        like = path.replace('\\', '%').replace('/', '%')
        like = f'%{like}%' if '%' not in like else like
        row = self.conn.execute(
            "SELECT rowid FROM XML WHERE path LIKE ?", [like.encode()]
        ).fetchone()
        if not row:
            return None
        blob = self.conn.execute(
            "SELECT xml_blob FROM XML WHERE rowid = ?", [row[0]]
        ).fetchone()[0]
        return _decompress_blob(blob)

    def get_xml_exact(self, path: str) -> str | None:
        """Return XML string for the exact DB path (upper-case, backslash)."""
        path_bytes = path.upper().replace('/', '\\').encode()
        row = self.conn.execute(
            "SELECT rowid FROM XML WHERE path LIKE ?",
            [path_bytes]
        ).fetchone()
        if not row:
            return None
        blob = self.conn.execute(
            "SELECT xml_blob FROM XML WHERE rowid = ?", [row[0]]
        ).fetchone()[0]
        return _decompress_blob(blob)

    def list_paths(self, model_code: str, subdir: str = 'POS') -> list[str]:
        """Return all DB paths for the given model code in the given subdir.

        Sorted by the numeric BMW procedure number embedded in the filename
        (e.g. 1100038 → 11 00 038), which matches the workshop manual order.
        Alphabetical string sort is wrong because section prefix length varies
        ('11_' vs '1111_'), causing 2-digit sections to sort after 4-digit ones.
        """
        pattern = f'%\\{subdir}\\%{model_code}%'.encode()
        rows = self.conn.execute(
            "SELECT path FROM XML WHERE path LIKE ?", [pattern]
        ).fetchall()

        def _sort_key(path: str) -> int:
            # Filename format: SECTION_MODEL_REVISION_PROCNUM_NAME_TYPE.XML
            # PROCNUM is always at split index 3 and is a 5-8 digit number.
            parts = path.rsplit('\\', 1)[-1].split('_')
            try:
                return int(parts[3])
            except (IndexError, ValueError):
                return 0

        return sorted((r[0].decode() for r in rows), key=_sort_key)

    def get_xml_by_rowid(self, rowid: int) -> str | None:
        row = self.conn.execute(
            "SELECT xml_blob FROM XML WHERE rowid = ?", [rowid]
        ).fetchone()
        if not row:
            return None
        return _decompress_blob(row[0])

    def close(self):
        self.conn.close()
