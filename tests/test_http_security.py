"""Gate HTTP du service-token (mode managé) + warn prod-like sans token."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.http_security import (
    ServiceTokenMiddleware,
    should_fail_unprotected,
    should_warn_unprotected,
)


def _app(token: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ServiceTokenMiddleware, expected_token=token)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/pipeline/things")
    async def things():
        return {"ok": True}

    @app.post("/pipeline/things")
    async def create_thing():
        return {"created": True}

    return app


# ---- Mode managé (token set) -------------------------------------------

def test_managed_rejects_without_token():
    c = TestClient(_app("s3cret"))
    r = c.get("/pipeline/things")
    assert r.status_code == 403


def test_managed_accepts_with_correct_bearer():
    c = TestClient(_app("s3cret"))
    r = c.get("/pipeline/things", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_managed_rejects_wrong_token():
    c = TestClient(_app("s3cret"))
    r = c.get("/pipeline/things", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_managed_health_is_public():
    c = TestClient(_app("s3cret"))
    r = c.get("/health")
    assert r.status_code == 200


def test_managed_options_not_blocked():
    """Le préflight CORS (OPTIONS) passe sans token (pas de payload)."""
    c = TestClient(_app("s3cret"))
    r = c.options("/pipeline/things", headers={
        "Origin": "http://x", "Access-Control-Request-Method": "POST",
    })
    assert r.status_code != 403


def test_managed_post_also_gated():
    c = TestClient(_app("s3cret"))
    assert c.post("/pipeline/things").status_code == 403
    assert c.post("/pipeline/things", headers={"Authorization": "Bearer s3cret"}).status_code == 200


# ---- Self-host (token vide) — le test le plus important ---------------

def test_selfhost_passes_everything_when_token_empty():
    c = TestClient(_app(""))
    assert c.get("/pipeline/things").status_code == 200
    assert c.post("/pipeline/things").status_code == 200
    assert c.get("/health").status_code == 200


# ---- Warn prod-like -----------------------------------------------------

def test_warn_when_prodlike_and_no_token():
    assert should_warn_unprotected(token="", host="0.0.0.0", env="production") is True
    assert should_warn_unprotected(token="", host="127.0.0.1", env="production") is True


def test_no_warn_when_token_set():
    assert should_warn_unprotected(token="x", host="0.0.0.0", env="production") is False


def test_no_warn_in_plain_dev():
    assert should_warn_unprotected(token="", host="127.0.0.1", env="") is False


def test_warn_tolerates_whitespace_padded_env():
    # ENV=" production " (mal configuré) doit quand même déclencher le warn.
    assert should_warn_unprotected(token="", host="127.0.0.1", env=" production ") is True


# ---- Fail-fast prod (hard refus de boot, symétrie avec le cloud) --------

def test_fail_when_env_production_and_no_token():
    assert should_fail_unprotected(token="", env="production") is True
    assert should_fail_unprotected(token="", env="prod") is True


def test_fail_tolerates_whitespace_and_case():
    assert should_fail_unprotected(token="", env="  Production ") is True


def test_no_fail_when_token_set_in_production():
    assert should_fail_unprotected(token="s3cret", env="production") is False


def test_no_fail_in_dev_even_without_token():
    # Self-host docker bind 0.0.0.0 mais ENV non-prod → ne doit PAS crasher.
    assert should_fail_unprotected(token="", env="") is False
    assert should_fail_unprotected(token="", env="development") is False
