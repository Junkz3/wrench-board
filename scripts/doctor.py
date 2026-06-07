"""Local health check for a wrench-board install.

Runs a battery of fast, offline checks on the local install and emits a
structured report on stdout. Intended for the workshop : the technician
runs `make doctor` (or `python scripts/doctor.py`) and gets a 10-second
answer about what works and what is broken in the environment.

No network, no Anthropic API calls — everything is filesystem +
import-level. Exit code 1 if any CRITICAL check fails, 0 otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Result type + status constants
# ---------------------------------------------------------------------------

CheckResult = namedtuple("CheckResult", ["status", "name", "message", "details"])

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_INFO = "INFO"

_ANSI = {
    STATUS_OK: "\033[32m",  # green
    STATUS_WARN: "\033[33m",  # yellow
    STATUS_FAIL: "\033[31m",  # red
    STATUS_INFO: "\033[36m",  # cyan
}
_ANSI_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_env_file(repo_root: Path) -> CheckResult:
    """`.env` exists at repo root and ANTHROPIC_API_KEY is non-empty.

    Reads the file directly rather than going through `api.config` because
    the latter falls back to an empty default for tests, which would mask a
    real misconfiguration in the workshop install.
    """
    env_path = repo_root / ".env"
    if not env_path.exists():
        return CheckResult(STATUS_FAIL, "env_file", ".env missing at repo root", {"path": str(env_path)})

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(STATUS_FAIL, "env_file", f".env unreadable: {exc}", {"path": str(env_path)})

    key_value: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("ANTHROPIC_API_KEY="):
            key_value = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    if key_value is None:
        return CheckResult(STATUS_FAIL, "env_file", "ANTHROPIC_API_KEY not set in .env", {"path": str(env_path)})
    if not key_value:
        return CheckResult(STATUS_FAIL, "env_file", "ANTHROPIC_API_KEY is empty", {"path": str(env_path)})

    masked = key_value[:8] + "..." if len(key_value) > 8 else "***"
    return CheckResult(STATUS_OK, "env_file", f"ANTHROPIC_API_KEY set ({masked})", {"path": str(env_path)})


def check_managed_ids(repo_root: Path) -> CheckResult:
    """`managed_ids.json` is present and well-formed (env + 3 tier agents)."""
    path = repo_root / "managed_ids.json"
    if not path.exists():
        return CheckResult(
            STATUS_WARN,
            "managed_ids",
            "managed_ids.json missing — Managed Agents disabled (DIAGNOSTIC_MODE=direct works)",
            {"path": str(path)},
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(STATUS_FAIL, "managed_ids", f"managed_ids.json malformed: {exc}", {"path": str(path)})

    if not isinstance(data, dict):
        return CheckResult(STATUS_FAIL, "managed_ids", "managed_ids.json is not a JSON object", {"path": str(path)})

    env_id = data.get("environment_id")
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    required_tiers = ("fast", "normal", "deep")
    missing_tiers = [t for t in required_tiers if not isinstance(agents.get(t), dict) or not agents[t].get("id")]

    if not env_id:
        return CheckResult(STATUS_FAIL, "managed_ids", "managed_ids.json missing environment_id", {"path": str(path)})
    if missing_tiers:
        return CheckResult(
            STATUS_FAIL,
            "managed_ids",
            f"managed_ids.json missing tier agent ids: {', '.join(missing_tiers)}",
            {"path": str(path), "missing_tiers": missing_tiers},
        )

    return CheckResult(
        STATUS_OK,
        "managed_ids",
        f"environment + {len(required_tiers)} tier agents (fast/normal/deep)",
        {"environment_id": env_id, "tiers": list(required_tiers)},
    )


def _list_pack_slugs(memory_root: Path) -> list[str]:
    if not memory_root.exists():
        return []
    slugs: list[str] = []
    for entry in sorted(memory_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        slugs.append(entry.name)
    return slugs


def check_memory_root(memory_root: Path) -> CheckResult:
    """`memory/` exists and lists at least one device pack."""
    if not memory_root.exists():
        return CheckResult(
            STATUS_WARN,
            "memory_root",
            f"memory/ missing at {memory_root} — no packs available",
            {"path": str(memory_root), "slugs": []},
        )

    slugs = _list_pack_slugs(memory_root)
    if not slugs:
        return CheckResult(
            STATUS_WARN,
            "memory_root",
            "memory/ has no device packs",
            {"path": str(memory_root), "slugs": []},
        )

    sample = slugs[:5]
    extra = "" if len(slugs) <= 5 else f" (+{len(slugs) - 5} more)"
    return CheckResult(
        STATUS_OK,
        "memory_root",
        f"{len(slugs)} pack(s): {', '.join(sample)}{extra}",
        {"path": str(memory_root), "slugs": slugs},
    )


def check_pack_health(memory_root: Path, slug: str) -> CheckResult:
    """For a single device slug, audit the pack JSON artefacts."""
    pack_dir = memory_root / slug
    name = f"pack:{slug}"

    if not pack_dir.is_dir():
        return CheckResult(STATUS_FAIL, name, "pack directory missing", {"slug": slug, "path": str(pack_dir)})

    required = {
        "registry.json": STATUS_FAIL,
        "knowledge_graph.json": STATUS_WARN,
        "rules.json": STATUS_WARN,
        "dictionary.json": STATUS_WARN,
        "audit_verdict.json": STATUS_WARN,
    }
    optional_schematic = ("schematic_graph.json", "electrical_graph.json", "simulator_reliability.json")

    present: list[str] = []
    missing: dict[str, str] = {}
    worst = STATUS_OK

    for filename, severity in required.items():
        if (pack_dir / filename).is_file():
            present.append(filename)
        else:
            missing[filename] = severity
            if severity == STATUS_FAIL:
                worst = STATUS_FAIL
            elif severity == STATUS_WARN and worst == STATUS_OK:
                worst = STATUS_WARN

    schematic_present = [f for f in optional_schematic if (pack_dir / f).is_file()]
    reliability_score: float | None = None
    sim_path = pack_dir / "simulator_reliability.json"
    if sim_path.is_file():
        try:
            sim_data = json.loads(sim_path.read_text(encoding="utf-8"))
            score = sim_data.get("score")
            if isinstance(score, (int, float)):
                reliability_score = float(score)
        except (OSError, json.JSONDecodeError):
            reliability_score = None

    parts: list[str] = []
    parts.append(f"{len(present)}/{len(required)} core artefacts")
    if missing:
        parts.append(f"missing: {', '.join(missing.keys())}")
    if schematic_present:
        parts.append(f"schematic: {', '.join(schematic_present)}")
    if reliability_score is not None:
        parts.append(f"sim_reliability={reliability_score:.3f}")

    return CheckResult(
        worst,
        name,
        "; ".join(parts),
        {
            "slug": slug,
            "present": present,
            "missing": missing,
            "schematic_present": schematic_present,
            "reliability_score": reliability_score,
        },
    )


def check_board_assets(repo_root: Path, max_files: int = 5) -> CheckResult:
    """Enumerate `board_assets/` and probe the parser dispatcher.

    Tries to import `api.board.parser.base.parser_for` lazily so the
    doctor script remains usable even if `api/` won't import (we'll
    surface that via `python_env` instead).
    """
    assets_dir = repo_root / "board_assets"
    name = "board_assets"

    if not assets_dir.exists():
        return CheckResult(STATUS_WARN, name, "board_assets/ missing", {"path": str(assets_dir), "files": []})

    interesting_exts = {".brd", ".kicad_pcb", ".pdf"}
    files = sorted(
        p for p in assets_dir.iterdir() if p.is_file() and p.suffix.lower() in interesting_exts
    )

    if not files:
        return CheckResult(STATUS_WARN, name, "no .brd / .kicad_pcb / .pdf files found", {"path": str(assets_dir), "files": []})

    try:
        from api.board.parser import (
            base as parser_base,  # noqa: WPS433 (local import is intentional)
        )
    except Exception as exc:  # pragma: no cover — surfaced via python_env check
        return CheckResult(
            STATUS_WARN,
            name,
            f"{len(files)} file(s) found, parser unavailable ({exc.__class__.__name__})",
            {"path": str(assets_dir), "files": [f.name for f in files], "parser_error": str(exc)},
        )

    probed: list[dict[str, str]] = []
    worst = STATUS_OK
    for path in files[:max_files]:
        if path.suffix.lower() == ".pdf":
            # PDFs are schematic input, not boardview — list, do not parse.
            probed.append({"file": path.name, "status": STATUS_INFO, "note": "schematic pdf"})
            continue
        try:
            parser_base.parser_for(path)
            probed.append({"file": path.name, "status": STATUS_OK, "note": "parser found"})
        except parser_base.UnsupportedFormatError as exc:
            probed.append({"file": path.name, "status": STATUS_WARN, "note": f"unsupported: {exc}"})
            if worst == STATUS_OK:
                worst = STATUS_WARN
        except Exception as exc:  # broad on purpose — anything else is a parser bug
            probed.append({"file": path.name, "status": STATUS_FAIL, "note": f"{exc.__class__.__name__}: {exc}"})
            worst = STATUS_FAIL

    truncated_note = "" if len(files) <= max_files else f" (+{len(files) - max_files} not probed)"
    summary_bits = [f"{p['file']} -> {p['status']}" for p in probed]
    msg = f"{len(files)} file(s){truncated_note}; " + ", ".join(summary_bits)

    return CheckResult(
        worst,
        name,
        msg,
        {"path": str(assets_dir), "files": [f.name for f in files], "probed": probed},
    )


def check_camera() -> CheckResult:
    """List `/dev/video*` for awareness — informational only."""
    dev = Path("/dev")
    if not dev.exists():
        return CheckResult(STATUS_INFO, "camera", "/dev not available on this platform", {"devices": []})

    devices = sorted(p.name for p in dev.glob("video*"))
    if not devices:
        return CheckResult(
            STATUS_INFO,
            "camera",
            "no camera detected — bv_capture_camera tools will be unavailable",
            {"devices": []},
        )
    return CheckResult(
        STATUS_INFO,
        "camera",
        f"found: {', '.join(devices)}",
        {"devices": devices},
    )


def check_python_env(repo_root: Path) -> CheckResult:
    """`.venv/` exists and the critical imports resolve."""
    venv = repo_root / ".venv"
    if not venv.is_dir():
        return CheckResult(
            STATUS_FAIL,
            "python_env",
            ".venv/ missing — run `make install`",
            {"path": str(venv), "missing_imports": []},
        )

    required_modules = ("anthropic", "fastapi", "pydantic", "pdfplumber")
    missing: list[str] = []
    for mod in required_modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        return CheckResult(
            STATUS_FAIL,
            "python_env",
            f"missing imports: {', '.join(missing)} — run `make install`",
            {"path": str(venv), "missing_imports": missing},
        )

    return CheckResult(
        STATUS_OK,
        "python_env",
        f".venv ok; imports ok ({', '.join(required_modules)})",
        {"path": str(venv), "missing_imports": []},
    )


def _human_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fname in files:
            fp = Path(root) / fname
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def check_disk_usage(repo_root: Path, memory_root: Path) -> CheckResult:
    """Footprint of `memory/` and `board_assets/`."""
    mem_size = _dir_size(memory_root)
    assets_size = _dir_size(repo_root / "board_assets")
    msg = f"memory={_human_size(mem_size)}, board_assets={_human_size(assets_size)}"
    return CheckResult(
        STATUS_INFO,
        "disk_usage",
        msg,
        {
            "memory_bytes": mem_size,
            "board_assets_bytes": assets_size,
            "memory_human": _human_size(mem_size),
            "board_assets_human": _human_size(assets_size),
        },
    )


# ---------------------------------------------------------------------------
# Dispatcher + reporting
# ---------------------------------------------------------------------------


def run_all_checks(repo_root: Path = REPO_ROOT, memory_root: Path | None = None) -> list[CheckResult]:
    if memory_root is None:
        memory_root = repo_root / "memory"

    results: list[CheckResult] = []
    results.append(check_env_file(repo_root))
    results.append(check_managed_ids(repo_root))
    results.append(check_python_env(repo_root))

    memory_result = check_memory_root(memory_root)
    results.append(memory_result)
    slugs = memory_result.details.get("slugs", []) if memory_result.details else []
    for slug in slugs[:5]:  # cap pack-level fan-out for huge installs
        results.append(check_pack_health(memory_root, slug))

    results.append(check_board_assets(repo_root))
    results.append(check_camera())
    results.append(check_disk_usage(repo_root, memory_root))
    return results


def _color(status: str, use_color: bool) -> str:
    if not use_color:
        return f"[{status}]"
    return f"{_ANSI[status]}[{status}]{_ANSI_RESET}"


def format_report(results: list[CheckResult], *, use_color: bool) -> str:
    width = max((len(r.name) for r in results), default=0)
    lines: list[str] = []
    for r in results:
        bracket = _color(r.status, use_color)
        # Pad the bracket so colored / plain align the same way visually.
        # `bracket` may contain ANSI escapes; pad based on plain text length.
        plain_bracket = f"[{r.status}]"
        pad = " " * (6 - len(plain_bracket))  # widest is [WARN]/[FAIL]/[INFO] = 6 chars
        lines.append(f"{bracket}{pad} {r.name.ljust(width)}  {r.message}")

    # Summary footer
    counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_INFO: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = (
        f"\nSummary: {counts[STATUS_OK]} ok, {counts[STATUS_WARN]} warn, "
        f"{counts[STATUS_FAIL]} fail, {counts[STATUS_INFO]} info"
    )
    lines.append(summary)
    return "\n".join(lines)


def results_to_json(results: list[CheckResult]) -> str:
    payload = {
        "results": [
            {"status": r.status, "name": r.name, "message": r.message, "details": r.details}
            for r in results
        ],
        "exit_code": exit_code_for(results),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def exit_code_for(results: list[CheckResult]) -> int:
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doctor",
        description="Local health check for a wrench-board install.",
    )
    p.add_argument("--json", action="store_true", help="emit a raw JSON report (machine-readable)")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors even on a TTY")
    p.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="override the memory/ root directory (default: <repo>/memory)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    memory_root = args.memory_root if args.memory_root else (REPO_ROOT / "memory")
    results = run_all_checks(REPO_ROOT, memory_root)

    if args.json:
        sys.stdout.write(results_to_json(results) + "\n")
    else:
        use_color = sys.stdout.isatty() and not args.no_color
        sys.stdout.write(format_report(results, use_color=use_color) + "\n")
    return exit_code_for(results)


if __name__ == "__main__":
    raise SystemExit(main())
