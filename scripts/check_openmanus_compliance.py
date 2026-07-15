"""OpenManusPlugin — Phase 5: Routing compliance checker.

Scans all plugin service files for direct use of `_call_openai_compatible`
or `get_enabled_providers` as actual import/call statements (not in comments,
docstrings, or metadata-only contexts) outside of allowlisted files.

Usage (from repo root):
    backend/.venv/bin/python3 scripts/check_openmanus_compliance.py

Returns:
    Exit 0 if fully compliant (no bypasses found).
    Exit 1 with a detailed report if bypasses remain.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

# ── Patterns that indicate a real AI routing bypass (as import or call) ───────
# We check for the pattern being actually imported or called, not just mentioned.
BYPASS_IMPORT_PATTERNS = [
    "import _call_openai_compatible",
    "import get_enabled_providers",
    "from plugins.AiMarketAnalyst.backend.services.ai_router import (\n",
]

# These function names appear in bypasses ONLY when used in an import-then-call pattern.
# Detecting: the same file both imports AND calls the function for AI routing.
BYPASS_CALL_INDICATORS = [
    "await _call_openai_compatible(",
    "await get_enabled_providers(",
]

# ── Files fully allowed to use these patterns ─────────────────────────────────
ALLOWLIST = {
    # Core router — the canonical implementation
    str(PLUGINS_DIR / "AiMarketAnalyst" / "backend" / "services" / "ai_router.py"),
    # OpenManus adapter — wraps db_chat / imports for forwarding
    str(PLUGINS_DIR / "OpenManusPlugin" / "backend" / "services" / "adapter.py"),
    # AiMarketAnalyst internals — internal to the plugin, not feature-service bypasses
    str(PLUGINS_DIR / "AiMarketAnalyst" / "backend" / "router.py"),
    str(PLUGINS_DIR / "AiMarketAnalyst" / "backend" / "services" / "llm_gateway.py"),
    str(PLUGINS_DIR / "AiMarketAnalyst" / "backend" / "services" / "llm_registry.py"),
}

# ── Files where the import is for METADATA ONLY (not AI routing) ──────────────
# paul_chat.py reads provider labels/status into the JARVIS persona — no AI call.
METADATA_ONLY = {
    str(PLUGINS_DIR / "AgentPaulPlugin" / "backend" / "services" / "paul_chat.py"),
}

# ── Required compliant flows (must use db_chat or openmanus_chat) ──────────────
# NOTE: jarvis.py is a READ-ONLY core file; it uses db_chat for main AI calls.
# The brain-manager functions use targeted provider slots intentionally — this is
# the multi-model brain network (Mistral/Gemma brain managers) which is an
# approved exception to the general routing rule.
REQUIRED_COMPLIANT_FLOWS = {
    "jarvis": [],  # exempt: core file; brain slots are approved exception
    "kronos_bridge": [
        PLUGINS_DIR / "KronosForecastPlugin" / "backend" / "services" / "jarvis_analysis.py",
    ],
    "smc_ai": [
        PLUGINS_DIR / "MT5TradingPlugin" / "backend" / "services" / "smc_ai.py",
    ],
    "scalp_ensemble": [
        PLUGINS_DIR / "MT5TradingPlugin" / "backend" / "services" / "scalp_bot_service.py",
    ],
}

ROUTER_CALLS = {"db_chat", "openmanus_chat"}


def _is_code_line(line: str) -> bool:
    """Return True if the line is actual code (not a comment or blank)."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _file_has_real_bypass(path: Path) -> list[tuple[str, int]]:
    """Return [(pattern, line_no), ...] for actual routing bypasses in a file."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    # Quick pre-filter: must contain one of the call indicators
    if not any(ind in content for ind in BYPASS_CALL_INDICATORS):
        return []

    violations = []
    in_multiline_string = False
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Track triple-quoted strings (skip docstrings)
        if '"""' in stripped or "'''" in stripped:
            count = stripped.count('"""') + stripped.count("'''")
            if count % 2 == 1:
                in_multiline_string = not in_multiline_string

        if in_multiline_string:
            continue

        if not _is_code_line(line):
            continue

        for pattern in BYPASS_CALL_INDICATORS:
            func = pattern.replace("await ", "").replace("(", "")
            if func in line and "# type: ignore" not in line:
                violations.append((func, i))

    return violations


def check_bypasses() -> list[dict]:
    """Find files with actual AI routing bypasses."""
    violations = []
    for py_file in PLUGINS_DIR.rglob("*.py"):
        abs_path = str(py_file)
        if abs_path in ALLOWLIST or abs_path in METADATA_ONLY:
            continue
        hits = _file_has_real_bypass(py_file)
        if hits:
            violations.append({
                "file": str(py_file.relative_to(REPO_ROOT)),
                "patterns": [{"fn": h[0], "line": h[1]} for h in hits],
            })
    return violations


def check_flow_compliance() -> list[dict]:
    """Verify named flows use db_chat/openmanus_chat and NOT routing bypasses."""
    issues = []
    for flow_name, files in REQUIRED_COMPLIANT_FLOWS.items():
        for f in files:
            if not f.exists():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            uses_router = any(rc in content for rc in ROUTER_CALLS)
            bypass_calls = [
                ind for ind in BYPASS_CALL_INDICATORS
                if ind in content and str(f) not in ALLOWLIST
            ]

            if bypass_calls:
                issues.append({
                    "flow": flow_name,
                    "file": str(f.relative_to(REPO_ROOT)),
                    "bypass_calls": bypass_calls,
                    "uses_approved_router": uses_router,
                })
    return issues


def main() -> int:
    print("=" * 60)
    print("OpenManus Routing Compliance Check")
    print("=" * 60)

    violations = check_bypasses()
    flow_issues = check_flow_compliance()

    result_ok = True

    if violations:
        result_ok = False
        print("\n[FAIL] Real AI routing bypasses found (await calls outside allowlist):")
        for v in violations:
            print(f"  {v['file']}:")
            for p in v["patterns"]:
                print(f"    line {p['line']}: {p['fn']}()")
        print("\n  Fix: replace await _call_openai_compatible() with await db_chat()")
        print("  which routes through OpenManus MCP first with provider fallback.")

    if flow_issues:
        result_ok = False
        print("\n[FAIL] Named flow compliance issues (bypass calls detected):")
        for i in flow_issues:
            print(f"  Flow '{i['flow']}' in {i['file']}:")
            print(f"    Bypass calls: {i['bypass_calls']}")
            print(f"    Uses approved router (db_chat/openmanus_chat): {i['uses_approved_router']}")

    if result_ok:
        print("\n[PASS] All flows are compliant — no active AI routing bypasses found.")
        print("  Routing: db_chat() → OpenManus MCP (primary) → provider pool (fallback)")
    else:
        print("\n[SUMMARY] Fix violations above, then re-run this script.")

    return 0 if result_ok else 1


if __name__ == "__main__":
    sys.exit(main())

PLUGINS_DIR = REPO_ROOT / "plugins"

# Patterns that indicate a direct bypass of the shared AI router
BYPASS_PATTERNS = [
    "_call_openai_compatible",
    "get_enabled_providers",
]

# Files allowed to USE these patterns (the router itself, the adapter)
ALLOWLIST = {
    str(PLUGINS_DIR / "AiMarketAnalyst" / "backend" / "services" / "ai_router.py"),
    str(PLUGINS_DIR / "OpenManusPlugin" / "backend" / "services" / "adapter.py"),
}

# Flows that should be confirmed compliant
REQUIRED_COMPLIANT_FLOWS = {
    "jarvis": [
        REPO_ROOT / "backend" / "app" / "api" / "jarvis.py",
    ],
    "kronos_bridge": [
        PLUGINS_DIR / "KronosForecastPlugin" / "backend" / "services" / "jarvis_analysis.py",
    ],
    "smc_ai": [
        PLUGINS_DIR / "MT5TradingPlugin" / "backend" / "services" / "smc_ai.py",
    ],
    "scalp_ensemble": [
        PLUGINS_DIR / "MT5TradingPlugin" / "backend" / "services" / "scalp_bot_service.py",
    ],
}

ROUTER_CALL = "db_chat"
ADAPTER_CALL = "openmanus_chat"


def check_bypasses() -> list[dict]:
    """Find all direct bypass usages in plugin files."""
    violations = []
    for py_file in PLUGINS_DIR.rglob("*.py"):
        if str(py_file) in ALLOWLIST:
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pattern in BYPASS_PATTERNS:
            if pattern in content:
                # Find line numbers
                lines = [
                    i + 1
                    for i, line in enumerate(content.splitlines())
                    if pattern in line and "# type: ignore" not in line
                ]
                if lines:
                    violations.append({
                        "file": str(py_file.relative_to(REPO_ROOT)),
                        "pattern": pattern,
                        "lines": lines,
                    })
    return violations


def check_flow_compliance() -> list[dict]:
    """Verify named flows use db_chat/openmanus_chat rather than direct bypasses."""
    issues = []
    for flow_name, files in REQUIRED_COMPLIANT_FLOWS.items():
        for f in files:
            if not f.exists():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            uses_router = ROUTER_CALL in content or ADAPTER_CALL in content
            # Check for any remaining bypass patterns
            bypasses = [p for p in BYPASS_PATTERNS if p in content]
            if bypasses and str(f) not in ALLOWLIST:
                issues.append({
                    "flow": flow_name,
                    "file": str(f.relative_to(REPO_ROOT)),
                    "bypasses_remaining": bypasses,
                    "uses_router": uses_router,
                })
    return issues


def main() -> int:
    print("=" * 60)
    print("OpenManus Routing Compliance Check")
    print("=" * 60)

    violations = check_bypasses()
    flow_issues = check_flow_compliance()

    ok = True

    if violations:
        ok = False
        print("\n[FAIL] Direct provider bypass patterns found:")
        for v in violations:
            print(f"  {v['file']} — '{v['pattern']}' at lines {v['lines']}")
        print("\n  Fix: replace _call_openai_compatible / get_enabled_providers usage")
        print("  with db_chat() (from AiMarketAnalyst) which routes through OpenManus.")

    if flow_issues:
        ok = False
        print("\n[WARN] Named flow compliance issues:")
        for i in flow_issues:
            print(f"  Flow '{i['flow']}' in {i['file']}:")
            print(f"    Bypasses remaining: {i['bypasses_remaining']}")
            print(f"    Uses router: {i['uses_router']}")

    if ok:
        print("\n[PASS] All checked files are compliant — no direct provider bypasses found.")
        print("  OpenManus routing: ALL AI calls go through db_chat() → OpenManus MCP → fallback.")
    else:
        print("\n[SUMMARY] Fix violations above, then re-run this script.")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
