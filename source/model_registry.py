"""
Discovers available models from the decoded database and MODELLBILD directory.
"""

import os
import re
import sqlite3
import zlib

import config


def _delta_decode(data: bytes) -> bytes | None:
    if len(data) < 2:
        return None
    B = data[0]; cl = data[1]
    if cl == 0:
        return b''
    output = []; pos = 2
    while True:
        if cl == 1:
            if pos >= len(data):
                return None
            nb = data[pos]; pos += 1
            if nb == 1: cl = 0
            elif nb == 2: cl = 1
            elif nb == 3: cl = 0x27
            else: return None
        output.append((B + cl) & 0xFF)
        if pos >= len(data): break
        cl = data[pos]; pos += 1
        if cl == 0: break
    return bytes(output)


def _get_model_name_from_db(conn: sqlite3.Connection, code: str) -> str | None:
    """Extract human-readable model name from the AD record for this code."""
    row = conn.execute(
        "SELECT rowid FROM XML WHERE path LIKE ?",
        [f'%\\AD\\00_{code}%AD.XML'.encode()]
    ).fetchone()
    if not row:
        return None
    blob = conn.execute("SELECT xml_blob FROM XML WHERE rowid = ?", [row[0]]).fetchone()[0]
    dec = _delta_decode(blob)
    if not dec:
        return None
    try:
        if dec[:4] == b'+PAK':
            xml = zlib.decompress(dec[8:]).decode('utf-16-le')
        elif dec[:3] == b'PAK':
            xml = zlib.decompress(dec[7:]).decode('utf-16-le')
        else:
            return None
        # Pattern: "Tightening torques 0458 - HP2 Sport"
        m = re.search(r'BOLD="1"\s*>([^<]+)</EMPH', xml)
        if m:
            full = m.group(1).strip()
            # Strip the "Tightening torques MMMM - " prefix
            m2 = re.match(r'Tightening torques \d+ - (.+)', full)
            return m2.group(1) if m2 else full
    except Exception:
        pass
    return None


def _load_modellbild_map() -> dict[str, str]:
    """
    Parse MODELLBILD/Modellbild.dat → {model_code: absolute_image_path}.

    Records are newline-separated: 4-char code + filename (may be truncated).
    """
    dat_path = os.path.join(config.MODELLBILD_DIR, 'Modellbild.dat')
    if not os.path.exists(dat_path):
        return {}
    with open(dat_path, 'rb') as f:
        data = f.read()

    # Find zlib stream
    for i in range(len(data) - 1):
        if data[i] == 0x78 and data[i + 1] in (0x9C, 0x01, 0xDA):
            try:
                dec = zlib.decompress(data[i:])
            except Exception:
                continue
            text = dec.decode('utf-16-le', errors='replace')
            result: dict[str, str] = {}
            for line in text.split('\n'):
                line = line.strip('\ufeff').strip()
                if len(line) < 5:
                    continue
                code = line[:4]
                fname = line[4:]
                # Reconstruct truncated '.jpg' suffix
                if fname.endswith('.'):
                    fname += 'jpg'
                # Resolve case-insensitively
                candidate = os.path.join(config.MODELLBILD_DIR, fname)
                if os.path.exists(candidate):
                    result[code] = candidate
                else:
                    # Case-insensitive fallback
                    for f in os.listdir(config.MODELLBILD_DIR):
                        if f.lower() == fname.lower():
                            result[code] = os.path.join(config.MODELLBILD_DIR, f)
                            break
            return result
    return {}


class ModelInfo:
    def __init__(self, code: str, name: str, image_path: str | None):
        self.code = code
        self.name = name
        self.image_path = image_path

    def __repr__(self):
        return f'ModelInfo({self.code}, {self.name!r})'


def list_models(db_path: str) -> list[ModelInfo]:
    """
    Return all models found in the database, sorted by code.
    Each model has a code, human-readable name, and optional image path.
    """
    conn = sqlite3.connect(db_path)
    conn.text_factory = bytes

    # Collect model codes from POS paths
    rows = conn.execute(
        "SELECT DISTINCT path FROM XML WHERE path LIKE 'BMW-MOTORRAD\\POS\\%' LIMIT 5000"
    ).fetchall()
    codes: set[str] = set()
    for row in rows:
        path = row[0].decode()
        m = re.match(r'BMW-MOTORRAD\\POS\\\w+_(\d{4})_', path)
        if m:
            codes.add(m.group(1))

    image_map = _load_modellbild_map()

    models = []
    for code in sorted(codes):
        name = _get_model_name_from_db(conn, code) or f'Model {code}'
        image = image_map.get(code)
        models.append(ModelInfo(code, name, image))

    conn.close()
    return models


def get_model_info(db_path: str, code: str) -> ModelInfo:
    """Return ModelInfo for a specific code."""
    conn = sqlite3.connect(db_path)
    conn.text_factory = bytes
    name = _get_model_name_from_db(conn, code) or f'Model {code}'
    conn.close()
    image_map = _load_modellbild_map()
    return ModelInfo(code, name, image_map.get(code))
