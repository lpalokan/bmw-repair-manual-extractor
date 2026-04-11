"""Configuration for BMW Repair Manual PDF extractor."""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
INPUT_DIR = os.path.join(_ROOT, 'input', 'BMW Repair manual application')
DATA_DIR = os.path.join(INPUT_DIR, 'DATAS', 'BMW-Motorrad')

XML_DAT = os.path.join(DATA_DIR, 'XML_01.Dat')
DECODED_DB = '/tmp/grips_decoded.db'
DYN_W_PLAN_DIR = os.path.join(DATA_DIR, 'DYN-W-PLAN')
XSL_PATH = os.path.join(DATA_DIR, 'XSL', 'RSD.XSL')
BILD_DIR = os.path.join(DATA_DIR, 'BILD')
MODELLBILD_DIR = os.path.join(INPUT_DIR, 'DATAS', 'MODELLBILD')

OUTPUT_DIR = os.path.join(_ROOT, 'output')

# Which DB subdirectory contains the main repair procedures
DEFAULT_SUBDIR = 'POS'
