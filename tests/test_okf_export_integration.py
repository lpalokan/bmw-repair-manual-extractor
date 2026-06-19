"""End-to-end orchestrator test for export_model_okf.

Hermetic: the GRIPS database reader and the XSLT renderer are monkeypatched so
the test exercises the full export pipeline (record loop, BFS over cross-refs,
image copying, Markdown + frontmatter serialization, index/log generation)
without the real ~400 MB database or RSD.XSL.
"""

import types

import yaml

import okf_exporter


POS_PATH = 'BMW-Motorrad\\POS\\1111_0458_01_RAD_HINTEN_POS.XML'


def _read_frontmatter(path):
    """Return (frontmatter_dict, body) for an OKF Markdown file."""
    text = path.read_text(encoding='utf-8')
    assert text.startswith('---'), f'no frontmatter in {path}'
    parts = text.split('---', 2)
    return yaml.safe_load(parts[1]), parts[2]


def _make_fakes(img_url):
    class FakeReader:
        def __init__(self, *a, **k):
            pass

        def get_xml_exact(self, db_path):
            if 'HALTER' in db_path.upper():
                return '<GRIPS-OUT><EMPH BOLD="1">Removing holder</EMPH></GRIPS-OUT>'
            return '<GRIPS-OUT><EMPH BOLD="1">Removing rear wheel</EMPH></GRIPS-OUT>'

        def close(self):
            pass

    def fake_xml_to_html(xml, xsl_path, data_parent):
        if 'holder' in xml.lower():
            return '<html><body><p>Holder removal steps.</p></body></html>'
        return (
            '<html><body>'
            '<p>This procedure describes removing the rear wheel.</p>'
            '<table border="1"><tr><th>Fastener</th><th>Torque</th></tr>'
            '<tr><td>Axle</td><td>100 Nm</td></tr></table>'
            f'<img src="{img_url}" alt="rear wheel">'
            '<a href="link::BMW-Motorrad/AUS/2222_0458_01_HALTER_AUS.xml">Removing holder</a>'
            '<a href="javascript:void(0)">Toggle</a>'
            '<a href="#sect1">Jump</a>'
            '</body></html>'
        )

    return FakeReader, fake_xml_to_html


def test_export_model_okf_writes_bundle(tmp_path, monkeypatch):
    img = tmp_path / 'wheel.jpg'
    img.write_bytes(b'\xff\xd8\xff\xe0fakejpeg')
    img_url = 'file://' + str(img)

    FakeReader, fake_xml_to_html = _make_fakes(img_url)
    monkeypatch.setattr(okf_exporter, 'GdbReader', FakeReader)
    monkeypatch.setattr(okf_exporter, 'xml_to_html', fake_xml_to_html)

    model_info = types.SimpleNamespace(code='0458', name='HP2 Sport', image_path=None)
    out_dir = tmp_path / 'bundle'

    okf_exporter.export_model_okf(
        model_info=model_info,
        paths=[POS_PATH],
        out_dir=str(out_dir),
        xsl_path='/unused/RSD.XSL',
        data_parent='/unused/DATAS',
        timestamp='2026-06-19T00:00:00+00:00',
    )

    # ── root index.md ─────────────────────────────────────────────────────────
    index = out_dir / 'index.md'
    assert index.exists()
    fm, body = _read_frontmatter(index)
    assert fm['type'] == 'Repair Manual'
    assert '0458' in str(fm.get('title', '')) or '0458' in body

    # ── POS concept file ──────────────────────────────────────────────────────
    pos_md = out_dir / 'procedures' / '1111_0458_01_RAD_HINTEN_POS.md'
    assert pos_md.exists()
    fm, body = _read_frontmatter(pos_md)
    assert fm['type'] == 'Repair Procedure'
    assert fm['title'] == 'Removing rear wheel'
    assert '# Removing rear wheel' in body
    assert '| --- |' in body                              # GFM table survived
    assert '![rear wheel](../images/wheel.jpg)' in body   # image rewritten
    assert './2222_0458_01_HALTER_AUS.md' in body         # cross-link rewritten
    assert 'javascript' not in body                       # js link stripped

    # ── BFS-rendered linked document ──────────────────────────────────────────
    aus_md = out_dir / 'procedures' / '2222_0458_01_HALTER_AUS.md'
    assert aus_md.exists()
    fm, _ = _read_frontmatter(aus_md)
    assert fm['type'] == 'Removal'

    # ── copied image + reserved index/log files ───────────────────────────────
    assert (out_dir / 'images' / 'wheel.jpg').exists()
    assert (out_dir / 'procedures' / 'index.md').exists()
    assert (out_dir / 'log.md').exists()
