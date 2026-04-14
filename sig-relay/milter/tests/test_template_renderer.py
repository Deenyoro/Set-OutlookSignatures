"""
Unit tests for template_renderer.

We redirect TEMPLATE_DIR to a tmp_path fixture so tests don't depend on the
real /opt/templates/relay path inside the container.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import template_renderer


@pytest.fixture(autouse=True)
def _isolated_template_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(template_renderer, "TEMPLATE_DIR", tmp_path)
    # Clear the module-level cache between tests
    template_renderer._template_cache.clear()
    yield tmp_path


PROFILE = {
    "display_name": "Alice Example",
    "job_title": "Engineer",
    "email": "alice@example.com",
    "phone": "+1 5550100",
    "department": "Eng",
    "company": "Example Co",
    "mobile": "",
    "office": "",
    "city": "",
    "state": "",
    "postal_code": "",
    "street": "",
    "country": "",
    "given_name": "Alice",
    "surname": "Example",
}


def _write(tmp_path, domain, content):
    (tmp_path / f"{domain}.html").write_text(content, encoding="utf-8")


def test_render_missing_template_returns_none():
    out = template_renderer.render_signature("unknown.com", PROFILE)
    assert out is None


def test_render_basic_template(_isolated_template_dir):
    _write(_isolated_template_dir, "example.com",
           "<div>{{ display_name }} — {{ email }}</div>")
    out = template_renderer.render_signature("example.com", PROFILE)
    assert "<div>Alice Example — alice@example.com</div>" in out


def test_render_cached_second_call(_isolated_template_dir):
    path = _isolated_template_dir / "example.com.html"
    path.write_text("<div>first</div>", encoding="utf-8")

    out1 = template_renderer.render_signature("example.com", PROFILE)
    # Rewrite on disk; cache should still return first
    path.write_text("<div>SECOND</div>", encoding="utf-8")
    out2 = template_renderer.render_signature("example.com", PROFILE)
    assert "<div>first</div>" in out1
    assert "<div>first</div>" in out2, "template cache should survive disk changes until reload_templates()"
    assert "SECOND" not in out2


def test_reload_templates_picks_up_disk_changes(_isolated_template_dir):
    path = _isolated_template_dir / "example.com.html"
    path.write_text("<div>v1</div>", encoding="utf-8")
    assert "<div>v1</div>" in template_renderer.render_signature("example.com", PROFILE)

    path.write_text("<div>v2</div>", encoding="utf-8")
    template_renderer.reload_templates()
    out = template_renderer.render_signature("example.com", PROFILE)
    assert "<div>v2</div>" in out
    assert "v1" not in out


def test_render_missing_variable_is_blank(_isolated_template_dir):
    _write(_isolated_template_dir, "example.com", "<b>{{ unknown_var }}</b>")
    # jinja2 default: missing variables render as empty string (no StrictUndefined)
    out = template_renderer.render_signature("example.com", PROFILE)
    assert "<b></b>" in out


# ---- safety injection: missing markers get auto-added ----

def test_render_injects_missing_markers(_isolated_template_dir):
    _write(_isolated_template_dir, "example.com", "<div>just content, no markers</div>")
    out = template_renderer.render_signature("example.com", PROFILE)
    assert '<!--KAWASIG:example.com-->' in out
    assert '<!--/KAWASIG:example.com-->' in out
    assert 'data-kawasig="example.com"' in out


def test_render_does_not_duplicate_existing_markers(_isolated_template_dir):
    _write(_isolated_template_dir, "example.com",
           '<!--KAWASIG:example.com-->\n'
           '<table data-kawasig="example.com"><tr><td>{{ display_name }}</td></tr></table>\n'
           '<!--/KAWASIG:example.com-->')
    out = template_renderer.render_signature("example.com", PROFILE)
    assert out.count('<!--KAWASIG:example.com-->') == 1
    assert out.count('<!--/KAWASIG:example.com-->') == 1
    assert out.count('data-kawasig="example.com"') == 1
    assert "Alice Example" in out


# ---- preload_and_validate ----

def test_preload_reports_missing_template(_isolated_template_dir):
    status = template_renderer.preload_and_validate(["example.com", "nope.com"])
    assert status["nope.com"] == "missing"


def test_preload_reports_invalid_jinja(_isolated_template_dir):
    _write(_isolated_template_dir, "broken.com", "<div>{{ unclosed_var")
    status = template_renderer.preload_and_validate(["broken.com"])
    assert status["broken.com"].startswith("invalid:")


def test_preload_reports_ok_for_good_template(_isolated_template_dir):
    _write(_isolated_template_dir, "good.com",
           '<!--KAWASIG:good.com-->\n<table data-kawasig="good.com">'
           '<tr><td>{{ display_name }}</td></tr></table>')
    status = template_renderer.preload_and_validate(["good.com"])
    assert status["good.com"] == "ok"


def test_preload_mixed_statuses(_isolated_template_dir):
    _write(_isolated_template_dir, "a.com", "<div>{{ display_name }}</div>")
    _write(_isolated_template_dir, "bad.com", "<div>{% if %}</div>")
    status = template_renderer.preload_and_validate(["a.com", "bad.com", "missing.com"])
    assert status["a.com"] == "ok"
    assert status["bad.com"].startswith("invalid:")
    assert status["missing.com"] == "missing"


def test_render_template_with_data_kawasig_attribute(_isolated_template_dir):
    """Ensure the shipped templates' marker pattern survives jinja rendering."""
    _write(_isolated_template_dir, "example.com",
           '<!--KAWASIG:example.com-->\n'
           '<table data-kawasig="example.com" style="margin:0">'
           '<tr><td>{{ display_name }}</td></tr></table>')
    out = template_renderer.render_signature("example.com", PROFILE)
    assert '<!--KAWASIG:example.com-->' in out
    assert 'data-kawasig="example.com"' in out
    assert "Alice Example" in out
