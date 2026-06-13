#!/usr/bin/env python3
"""cProfile CHAQUE parser de board sur quelques fichiers réels.

Pinpointe la/les fonction(s) chaude(s) par format (tri tottime = CPU brûlé DANS
la fonction, hors sous-appels). C'est la carte pour décider, par parser :
  - hot-loop Python pathologique → optimisable en Python (numpy/bytes.translate)
  - ou calcul intrinsèquement lourd → candidat rewrite Rust/PyO3.

Charge .env pour les clés de déchiffrement (FZ/XZZ) si présent.

Usage :
    WB_BOARD_CORPUS="$HOME/Documents/Boardview XZZ:$HOME/Documents/XZZ Laptop" \
        .venv/bin/python scripts/profile_parsers.py --files 3
    ... --only .fz,.pcb --files 3 --top 15
"""
from __future__ import annotations

import argparse
import cProfile
import io
import logging
import os
import pstats
import random
import sys
import time
from pathlib import Path

logging.disable(logging.CRITICAL)

SUPPORTED_EXT = {".pcb", ".cad", ".fz", ".tvw", ".brd", ".bv", ".bvr",
                 ".asc", ".bdv", ".cst", ".f2b", ".gr"}


def _load_env(repo: Path) -> None:
    # Exporte les KEY=val simples du .env dans os.environ (les parsers lisent
    # WRENCH_BOARD_FZ_KEY / _XZZ_KEY via os.environ directement).
    env = repo / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip()


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _load_env(repo)
    from api.board.parser import parser_for

    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*")
    ap.add_argument("--files", type=int, default=3, help="fichiers profilés par format")
    ap.add_argument("--only", default="", help="ex: .fz,.pcb")
    ap.add_argument("--top", type=int, default=12, help="fonctions chaudes à afficher")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    only = {e if e.startswith(".") else f".{e}" for e in args.only.split(",") if e.strip()}
    roots = args.roots or [d for d in os.environ.get("WB_BOARD_CORPUS", "").split(os.pathsep) if d]
    roots = [r for r in roots if r and os.path.isdir(r)]
    if not roots:
        print("ERROR: aucun corpus (WB_BOARD_CORPUS).", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    by_ext: dict[str, list[str]] = {}
    for root in roots:
        for dp, _, fs in os.walk(root):
            for f in fs:
                ext = os.path.splitext(f)[1].lower()
                if ext in SUPPORTED_EXT and (not only or ext in only):
                    by_ext.setdefault(ext, []).append(os.path.join(dp, f))

    for ext in sorted(by_ext):
        files = by_ext[ext]
        rng.shuffle(files)
        sample = files[: args.files]
        prof = cProfile.Profile()
        ok = 0
        total_bytes = 0
        t0 = time.perf_counter()
        for path in sample:
            p = Path(path)
            try:
                total_bytes += p.stat().st_size
                prof.enable()
                parser_for(p).parse_file(p)
                prof.disable()
                ok += 1
            except Exception as e:
                prof.disable()
                print(f"   {ext}: échec {os.path.basename(path)[:50]} → {type(e).__name__}: {str(e)[:60]}",
                      file=sys.stderr)
        wall = time.perf_counter() - t0

        print("\n" + "=" * 80)
        mbps = (total_bytes / 1e6 / wall) if wall else 0
        print(f" {ext}  — {ok}/{len(sample)} ok, {total_bytes//1024} Ko en {wall:.2f}s ({mbps:.2f} Mo/s)")
        print("-" * 80)
        if ok == 0:
            print(" (aucun parse réussi — clé manquante ? voir stderr)")
            continue
        s = io.StringIO()
        pstats.Stats(prof, stream=s).sort_stats("tottime").print_stats(args.top)
        # Garde uniquement les lignes de fonctions (après l'entête pstats).
        lines = s.getvalue().splitlines()
        started = False
        for ln in lines:
            if "ncalls" in ln and "tottime" in ln:
                started = True
            if started and ln.strip():
                print(" " + ln)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
