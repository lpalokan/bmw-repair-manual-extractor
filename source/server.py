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

    render.py produces:  src="file:///path/to/DATAS/BMW-Motorrad/BILD/foo.jpg"
    We emit:             src="/image/BMW-Motorrad/BILD/foo.jpg"
    """
    data_parent = os.path.dirname(config.DATA_DIR)  # …/DATAS
    # Normalise to forward slashes for the regex (macOS paths have no spaces
    # in the DATAS tree, but URL-encode any that do)
    base_url = 'file://' + data_parent.replace('\\', '/')

    def _replace(m: re.Match) -> str:
        attr = m.group(1)   # 'src=' or 'href='
        url  = m.group(2)   # full file:// URL
        # Strip the base, keep the relative portion (BMW-Motorrad/...)
        rel = url.removeprefix(base_url).lstrip('/')
        return f'{attr}"/image/{rel}"'

    escaped_base = re.escape(base_url)
    return re.sub(
        rf'(src=|href=)"{escaped_base}/([^"]+\.(jpg|gif|png|JPG|GIF|PNG))"',
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

    @app.route('/model/<code>')
    def model_detail(code):
        model_info = get_model_info(config.DECODED_DB, code)
        reader = GdbReader(config.DECODED_DB)
        paths = reader.list_paths(code, config.DEFAULT_SUBDIR)
        reader.close()

        procedures = []
        for p in paths:
            m = re.search(
                r'\d{4}_\d{2}_\d+_(.+)_(?:POS|AD|BS|SW|TD|WAU|REPSCH)\.XML$',
                p, re.IGNORECASE
            )
            name = m.group(1).replace('_', ' ').title() if m else p
            procedures.append({'name': name, 'db_path': p})

        return render_template('model.html', model=model_info, procedures=procedures)

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
