"""
body_parser.py — Finds the reply/forward boundary in HTML email bodies.

Email clients insert a predictable HTML marker between the user's reply
and the quoted original message. This module finds that boundary so we
can inject the signature in the correct position.
"""

import re
import logging
from typing import Optional, Tuple

logger = logging.getLogger("kawasig.parser")

BOUNDARY_PATTERNS: list[Tuple[str, re.Pattern]] = [
    ("outlook_desktop",
     re.compile(r'<div\s[^>]*id\s*=\s*["\']?divRplyFwdMsg["\']?', re.IGNORECASE)),

    ("owa_new_outlook",
     re.compile(r'<div\s[^>]*id\s*=\s*["\']?appendonsend["\']?', re.IGNORECASE)),

    ("outlook_qp_encoded",
     re.compile(r'<div\s[^>]*id\s*=\s*3D[\\"]?divRplyFwdMsg', re.IGNORECASE)),

    ("ios_apple_mail",
     re.compile(r'<blockquote\s[^>]*type\s*=\s*["\']?cite["\']?', re.IGNORECASE)),

    ("gmail",
     re.compile(r'<div\s[^>]*class\s*=\s*["\']?gmail_quote', re.IGNORECASE)),

    ("yahoo",
     re.compile(r'<div\s[^>]*id\s*=\s*["\']?yahoo_quoted', re.IGNORECASE)),

    ("thunderbird",
     re.compile(r'<div\s[^>]*class\s*=\s*["\']?moz-cite-prefix["\']?', re.IGNORECASE)),

    ("hr_separator",
     re.compile(r'<hr\s[^>]*style\s*=\s*["\'][^"\']*border[^"\']*["\']', re.IGNORECASE)),
]


def find_reply_boundary(html: str) -> Optional[Tuple[str, int]]:
    """Find first matching reply boundary. Returns (client_name, position) or None."""
    for name, pattern in BOUNDARY_PATTERNS:
        match = pattern.search(html)
        if match:
            logger.debug("Boundary found: %s at position %d", name, match.start())
            return (name, match.start())

    logger.debug("No reply boundary found - treating as new message")
    return None


def inject_at_boundary(html: str, sig_html: str) -> str:
    """Inject signature HTML at the reply boundary, before </body>, or appended."""
    result = find_reply_boundary(html)

    if result:
        client_name, pos = result
        injected = (
            html[:pos]
            + "\n<!-- sig-relay injection point -->\n"
            + sig_html
            + "\n<!-- /sig-relay injection point -->\n"
            + html[pos:]
        )
        logger.info("Injected at %s boundary (pos=%d)", client_name, pos)
        return injected

    body_close = re.search(r'</\s*body\s*>', html, re.IGNORECASE)
    if body_close:
        pos = body_close.start()
        injected = (
            html[:pos]
            + "\n<!-- sig-relay injection point -->\n"
            + sig_html
            + "\n<!-- /sig-relay injection point -->\n"
            + html[pos:]
        )
        logger.info("Injected before </body> (new message)")
        return injected

    logger.info("Appended signature (no </body> found)")
    return html + "\n" + sig_html
