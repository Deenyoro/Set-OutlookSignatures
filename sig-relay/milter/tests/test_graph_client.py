"""
Unit tests for graph_client.GraphClient.

MSAL + requests + redis are stubbed directly on the client instance so the
tests run without network, without Redis, and without real Entra credentials.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _env():
    os.environ.setdefault("TENANT_ID", "tenant-guid")
    os.environ.setdefault("CLIENT_ID", "client-guid")
    os.environ.setdefault("CLIENT_SECRET", "secret")
    os.environ["MANAGED_DOMAINS"] = "example.com,other.com"
    os.environ.setdefault("REDIS_CACHE_TTL", "3600")
    yield


@pytest.fixture
def client_factory():
    """Yields a function that constructs a GraphClient with Redis + MSAL stubbed."""

    from graph_client import GraphClient

    def _make(*, redis_ok=False, msal_token="tok", msal_error=None):
        # Build without triggering real Redis / MSAL
        with patch.object(GraphClient, "__init__", lambda self: None):
            c = GraphClient()
        c.tenant_id = "tenant-guid"
        c.client_id = "client-guid"
        c.client_secret = "secret"
        c.managed_domains = ["example.com", "other.com"]
        c.cache_ttl = 3600
        c._msal_init_error = None
        c._cb_failures = 0
        c._cb_open_until = 0.0

        # MSAL stub — pre-set so _ensure_msal() is a no-op
        msal = MagicMock()
        if msal_error is not None:
            msal.acquire_token_for_client.return_value = msal_error
        else:
            msal.acquire_token_for_client.return_value = {"access_token": msal_token}
        c._msal = msal

        # Redis stub
        c._redis = MagicMock()
        c._redis_ok = redis_ok
        return c

    return _make


def _response(status, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = json.dumps(body) if isinstance(body, dict) else (body or "")
    r.json = MagicMock(return_value=body if isinstance(body, dict) else {})
    return r


# ---------- get_sender_domain ----------

def test_sender_domain_managed(client_factory):
    c = client_factory()
    assert c.get_sender_domain("Alice@Example.COM") == "example.com"


def test_sender_domain_unmanaged(client_factory):
    c = client_factory()
    assert c.get_sender_domain("stranger@elsewhere.org") is None


def test_sender_domain_malformed(client_factory):
    c = client_factory()
    assert c.get_sender_domain("not-an-email") is None
    assert c.get_sender_domain("") is None


# ---------- get_user_profile: status codes ----------

def test_profile_200_success(client_factory):
    c = client_factory()
    body = {
        "displayName": "Alice A",
        "jobTitle": "Engineer",
        "businessPhones": ["+1 5550100"],
        "mail": "alice@example.com",
        "mobilePhone": "",
        "department": "Eng",
        "officeLocation": "HQ",
        "city": "Pgh", "state": "PA", "postalCode": "15213",
        "streetAddress": "1 Main", "country": "US",
        "companyName": "Example Co",
        "givenName": "Alice", "surname": "A",
    }
    with patch("graph_client.requests.get", return_value=_response(200, body)):
        p = c.get_user_profile("alice@example.com")
    assert p is not None
    assert p["display_name"] == "Alice A"
    assert p["phone"] == "+1 5550100"
    assert p["city"] == "Pgh"
    assert p["company"] == "Example Co"


def test_profile_200_phone_falls_back_to_mobile(client_factory):
    c = client_factory()
    body = {"businessPhones": [], "mobilePhone": "+1 5550999", "mail": "a@example.com"}
    with patch("graph_client.requests.get", return_value=_response(200, body)):
        p = c.get_user_profile("a@example.com")
    assert p["phone"] == "+1 5550999"


def test_profile_404_user_not_found_returns_none(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(404)):
        with caplog.at_level("WARNING", logger="kawasig.graph"):
            assert c.get_user_profile("ghost@example.com") is None
    assert any("not found" in r.message.lower() for r in caplog.records)


def test_profile_401_bad_creds_loud_error(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(401, "bad creds")):
        with caplog.at_level("ERROR", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    msgs = [r.message for r in caplog.records]
    assert any("CLIENT_SECRET" in m for m in msgs), msgs


def test_profile_403_loud_error(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(403)):
        with caplog.at_level("ERROR", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_profile_429_rate_limit_warning(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get",
               return_value=_response(429, headers={"Retry-After": "30"})):
        with caplog.at_level("WARNING", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    msgs = [r.message for r in caplog.records]
    assert any("429" in m for m in msgs), msgs


def test_profile_500_transient_warning(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(500)):
        with caplog.at_level("WARNING", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    assert any("transient" in r.message.lower() for r in caplog.records)


def test_profile_unknown_status_logged(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(418, "teapot")):
        with caplog.at_level("ERROR", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    assert any("418" in r.message for r in caplog.records)


# ---------- short-circuit paths ----------

def test_profile_unmanaged_domain_skips_graph(client_factory):
    c = client_factory()
    with patch("graph_client.requests.get") as get:
        assert c.get_user_profile("stranger@elsewhere.org") is None
    assert not get.called


def test_profile_network_exception_returns_none(client_factory, caplog):
    c = client_factory()
    with patch("graph_client.requests.get", side_effect=OSError("network dead")):
        with caplog.at_level("ERROR", logger="kawasig.graph"):
            assert c.get_user_profile("alice@example.com") is None
    assert any("request failed" in r.message.lower() for r in caplog.records)


# ---------- cache interaction ----------

def test_profile_cache_hit_skips_graph(client_factory):
    c = client_factory(redis_ok=True)
    cached = {"display_name": "Cached", "email": "alice@example.com"}
    c._redis.get.return_value = json.dumps(cached)
    with patch("graph_client.requests.get") as get:
        p = c.get_user_profile("alice@example.com")
    assert p == cached
    assert not get.called


def test_profile_cache_miss_fetches_and_caches(client_factory):
    c = client_factory(redis_ok=True)
    c._redis.get.return_value = None  # cache miss
    body = {"displayName": "A", "mail": "a@example.com"}
    with patch("graph_client.requests.get", return_value=_response(200, body)):
        p = c.get_user_profile("a@example.com")
    assert p is not None
    assert c._redis.setex.called
    # first positional arg is the cache key
    key = c._redis.setex.call_args.args[0]
    assert "kawasig:profile:a@example.com" == key


def test_profile_transient_failure_does_not_cache(client_factory):
    """If Graph returns 429 or 5xx, we must not cache a None — next request
    should hit Graph again (in case the problem is transient)."""
    c = client_factory(redis_ok=True)
    c._redis.get.return_value = None
    with patch("graph_client.requests.get", return_value=_response(500)):
        c.get_user_profile("alice@example.com")
    assert not c._redis.setex.called


# ---- retry on transient ----

def test_retry_on_429_then_success(client_factory):
    """First request returns 429, second succeeds — retry path should yield
    a valid profile."""
    c = client_factory()
    body = {"displayName": "Alice", "mail": "alice@example.com"}
    responses = [_response(429, headers={"Retry-After": "0"}),
                 _response(200, body)]
    with patch("graph_client.requests.get", side_effect=responses), \
         patch("graph_client.time.sleep"):  # skip actual sleep in tests
        p = c.get_user_profile("alice@example.com")
    assert p is not None
    assert p["display_name"] == "Alice"


def test_retry_on_503_then_success(client_factory):
    c = client_factory()
    body = {"displayName": "A", "mail": "a@example.com"}
    responses = [_response(503), _response(200, body)]
    with patch("graph_client.requests.get", side_effect=responses), \
         patch("graph_client.time.sleep"):
        p = c.get_user_profile("a@example.com")
    assert p is not None


def test_retry_still_fails_returns_none(client_factory):
    c = client_factory()
    responses = [_response(429), _response(429)]
    with patch("graph_client.requests.get", side_effect=responses), \
         patch("graph_client.time.sleep"):
        assert c.get_user_profile("alice@example.com") is None


def test_retry_after_header_is_capped(client_factory):
    """Retry-After of 60s should be capped at 3s so we don't blow the milter
    timeout if Graph is flaky."""
    c = client_factory()
    responses = [_response(429, headers={"Retry-After": "60"}),
                 _response(200, {"displayName": "A", "mail": "a@example.com"})]
    with patch("graph_client.requests.get", side_effect=responses) as req, \
         patch("graph_client.time.sleep") as sleep:
        c.get_user_profile("alice@example.com")
    sleep.assert_called_once()
    waited = sleep.call_args.args[0]
    assert waited <= 3.0, f"waited {waited}s — should be capped to 3"


def test_non_transient_status_not_retried(client_factory):
    """404 is a real 'user not found' — no retry, no noise."""
    c = client_factory()
    with patch("graph_client.requests.get", return_value=_response(404)) as get:
        c.get_user_profile("ghost@example.com")
    assert get.call_count == 1


# ---- circuit breaker ----

def test_breaker_opens_after_n_failures(client_factory):
    """After CB_FAIL_THRESHOLD consecutive 5xx failures, the breaker opens
    and subsequent calls short-circuit without touching the network."""
    c = client_factory()
    n = c.CB_FAIL_THRESHOLD
    responses = [_response(500) for _ in range(n * 2)]  # retry counts, so x2
    with patch("graph_client.requests.get", side_effect=responses), \
         patch("graph_client.time.sleep"):
        for _ in range(n):
            assert c.get_user_profile("alice@example.com") is None
    # Breaker should now be OPEN
    assert c._cb_is_open()
    # Next call must not make any HTTP request
    with patch("graph_client.requests.get") as get:
        assert c.get_user_profile("alice@example.com") is None
        assert not get.called, "breaker-open should short-circuit the request"


def test_breaker_success_resets_counter(client_factory):
    """A single successful 200 resets the failure counter and closes the breaker."""
    c = client_factory()
    # 4 failures (below threshold of 5) followed by a 200
    body = {"displayName": "Alice", "mail": "alice@example.com"}
    responses = [_response(500), _response(500),   # req 1 fails (retry still 500)
                 _response(500), _response(500),   # req 2 fails
                 _response(500), _response(500),   # req 3 fails
                 _response(500), _response(500),   # req 4 fails
                 _response(200, body),              # req 5 succeeds first try
                 ]
    with patch("graph_client.requests.get", side_effect=responses), \
         patch("graph_client.time.sleep"):
        for _ in range(4):
            c.get_user_profile("alice@example.com")
        assert c._cb_failures == 4
        # Success resets
        assert c.get_user_profile("alice@example.com") is not None
        assert c._cb_failures == 0
        assert not c._cb_is_open()


def test_breaker_404_counts_as_success_for_counter(client_factory):
    """404 = user not found != Graph is broken. Breaker stays closed."""
    c = client_factory()
    # Seed with some failures so we can observe the reset
    c._cb_failures = 3
    with patch("graph_client.requests.get", return_value=_response(404)):
        c.get_user_profile("ghost@example.com")
    assert c._cb_failures == 0


# ---------- token failures ----------

def test_token_acquisition_failure_surfaces_loudly(client_factory, caplog):
    c = client_factory(msal_error={"error": "invalid_client",
                                   "error_description": "AADSTS7000215 fake"})
    with caplog.at_level("ERROR", logger="kawasig.graph"):
        result = c.get_user_profile("alice@example.com")
    assert result is None
    assert any("failed" in r.message.lower() for r in caplog.records)
