"""The curated strategy profile must stay consistent with the live registry."""

from __future__ import annotations

from app.strategies import STRATEGY_REGISTRY
from app.strategies.curated import (
    CURATED_EDGES,
    SUBSUMED_BY,
    curated_edge_names,
    subsumed_family_names,
)


def test_curated_names_are_real_registry_families():
    for name in CURATED_EDGES:
        assert name in STRATEGY_REGISTRY, f"curated edge {name!r} is not a registered strategy"


def test_subsumed_names_are_real_registry_families():
    for name in subsumed_family_names():
        assert name in STRATEGY_REGISTRY, f"subsumed family {name!r} is not a registered strategy"


def test_curation_partitions_the_whole_registry():
    curated = set(CURATED_EDGES)
    subsumed = set(subsumed_family_names())
    # A representative is never also listed as subsumed.
    assert curated.isdisjoint(subsumed)
    # No family is subsumed by two different representatives.
    assert len(subsumed_family_names()) == len(subsumed)
    # Every registered family is either a curated representative or subsumed —
    # so adding/renaming a strategy forces this map to be updated.
    assert curated | subsumed == set(STRATEGY_REGISTRY)


def test_curated_set_is_compact():
    # The whole point is a small, non-overlapping book.
    assert 12 <= len(CURATED_EDGES) <= 15


def test_every_representative_has_a_mapping_or_stands_alone():
    # Representatives with no subsumed variants are genuinely distinct edges;
    # that's allowed, but a representative listed in SUBSUMED_BY must be curated.
    for representative in SUBSUMED_BY:
        assert representative in set(CURATED_EDGES)


def test_curated_edge_names_returns_a_fresh_list():
    a = curated_edge_names()
    a.append("x")
    assert "x" not in curated_edge_names()
