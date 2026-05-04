"""Per-phase token + cache accounting for the knowledge-factory pipeline.

Each phase (scout, registry, writer_*, auditor, auditor_rev_N) gets a
PhaseTokenStats instance. The tool_call helper records into it on every
Anthropic call; the orchestrator writes the full list to
memory/{slug}/token_stats.json at pipeline end.

CLI: python -m api.pipeline.telemetry.token_stats --slug=<slug>
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class PhaseTokenStats:
    phase: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    duration_s: float = 0.0
    call_count: int = 0

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_input_tokens += cache_read
        self.cache_creation_input_tokens += cache_write
        self.duration_s += duration_s
        self.call_count += 1


def write_token_stats(path: Path, stats: list[PhaseTokenStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"phases": [asdict(s) for s in stats]}
    path.write_text(json.dumps(payload, indent=2))


def read_token_stats(path: Path) -> list[PhaseTokenStats]:
    data = json.loads(path.read_text())
    return [PhaseTokenStats(**entry) for entry in data["phases"]]


def render_table(stats: list[PhaseTokenStats]) -> str:
    headers = ("phase", "calls", "in", "out", "cache_r", "cache_w", "hit%", "sec")
    rows = [headers]
    for s in stats:
        total_in = s.input_tokens + s.cache_read_input_tokens
        hit_pct = (s.cache_read_input_tokens / total_in * 100) if total_in else 0.0
        rows.append((
            s.phase,
            str(s.call_count),
            str(s.input_tokens),
            str(s.output_tokens),
            str(s.cache_read_input_tokens),
            str(s.cache_creation_input_tokens),
            f"{hit_pct:.0f}",
            f"{s.duration_s:.1f}",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    out = []
    for i, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            out.append("  ".join("-" * widths[j] for j in range(len(headers))))
    return "\n".join(out)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render per-phase token stats for a knowledge-pack run.",
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--memory-root", default="memory")
    args = parser.parse_args(argv)
    path = Path(args.memory_root) / args.slug / "token_stats.json"
    if not path.exists():
        print(f"no token_stats.json found at {path}", file=sys.stderr)
        return 1
    stats = read_token_stats(path)
    print(render_table(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
