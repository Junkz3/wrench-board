"""Helper partagé de vérification du service-token (parsing Bearer + compare
constant-time). Réutilisé par le guard WS (ws_security) ET le middleware HTTP
(http_security)."""

from api._token_check import extract_bearer, token_matches


def test_extract_bearer_valid():
    assert extract_bearer("Bearer abc123") == "abc123"


def test_extract_bearer_wrong_scheme():
    assert extract_bearer("Basic abc123") is None
    assert extract_bearer("Token abc123") is None


def test_extract_bearer_missing_or_empty():
    assert extract_bearer(None) is None
    assert extract_bearer("") is None
    assert extract_bearer("Bearer ") is None   # scheme mais token vide
    assert extract_bearer("Bearer") is None     # pas de séparateur


def test_token_matches_true():
    assert token_matches("secret", "secret") is True


def test_token_matches_false():
    assert token_matches("secret", "other") is False
    assert token_matches(None, "secret") is False
    assert token_matches("", "secret") is False
