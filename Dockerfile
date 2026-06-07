# wrench-board (moteur de diagnostic) — image de prod.
# Service PRIVÉ : aucun port publié (cf. docker-compose.prod.yml du cloud) ; seul
# le cloud l'atteint sur le réseau Docker interne. Durci par ENGINE_SERVICE_TOKEN.
#
# poppler-utils = dépendance SYSTÈME requise : le pipeline rasterise les PDF de
# schéma via `pdftoppm` (api/pipeline/schematic/renderer.py).
FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# Deps d'abord (couche cache) : copie le manifeste, installe, PUIS le code.
COPY pyproject.toml README.md ./
COPY api/ ./api/
RUN pip install --no-cache-dir -e .

# Reste du runtime : l'UI web réutilisée par le cloud, les boards démo, le start
# script, et managed_ids.json (mode Managed Agents → pas de bootstrap au boot).
COPY web/ ./web/
COPY board_assets/ ./board_assets/
COPY scripts/ ./scripts/
COPY managed_ids.json ./

# memory/ est un VOLUME monté par le compose (/app/memory) → persistance des
# packs/cache (le moat). On ne bake AUCUNE donnée tenant dans l'image.
# Pas de `ports:` côté compose ; on écoute sur 0.0.0.0 (réseau interne seul).
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
