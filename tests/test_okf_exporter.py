"""Unit tests for the pure helpers in okf_exporter.

These are hermetic: they take HTML / string inputs and assert on the produced
Markdown / YAML, with no database, XSLT, or filesystem dependencies.
"""

import yaml

from okf_exporter import (
    okf_type,
    build_frontmatter,
    rewrite_links_okf,
    html_to_markdown,
    derive_description,
)


# ── okf_type ──────────────────────────────────────────────────────────────────

def test_okf_type_maps_known_types():
    assert okf_type('POS') == 'Repair Procedure'
    assert okf_type('AD') == 'Tightening Torques'
    assert okf_type('AUS') == 'Removal'
    assert okf_type('EIN') == 'Installation'


def test_okf_type_is_case_insensitive():
    assert okf_type('ein') == 'Installation'


def test_okf_type_unknown_falls_back():
    assert okf_type('ZZZ') == 'Repair Document'


# ── build_frontmatter ─────────────────────────────────────────────────────────

def _parse_frontmatter(block: str) -> dict:
    """Parse a '---\\n…\\n---\\n' frontmatter block into a dict."""
    assert block.startswith('---')
    parts = block.split('---', 2)
    return yaml.safe_load(parts[1])


def test_build_frontmatter_is_valid_yaml_with_required_type():
    fm = build_frontmatter({
        'type': 'Repair Procedure',
        'title': 'Removing rear wheel',
        'description': 'Steps to remove the rear wheel.',
        'resource': 'BMW-Motorrad\\POS\\1111_0458_01_RAD_POS.XML',
        'tags': ['bmw', '0458', 'POS'],
        'timestamp': '2026-06-19T00:00:00+00:00',
    })
    data = _parse_frontmatter(fm)
    assert data['type'] == 'Repair Procedure'
    assert data['title'] == 'Removing rear wheel'
    assert data['tags'] == ['bmw', '0458', 'POS']
    assert data['timestamp'] == '2026-06-19T00:00:00+00:00'
    assert data['resource'].endswith('1111_0458_01_RAD_POS.XML')


def test_build_frontmatter_lists_type_first():
    fm = build_frontmatter({'type': 'Metric', 'title': 'X'})
    assert fm.index('type:') < fm.index('title:')


def test_build_frontmatter_handles_special_characters():
    # Colons and German characters must not break the YAML.
    fm = build_frontmatter({
        'type': 'Repair Procedure',
        'title': 'Federbein: hinten ersetzen (Öldämpfer)',
    })
    data = _parse_frontmatter(fm)
    assert data['title'] == 'Federbein: hinten ersetzen (Öldämpfer)'


# ── rewrite_links_okf ─────────────────────────────────────────────────────────

def test_rewrite_links_okf_converts_link_sentinel_to_relative_md():
    html = '<a href="link::BMW-Motorrad/AUS/1111_0458_01_foo_AUS.xml">Removal</a>'
    out = rewrite_links_okf(html)
    assert 'href="./1111_0458_01_FOO_AUS.md"' in out
    assert 'link::' not in out


def test_rewrite_links_okf_strips_javascript_links_keeping_text():
    html = '<a href="javascript:void(0)">Toggle</a>'
    out = rewrite_links_okf(html)
    assert 'javascript' not in out
    assert 'Toggle' in out


def test_rewrite_links_okf_drops_anchor_hrefs():
    html = '<a href="#sect1">Jump</a>'
    out = rewrite_links_okf(html)
    assert 'href="#' not in out
    assert 'Jump' in out


def test_rewrite_links_okf_strips_image_zoom_links_keeping_text():
    html = '<a href="image::BMW-Motorrad/BILD/x_small.jpg::1::2::3::4">(arrows)</a>'
    out = rewrite_links_okf(html)
    assert 'image::' not in out
    assert '(arrows)' in out


# ── html_to_markdown ──────────────────────────────────────────────────────────

def test_html_to_markdown_headings_and_paragraphs():
    out = html_to_markdown('<h1>Title</h1><p>hello world</p>')
    assert '# Title' in out
    assert 'hello world' in out


def test_html_to_markdown_data_table_becomes_gfm():
    # RSD.XSL marks real data tables with border="1".
    html = ('<table border="1"><tr><th>Fastener</th><th>Torque</th></tr>'
            '<tr><td>Axle</td><td>100 Nm</td></tr></table>')
    out = html_to_markdown(html)
    assert '| Fastener | Torque |' in out
    assert '| --- |' in out


def test_html_to_markdown_flattens_layout_tables():
    # Borderless tables are layout-only and must not become pipe-tables.
    html = ('<table border="0"><tr><td>Step one</td></tr>'
            '<tr><td>Step two</td></tr></table>')
    out = html_to_markdown(html)
    assert 'Step one' in out
    assert 'Step two' in out
    assert '| --- |' not in out


def test_html_to_markdown_image_preserved():
    out = html_to_markdown('<img src="../images/a.jpg" alt="x">')
    assert '![x](../images/a.jpg)' in out


def test_html_to_markdown_drops_ui_icons():
    html = ('<img src="file:///d/BMW-Motorrad/imgs/icon/bullet.gif" alt="bullet">'
            '<img src="../images/real.jpg" alt="diagram">')
    out = html_to_markdown(html)
    assert 'bullet' not in out
    assert '![diagram](../images/real.jpg)' in out


def test_html_to_markdown_strips_scripts_and_styles():
    out = html_to_markdown('<script>junk()</script><style>.x{}</style><p>ok</p>')
    assert 'junk' not in out
    assert '.x{}' not in out
    assert 'ok' in out


def test_html_to_markdown_link_preserved():
    out = html_to_markdown('<a href="./X.md">link text</a>')
    assert '[link text](./X.md)' in out


# ── derive_description ────────────────────────────────────────────────────────

def test_derive_description_returns_first_paragraph():
    md = '# Title\n\nThis is the first paragraph.\n\nSecond paragraph.'
    assert derive_description(md) == 'This is the first paragraph.'


def test_derive_description_skips_headings_tables_and_images():
    md = '# Heading\n\n| a | b |\n| --- | --- |\n\n![pic](x.jpg)\n\nReal prose here.'
    assert derive_description(md) == 'Real prose here.'


def test_derive_description_truncates_long_text():
    md = 'word ' * 100  # 500 chars
    out = derive_description(md, limit=200)
    assert len(out) <= 201
    assert out.endswith('…')
