"""
sig_milter.py — Postfix milter for server-side email signature injection.

1. Receives emails from Postfix
2. Checks if the sender is in a managed domain
3. Checks if the signature marker already exists
4. If missing: fetches profile, renders template, injects at reply boundary
5. Passes the (possibly modified) message back to Postfix for delivery

A milter crash NEVER blocks mail delivery
(Postfix milter_default_action=accept handles this).
"""

import logging
import os
import re
import sys
from io import BytesIO

import Milter

from graph_client import GraphClient
from template_renderer import render_signature, preload_and_validate
from message_processor import process_message

logging.basicConfig(
    level=os.environ.get("DEPLOY_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kawasig.milter")

MANAGED_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("MANAGED_DOMAINS", "").split(",")
    if d.strip()
]
SIG_MARKER_PREFIX = os.environ.get("SIG_MARKER_PREFIX", "KAWASIG")

graph = GraphClient()


class SigMilter(Milter.Base):
    """Milter that inspects outbound email and injects missing signatures."""

    def __init__(self):
        self.id = Milter.uniqueID()
        self.sender = None
        self.sender_domain = None
        self.recipients = []
        self.fp = BytesIO()
        self.should_process = False
        # Header counters for chgheader() removal on inject (DKIM breaks when
        # we modify the body; receivers will DKIM-fail, so strip the now-stale
        # signature and let DMARC fall back to SPF alignment).
        self._dkim_count = 0
        self._already_processed = False

    def envfrom(self, mailfrom, *args):
        self.sender = mailfrom.strip().strip("<>").lower()
        self.recipients = []
        self.fp = BytesIO()
        self.should_process = False

        parts = self.sender.split("@")
        if len(parts) == 2 and parts[1] in MANAGED_DOMAINS:
            self.sender_domain = parts[1]
            self.should_process = True
            logger.debug("[%d] Will process: %s", self.id, self.sender)
        else:
            logger.debug("[%d] Skipping: %s", self.id, self.sender)

        return Milter.CONTINUE

    def envrcpt(self, to, *args):
        self.recipients.append(to.strip().strip("<>"))
        return Milter.CONTINUE

    @Milter.noreply
    def header(self, name, value):
        lname = name.lower()
        if lname == "dkim-signature":
            self._dkim_count += 1
        elif lname == "x-kawasig-processed":
            self._already_processed = True
        self.fp.write(f"{name}: {value}\r\n".encode("utf-8", errors="replace"))
        return Milter.CONTINUE

    @Milter.noreply
    def eoh(self):
        self.fp.write(b"\r\n")
        return Milter.CONTINUE

    @Milter.noreply
    def body(self, chunk):
        self.fp.write(chunk)
        return Milter.CONTINUE

    def eom(self):
        if not self.should_process:
            return Milter.ACCEPT

        # Loop defense: if the message already carries our processed marker,
        # don't re-inject. Just ACCEPT (marker still present from upstream).
        if self._already_processed:
            logger.info("[%d] Skipping (already X-KawaSig-Processed): %s",
                        self.id, self.sender)
            return Milter.ACCEPT

        try:
            return self._process_message()
        except Exception as e:
            logger.error("[%d] Processing error for %s: %s",
                         self.id, self.sender, e, exc_info=True)
            return Milter.ACCEPT

    def _process_message(self):
        self.fp.seek(0)
        raw = self.fp.read()

        result = process_message(
            raw=raw,
            sender=self.sender,
            sender_domain=self.sender_domain,
            sig_marker_prefix=SIG_MARKER_PREFIX,
            profile_lookup=graph.get_user_profile,
            template_renderer=render_signature,
        )

        # Always stamp our own header on managed-sender mail so a re-routed
        # message can't loop back through us via the Exchange connector rule
        # (the rule must include "Except if X-KawaSig-Processed is set").
        self.addheader("X-KawaSig-Processed", self.sender_domain)

        if result.action == "passthrough":
            logger.info("[%d] Passthrough (%s): %s",
                        self.id, result.reason, self.sender)
            return Milter.ACCEPT

        if result.action == "modified":
            # Skip the duplicate X-KawaSig-Processed already in headers_to_add
            for name, value in result.headers_to_add:
                if name == "X-KawaSig-Processed":
                    continue
                self.addheader(name, value)

            # Body is about to change — any inbound DKIM-Signature becomes
            # invalid. chgheader with empty value removes each one (postfix
            # milter protocol). Remove in REVERSE order so the 1-based index
            # of earlier headers isn't shifted by prior deletions.
            for idx in range(self._dkim_count, 0, -1):
                try:
                    self.chgheader("DKIM-Signature", idx, "")
                except Exception as e:
                    logger.warning("[%d] chgheader DKIM-Signature #%d failed: %s",
                                   self.id, idx, e)
            if self._dkim_count:
                logger.info("[%d] Stripped %d inbound DKIM-Signature header(s)",
                            self.id, self._dkim_count)

            self.replacebody(result.body)
            logger.info("[%d] Signature injected: %s -> %s",
                        self.id, self.sender, ", ".join(self.recipients[:3]))
            return Milter.ACCEPT

        logger.debug("[%d] Action=%s reason=%s", self.id, result.action, result.reason)
        return Milter.ACCEPT


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)


def _validate_env() -> list[str]:
    """Return a list of config problems (empty = good)."""
    problems: list[str] = []
    if not MANAGED_DOMAINS:
        problems.append("MANAGED_DOMAINS is empty — set it in .env (comma-separated)")
    for dom in MANAGED_DOMAINS:
        if not _DOMAIN_RE.match(dom):
            problems.append(f"MANAGED_DOMAINS entry not a valid domain: {dom!r}")

    tenant = os.environ.get("TENANT_ID", "")
    if not (_UUID_RE.match(tenant) or _DOMAIN_RE.match(tenant)):
        problems.append(f"TENANT_ID must be a UUID or tenant domain, got {tenant!r}")

    client = os.environ.get("CLIENT_ID", "")
    if not _UUID_RE.match(client):
        problems.append(f"CLIENT_ID must be a UUID, got {client!r}")

    if not os.environ.get("CLIENT_SECRET"):
        problems.append("CLIENT_SECRET is empty")

    return problems


def main():
    socket_path = "/var/spool/postfix/milter/sig-milter.sock"

    logger.info("=" * 60)
    logger.info("KawaSig milter starting")
    logger.info("Socket: %s", socket_path)
    logger.info("Domains: %s", MANAGED_DOMAINS)
    logger.info("Marker prefix: %s", SIG_MARKER_PREFIX)
    logger.info("=" * 60)

    # ---- config validation ----
    problems = _validate_env()
    for p in problems:
        logger.warning("CONFIG: %s", p)

    # ---- template validation ----
    tmpl_status = preload_and_validate(MANAGED_DOMAINS)
    for dom, st in tmpl_status.items():
        if st == "ok":
            logger.info("Template check: %s -> ok", dom)
        else:
            logger.warning("Template check: %s -> %s", dom, st)

    if problems or any(v != "ok" for v in tmpl_status.values()):
        logger.warning("Milter will start anyway — messages whose config is broken "
                       "will pass through unmodified (mail flow is never blocked).")

    # Postfix smtpd runs as the unprivileged 'postfix' user. Make any sockets
    # we create world-writable so postfix can connect.
    os.umask(0o000)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    Milter.factory = SigMilter
    Milter.set_flags(Milter.CHGBODY | Milter.ADDHDRS)
    Milter.runmilter("sig-milter", socket_path, timeout=300)

    logger.info("KawaSig milter stopped")


if __name__ == "__main__":
    main()
