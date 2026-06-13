#!/usr/bin/env python3
"""Benchmark CHAQUE parser de board sur un échantillon de fichiers RÉELS.

Complète scan_board_corpus.py (couverture) avec les axes qui décident d'un
rewrite Rust : pic RAM par parse, temps p50/p95/max, débit Mo/s, et détection
de hang (timeout SIGALRM = vecteur DoS sur input non fiable).

Pourquoi double-parse par fichier : tracemalloc (mesure RAM Python fiable car
ces parsers sont Python pur) ralentit ~3-5× → on chronomètre une 1ʳᵉ passe SANS
tracemalloc (temps réel), puis une 2ᵉ passe AVEC pour le pic RAM. Échantillon
borné → coût acceptable.

Usage :
    WB_BOARD_CORPUS="$HOME/Documents/Boardview XZZ:$HOME/Documents/XZZ Laptop" \
        .venv/bin/python scripts/bench_board_corpus.py --per-format 120
    .venv/bin/python scripts/bench_board_corpus.py "/chemin/corpus" --per-format 200
"""
from __future__ import annotations

import argparse
import collections
import logging
import os
import random
import signal
import sys
import time
import tracemalloc
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
logging.disable(logging.CRITICAL)

SUPPORTED_EXT = {
    ".pcb", ".cad", ".fz", ".tvw", ".brd", ".bv", ".bvr",
    ".asc", ".bdv", ".cst", ".f2b", ".gr", ".kicad_pcb",
}
# Formats à parsing BINAIRE (candidats Rust potentiels) — pour annoter le tableau.
BINARY_EXT = {".pcb", ".fz", ".tvw", ".bdv", ".bv", ".bvr", ".brd"}


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


def _find_files(roots: list[str]) -> list[str]:
    out: list[str] = []
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXT:
                    out.append(os.path.join(dirpath, f))
    return out


def main() -> int:
    from api.board.parser import parser_for

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="dossiers du corpus (défaut: $WB_BOARD_CORPUS)")
    ap.add_argument("--per-format", type=int, default=120, help="échantillon max par format (défaut 120)")
    ap.add_argument("--timeout", type=int, default=45, help="timeout par fichier en s (défaut 45)")
    ap.add_argument("--seed", type=int, default=1234, help="graine d'échantillonnage (reproductible)")
    ap.add_argument("--only", default="", help="ne tester que ces extensions, ex: .fz,.pcb")
    args = ap.parse_args()
    only = {e if e.startswith(".") else f".{e}" for e in args.only.split(",") if e.strip()}

    roots = args.roots or [d for d in os.environ.get("WB_BOARD_CORPUS", "").split(os.pathsep) if d]
    roots = [r for r in roots if r]
    if not roots:
        print("ERROR: aucun corpus. Passe des dossiers ou définis WB_BOARD_CORPUS.", file=sys.stderr)
        return 2
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        print(f"ERROR: dossiers introuvables: {missing}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    files = _find_files(roots)
    by_ext: dict[str, list[str]] = collections.defaultdict(list)
    for p in files:
        by_ext[os.path.splitext(p)[1].lower()].append(p)

    # Échantillon borné aléatoire par format.
    sample: dict[str, list[str]] = {}
    for ext, lst in by_ext.items():
        if only and ext not in only:
            continue
        rng.shuffle(lst)
        sample[ext] = lst[: args.per_format]

    total_sampled = sum(len(v) for v in sample.values())
    print(f"Corpus: {len(files)} fichiers / {len(by_ext)} formats")
    print(f"Échantillon: {total_sampled} fichiers (≤{args.per_format}/format, seed={args.seed}, timeout={args.timeout}s)\n")
    for ext in sorted(sample):
        print(f"  {ext:12s} {len(by_ext[ext]):5d} dispo → {len(sample[ext]):4d} testés")
    print()

    signal.signal(signal.SIGALRM, _alarm)
    stats: dict[str, dict] = {}

    hdr = (f"{'EXT':6s} {'bin':>3s} {'OK':>4s} {'FAIL':>4s} {'HANG':>4s} {'PASS%':>6s} "
           f"{'p50ms':>8s} {'p95ms':>9s} {'maxms':>9s} {'Mo/s':>6s} "
           f"{'ramP95':>8s} {'ramMax':>8s} {'parts':>7s} {'pins':>8s}")

    def _row(ext: str, s: dict) -> str:
        total = s["ok"] + s["fail"] + s["hang"]
        pct = 100.0 * s["ok"] / total if total else 0
        t = sorted(s["times_ms"]); r = sorted(s["peak_kb"])
        p50, p95, mx = _pct(t, .50), _pct(t, .95), (t[-1] if t else 0)
        mbps = 0.0
        if s["times_ms"] and s["sizes"]:
            per = [(sz/1e6)/(ms/1000) for sz, ms in zip(s["sizes"], s["times_ms"]) if ms > 0]
            mbps = sorted(per)[len(per)//2] if per else 0.0
        ram95 = _pct(r, .95)/1024; rammax = (r[-1]/1024 if r else 0)
        binflag = "B" if ext in BINARY_EXT else "·"
        return (f"{ext:6s} {binflag:>3s} {s['ok']:4d} {s['fail']:4d} {s['hang']:4d} {pct:6.1f} "
                f"{p50:8.1f} {p95:9.1f} {mx:9.1f} {mbps:6.1f} "
                f"{ram95:7.1f}M {rammax:7.1f}M {s['parts']:7d} {s['pins']:8d}")

    print("=" * 110); print(hdr); print("-" * 110)
    for ext in sorted(sample):
        s = dict(ok=0, fail=0, hang=0, parts=0, pins=0, nets=0,
                 times_ms=[], peak_kb=[], sizes=[], errors=[])
        n_ext = len(sample[ext])
        for j, path in enumerate(sample[ext], 1):
            ppath = Path(path)
            try:
                size = ppath.stat().st_size
            except OSError:
                continue
            try:
                parser_inst = parser_for(ppath)
            except Exception as e:
                s["fail"] += 1
                s["errors"].append(f"dispatch {type(e).__name__}: {e}")
                continue

            # --- Passe 1 : temps réel (sans tracemalloc) + correction ---
            signal.alarm(args.timeout)
            try:
                t0 = time.perf_counter()
                board = parser_inst.parse_file(ppath)
                dt = (time.perf_counter() - t0) * 1000
                signal.alarm(0)
            except _Timeout:
                s["hang"] += 1
                s["errors"].append(f"TIMEOUT >{args.timeout}s (taille {size//1024} Ko)")
                continue
            except Exception as e:
                signal.alarm(0)
                s["fail"] += 1
                s["errors"].append(f"{type(e).__name__}: {e}")
                continue

            s["ok"] += 1
            s["times_ms"].append(dt)
            s["sizes"].append(size)
            s["parts"] += len(board.parts)
            s["pins"] += len(board.pins)
            s["nets"] += len(board.nets)

            # --- Passe 2 : pic RAM Python (tracemalloc) sur le même fichier ---
            signal.alarm(args.timeout)
            try:
                tracemalloc.start()
                tracemalloc.reset_peak()
                parser_for(ppath).parse_file(ppath)
                _, peak = tracemalloc.get_traced_memory()
                s["peak_kb"].append(peak / 1024)
            except Exception:
                pass
            finally:
                tracemalloc.stop()
                signal.alarm(0)

            if j % 10 == 0:
                print(f"   [{ext}] {j}/{n_ext}  (ok={s['ok']} fail={s['fail']} hang={s['hang']})",
                      file=sys.stderr, flush=True)
        stats[ext] = s
        # Émet la ligne DÈS que le format est fini → résultats partiels sauvés
        # même si le run est interrompu plus tard (formats lents en dernier).
        print(_row(ext, s), flush=True)

    # ---- Légende ----
    print("=" * 110)
    print(" bin=B : parsing binaire (candidat Rust potentiel). ramP95/Max = pic RAM Python/parse (tracemalloc).")
    print(" Lecture Rust : un format lent (maxms élevé, Mo/s faible) ET/OU gourmand (ramMax élevé) = candidat.")
    print(" Un format rapide+léger reste en Python. HANG>0 = robustesse à corriger (vecteur DoS upload).\n")

    print("=== Échantillons d'erreurs (top 4 distinctes/format) ===")
    for ext, s in sorted(stats.items()):
        if not s["errors"]:
            continue
        kinds: dict[str, int] = collections.defaultdict(int)
        for msg in s["errors"]:
            kinds[msg.split(":", 1)[0][:40]] += 1
        print(f"\n--- {ext} ({len(s['errors'])} échecs/{s['ok']+s['fail']+s['hang']}) ---")
        for kind, n in sorted(kinds.items(), key=lambda x: -x[1])[:4]:
            print(f"  [{n:3d}×] {kind}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
