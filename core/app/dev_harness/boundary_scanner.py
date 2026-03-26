#!/usr/bin/env python3
"""
Sovereign boundary scanner — deterministic static analysis.

Rules enforced (ref: dev-harness-assessment.md §9):
  B1  call_llm or direct ollama invocation inside governance/ or execution/adapters/
  B2  call_llm inside any harness gate or validate function
  B3  Freeform string literal passed to translator_pass() instead of typed envelope
  B4  Specialist agent writing directly to a restricted Qdrant collection
      (semantic | associative | relational | meta)
  B5  Translator output path string literal containing a meta-commentary leak phrase
      (any phrase from _TRANSLATOR_LEAK_PHRASES baked into translator prompt/output code)

Design constraints:
  - No imports from sovereign-core. Fully standalone.
  - Emits newline-delimited JSON Finding objects to stdout only.
  - Never raises. Always exits 0. Parse failures are low-severity Findings.
  - LLM/deterministic boundary: this script contains no LLM calls by definition.
    Its presence in the scan scope is the self-referential acceptance criterion.

Usage:
  python3 boundary_scanner.py <target_dir>
  python3 boundary_scanner.py /docker/sovereign/core/app
"""

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Finding schema — must match analyser.py Finding fields exactly
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    source:   str = "boundary"
    type:     str = "boundary"
    file:     str = ""
    line:     int = 0
    message:  str = ""
    severity: str = "high"
    rule_id:  str = ""


def _emit(f: Finding) -> None:
    print(json.dumps(asdict(f)), flush=True)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read(path: Path, root: Path) -> list[str] | None:
    """Read file lines. Emit a Finding and return None on any error."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _emit(Finding(
            file=_rel(path, root), line=0,
            message=f"Could not read file: {exc}",
            severity="low", rule_id="SCANNER",
        ))
        return None


# ---------------------------------------------------------------------------
# Rule B1 — LLM invocation in forbidden zones
#
# Forbidden: governance/ and execution/adapters/ must never call an LLM.
# The ollama adapter (execution/adapters/ollama.py) and grok adapter are
# the *definitions* of the LLM interface — they are excluded from B1 so
# they don't flag themselves. All other files in those directories must not
# contain patterns that invoke an LLM.
# ---------------------------------------------------------------------------

# Patterns that constitute an actual LLM invocation (not just an import or
# class definition).  Each is (compiled_regex, human_description).
import re as _re

_B1_PATTERNS = [
    (_re.compile(r'\bcall_llm\s*\('),           "call_llm() invocation"),
    (_re.compile(r'\b_ollama_complete\s*\('),    "_ollama_complete() call"),
    (_re.compile(r'\bollama_adapter\.'),         "ollama_adapter. attribute access"),
    (_re.compile(r'\bOllamaAdapter\s*\('),       "OllamaAdapter() instantiation"),
    (_re.compile(r'\bGrokAdapter\s*\('),         "GrokAdapter() instantiation"),
    (_re.compile(r'\bgrok_adapter\.'),           "grok_adapter. attribute access"),
]

# Files excluded from B1.
# ollama.py / grok.py / claude.py: define the LLM interface — they ARE the boundary.
# qdrant.py: contains one intentional direct Ollama call (_generate_key_and_title,
#   /api/generate) for MIP key generation. The qdrant adapter IS the LLM interface
#   layer for memory operations, not a caller of it.
#   Rationale comment lives at the call site in execution/adapters/qdrant.py.
#   If this pattern spreads to other files, add a B5 rule for raw /api/generate URLs.
_B1_EXCLUSIONS = {"ollama.py", "grok.py", "claude.py", "qdrant.py"}


def scan_b1(root: Path) -> None:
    forbidden = [
        root / "governance",
        root / "execution" / "adapters",
    ]
    for zone in forbidden:
        if not zone.exists():
            continue
        for py_file in sorted(zone.rglob("*.py")):
            if py_file.name in _B1_EXCLUSIONS:
                continue
            lines = _read(py_file, root)
            if lines is None:
                continue
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern, desc in _B1_PATTERNS:
                    if pattern.search(line):
                        _emit(Finding(
                            file=_rel(py_file, root),
                            line=lineno,
                            message=(
                                f"B1: {desc} inside forbidden zone "
                                f"({zone.relative_to(root)}/). "
                                "Governance and adapters must never invoke an LLM."
                            ),
                            severity="critical",
                            rule_id="B1",
                        ))
                        break  # one Finding per line per file


# ---------------------------------------------------------------------------
# Rule B2 — call_llm inside a harness gate or validate function
#
# Gate/validate functions are the deterministic decision points in every
# harness. They must never call an LLM — the LLM/deterministic boundary
# is enforced here.
#
# Detection: use AST to find function definitions whose names end with
# _gate or _validate, then walk their bodies for call_llm Call nodes.
# Falls back to regex line-scan if AST parse fails.
# ---------------------------------------------------------------------------

_B2_SUFFIXES  = ("_gate", "_validate")
_B2_CALL_NAME = "call_llm"
_B2_DIRS = ("dev_harness", "execution", "monitoring", "cognition", "skills")


def _ast_contains_call_llm(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """Return first line number of a call_llm() call in func_node, or None."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            # Direct call: call_llm(...)
            if isinstance(node.func, ast.Name) and node.func.id == _B2_CALL_NAME:
                return node.lineno
            # Method call: self.call_llm(...)
            if isinstance(node.func, ast.Attribute) and node.func.attr == _B2_CALL_NAME:
                return node.lineno
    return None


def _scan_b2_ast(py_file: Path, root: Path) -> None:
    src = py_file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(py_file))
    except SyntaxError as exc:
        _emit(Finding(
            file=_rel(py_file, root), line=exc.lineno or 0,
            message=f"B2: AST parse failed (syntax error) — {exc.msg}",
            severity="low", rule_id="B2-PARSE",
        ))
        return

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.endswith(_B2_SUFFIXES):
            continue
        hit_line = _ast_contains_call_llm(node)
        if hit_line is not None:
            _emit(Finding(
                file=_rel(py_file, root),
                line=hit_line,
                message=(
                    f"B2: call_llm() inside gate/validate function '{node.name}' "
                    "(line {node.lineno}). Gate functions must be deterministic — "
                    "no LLM calls permitted."
                ).format(node=node),
                severity="critical",
                rule_id="B2",
            ))


def scan_b2(root: Path) -> None:
    for dirname in _B2_DIRS:
        d = root / dirname
        if not d.exists():
            continue
        for py_file in sorted(d.rglob("*.py")):
            try:
                _scan_b2_ast(py_file, root)
            except OSError as exc:
                _emit(Finding(
                    file=_rel(py_file, root), line=0,
                    message=f"B2: Could not read file: {exc}",
                    severity="low", rule_id="SCANNER",
                ))


# ---------------------------------------------------------------------------
# Rule B3 — Freeform string literal passed to translator_pass()
#
# translator_pass() must always receive a typed result envelope (dict).
# Passing a bare string or f-string bypasses the envelope schema and risks
# leaking raw LLM output to the Director without governance wrapping.
#
# Detected pattern: translator_pass( immediately followed by a quote char
# (single, double, triple, or f-string prefix).  String variables are not
# caught here (Phase 2 LLM classification handles borderline cases).
# ---------------------------------------------------------------------------

_B3_RE = _re.compile(
    r'\btranslator_pass\s*\(\s*'   # function call opening
    r'(?:'
    r'f?"""'                        # triple-double-quote (f or plain)
    r"|f?'''"                       # triple-single-quote
    r'|f?"'                         # double-quote
    r"|f?'"                         # single-quote
    r'|str\s*\('                    # str(...) call — almost always wrong here
    r')'
)


def scan_b3(root: Path) -> None:
    for py_file in sorted(root.rglob("*.py")):
        lines = _read(py_file, root)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            if _B3_RE.search(line):
                _emit(Finding(
                    file=_rel(py_file, root),
                    line=lineno,
                    message=(
                        "B3: String literal (or str() call) passed directly to "
                        "translator_pass(). Must use a typed result envelope dict. "
                        "Raw strings bypass governance wrapping and schema validation."
                    ),
                    severity="high",
                    rule_id="B3",
                ))


# ---------------------------------------------------------------------------
# Rule B4 — Specialist agent writing to a restricted Qdrant collection
#
# Only sovereign-core's qdrant adapter (execution/adapters/qdrant.py) may
# write to semantic, associative, relational, or meta collections via
# archive_client. Specialist agent code in cognition/ or execution/ that
# calls archive_client write methods on these collections violates the write
# permission matrix in Sovereign-cognition.md.
#
# Detection strategy:
#   1. Scan cognition/ and execution/ for archive_client write calls.
#   2. Exclude execution/adapters/qdrant.py (the legitimate writer).
#   3. For each write call found, check the surrounding 6-line window for a
#      collection= argument naming a restricted collection.
# ---------------------------------------------------------------------------

_RESTRICTED_COLLECTIONS = frozenset({"semantic", "associative", "relational", "meta"})
_B4_WRITE_RE            = _re.compile(
    r'\barchive_client\s*\.\s*(?:store|upsert|set_payload)\s*\('
)
_B4_COLLECTION_RE       = _re.compile(r'\bcollection\s*=\s*["\'](\w+)["\']')

_B4_DIRS                = ("cognition", "execution")
_B4_EXCLUSION           = _re.compile(r'execution[\\/]adapters[\\/]qdrant\.py$')


def scan_b4(root: Path) -> None:
    for dirname in _B4_DIRS:
        d = root / dirname
        if not d.exists():
            continue
        for py_file in sorted(d.rglob("*.py")):
            rel = _rel(py_file, root)
            # Exclude the legitimate writer
            if _B4_EXCLUSION.search(rel.replace("\\", "/")):
                continue
            lines = _read(py_file, root)
            if lines is None:
                continue
            for lineno, line in enumerate(lines, 1):
                if line.strip().startswith("#"):
                    continue
                if not _B4_WRITE_RE.search(line):
                    continue
                # Check a window of lines around the call for collection= kwarg
                win_start = max(0, lineno - 1)
                win_end   = min(len(lines), lineno + 5)
                window    = " ".join(lines[win_start:win_end])
                m         = _B4_COLLECTION_RE.search(window)
                if m and m.group(1) in _RESTRICTED_COLLECTIONS:
                    _emit(Finding(
                        file=rel,
                        line=lineno,
                        message=(
                            f"B4: archive_client write to restricted collection "
                            f"'{m.group(1)}' outside qdrant adapter. "
                            "Only execution/adapters/qdrant.py may write to "
                            "semantic, associative, relational, or meta collections."
                        ),
                        severity="critical",
                        rule_id="B4",
                    ))


# ---------------------------------------------------------------------------
# Rule B5 — Translator output path string literal containing a leak phrase
#
# The _TRANSLATOR_LEAK_PHRASES list in cognition/engine.py defines the set of
# meta-commentary strings the sanitiser strips at runtime.  If any of those
# phrases appear as string LITERALS (not comments, not the definition tuple
# itself) inside the translator output path, it means the phrase has been baked
# into a prompt, a hardcoded fallback, or a return value — and will appear in
# the Director's output even if the runtime sanitiser is bypassed.
#
# Translator output path files:
#   cognition/engine.py   — translator_pass() + _log_translator_violation()
#   cognition/prompts.py  — translate_from_orchestrator() (PASS 5 prompt)
#   gateway/main.py       — Telegram dispatch (final output hop)
#
# Exclusions (line-level):
#   - Lines that define or reference _TRANSLATOR_LEAK_PHRASES (the authoritative
#     list itself; it MUST contain these strings by design).
#   - Comment lines (leading #).
# ---------------------------------------------------------------------------

_B5_LEAK_PHRASES: tuple[str, ...] = (
    "I followed the rules",
    "Led with the answer",
    "per the instructions",
    "as instructed",
)

# Pre-compile case-insensitive phrase patterns
_B5_PATTERNS = [
    (_re.compile(_re.escape(phrase), _re.IGNORECASE), phrase)
    for phrase in _B5_LEAK_PHRASES
]

# String-literal detector: matches content inside single/double/triple quotes
# We only flag when the match falls within a quoted region on the line.
_B5_STRING_RE = _re.compile(
    r'(?:'
    r'"""(?:[^"\\]|\\.)*?"""'           # triple double
    r"|'''(?:[^'\\]|\\.)*?'''"          # triple single
    r'|"(?:[^"\\]|\\.)*?"'              # double
    r"|'(?:[^'\\]|\\.)*?'"              # single
    r')'
)

# Files excluded from B5 by filename (the definitions themselves)
_B5_EXCLUSION_NAMES = {"boundary_scanner.py"}
# Line-level skip: lines that reference the canonical definition
_B5_SKIP_LINE_RE = _re.compile(
    r'_TRANSLATOR_LEAK_PHRASES|_B5_LEAK_PHRASES|_translator_sanitise\s*=|_translator_sanitise\s*\('
)

# Translator output path directories/files relative to root
_B5_PATHS = [
    ("cognition", None),     # all .py in cognition/
    ("gateway", None),       # all .py in gateway/
]


def scan_b5(root: Path) -> None:
    targets: list[Path] = []
    for dirname, fname in _B5_PATHS:
        d = root / dirname
        if not d.exists():
            # gateway/ may be a sibling of core/app — try two levels up
            d = root.parent.parent / dirname
        if not d.exists():
            continue
        if fname:
            p = d / fname
            if p.exists():
                targets.append(p)
        else:
            targets.extend(sorted(d.rglob("*.py")))

    for py_file in targets:
        if py_file.name in _B5_EXCLUSION_NAMES:
            continue
        lines = _read(py_file, root)
        if lines is None:
            continue
        in_skip_block = False  # True while inside _TRANSLATOR_LEAK_PHRASES / _B5_LEAK_PHRASES tuple body
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _B5_SKIP_LINE_RE.search(line):
                # Entering the canonical definition block — skip until closing paren
                in_skip_block = True
                continue
            if in_skip_block:
                if stripped.startswith(")"):
                    in_skip_block = False
                continue
            # Collect only the string literal portions of this line
            string_portions = "".join(m.group() for m in _B5_STRING_RE.finditer(line))
            if not string_portions:
                continue
            for pattern, phrase in _B5_PATTERNS:
                if pattern.search(string_portions):
                    _emit(Finding(
                        file=_rel(py_file, root),
                        line=lineno,
                        message=(
                            f"B5: translator output path string literal contains "
                            f"leak phrase {phrase!r}. Baking meta-commentary phrases "
                            "into prompts or fallback strings causes logic bleed into "
                            "Director output. Move to _TRANSLATOR_LEAK_PHRASES or remove."
                        ),
                        severity="high",
                        rule_id="B5",
                    ))
                    break  # one Finding per line


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sovereign boundary scanner. "
            "Emits newline-delimited JSON Finding objects to stdout. "
            "Always exits 0."
        )
    )
    parser.add_argument(
        "target",
        help="Root directory to scan (e.g. /docker/sovereign/core/app)",
    )
    args = parser.parse_args()

    root = Path(args.target).resolve()
    if not root.exists():
        _emit(Finding(
            file=str(root), line=0,
            message=f"Target directory does not exist: {root}",
            severity="low", rule_id="SCANNER",
        ))
        sys.exit(0)

    scan_b1(root)
    scan_b2(root)
    scan_b3(root)
    scan_b4(root)
    scan_b5(root)


if __name__ == "__main__":
    main()
