"""Architecture fitness functions.

These tests encode structural invariants of the codebase as executable,
enforceable rules. They are deliberately *ratcheted*: each rule captures the
current state as a baseline and fails when the codebase regresses (a new import
cycle, a new god-file, coupling into the models leaf). When you legitimately
improve the architecture — break a cycle, shrink a large file below budget —
the ratchet forces you to tighten the baseline in the same change, so the gains
can never silently erode.

Scope: only *runtime* imports are considered. Imports guarded by
``if TYPE_CHECKING:`` and imports nested inside functions/methods are ignored,
because they do not create import-time coupling and are the sanctioned escape
hatch used elsewhere in this repo to break cycles.
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


# --------------------------------------------------------------------------- #
# Import-graph extraction
# --------------------------------------------------------------------------- #
def _module_name(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).as_posix()
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", ".")
    if rel.endswith(".py"):
        return rel[: -len(".py")].replace("/", ".")
    return rel.replace("/", ".")


def _resolve(module: str, node: ast.ImportFrom) -> str:
    if node.level:
        base = module.rsplit(".", node.level)[0]
        return f"{base}.{node.module}" if node.module else base
    return node.module or ""


def _iter_app_modules():
    for dirpath, _dirs, files in os.walk(APP_ROOT):
        for name in files:
            if name.endswith(".py"):
                path = Path(dirpath) / name
                yield _module_name(path), path


def _runtime_app_imports(module: str, path: Path) -> set[str]:
    """Top-level (runtime) imports of this module that target the app package."""
    tree = ast.parse(path.read_text())
    targets: set[str] = set()
    for stmt in tree.body:
        # Skip `if TYPE_CHECKING:` guarded imports entirely.
        if isinstance(stmt, ast.If) and "TYPE_CHECKING" in ast.dump(stmt.test):
            continue
        if isinstance(stmt, ast.ImportFrom):
            resolved = _resolve(module, stmt)
            if resolved.startswith("app"):
                targets.add(resolved)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name.startswith("app"):
                    targets.add(alias.name)
    return targets


def _subpackage(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else module


def _subpackage_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for module, path in _iter_app_modules():
        src = _subpackage(module)
        for target in _runtime_app_imports(module, path):
            dst = _subpackage(target)
            if src != dst:
                edges.add((src, dst))
    return edges


def _cyclic_edges() -> set[tuple[str, str]]:
    """Edges that participate in at least one import cycle (SCC of size > 1)."""
    graph: dict[str, set[str]] = defaultdict(set)
    for src, dst in _subpackage_edges():
        graph[src].add(dst)

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    counter = [0]
    sccs: list[list[str]] = []

    def strong_connect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in graph.get(v, ()):
            if w not in index:
                strong_connect(w)
                low[v] = min(low[v], low[w])
            elif on_stack.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            sccs.append(component)

    for node in list(graph):
        if node not in index:
            strong_connect(node)

    cyclic_nodes: set[str] = set()
    for component in sccs:
        if len(component) > 1:
            cyclic_nodes.update(component)

    return {
        (src, dst)
        for src in cyclic_nodes
        for dst in graph[src]
        if dst in cyclic_nodes
    }


# --------------------------------------------------------------------------- #
# Baselines (the ratchet). Shrink these; never grow them without a hard reason.
# --------------------------------------------------------------------------- #

# Known subpackage import cycles as of the Phase A architecture pass. Each pair
# is a runtime edge that participates in a cycle. Adding a *new* cyclic edge is
# a regression; removing coupling (breaking a cycle) must be reflected here by
# deleting the now-dead entries in the same change.
ALLOWED_CYCLIC_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("app.approvals", "app.execution"),
        ("app.backtesting", "app.strategies"),
        ("app.execution", "app.approvals"),
        ("app.execution", "app.strategies"),
        ("app.screener", "app.backtesting"),
        ("app.screener", "app.strategies"),
        ("app.strategies", "app.backtesting"),
        ("app.strategies", "app.screener"),
    }
)

# `app.models` is the data-contract leaf. It may depend only on these pure
# helper/schema modules — never on service, storage, screener, or infra layers.
MODELS_ALLOWED_DEPENDENCIES: frozenset[str] = frozenset(
    {"app.utils", "app.live_signal_schema"}
)

# Per-file line budget. Any file above this must be listed in KNOWN_LARGE_FILES
# with its current ceiling, so god-files cannot grow and no new one can appear.
FILE_LINE_BUDGET = 1000

KNOWN_LARGE_FILES: dict[str, int] = {
    "app/storage/repositories.py": 2840,
    "app/notifications/telegram_bot.py": 1951,
    "app/workflow/service.py": 1158,
    "app/storage/db.py": 1012,
}


# --------------------------------------------------------------------------- #
# Fitness functions
# --------------------------------------------------------------------------- #
def test_no_new_import_cycles() -> None:
    """No new runtime import cycle may be introduced between subpackages."""
    current = _cyclic_edges()

    new_cycles = current - ALLOWED_CYCLIC_EDGES
    assert not new_cycles, (
        "New import cycle(s) introduced between subpackages: "
        f"{sorted(new_cycles)}. Break the cycle (TYPE_CHECKING / function-local "
        "import) or, if truly unavoidable, add it to ALLOWED_CYCLIC_EDGES with a "
        "justification."
    )

    stale = ALLOWED_CYCLIC_EDGES - current
    assert not stale, (
        "These cyclic edges are in the allowlist but no longer exist — the "
        f"architecture improved. Remove them from ALLOWED_CYCLIC_EDGES: {sorted(stale)}"
    )


def test_models_layer_stays_a_leaf() -> None:
    """`app.models.*` must not couple to service/storage/infra layers."""
    violations: list[str] = []
    for module, path in _iter_app_modules():
        if not module.startswith("app.models"):
            continue
        for target in _runtime_app_imports(module, path):
            target_sub = _subpackage(target)
            if (
                not target.startswith("app.models")
                and target_sub not in MODELS_ALLOWED_DEPENDENCIES
                and target not in MODELS_ALLOWED_DEPENDENCIES
            ):
                violations.append(f"{module} -> {target}")

    assert not violations, (
        "app.models must remain a dependency leaf. Disallowed imports: "
        f"{sorted(violations)}. Allowed: {sorted(MODELS_ALLOWED_DEPENDENCIES)}."
    )


def test_file_size_budget_ratchet() -> None:
    """No new god-files; existing large files may not grow past their ceiling."""
    over_budget: dict[str, int] = {}
    for _module, path in _iter_app_modules():
        lines = len(path.read_text().splitlines())
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        if lines > FILE_LINE_BUDGET:
            over_budget[rel] = lines

    # (1) Nothing over budget that isn't a tracked exception.
    untracked = {f: n for f, n in over_budget.items() if f not in KNOWN_LARGE_FILES}
    assert not untracked, (
        f"New file(s) exceed the {FILE_LINE_BUDGET}-line budget: {untracked}. "
        "Split them into a cohesive package, or (last resort) record the ceiling "
        "in KNOWN_LARGE_FILES."
    )

    # (2) Tracked files may not exceed their recorded ceiling.
    grew = {
        f: (over_budget[f], ceiling)
        for f, ceiling in KNOWN_LARGE_FILES.items()
        if f in over_budget and over_budget[f] > ceiling
    }
    assert not grew, (
        "Tracked large file(s) grew past their ceiling {file: (now, ceiling)}: "
        f"{grew}. God-files must shrink, not grow."
    )

    # (3) A tracked file that dropped below budget must leave the list (ratchet).
    resolved = [f for f in KNOWN_LARGE_FILES if f not in over_budget]
    assert not resolved, (
        "These files fell below the budget — remove them from KNOWN_LARGE_FILES "
        f"to lock in the win: {sorted(resolved)}"
    )
