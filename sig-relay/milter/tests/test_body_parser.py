"""Unit tests for body_parser. Run from milter dir: python -m pytest tests/"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from body_parser import find_reply_boundary, inject_at_boundary

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_outlook_desktop_boundary():
    html = _load("outlook_reply.html")
    result = find_reply_boundary(html)
    assert result is not None
    assert result[0] == "outlook_desktop"


def test_ios_mail_boundary():
    html = _load("ios_mail_reply.html")
    result = find_reply_boundary(html)
    assert result is not None
    assert result[0] == "ios_apple_mail"


def test_gmail_boundary():
    html = _load("gmail_reply.html")
    result = find_reply_boundary(html)
    assert result is not None
    assert result[0] == "gmail"


def test_new_message_no_boundary():
    html = _load("new_message.html")
    result = find_reply_boundary(html)
    assert result is None


def test_inject_at_outlook_boundary_places_before_quoted():
    html = _load("outlook_reply.html")
    sig = "<!--SIG_HERE-->"
    out = inject_at_boundary(html, sig)
    assert sig in out
    assert out.index(sig) < out.index("divRplyFwdMsg")


def test_inject_into_new_message_uses_body_close():
    html = _load("new_message.html")
    sig = "<!--SIG_HERE-->"
    out = inject_at_boundary(html, sig)
    assert sig in out
    assert out.index(sig) < out.index("</body>")


def test_inject_into_ios_places_before_blockquote():
    html = _load("ios_mail_reply.html")
    sig = "<!--SIG_HERE-->"
    out = inject_at_boundary(html, sig)
    assert sig in out
    assert out.index(sig) < out.index("<blockquote")
