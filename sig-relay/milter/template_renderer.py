"""
template_renderer.py — Loads and renders domain-specific signature templates.

Templates are Jinja2 HTML files named by domain:
    /opt/templates/relay/treconstruction.net.html
    /opt/templates/relay/kawaconnect.com.html
"""

import logging
from pathlib import Path
from typing import Optional

from jinja2 import Template

logger = logging.getLogger("kawasig.renderer")

TEMPLATE_DIR = Path("/opt/templates/relay")

_template_cache: dict[str, Template] = {}


def get_template(domain: str) -> Optional[Template]:
    """Load the Jinja2 template for a domain. Returns None if not found."""
    if domain in _template_cache:
        return _template_cache[domain]

    template_path = TEMPLATE_DIR / f"{domain}.html"
    if not template_path.exists():
        logger.warning("No relay template for domain: %s (looked for %s)",
                       domain, template_path)
        return None

    try:
        raw = template_path.read_text(encoding="utf-8")
        tmpl = Template(raw)
        _template_cache[domain] = tmpl
        logger.info("Loaded template for %s (%d bytes)", domain, len(raw))
        return tmpl
    except Exception as e:
        logger.error("Failed to load template %s: %s", template_path, e)
        return None


def render_signature(domain: str, profile: dict, marker_prefix: str = "KAWASIG") -> Optional[str]:
    """Render a signature for the given domain using profile data.

    Defensive: if the operator's template forgot the dual-marker requirement,
    inject the missing marker(s) so the relay can still recognise its own work
    on subsequent passes (no double-signatures).

    Returns HTML or None.
    """
    tmpl = get_template(domain)
    if not tmpl:
        return None

    try:
        rendered = tmpl.render(**profile)
    except Exception as e:
        logger.error("Template render error for %s: %s", domain, e)
        return None

    open_comment = f"<!--{marker_prefix}:{domain}-->"
    close_comment = f"<!--/{marker_prefix}:{domain}-->"
    attr_marker = f'data-{marker_prefix.lower()}="{domain}"'

    needs_open = open_comment not in rendered
    needs_close = close_comment not in rendered
    needs_attr = attr_marker not in rendered

    if needs_attr:
        # Hidden span carries the data-attribute even if the visible template
        # doesn't have one. CSS notes:
        #   display:none        — respected by every CSS client except Outlook desktop
        #   mso-hide:all        — Outlook-specific hide directive (Word engine)
        #   font-size:0;line-height:0 — makes any accidental text render as zero
        #   color:transparent;max-height:0;overflow:hidden — belt-and-braces
        # Content is an empty span so there's nothing for a broken renderer to show.
        rendered = (
            f'<span data-{marker_prefix.lower()}="{domain}" '
            f'style="display:none;mso-hide:all;font-size:0;line-height:0;'
            f'color:transparent;max-height:0;overflow:hidden;"></span>\n'
            + rendered
        )
        logger.warning("Template for %s missing data-%s attribute — injected fallback",
                       domain, marker_prefix.lower())

    if needs_open:
        rendered = open_comment + "\n" + rendered
    if needs_close:
        rendered = rendered + "\n" + close_comment

    logger.debug("Rendered signature for %s@%s (%d bytes)",
                 profile.get("email", "?"), domain, len(rendered))
    return rendered


def reload_templates():
    """Clear the template cache, forcing reload on next render."""
    _template_cache.clear()
    logger.info("Template cache cleared")


def preload_and_validate(managed_domains: list[str]) -> dict[str, str]:
    """Load every MANAGED_DOMAINS template at startup so Jinja2 syntax errors
    and missing files surface BEFORE we start accepting mail. Returns a dict
    of domain -> status ("ok" | "missing" | "invalid: <err>").
    """
    status: dict[str, str] = {}
    dummy = {  # render probe with empty fields — confirms compile + render
        "display_name": "", "given_name": "", "surname": "", "job_title": "",
        "company": "", "department": "", "email": "", "phone": "",
        "mobile": "", "office": "", "street": "", "city": "", "state": "",
        "postal_code": "", "country": "",
    }
    for dom in managed_domains:
        template_path = TEMPLATE_DIR / f"{dom}.html"
        if not template_path.exists():
            status[dom] = "missing"
            logger.warning("Template missing for %s: %s", dom, template_path)
            continue
        try:
            raw = template_path.read_text(encoding="utf-8")
            tmpl = Template(raw)  # may raise on bad jinja syntax
            tmpl.render(**dummy)  # may raise on missing include or bad ref
        except Exception as e:
            status[dom] = f"invalid: {e}"
            logger.error("Template for %s is invalid: %s", dom, e)
            continue
        # Cache the successfully compiled template so get_template() skips disk
        _template_cache[dom] = tmpl
        status[dom] = "ok"
    return status
