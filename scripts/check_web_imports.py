#!/usr/bin/env python3
"""Static ESM import validator for the no-build vanilla frontend.

The web/ frontend ships as raw ES modules served byte-for-byte (no bundler,
no package-manager step — see CLAUDE.md). That means a broken import path or a
renamed export is only discovered at runtime in the browser; `node --check`
(syntax only) and `make test` (backend) both stay green. This script closes
that gap with zero dependencies:

  1. Resolves every RELATIVE static + dynamic import specifier against the
     importing file's directory (stripping the `?v=...` cache-bust query that
     ESM keys modules by) and fails if the target file does not exist.
     → catches the import-depth class (e.g. a module moved one level deeper
       and `./x.js` should have become `../../../x.js`).

  2. For every NAMED import from a relative module, checks the imported name is
     actually exported by the target module.
     → catches a removed/renamed export whose consumers were not updated, and
       import-name typos.

What it does NOT catch (be honest): a bare undefined reference — an identifier
*used* but never imported nor declared (the kind of bug that needs real scope
analysis via ESLint `no-undef` or `tsc --checkJs`, which both require a
node_modules toolchain this repo deliberately avoids). Those remain a
browser-verification responsibility.

Exit 0 if every relative import resolves and every named import is exported;
exit 1 with a per-problem report otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

# --- comment stripping (so `import` examples inside comments don't trip us) ---
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")  # (?<!:) keeps http:// in strings intact-ish


def _strip_comments(src: str) -> str:
    src = _BLOCK_COMMENT.sub("", src)
    src = _LINE_COMMENT.sub("", src)
    return src


# --- specifier extraction ----------------------------------------------------
# static:  import ... from 'spec'   |   import 'spec'   |   export ... from 'spec'
# dynamic: import('spec')
_FROM_SPEC = re.compile(r"""\bfrom\s*['"]([^'"]+)['"]""")
_BARE_IMPORT = re.compile(r"""\bimport\s*['"]([^'"]+)['"]""")
_DYNAMIC_IMPORT = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# named import block:  import { a, b as c } from 'spec'   (may span lines)
_NAMED_IMPORT = re.compile(
    r"""\bimport\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""",
    re.DOTALL,
)
# default import:  import Name from 'spec'  (not { } and not * as)
_DEFAULT_IMPORT = re.compile(
    r"""\bimport\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"]([^'"]+)['"]""",
)


def _is_relative(spec: str) -> bool:
    return spec.startswith("./") or spec.startswith("../") or spec.startswith("/")


def _strip_query(spec: str) -> str:
    return spec.split("?", 1)[0].split("#", 1)[0]


def _resolve(importer: Path, spec: str) -> Path:
    clean = _strip_query(spec)
    if clean.startswith("/"):
        # Root-relative — resolve against web/ (how the server serves it).
        return (WEB_ROOT / clean.lstrip("/")).resolve()
    return (importer.parent / clean).resolve()


# --- export extraction (of a target module) ----------------------------------
_EXPORT_DECL = re.compile(
    r"\bexport\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)"
)
_EXPORT_BLOCK = re.compile(r"\bexport\s*\{([^}]*)\}(?!\s*from)")  # not a re-export
_REEXPORT_BLOCK = re.compile(r"\bexport\s*\{([^}]*)\}\s*from\s*['\"][^'\"]+['\"]")
_EXPORT_STAR = re.compile(r"\bexport\s*\*\s*from\b")
_EXPORT_DEFAULT = re.compile(r"\bexport\s+default\b")


def _module_exports(path: Path, _cache: dict[Path, tuple[set[str], bool]]) -> tuple[set[str], bool]:
    """Return (named exports, has_export_star). has_export_star → treat as opaque."""
    if path in _cache:
        return _cache[path]
    try:
        src = _strip_comments(path.read_text(encoding="utf-8"))
    except OSError:
        result = (set(), False)
        _cache[path] = result
        return result

    names: set[str] = set()
    for m in _EXPORT_DECL.finditer(src):
        names.add(m.group(1))
    # export { a, b as c }  → exported names are the alias (post-`as`)
    for block in list(_EXPORT_BLOCK.finditer(src)) + list(_REEXPORT_BLOCK.finditer(src)):
        for part in block.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            alias = part.split(" as ")[-1].strip()
            names.add(alias)
    if _EXPORT_DEFAULT.search(src):
        names.add("default")
    has_star = bool(_EXPORT_STAR.search(src))
    result = (names, has_star)
    _cache[path] = result
    return result


def _imported_names(block: str) -> list[str]:
    """Parse the inside of `import { ... }` → list of SOURCE names (pre-`as`)."""
    out = []
    for part in block.split(","):
        part = part.strip()
        if not part:
            continue
        src_name = part.split(" as ")[0].strip()
        if src_name:
            out.append(src_name)
    return out


def main() -> int:
    js_files = sorted(WEB_ROOT.rglob("*.js"))
    # Skip vendored/minified third-party drops if any land under web/vendor/.
    js_files = [p for p in js_files if "vendor" not in p.parts]

    export_cache: dict[Path, tuple[set[str], bool]] = {}
    problems: list[str] = []

    for f in js_files:
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"{f}: cannot read ({e})")
            continue
        src = _strip_comments(raw)
        rel = f.relative_to(WEB_ROOT.parent)

        # 1. collect every specifier (static from, bare, dynamic, re-export)
        specs: set[str] = set()
        specs.update(_FROM_SPEC.findall(src))
        specs.update(_BARE_IMPORT.findall(src))
        specs.update(_DYNAMIC_IMPORT.findall(src))

        for spec in specs:
            if not _is_relative(spec):
                continue  # CDN / bare specifiers are out of scope
            target = _resolve(f, spec)
            if not target.is_file():
                problems.append(
                    f"{rel}: import '{spec}' -> missing file "
                    f"({target.relative_to(WEB_ROOT.parent) if WEB_ROOT.parent in target.parents else target})"
                )

        # 2. named-import ↔ export validation (relative targets only)
        for m in _NAMED_IMPORT.finditer(src):
            block, spec = m.group(1), m.group(2)
            if not _is_relative(spec):
                continue
            target = _resolve(f, spec)
            if not target.is_file():
                continue  # already reported in step 1
            exports, has_star = _module_exports(target, export_cache)
            if has_star:
                continue  # opaque re-export surface — can't validate safely
            for name in _imported_names(block):
                if name not in exports:
                    problems.append(
                        f"{rel}: imports {{ {name} }} from '{spec}' "
                        f"but {target.name} does not export it"
                    )

        # 3. default-import validation
        for m in _DEFAULT_IMPORT.finditer(src):
            name, spec = m.group(1), m.group(2)
            if not _is_relative(spec):
                continue
            target = _resolve(f, spec)
            if not target.is_file():
                continue
            exports, has_star = _module_exports(target, export_cache)
            if has_star:
                continue
            if "default" not in exports:
                problems.append(
                    f"{rel}: imports default '{name}' from '{spec}' "
                    f"but {target.name} has no default export"
                )

    scanned = len(js_files)
    if problems:
        print(f"[check-web-imports] {len(problems)} problem(s) across {scanned} JS file(s):\n")
        for p in problems:
            print(f"  ✗ {p}")
        print("\nFAIL")
        return 1
    print(f"[check-web-imports] OK — {scanned} JS files, all relative imports resolve "
          f"and all named/default imports match an export.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
