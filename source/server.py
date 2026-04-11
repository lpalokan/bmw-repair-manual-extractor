"""
Local web server for browsing BMW repair procedures.

Routes:
  GET /                              — home: all models
  GET /model/<code>                  — procedure list for one model
  GET /procedure/<code>/<path:…>     — render a single procedure
  GET /image/<path:rel_path>         — serve a DATAS/ image
  GET /model-image/<code>            — serve the model cover photo
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import config
from gdb_reader import GdbReader
from model_registry import list_models, get_model_info, _load_modellbild_map
from render import xml_to_html

# ── image URL rewriting ──────────────────────────────────────────────────────

def _rewrite_image_urls(html: str) -> str:
    """
    Replace file:// image URLs produced by render.xml_to_html with Flask
    /image/<rel_path> routes.

    render.py produces URLs like:
      src="file:///Users/.../BMW%20Repair%20manual%20application/DATAS/BMW-Motorrad/BILD/foo.jpg"

    The base path contains %20-encoded spaces, so we anchor on the stable
    'BMW-Motorrad/' segment instead of trying to match the full base path.

    We emit: src="/image/BMW-Motorrad/BILD/foo.jpg"
    """
    return re.sub(
        r'(src=|href=)"file://[^"]*/(BMW-Motorrad[^"]+\.(jpg|gif|png|JPG|GIF|PNG))"',
        lambda m: f'{m.group(1)}"/image/{m.group(2)}"',
        html,
    )


# ── Flask app factory ────────────────────────────────────────────────────────

def create_app() -> 'Flask':
    from flask import Flask, abort, render_template, send_file as flask_send_file

    app = Flask(__name__, template_folder='templates')

    # Simple in-process cache so list_models() (slow) only runs once per server
    # lifetime.  Safe because the DB is read-only during a server run.
    _models_cache: list = []

    def _get_models():
        if not _models_cache:
            _models_cache.extend(list_models(config.DECODED_DB))
        return _models_cache

    # ── home ──────────────────────────────────────────────────────────────────

    @app.route('/')
    def home():
        return render_template('home.html', models=_get_models())

    # ── model detail ──────────────────────────────────────────────────────────

    # Cache: model code → list of procedure dicts (avoids re-reading 236 blobs)
    _proc_cache: dict[str, list] = {}

    @app.route('/model/<code>')
    def model_detail(code):
        model_info = get_model_info(config.DECODED_DB, code)

        if code not in _proc_cache:
            _proc_cache[code] = _build_procedure_list(code)

        return render_template('model.html', model=model_info,
                               procedures=_proc_cache[code])

    def _build_procedure_list(code: str) -> list[dict]:
        """Read all POS-subdir paths for a model, extract real titles from XML."""
        _SUFFIX_LABELS = {
            '_AD': 'Technical data', '_BS': 'Safety', '_SW': 'Tools',
            '_TD': 'Torque', '_WAU': 'Notes', '_REPSCH': 'Diagram',
        }

        reader = GdbReader(config.DECODED_DB)
        all_paths = reader.list_paths(code, config.DEFAULT_SUBDIR)

        proc_map: dict[str, dict] = {}
        for p in all_paths:
            m = re.search(
                r'(\d{4}_\d{2}_\d+_(.+))(_(?:POS|AD|BS|SW|TD|WAU|REPSCH))\.XML$',
                p, re.IGNORECASE
            )
            if not m:
                continue
            key      = m.group(1)
            name_raw = m.group(2)
            suffix   = m.group(3).upper()

            if key not in proc_map:
                proc_map[key] = {
                    'name': name_raw.replace('_', ' ').title(),  # fallback
                    'main_path': None,
                    'sub_docs': [],
                }

            if suffix == '_POS':
                proc_map[key]['main_path'] = p
                # Extract real title from the XML blob (fast — no XSLT)
                xml = reader.get_xml_exact(p) or ''
                tm = re.search(r'<EMPH[^>]*BOLD="1"[^>]*>([^<]+)', xml)
                if tm:
                    proc_map[key]['name'] = tm.group(1).strip()
            else:
                label = _SUFFIX_LABELS.get(suffix, suffix.lstrip('_'))
                proc_map[key]['sub_docs'].append({'label': label, 'db_path': p})

        reader.close()
        return [v for v in proc_map.values() if v['main_path']]

    # ── procedure renderer ────────────────────────────────────────────────────

    @app.route('/procedure/<code>/<path:db_path>')
    def procedure(code, db_path):
        reader = GdbReader(config.DECODED_DB)
        xml = reader.get_xml_exact(db_path)
        reader.close()

        if not xml:
            abort(404)

        data_parent = os.path.dirname(config.DATA_DIR)
        html = xml_to_html(xml, config.XSL_PATH, data_parent)
        html = _rewrite_image_urls(html)

        # Inject screen CSS and back-navigation
        screen_css = """<style>
  body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt;
         max-width: 960px; margin: 0 auto; padding: 16px; }
  img  { max-width: 100% !important; height: auto !important; }
  table { max-width: 100% !important; word-break: break-word; }
  td, th { overflow-wrap: break-word; word-break: break-word; }
  td[style*="padding-left:8mm"] { padding-left: 2mm !important; }
  table[border="0"] > tbody > tr > td[style*="padding-left"] { padding-left: 2mm !important; }
  input, button, script, .noPrint { display: none !important; }
  .bmw-back { display:block; margin-bottom:12px; color:#003399;
               font-size:12pt; text-decoration:none; }
  .bmw-back:hover { text-decoration:underline; }
</style>"""

        model_info = get_model_info(config.DECODED_DB, code)
        back_link = (f'<a class="bmw-back" href="/model/{code}">'
                     f'&larr; {model_info.name} ({code})</a>')

        if '</head>' in html:
            html = html.replace('</head>', screen_css + '</head>', 1)
        else:
            html = screen_css + html

        if '<body>' in html:
            html = html.replace('<body>', '<body>' + back_link, 1)
        else:
            html = back_link + html

        return html

    # ── image serving ─────────────────────────────────────────────────────────

    @app.route('/image/<path:rel_path>')
    def serve_image(rel_path):
        data_parent = os.path.dirname(config.DATA_DIR)
        abs_path = os.path.join(data_parent, rel_path.replace('/', os.sep))
        if not os.path.isfile(abs_path):
            abort(404)
        return flask_send_file(abs_path)

    @app.route('/model-image/<code>')
    def model_image(code):
        image_map = _load_modellbild_map()
        path = image_map.get(code)
        if not path or not os.path.isfile(path):
            abort(404)
        return flask_send_file(path)

    return app
