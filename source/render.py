"""
XML → HTML renderer.

Applies RSD.XSL (XSLT 1.0) to a GRIPS-OUT XML tree to produce HTML,
then patches image src attributes to resolve to the local BILD/ directory.
"""

import os
import re
from lxml import etree


_XSL_CACHE: etree.XSLT | None = None


def _get_transform(xsl_path: str) -> etree.XSLT:
    global _XSL_CACHE
    if _XSL_CACHE is None:
        xsl_doc = etree.parse(xsl_path)
        _XSL_CACHE = etree.XSLT(xsl_doc)
    return _XSL_CACHE


def xml_to_html(xml_str: str, xsl_path: str, data_dir: str) -> str:
    """
    Transform a GRIPS-OUT XML string to HTML using RSD.XSL.

    Image src attributes (of the form 'BMW-Motorrad/...' or 'BMW-Motorrad\\...')
    are rewritten to absolute file:// URLs pointing into data_dir.

    data_dir should be the parent of 'BMW-Motorrad/', i.e. the DATAS/ directory.
    """
    if not xml_str:
        return '<html><body><p>(empty record)</p></body></html>'
    # Parse XML (UTF-16 LE content arrives as a Python str)
    xml_bytes = xml_str.encode('utf-8')
    # Remove the XML declaration if present (it declares UTF-16, we're using UTF-8)
    xml_bytes = re.sub(rb'<\?xml[^?]*\?>', b'', xml_bytes, count=1).lstrip()

    try:
        xml_doc = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        # Fall back to a minimal error page
        return f'<html><body><pre>XML parse error: {e}</pre></body></html>'

    transform = _get_transform(xsl_path)

    try:
        result = transform(xml_doc)
        html = str(result)
    except etree.XSLTError as e:
        return f'<html><body><pre>XSLT error: {e}</pre></body></html>'

    # Patch image paths: 'BMW-Motorrad/...' → absolute file:// URL
    # data_dir is the DATAS/ dir (parent of BMW-Motorrad/)
    def make_abs(m: re.Match) -> str:
        attr, path = m.group(1), m.group(2)
        # Normalise backslashes
        path_norm = path.replace('\\', '/')
        abs_path = os.path.join(data_dir, path_norm)
        abs_url = 'file://' + abs_path.replace(' ', '%20')
        return f'{attr}"{abs_url}"'

    # Match src="..." and href="..." that look like relative image paths
    html = re.sub(
        r'(src=|href=)"(BMW-Motorrad[^"]*\.(jpg|gif|png|JPG|GIF|PNG))"',
        make_abs,
        html,
    )

    return html


def strip_pdf_hrefs(html: str) -> str:
    """Remove non-file hrefs that are meaningless or broken in a merged PDF.

    Used for the *short* PDF export where linked documents are not rendered.

    The XSLT produces three kinds of non-image hrefs:
      - href="link::BMW-Motorrad/..."  cross-procedure links (target not a PDF page)
      - href="javascript:void(null)"   Windows-app expand/collapse callbacks
      - href="#anchor"                 same-page anchors (useless across merged docs)

    All three are stripped: the <a> tag is replaced with a plain <span> so the
    visible link text is preserved but no dead hyperlink is created in the PDF.
    """
    # link:: and javascript: — replace whole <a href="...">...</a> with <span>
    html = re.sub(
        r'<a\b([^>]*)\bhref="(?:link::|javascript:)[^"]*"([^>]*)>(.*?)</a>',
        r'<span\1\2>\3</span>',
        html,
        flags=re.S | re.I,
    )
    # Same-page #anchors — just drop the href attribute, keep the <a> tag
    html = re.sub(r'\bhref="#[^"]*"', '', html)
    return html


def sentinel_pdf_hrefs(html: str) -> str:
    """Replace link:: hrefs with bmwlink://SLUG sentinels for GoTo patching.

    Used for the *long* PDF export where all linked documents are rendered.
    WeasyPrint turns these into PDF URI actions; after merging, patch_goto_links()
    replaces the bmwlink:// URIs with actual GoTo page-number actions.

    javascript: and #anchor hrefs are still stripped as they have no PDF target.
    """
    def _replace(m: re.Match) -> str:
        link_path = m.group(1)   # BMW-Motorrad/AUS/1111_0458_01_foo_AUS.xml
        basename = link_path.rsplit('/', 1)[-1]
        slug = re.sub(r'\.xml$', '', basename, flags=re.IGNORECASE).upper()
        return f'href="bmwlink://{slug}"'

    html = re.sub(
        r'href="link::(BMW-Motorrad[^"]+)"',
        _replace,
        html,
    )
    # javascript: hrefs — strip the <a> to a <span>
    html = re.sub(
        r'<a\b([^>]*)\bhref="javascript:[^"]*"([^>]*)>(.*?)</a>',
        r'<span\1\2>\3</span>',
        html,
        flags=re.S | re.I,
    )
    # Same-page #anchors — drop the href attribute
    html = re.sub(r'\bhref="#[^"]*"', '', html)
    return html


def extract_ref_links(xml: str) -> list[str]:
    """Return all cross-document REF LINK paths from a raw XML blob.

    These are <REF LINK="BMW-Motorrad/..."> elements that the XSLT renders
    as link:: hrefs.  Image paths (jpg/gif/png) are excluded.
    """
    refs = re.findall(r'<REF\b[^>]*\bLINK="(BMW-Motorrad[^"]+)"', xml, re.I)
    return [r for r in refs if not re.search(r'\.(jpg|gif|png)$', r, re.I)]
