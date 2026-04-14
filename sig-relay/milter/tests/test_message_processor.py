"""
Comprehensive tests for message_processor.process_message.
Covers the real-world cases that would otherwise destroy MIME structure:
multipart/alternative, QP-encoded HTML, attachments, marker detection.
"""

import sys
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy as email_policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from message_processor import (
    process_message,
    html_to_text,
    _inject_text_signature,
)


SIG_HTML = '<!--KAWASIG:example.com--><div><b>Jane Doe</b><br>jane@example.com</div><!--/KAWASIG:example.com-->'

PROFILE = {
    "display_name": "Jane Doe",
    "job_title": "Engineer",
    "email": "jane@example.com",
    "phone": "+1 555 0100",
    "department": "Eng",
}


def _profile_ok(_email):
    return PROFILE


def _profile_none(_email):
    return None


def _render_ok(_domain, _profile):
    return SIG_HTML


def _render_none(_domain, _profile):
    return None


def _build_multipart_alternative(text_body: str, html_body: str, attachment: bytes = None) -> bytes:
    msg = EmailMessage(policy=email_policy.SMTP)
    msg["From"] = "jane@example.com"
    msg["To"] = "bob@external.com"
    msg["Subject"] = "test"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf",
                           filename="report.pdf")
    return msg.as_bytes()


def _reassemble(raw_in: bytes, body_bytes: bytes) -> bytes:
    """
    Postfix keeps the original headers and replaces only the body.
    Reproduce that for tests: take original headers + new body.
    """
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw_in else b"\n\n"
    headers = raw_in.split(sep, 1)[0]
    return headers + b"\r\n\r\n" + body_bytes


def _build_qp_html() -> bytes:
    """Build a multipart/alternative with QP-encoded HTML (Outlook style)."""
    raw = (
        b"From: jane@example.com\r\n"
        b"To: bob@external.com\r\n"
        b"Subject: QP test\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"Hi Bob,\r\n\r\nThanks for the update.\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"<html><body><p>Hi Bob,</p><p>Thanks =\r\n"
        b"for the update.</p></body></html>\r\n"
        b"--BOUND--\r\n"
    )
    return raw


# ---------- marker detection ----------

def test_passthrough_when_html_has_marker():
    raw = _build_multipart_alternative(
        text_body="hello",
        html_body='<html><body>hi <!--KAWASIG:example.com-->X<!--/KAWASIG:example.com--></body></html>',
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"
    assert "marker present" in r.reason


def test_passthrough_when_html_has_generic_marker_other_domain():
    raw = _build_multipart_alternative(
        text_body="hello",
        html_body='<html><body><!--KAWASIG:other.com-->X<!--/KAWASIG:other.com--></body></html>',
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"


def test_passthrough_when_html_has_data_kawasig_attribute():
    """If Outlook strips the HTML comment but preserves data-attributes, the
    data-kawasig fallback should still trigger passthrough."""
    raw = _build_multipart_alternative(
        text_body="body",
        html_body='<html><body><table data-kawasig="example.com"><tr><td>sig</td></tr></table></body></html>',
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"
    assert "data-attr" in r.reason, r.reason


def test_passthrough_when_data_attr_is_qp_encoded():
    """Outlook in QP mode encodes `=` as `=3D`. Make sure we still detect."""
    raw = (
        b"From: jane@example.com\r\nTo: x\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: text/html; charset=utf-8\r\n'
        b"Content-Transfer-Encoding: 7bit\r\n\r\n"
        b'<html><body><table data-kawasig=3D"example.com">sig</table></body></html>\r\n'
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"


def test_passthrough_when_text_has_marker():
    raw = _build_multipart_alternative(
        text_body="Hi\n[KAWASIG:example.com]\nJane Doe\nReg",
        html_body="<html><body>hi</body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"


# ---------- multipart preservation ----------

def test_multipart_inject_preserves_text_and_html_parts():
    raw = _build_multipart_alternative(
        text_body="Hello Bob,\nReply text here.\n",
        html_body="<html><body><p>Hello Bob,</p><p>Reply.</p></body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified", r.reason
    assert r.body is not None

    # Postfix keeps original headers + replaces body. Reassemble that way.
    full = _reassemble(raw, r.body)
    parsed = BytesParser(policy=email_policy.SMTP).parsebytes(full)
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types, types
    assert "text/html" in types, types


def test_multipart_inject_preserves_attachments():
    raw = _build_multipart_alternative(
        text_body="hi",
        html_body="<html><body>hi</body></html>",
        attachment=b"%PDF-1.4 fake pdf bytes",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"

    full = _reassemble(raw, r.body)
    parsed = BytesParser(policy=email_policy.SMTP).parsebytes(full)
    types = [p.get_content_type() for p in parsed.walk()]
    assert "application/pdf" in types, types
    assert "text/html" in types, types
    assert "text/plain" in types, types


def test_multipart_inject_adds_signature_html():
    raw = _build_multipart_alternative(
        text_body="hi",
        html_body="<html><body><p>Hello</p></body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"

    # The body bytes should now contain the marker (decoded or QP-encoded)
    # SMTP policy may QP-encode; check both
    body_text = r.body.decode("utf-8", errors="replace")
    assert ("KAWASIG:example.com" in body_text
            or "KAWASIG=3Aexample.com" in body_text), body_text[:500]


def test_multipart_inject_adds_text_signature_with_text_marker():
    raw = _build_multipart_alternative(
        text_body="Hello Bob,\nReply text.\n",
        html_body="<html><body><p>Hello.</p></body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"
    body_text = r.body.decode("utf-8", errors="replace")
    assert "[KAWASIG:example.com]" in body_text


def test_x_kawasig_processed_header_added():
    raw = _build_multipart_alternative(
        text_body="hi",
        html_body="<html><body>hi</body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert ("X-KawaSig-Processed", "example.com") in r.headers_to_add


# ---------- base64-encoded HTML ----------

def test_base64_encoded_html_part_processed():
    """Some clients (Apple Mail, certain Outlook versions) encode HTML as
    base64 instead of QP. Ensure decoding + re-encoding works."""
    import base64
    html = b"<html><body><p>Base64 body.</p></body></html>"
    b64 = base64.b64encode(html).decode("ascii")
    raw = (
        b"From: jane@example.com\r\nTo: x\r\nSubject: b64\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: text/html; charset=utf-8\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + b64.encode("ascii") + b"\r\n"
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified", r.reason


# ---------- QP-encoded HTML ----------

def test_qp_encoded_html_is_processed():
    raw = _build_qp_html()
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified", r.reason
    assert r.body is not None


def test_qp_encoded_marker_detected_after_decode():
    """If a QP-encoded HTML body contains the marker, processor should detect it."""
    raw = (
        b"From: jane@example.com\r\nTo: x\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: text/html; charset=utf-8\r\n'
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        # "<!--KAWASIG:example.com-->" survives QP unmodified (no special chars)
        # but if we wrote it as <=21--KAWASIG... it would be QP-encoded. Test the
        # plain case which is the realistic one.
        b"<html><body><!--KAWASIG:example.com-->sig<!--/KAWASIG:example.com--></body></html>\r\n"
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"


# ---------- single-part HTML ----------

def test_single_part_html_inject():
    raw = (
        b"From: jane@example.com\r\nTo: bob@external.com\r\n"
        b"Subject: simple\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Hi</p></body></html>\r\n"
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"


# ---------- error / passthrough paths ----------

def test_no_html_or_text_passes_through():
    raw = (
        b"From: jane@example.com\r\nTo: x\r\n"
        b"Content-Type: application/octet-stream\r\n\r\nbinary"
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "passthrough"


def test_no_profile_passes_through():
    raw = _build_multipart_alternative(text_body="hi", html_body="<html><body>hi</body></html>")
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_none, _render_ok)
    assert r.action == "passthrough"
    assert "graph profile" in r.reason


def test_no_template_passes_through():
    raw = _build_multipart_alternative(text_body="hi", html_body="<html><body>hi</body></html>")
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_none)
    assert r.action == "passthrough"
    assert "no template" in r.reason


# ---------- html_to_text helper ----------

def test_html_to_text_extracts_visible_content():
    html = '<html><head><style>.x{color:red}</style></head><body><p>Hello</p><div>World</div></body></html>'
    text = html_to_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "color:red" not in text


def test_html_to_text_skips_script():
    html = '<html><body><script>alert(1)</script><p>Visible</p></body></html>'
    text = html_to_text(html)
    assert "alert" not in text
    assert "Visible" in text


def test_inject_text_signature_appends_when_no_boundary():
    text = "Plain text reply."
    sig = "Jane Doe\nEng"
    out = _inject_text_signature(text, sig)
    assert "Plain text reply." in out
    assert "Jane Doe" in out
    assert out.index("Plain text reply") < out.index("Jane Doe")


def test_inject_text_signature_places_before_quoted_reply():
    text = "Yes, agreed.\n\nOn Mon, Apr 14, 2026 at 9:00 AM Bob wrote:\n> original message\n"
    sig = "Jane Doe"
    out = _inject_text_signature(text, sig)
    assert "Jane Doe" in out
    assert out.index("Jane Doe") < out.index("On Mon, Apr 14")


# ---------- extra production-shape cases ----------

def test_utf8_body_preserved():
    raw = _build_multipart_alternative(
        text_body="Hej Bob — emoji 😀 accented é ñ.\n",
        html_body="<html><body>Hej Bob — <span>émoji 😀 é ñ</span>.</body></html>",
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"
    full = _reassemble(raw, r.body)
    parsed = BytesParser(policy=email_policy.SMTP).parsebytes(full)

    seen_text = False
    seen_html = False
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            body = part.get_content()
            assert "😀" in body, body
            assert "Hej Bob" in body
            seen_text = True
        elif part.get_content_type() == "text/html":
            body = part.get_content()
            assert "😀" in body or "&#128512;" in body
            seen_html = True
    assert seen_text and seen_html


def test_nested_multipart_mixed_with_attachment():
    """Real Exchange replies with attachments are multipart/mixed wrapping a
    multipart/alternative. Make sure we don't lose either the HTML part or
    the attachment."""
    msg = EmailMessage(policy=email_policy.SMTP)
    msg["From"] = "jane@example.com"
    msg["To"] = "bob@external.com"
    msg["Subject"] = "nested"
    msg.set_content("text body")
    msg.add_alternative("<html><body>html body</body></html>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 attachment", maintype="application",
                       subtype="pdf", filename="report.pdf")
    raw = msg.as_bytes()

    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"
    full = _reassemble(raw, r.body)
    parsed = BytesParser(policy=email_policy.SMTP).parsebytes(full)
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types
    assert "text/html" in types
    assert "application/pdf" in types


def test_text_only_no_html_still_gets_text_signature():
    raw = (
        b"From: jane@example.com\r\nTo: bob@external.com\r\nSubject: text only\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Short reply.\r\n"
    )
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    assert r.action == "modified"
    body_text = r.body.decode("utf-8", errors="replace")
    assert "[KAWASIG:example.com]" in body_text
    assert "Short reply" in body_text


def test_large_body_injection_completes_fast():
    """10 KB of text + html bodies, make sure we don't choke."""
    import time
    big_text = "line of text\n" * 800
    big_html = "<html><body>" + ("<p>line</p>" * 1500) + "</body></html>"
    raw = _build_multipart_alternative(text_body=big_text, html_body=big_html)
    t0 = time.perf_counter()
    r = process_message(raw, "jane@example.com", "example.com", "KAWASIG",
                       _profile_ok, _render_ok)
    elapsed = time.perf_counter() - t0
    assert r.action == "modified"
    assert elapsed < 2.0, f"took {elapsed:.2f}s"
