"""Vérification du service-token — parsing Bearer + comparaison constant-time.

Source unique partagée par ws_security (guard WS) et http_security (middleware
HTTP) pour ne pas dupliquer la logique de comparaison de secret. Le parsing
reproduit EXACTEMENT le comportement historique du guard WS (str.partition sur
le premier espace) pour ne rien régresser.
"""

from __future__ import annotations

import secrets


def extract_bearer(header: str | None) -> str | None:
    """'Authorization: Bearer <token>' → <token>, sinon None.

    Utilise partition(' ') comme le guard WS historique : scheme=avant le 1er
    espace, presented=après. Retourne None si le scheme n'est pas 'Bearer' ou
    si le token est vide.
    """
    scheme, _, presented = (header or "").partition(" ")
    if scheme == "Bearer" and presented:
        return presented
    return None


def token_matches(presented: str | None, expected: str) -> bool:
    """Comparaison constant-time. False si presented est vide/None."""
    return bool(presented) and secrets.compare_digest(presented, expected)
