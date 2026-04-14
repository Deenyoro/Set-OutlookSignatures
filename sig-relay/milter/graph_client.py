"""
graph_client.py — Microsoft Graph API client with Redis caching.

Fetches user profile attributes from Entra ID for signature template rendering.
Caches results in Redis to avoid hammering Graph API on every email.
"""

import json
import logging
import os
import time
from typing import Optional

import msal
import redis
import requests

logger = logging.getLogger("kawasig.graph")


class GraphClient:
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    SCOPE = ["https://graph.microsoft.com/.default"]

    SELECT_FIELDS = (
        "displayName,jobTitle,businessPhones,mail,mobilePhone,"
        "department,officeLocation,city,state,postalCode,"
        "streetAddress,country,companyName,surname,givenName,"
        "userPrincipalName"
    )

    # Circuit breaker: if Graph fails repeatedly, stop hitting it for a cooldown
    # window so we don't lock up every inbound message on a 10s timeout.
    CB_FAIL_THRESHOLD = 5      # consecutive failures before opening the breaker
    CB_COOLDOWN_SECONDS = 60   # how long we stay short-circuited

    def __init__(self):
        self.tenant_id = os.environ["TENANT_ID"]
        self.client_id = os.environ["CLIENT_ID"]
        self.client_secret = os.environ["CLIENT_SECRET"]
        self.managed_domains = [
            d.strip().lower()
            for d in os.environ.get("MANAGED_DOMAINS", "").split(",")
            if d.strip()
        ]
        self.cache_ttl = int(os.environ.get("REDIS_CACHE_TTL", "3600"))

        # Lazy-init: do not contact Microsoft on construction. Some deployments
        # start the milter with placeholder creds; we want it to come up and just
        # log auth failures per-message rather than crashloop at boot.
        self._msal = None
        self._msal_init_error: Optional[str] = None

        # Circuit-breaker state
        self._cb_failures = 0
        self._cb_open_until = 0.0

        try:
            self._redis = redis.Redis(
                host=os.environ.get("REDIS_HOST", "redis"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=5,
            )
            self._redis.ping()
            self._redis_ok = True
            logger.info("Redis connected: %s:%s",
                        os.environ.get("REDIS_HOST"), os.environ.get("REDIS_PORT"))
        except Exception as e:
            logger.warning("Redis unavailable, running without cache: %s", e)
            self._redis_ok = False

        logger.info(
            "GraphClient ready - tenant=%s, domains=%s, cache_ttl=%ds",
            self.tenant_id, self.managed_domains, self.cache_ttl,
        )

    def _ensure_msal(self):
        if self._msal is not None:
            return
        try:
            self._msal = msal.ConfidentialClientApplication(
                self.client_id,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
                client_credential=self.client_secret,
                validate_authority=False,
            )
        except Exception as e:
            self._msal_init_error = str(e)
            raise

    def _get_token(self) -> str:
        self._ensure_msal()
        result = self._msal.acquire_token_for_client(scopes=self.SCOPE)
        if "access_token" not in result:
            err = result.get("error_description", str(result))
            raise RuntimeError(f"Token acquisition failed: {err}")
        return result["access_token"]

    def get_sender_domain(self, email: str) -> Optional[str]:
        """Extract domain and check if it's managed. Returns domain or None."""
        email = email.strip().lower()
        parts = email.split("@")
        if len(parts) != 2:
            return None
        domain = parts[1]
        if domain in self.managed_domains:
            return domain
        return None

    def _graph_get(self, email: str, token: str):
        """Single HTTP GET against Graph /users/{email}. Returns the response
        or raises. Separated so retry wrapper can call it."""
        return requests.get(
            f"{self.GRAPH_BASE}/users/{email}",
            params={"$select": self.SELECT_FIELDS},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    def _cb_is_open(self) -> bool:
        return time.monotonic() < self._cb_open_until

    def _cb_record_success(self) -> None:
        if self._cb_failures:
            logger.info("Graph circuit-breaker: recovered after %d failures",
                        self._cb_failures)
        self._cb_failures = 0
        self._cb_open_until = 0.0

    def _cb_record_failure(self) -> None:
        self._cb_failures += 1
        if self._cb_failures >= self.CB_FAIL_THRESHOLD and not self._cb_is_open():
            self._cb_open_until = time.monotonic() + self.CB_COOLDOWN_SECONDS
            logger.error("Graph circuit-breaker OPEN after %d consecutive "
                         "failures — short-circuiting for %ds",
                         self._cb_failures, self.CB_COOLDOWN_SECONDS)

    def get_user_profile(self, email: str) -> Optional[dict]:
        """
        Fetch user profile from Graph API (with Redis cache + transient retry
        + circuit breaker). Returns flat dict matching Jinja2 variable names,
        or None.
        """
        email = email.strip().lower()

        domain = self.get_sender_domain(email)
        if not domain:
            logger.debug("Not a managed domain: %s", email)
            return None

        cache_key = f"kawasig:profile:{email}"
        if self._redis_ok:
            try:
                cached = self._redis.get(cache_key)
                if cached:
                    logger.debug("Cache HIT: %s", email)
                    return json.loads(cached)
            except redis.RedisError as e:
                logger.warning("Redis read error: %s", e)

        # If the breaker is tripped, skip the Graph call entirely — mail still
        # passes through, but we don't waste the 10s request timeout per msg.
        if self._cb_is_open():
            logger.debug("Graph circuit-breaker open — skipping fetch for %s", email)
            return None

        try:
            token = self._get_token()
            resp = self._graph_get(email, token)
        except Exception as e:
            # Network / auth init failure. Never blocks mail — returning None
            # makes the milter pass the message through unchanged.
            logger.error("Graph request failed for %s: %s", email, e)
            self._cb_record_failure()
            return None

        # One-shot retry on transient errors. Short delays keep us well under
        # the milter 300s timeout even on multiple concurrent in-flight msgs.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if resp.status_code == 429:
                try:
                    retry_after = min(float(resp.headers.get("Retry-After", "1")), 3.0)
                except ValueError:
                    retry_after = 1.0
            else:
                retry_after = 1.0
            logger.info("Graph %s for %s — retrying once in %.1fs",
                        resp.status_code, email, retry_after)
            time.sleep(retry_after)
            try:
                resp = self._graph_get(email, token)
            except Exception as e:
                logger.error("Graph retry failed for %s: %s", email, e)
                self._cb_record_failure()
                return None

        status = resp.status_code

        if status == 200:
            try:
                data = resp.json()
            except ValueError:
                logger.error("Graph returned 200 but non-JSON body for %s", email)
                self._cb_record_failure()
                return None
            self._cb_record_success()

        elif status == 404:
            # User genuinely not in Entra — not a Graph problem.
            logger.warning("Graph: user not found (%s) — passing through", email)
            self._cb_record_success()
            return None

        elif status in (401, 403):
            # Bad app creds or missing permission grants. Loud error so it
            # shows up in monitoring. Counts as a failure — if nothing else
            # is working, we should stop hammering.
            logger.error("Graph %s for %s: %s — check CLIENT_SECRET and admin "
                         "consent for User.Read.All / MailboxSettings.ReadWrite",
                         status, email, resp.text[:200])
            self._cb_record_failure()
            return None

        elif status == 429:
            # Rate limited even after retry. Count as a failure so the breaker
            # opens if we keep getting throttled.
            retry_after = resp.headers.get("Retry-After", "?")
            logger.warning("Graph 429 rate-limit for %s (Retry-After=%s) — "
                           "passing through", email, retry_after)
            self._cb_record_failure()
            return None

        elif 500 <= status < 600:
            logger.warning("Graph %s transient error for %s — passing through",
                           status, email)
            self._cb_record_failure()
            return None

        else:
            logger.error("Graph %s unexpected for %s: %s",
                         status, email, resp.text[:200])
            self._cb_record_failure()
            return None

        phones = data.get("businessPhones") or []
        profile = {
            "display_name": data.get("displayName") or "",
            "job_title": data.get("jobTitle") or "",
            "phone": phones[0] if phones else (data.get("mobilePhone") or ""),
            "email": data.get("mail") or email,
            "mobile": data.get("mobilePhone") or "",
            "department": data.get("department") or "",
            "office": data.get("officeLocation") or "",
            "city": data.get("city") or "",
            "state": data.get("state") or "",
            "postal_code": data.get("postalCode") or "",
            "street": data.get("streetAddress") or "",
            "country": data.get("country") or "",
            "company": data.get("companyName") or "",
            "surname": data.get("surname") or "",
            "given_name": data.get("givenName") or "",
        }

        if self._redis_ok:
            try:
                self._redis.setex(cache_key, self.cache_ttl, json.dumps(profile))
                logger.debug("Cached: %s (TTL=%ds)", email, self.cache_ttl)
            except redis.RedisError as e:
                logger.warning("Redis write error: %s", e)

        return profile
