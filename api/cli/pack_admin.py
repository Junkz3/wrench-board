"""CLI opérateur wrench-board — gestion du pack partagé T8 (Option C).

Exécution engine-side via SSH (pas d'exposition réseau) :

    python -m api.cli.pack_admin <commande> [options]

V1 Option C : invalidation + inspection.
  - revoke          : retire une expansion (ou un fact) de promoted/ + journal
  - list-expansions : liste le journal (filtrable par statut)
  - show-expansion  : affiche le détail JSON d'une entrée du journal
  - list-affected-by: heuristique cascade-revoke (expansions plus récentes)
  - promote         : STUB V2 — n'existe pas en Option C (rc=3, expliqué)

Pourquoi pas de promote : en Option C les expansions vont directement dans
promoted/ (pas de staging tenant-local). Il n'y a donc rien à "promouvoir".
Utilise 'revoke' pour invalider une expansion. La promote tenant-staging est
reportée en V2.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from api.pipeline.pack_storage import (
    JournalEntry,
    read_journal,
    revoke_expansion,
    revoke_fact,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_serializable(obj):
    """Convertit les datetime en isoformat pour json.dumps."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _entry_to_dict(entry: JournalEntry) -> dict:
    """Dataclass → dict sérialisable."""
    return asdict(entry)


def _count_facts(entry: JournalEntry) -> int:
    """Nombre de facts déclarés dans delta_summary (heuristique affichage)."""
    ds = entry.delta_summary or {}
    total = 0
    for v in ds.values():
        if isinstance(v, list):
            total += len(v)
        elif isinstance(v, int):
            total += v
    return total


def _find_entry(memory_root: Path, slug: str, expansion_id: str) -> JournalEntry | None:
    return next((e for e in read_journal(memory_root, slug) if e.id == expansion_id), None)


# ---------------------------------------------------------------------------
# Sous-commandes
# ---------------------------------------------------------------------------


def _cmd_revoke(args: argparse.Namespace) -> int:
    memory_root = Path(args.memory_root)
    slug = args.slug
    reason = getattr(args, "reason", None)

    try:
        if args.expansion:
            revoke_expansion(memory_root, slug, expansion_id=args.expansion, reason=reason)
            print(f"OK: expansion {args.expansion!r} révoquée du slug {slug!r}")
        else:
            revoke_fact(memory_root, slug, fact_id=args.fact_id, reason=reason)
            print(f"OK: fact {args.fact_id!r} révoqué du slug {slug!r}")
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _cmd_list_expansions(args: argparse.Namespace) -> int:
    memory_root = Path(args.memory_root)
    slug = args.slug
    status_filter = getattr(args, "status", None)

    entries = list(read_journal(memory_root, slug))
    if status_filter:
        entries = [e for e in entries if e.status == status_filter]

    if not entries:
        print("(aucune entrée)")
        return 0

    # Ligne d'en-tête
    print(f"{'ID':<20}  {'ts':24}  {'owner':20}  {'status':10}  {'facts':>5}")
    print("-" * 85)
    for e in entries:
        ts_str = e.ts.isoformat() if isinstance(e.ts, datetime) else str(e.ts)
        owner_str = e.owner_ref or "anon"
        n_facts = _count_facts(e)
        print(f"{e.id:<20}  {ts_str:<24}  {owner_str:<20}  {e.status:<10}  {n_facts:>5}")

    return 0


def _cmd_show_expansion(args: argparse.Namespace) -> int:
    memory_root = Path(args.memory_root)
    slug = args.slug
    expansion_id = args.expansion

    entry = _find_entry(memory_root, slug, expansion_id)
    if entry is None:
        print(f"ERROR: expansion {expansion_id!r} introuvable dans le journal de {slug!r}", file=sys.stderr)
        return 2

    data = _entry_to_dict(entry)
    print(json.dumps(data, indent=2, ensure_ascii=False, default=_json_serializable))
    return 0


def _cmd_list_affected_by(args: argparse.Namespace) -> int:
    """Heuristique cascade-revoke : liste les expansions plus récentes dont
    delta_summary contient des champs 'modified_*' non-vides.

    Utilité : aide l'opérateur à décider quelles expansions révoquer en cascade
    après avoir révoqué la cible (si elles ont enrichi des facts qu'elle avait
    posé). L'heuristique est volontairement prudente (faux positifs OK, faux
    négatifs non) : toute expansion postérieure avec une section modified_* est
    signalée.
    """
    memory_root = Path(args.memory_root)
    slug = args.slug
    expansion_id = args.expansion

    target = _find_entry(memory_root, slug, expansion_id)
    if target is None:
        print(f"ERROR: expansion {expansion_id!r} introuvable dans le journal de {slug!r}", file=sys.stderr)
        return 2

    target_ts = target.ts

    affected: list[JournalEntry] = []
    for e in read_journal(memory_root, slug):
        if e.id == expansion_id:
            continue
        # Plus récente que la cible
        try:
            if e.ts <= target_ts:
                continue
        except TypeError:
            continue
        # Contient des modified_* non-vides
        ds = e.delta_summary or {}
        has_modifications = any(
            k.startswith("modified_") and v
            for k, v in ds.items()
        )
        if has_modifications:
            affected.append(e)

    if not affected:
        print(f"(aucune expansion affectée par {expansion_id!r} selon l'heuristique)")
        return 0

    print(f"Expansions potentiellement affectées par la révocation de {expansion_id!r} :")
    print(f"{'ID':<20}  {'ts':24}  {'owner':20}  {'status':10}")
    print("-" * 80)
    for e in affected:
        ts_str = e.ts.isoformat() if isinstance(e.ts, datetime) else str(e.ts)
        owner_str = e.owner_ref or "anon"
        print(f"{e.id:<20}  {ts_str:<24}  {owner_str:<20}  {e.status:<10}")

    return 0


def _cmd_promote(_args: argparse.Namespace) -> int:
    """STUB V2 — promote n'est pas une opération V1 en Option C.

    En Option C, les expansions vont directement dans promoted/ sans staging
    tenant-local intermédiaire. Il n'y a donc rien à "promouvoir".

    La commande existe pour satisfaire le contrat CLI (un opérateur qui
    tente 'promote' reçoit un message clair, pas un 'command not found').
    Le staging tenant-local + promote est reporté en V2.
    """
    print(
        "NOT_SUPPORTED_V1: la promotion suppose le staging tenant-local (reporté V2). "
        "En Option C les expansions vont directement dans promoted/ ; "
        "utilise 'revoke' pour invalider.",
        file=sys.stderr,
    )
    return 3


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_admin",
        description="CLI opérateur wrench-board — gestion du pack partagé T8 (Option C).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- revoke ----
    p_revoke = sub.add_parser("revoke", help="Révoque une expansion ou un fact de promoted/")
    p_revoke.add_argument("--memory-root", required=True, metavar="PATH",
                          help="Racine du répertoire memory/ (ex : /data/wrench-board/memory)")
    p_revoke.add_argument("--slug", required=True, metavar="SLUG",
                          help="Slug du device (ex : iphone-12)")
    grp = p_revoke.add_mutually_exclusive_group(required=True)
    grp.add_argument("--expansion", metavar="E", default=None,
                     help="ID de l'expansion à révoquer (ex : E-abc123)")
    grp.add_argument("--fact-id", metavar="F", default=None,
                     help="ID du fact à révoquer (ex : F-cmp-a1b2c3d4)")
    p_revoke.add_argument("--reason", metavar="REASON", default=None,
                          help="Motif de révocation (libre, tracé dans le journal)")

    # ---- list-expansions ----
    p_list = sub.add_parser("list-expansions", help="Liste les entrées du journal d'expansions")
    p_list.add_argument("--memory-root", required=True, metavar="PATH")
    p_list.add_argument("--slug", required=True, metavar="SLUG")
    p_list.add_argument("--status", metavar="STATUS",
                        choices=["staged", "promoted", "revoked", "baseline"],
                        default=None, help="Filtre par statut")

    # ---- show-expansion ----
    p_show = sub.add_parser("show-expansion", help="Affiche le détail JSON d'une entrée du journal")
    p_show.add_argument("--memory-root", required=True, metavar="PATH")
    p_show.add_argument("--slug", required=True, metavar="SLUG")
    p_show.add_argument("--expansion", required=True, metavar="E",
                        help="ID de l'expansion")

    # ---- list-affected-by ----
    p_aff = sub.add_parser("list-affected-by",
                           help="Heuristique cascade-revoke : expansions postérieures avec modified_*")
    p_aff.add_argument("--memory-root", required=True, metavar="PATH")
    p_aff.add_argument("--slug", required=True, metavar="SLUG")
    p_aff.add_argument("--expansion", required=True, metavar="E",
                       help="ID de l'expansion cible")

    # ---- promote (stub V2) ----
    p_promo = sub.add_parser(
        "promote",
        help="STUB V2 — non disponible en Option C (rc=3). Utilise 'revoke' pour invalider.",
    )
    p_promo.add_argument("--memory-root", required=True, metavar="PATH")
    p_promo.add_argument("--slug", required=True, metavar="SLUG")
    grp_p = p_promo.add_mutually_exclusive_group(required=True)
    grp_p.add_argument("--expansion", metavar="E", default=None)
    grp_p.add_argument("--fact-id", metavar="F", default=None)

    return parser


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch CLI. Retourne le code de retour entier (0 = succès).

    argv=None → sys.argv[1:] (comportement argparse standard).
    argv=[...] → utilisé par les tests (pas de manipulation de sys.argv).
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1

    dispatch = {
        "revoke": _cmd_revoke,
        "list-expansions": _cmd_list_expansions,
        "show-expansion": _cmd_show_expansion,
        "list-affected-by": _cmd_list_affected_by,
        "promote": _cmd_promote,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        print(f"ERROR: commande inconnue {args.command!r}", file=sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
