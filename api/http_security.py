"""Gate HTTP du service-token (mode managé). Miroir HTTP de ws_security.

No-op quand ENGINE_SERVICE_TOKEN est vide (self-host : la home UI + l'API sont
accessibles en direct, comme aujourd'hui). Quand le token est set, toute requête
HTTP hors /health (et hors préflight OPTIONS) doit porter
'Authorization: Bearer <token>' sinon 403.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api._token_check import extract_bearer, token_matches

# Chemins joignables SANS token même en mode managé (sondes infra).
# Match EXACT, sensible à la casse, sans slash final (request.url.path est le
# chemin ASGI brut, non normalisé — '/health/' ou '/Health' ne matchent pas).
_PUBLIC_PATHS = frozenset({"/health"})


class ServiceTokenMiddleware(BaseHTTPMiddleware):
    """Exige Authorization: Bearer <expected_token> sur toute requête HTTP
    quand expected_token est non vide. /health + OPTIONS restent ouverts.
    Token vide → no-op (self-host)."""

    def __init__(self, app, *, expected_token: str):
        super().__init__(app)
        self._expected = expected_token or ""

    async def dispatch(self, request: Request, call_next):
        if not self._expected:
            return await call_next(request)  # self-host : no-op
        # OPTIONS = préflight CORS, jamais de payload, géré par CORSMiddleware.
        if request.url.path in _PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        presented = extract_bearer(request.headers.get("authorization"))
        if token_matches(presented, self._expected):
            return await call_next(request)
        return JSONResponse({"detail": "service token required"}, status_code=403)


def should_warn_unprotected(*, token: str, host: str, env: str) -> bool:
    """True si le moteur tourne dans un contexte prod-like SANS token (donc
    ouvert). Heuristique (filet, pas garantie) : token vide ET (bind 0.0.0.0
    OU env de type production). Le self-host légitime sur 127.0.0.1 en dev ne
    déclenche pas."""
    if token:
        return False
    prodlike = host == "0.0.0.0" or env.strip().lower() in {"production", "prod"}
    return prodlike


def should_fail_unprotected(*, token: str, env: str) -> bool:
    """True si le moteur doit REFUSER de booter : contexte EXPLICITEMENT
    production (ENV in {production, prod}) ET aucun service-token → le moteur
    serait ouvert à tout Internet. Plus strict que should_warn_unprotected : on
    ne se base PAS sur host=0.0.0.0 (un docker self-host légitime bind 0.0.0.0),
    uniquement sur ENV prod explicite — symétrie avec le fail-fast cloud
    (assertSecureProductionConfig)."""
    if token:
        return False
    return env.strip().lower() in {"production", "prod"}
