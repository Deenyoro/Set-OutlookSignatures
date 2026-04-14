"""
message_processor.py — pure-function body modification.

Pulled out of sig_milter.py so it can be unit-tested without a live milter:
take the raw RFC 5322 bytes that postfix handed us, decide what to do, and
return the modified RAW BODY bytes (the part after the header/body separator)
plus any headers to add/change.

Preserves multipart structure (text/plain alternative, attachments). The HTML
part gets the signature injected at the reply boundary; the text/plain part
gets a plaintext rendering of the same signature appended at the corresponding
spot.
"""

from __future__ import annotations

import email
import email.policy
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

from body_parser import inject_at_boundary

logger = logging.getLogger("kawasig.processor")


@dataclass
class ProcessResult:
    """What the milter should do with the message after processing."""
    action: str  # "accept" | "passthrough" | "modified"
    body: Optional[bytes] = None  # raw body bytes to feed to replacebody (modified only)
    headers_to_add: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""


class _HtmlToText(HTMLParser):
    """Minimal HTML -> text converter for the text/plain alternative."""

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip_depth += 1
        if tag in ("br", "tr"):
            self._chunks.append("\n")
        elif tag in ("p", "div", "li", "table"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip_depth:
            self._skip_depth -= 1
        if tag in ("p", "div", "li", "table"):
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse runs of whitespace within lines, keep paragraph breaks
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip() + "\n"


def html_to_text(html: str) -> str:
    p = _HtmlToText()
    p.feed(html)
    return p.text()


def _decode_part(part) -> Optional[str]:
    """Best-effort decode of a MIME part to a unicode string."""
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if not payload:
            return None
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _split_header_body(raw: bytes) -> tuple[bytes, bytes]:
    """Split RFC 5322 bytes on the first blank line. Returns (headers, body)."""
    idx = raw.find(b"\r\n\r\n")
    if idx != -1:
        return raw[: idx + 4], raw[idx + 4 :]
    idx = raw.find(b"\n\n")
    if idx != -1:
        return raw[: idx + 2], raw[idx + 2 :]
    return raw, b""


def _normalize_crlf(data: bytes) -> bytes:
    """Force CRLF line endings (SMTP requirement)."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _inject_text_signature(text_body: str, sig_text: str) -> str:
    """Insert the text-mode signature at the reply boundary, or append."""
    boundary_re = re.compile(
        r"(\n)(On .{0,80}wrote:\s*\n|From: .+\nSent: .+\n|-{2,}\s*Original Message\s*-{2,}\s*\n)",
        re.IGNORECASE,
    )
    m = boundary_re.search(text_body)
    if m:
        return text_body[: m.start()] + "\n" + sig_text.rstrip() + "\n\n" + text_body[m.start():]
    return text_body.rstrip() + "\n\n-- \n" + sig_text.rstrip() + "\n"


def process_message(
    raw: bytes,
    sender: str,
    sender_domain: str,
    sig_marker_prefix: str,
    profile_lookup: Callable[[str], Optional[dict]],
    template_renderer: Callable[[str, dict], Optional[str]],
) -> ProcessResult:
    """
    Decide what to do with a raw inbound message, and return the new body bytes
    if a modification is required.

    Args:
        raw: full RFC 5322 bytes (headers + body)
        sender: lowercased envelope-from
        sender_domain: managed domain extracted from envelope-from
        sig_marker_prefix: e.g. "KAWASIG"
        profile_lookup(email) -> dict | None: graph profile fetcher
        template_renderer(domain, profile) -> str | None: jinja2 renderer

    Returns:
        ProcessResult describing the action.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.SMTP)

    html_part = None
    text_part = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and html_part is None:
                html_part = part
            elif ct == "text/plain" and text_part is None:
                text_part = part
    else:
        ct = msg.get_content_type()
        if ct == "text/html":
            html_part = msg
        elif ct == "text/plain":
            text_part = msg

    if html_part is None and text_part is None:
        return ProcessResult(action="passthrough", reason="no text/html or text/plain part")

    # Marker check — we look for any of:
    #   1. HTML comment  : <!--KAWASIG:domain-->    (primary)
    #   2. Data attribute: data-kawasig="domain"    (fallback — Outlook strips
    #                                                 HTML comments on some
    #                                                 reply/forward paths;
    #                                                 data-* attributes survive)
    #   3. Text marker   : [KAWASIG:domain]         (text/plain parts)
    # Also accept the "any-domain" generic variant so user-customised
    # signatures with a different domain don't get double-processed.
    full_marker = f"<!--{sig_marker_prefix}:{sender_domain}-->"
    generic_marker = f"<!--{sig_marker_prefix}:"
    attr_marker = f'data-{sig_marker_prefix.lower()}="{sender_domain}"'
    attr_generic_marker = f"data-{sig_marker_prefix.lower()}="
    # Outlook sometimes QP-encodes the attribute value's `=` inside comments/tags.
    attr_qp_marker = f'data-{sig_marker_prefix.lower()}=3D"{sender_domain}"'
    text_marker = f"[{sig_marker_prefix}:{sender_domain}]"
    text_generic_marker = f"[{sig_marker_prefix}:"

    html_body = _decode_part(html_part) if html_part is not None else None
    text_body = _decode_part(text_part) if text_part is not None else None

    def _has_any_marker(body: str) -> Optional[str]:
        if full_marker in body or generic_marker in body:
            return f"{sig_marker_prefix} comment marker"
        if attr_marker in body or attr_generic_marker in body or attr_qp_marker in body:
            return f"{sig_marker_prefix} data-attr marker"
        if text_marker in body or text_generic_marker in body:
            return f"{sig_marker_prefix} text marker"
        return None

    for body in (html_body, text_body):
        if body is None:
            continue
        hit = _has_any_marker(body)
        if hit:
            return ProcessResult(action="passthrough", reason=f"{hit} present")

    profile = profile_lookup(sender)
    if not profile:
        return ProcessResult(action="passthrough", reason="no graph profile for sender")

    sig_html = template_renderer(sender_domain, profile)
    if not sig_html:
        return ProcessResult(action="passthrough", reason=f"no template for {sender_domain}")

    sig_text = html_to_text(sig_html)
    # Stamp a text-mode marker so we recognise our own signature on subsequent
    # passes even if the email is text-only.
    sig_text = f"{text_marker}\n{sig_text}"

    modified_any = False
    if html_part is not None and html_body is not None:
        new_html = inject_at_boundary(html_body, sig_html)
        html_part.set_content(new_html, subtype="html")
        modified_any = True

    if text_part is not None and text_body is not None:
        new_text = _inject_text_signature(text_body, sig_text)
        text_part.set_content(new_text)
        modified_any = True

    if not modified_any:
        return ProcessResult(action="passthrough", reason="nothing to modify")

    full_bytes = msg.as_bytes(policy=email.policy.SMTP)
    _, body_bytes = _split_header_body(full_bytes)
    body_bytes = _normalize_crlf(body_bytes)

    return ProcessResult(
        action="modified",
        body=body_bytes,
        headers_to_add=[("X-KawaSig-Processed", sender_domain)],
        reason="signature injected",
    )
