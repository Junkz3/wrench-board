#!/usr/bin/env python3
"""Mesure le coût réel d'un build schématique→graphe (CPU/RAM vs LLM).

But : trancher la question « faut-il réécrire le parser en Rust ? » et « quel
pic RAM un build consomme-t-il sur le VPS ? » avec des CHIFFRES, pas des estimations.

Deux modes :

  --mode parse-only   (DÉFAUT, GRATUIT — aucun appel LLM)
      Rasterise les pages (pdftoppm) + extrait le grounding (pdfplumber) pour
      TOUTES les pages. C'est la moitié CPU/RAM du build, AVANT le LLM, et c'est
      là que se trouve le pic RAM (rasterisation 200 DPI + buffers PNG). Donne
      directement la réponse « le VPS tient-il ? » sans dépenser un centime.

  --mode full         (PAYANT — ~7-19 $ sur un schéma 12 pages en Opus)
      Build complet via ingest_schematic, sous cProfile + échantillonnage RAM.
      Donne le split précis temps-de-parsing vs temps-LLM (tottime cProfile =
      CPU réel brûlé dans le parsing ; le reste du wall-clock = attente réseau LLM).

L'échantillonneur RAM lit /proc (stdlib pur, pas de psutil) et somme la RSS de
TOUT l'arbre de processus (Python + sous-process pdftoppm) → pic réaliste.

Le build écrit dans un memory_root TEMPORAIRE (jamais le vrai `memory/`).

Usage :
    .venv/bin/python scripts/measure_build.py                       # parse-only, PDF démo
    .venv/bin/python scripts/measure_build.py --pdf chemin.pdf
    .venv/bin/python scripts/measure_build.py --mode full           # build réel (payant)
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import logging
import os
import pstats
import shutil
import tempfile
import threading
import time
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
DEFAULT_PDF = "board_assets/mnt-reform-motherboard.pdf"


# ---------------------------------------------------------------------------
# Échantillonneur RAM — somme la RSS de l'arbre de processus via /proc.
# pdftoppm est un SOUS-PROCESS : resource.getrusage(self) ne le verrait pas,
# d'où le parcours de l'arbre. Échantillonnage 50 ms → capture les pics courts.
# ---------------------------------------------------------------------------
class RssSampler(threading.Thread):
    def __init__(self, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.peak_bytes = 0
        self.timeline: list[tuple[float, int]] = []  # (t_relative_s, bytes)
        self._stop_evt = threading.Event()
        self._root_pid = os.getpid()
        self._t0 = time.monotonic()

    @staticmethod
    def _ppid(pid: int) -> int | None:
        # /proc/<pid>/stat : "pid (comm) state ppid ...". comm peut contenir
        # espaces/parenthèses → on coupe après le DERNIER ')'.
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
            rparen = data.rfind(")")
            fields = data[rparen + 2 :].split()
            return int(fields[1])  # ppid (state est fields[0])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _rss_bytes(pid: int) -> int:
        try:
            with open(f"/proc/{pid}/statm") as f:
                resident_pages = int(f.read().split()[1])
            return resident_pages * PAGE_SIZE
        except (OSError, ValueError, IndexError):
            return 0

    def _tree_rss(self) -> int:
        # Construit l'ensemble des descendants du PID racine (+ lui-même).
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return 0
        parent_of = {pid: self._ppid(pid) for pid in pids}
        tree = {self._root_pid}
        changed = True
        while changed:
            changed = False
            for pid, ppid in parent_of.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    changed = True
        return sum(self._rss_bytes(pid) for pid in tree)

    def run(self) -> None:
        while not self._stop_evt.is_set():
            total = self._tree_rss()
            if total > self.peak_bytes:
                self.peak_bytes = total
            self.timeline.append((time.monotonic() - self._t0, total))
            self._stop_evt.wait(self.interval)

    def stop(self) -> int:
        self._stop_evt.set()
        self.join(timeout=2.0)
        return self.peak_bytes


def _mb(b: int) -> str:
    return f"{b / 1024 / 1024:.1f} Mo"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def run_parse_only(pdf_path: Path, dpi: int) -> dict:
    """Rasterisation + grounding pour toutes les pages. GRATUIT (zéro LLM)."""
    from api.pipeline.schematic.grounding import (
        extract_grounding,
        format_grounding_for_prompt,
    )
    from api.pipeline.schematic.renderer import render_pages

    out = {}
    with tempfile.TemporaryDirectory(prefix="measure_parse_") as tmp:
        tmp_dir = Path(tmp)

        t0 = time.monotonic()
        pages = render_pages(pdf_path, tmp_dir, dpi=dpi)
        t_render = time.monotonic() - t0
        out["pages"] = len(pages)
        out["t_render_s"] = t_render

        t0 = time.monotonic()
        for page in pages:
            g = extract_grounding(pdf_path, page.page_number)
            _ = format_grounding_for_prompt(g)
        out["t_grounding_s"] = time.monotonic() - t0

        # Taille des PNG produits (proxy de la pression mémoire de la vision).
        png_bytes = sum(p.stat().st_size for p in tmp_dir.glob("*.png"))
        out["png_total_bytes"] = png_bytes
    return out


def run_full(pdf_path: Path, slug: str, dpi: int) -> tuple[dict, str]:
    """Build complet ingest_schematic sous cProfile. PAYANT (~7-19 $)."""
    from anthropic import AsyncAnthropic

    from api.config import get_settings
    from api.pipeline.schematic.orchestrator import ingest_schematic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY manquante dans .env")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    out: dict = {}
    tmp_memory = Path(tempfile.mkdtemp(prefix="measure_build_mem_"))
    profiler = cProfile.Profile()
    t0 = time.monotonic()
    try:
        profiler.enable()
        graph = asyncio.run(
            ingest_schematic(
                device_slug=slug,
                pdf_path=pdf_path,
                client=client,
                memory_root=tmp_memory,
                render_dpi=dpi,
            )
        )
        profiler.disable()
        out["wall_s"] = time.monotonic() - t0
        out["components"] = len(getattr(graph, "components", []) or [])
        out["nets"] = len(getattr(graph, "nets", []) or [])
    finally:
        shutil.rmtree(tmp_memory, ignore_errors=True)

    # Top par tottime (CPU brûlé DANS la fonction, hors sous-appels) → ce qui
    # est candidat à un rewrite Rust. Et par cumtime (inclut l'attente LLM).
    s = io.StringIO()
    st = pstats.Stats(profiler, stream=s)
    s.write("\n--- TOP 25 par tottime (CPU pur — candidats Rust) ---\n")
    st.sort_stats("tottime").print_stats(25)
    s.write("\n--- TOP 15 par cumtime (inclut attente LLM/réseau) ---\n")
    st.sort_stats("cumulative").print_stats(15)
    return out, s.getvalue()


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", default=DEFAULT_PDF, help=f"PDF à builder (défaut {DEFAULT_PDF})")
    ap.add_argument("--slug", default="measure-tmp", help="device slug (mode full)")
    ap.add_argument("--dpi", type=int, default=200, help="DPI de rasterisation (défaut 200)")
    ap.add_argument("--mode", choices=["parse-only", "full"], default="parse-only",
                    help="parse-only=GRATUIT (défaut) ; full=build complet PAYANT (~7-19 $)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.is_file():
        raise SystemExit(f"PDF introuvable : {pdf_path}")

    if args.mode == "full":
        print("\n⚠️  MODE FULL — build réel avec appels LLM Opus.")
        print("    Coût estimé ~7-19 $ sur un schéma 12 pages. Ctrl-C pour annuler.\n")
        time.sleep(3)

    sampler = RssSampler()
    sampler.start()
    profile_text = ""
    try:
        if args.mode == "parse-only":
            result = run_parse_only(pdf_path, args.dpi)
        else:
            result, profile_text = run_full(pdf_path, args.slug, args.dpi)
    finally:
        peak = sampler.stop()

    print("\n" + "=" * 64)
    print(f" RÉSULTAT — mode {args.mode}")
    print("=" * 64)
    print(f" PDF                : {pdf_path.name}  ({pdf_path.stat().st_size/1024/1024:.1f} Mo)")
    print(f" PIC RAM (arbre)    : {_mb(peak)}   ← chiffre clé pour le VPS")
    for k, v in result.items():
        if k.endswith("_bytes"):
            print(f" {k:18}: {_mb(int(v))}")
        elif k.endswith("_s"):
            print(f" {k:18}: {v:.2f} s")
        else:
            print(f" {k:18}: {v}")
    if profile_text:
        print(profile_text)
    print("=" * 64)
    print(" Lecture : pic RAM = ce qu'un build prend dans les ~3 Go de marge VPS.")
    if args.mode == "parse-only":
        print(" parse-only ne couvre PAS les buffers vision ; mais le pic RAM brut")
        print(" (rasterisation) est ici. png_total = ordre de grandeur des buffers LLM.")
    else:
        print(" tottime élevé sur compile/merge/grounding = candidat Rust ; sinon")
        print(" le wall-clock est dans l'attente LLM (cumtime) → Rust ne sert à rien.")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()
