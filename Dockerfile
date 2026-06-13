# wrench-board (moteur de diagnostic) — image de prod.
# Service PRIVÉ : aucun port publié (cf. docker-compose.prod.yml du cloud) ; seul
# le cloud l'atteint sur le réseau Docker interne. Durci par ENGINE_SERVICE_TOKEN.
#
# poppler-utils = dépendance SYSTÈME requise : le pipeline rasterise les PDF de
# schéma via `pdftoppm` (api/pipeline/schematic/renderer.py).
# ghostscript = fallback de réparation : certains PDF (bibliothèque XZZ) ont des
# objets non-standard illisibles par pdfminer ; `gs` les re-distille avant le
# rendu (sinon : 0 page → pack vide). Voir renderer.ensure_renderable_pdf.

# === Stage builder : compile les accélérateurs PyO3 (rust/*) en wheels ==========
# Les crates `wb_fz_cipher` / `wb_tvw_walker` accélèrent les hot-loops des parsers
# boardview (cipher FZ-xor ~×8, walk/scan TVW ~×2,6). Construits ici une fois, en
# wheels, pour garder le runtime SLIM (aucun toolchain Rust dans l'image finale).
# Même base Python 3.11 que le runtime → ABI cp311 compatible.
FROM python:3.11-slim AS rust-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install --no-cache-dir maturin
WORKDIR /build
COPY rust/ ./rust/
RUN mkdir -p /wheels \
    && for d in rust/*/ ; do (cd "$d" && maturin build --release --out /wheels) ; done

# === Stage runtime ==============================================================
FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils ghostscript \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# Deps d'abord (couche cache) : copie le manifeste, installe, PUIS le code.
COPY pyproject.toml README.md ./
COPY api/ ./api/
RUN pip install --no-cache-dir -e .

# Accélérateurs Rust (PyO3) installés depuis les wheels du stage builder. OPTIONNELS
# par conception : si on retire cette couche, le moteur retombe sur le fallback
# Python pur (cf. _fz_engine/cipher.py + _tvw_engine/walker.py) — il fonctionne,
# juste plus lentement. Aucune dépendance dure ajoutée pour un self-hoster.
COPY --from=rust-builder /wheels/ /wheels/
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

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
