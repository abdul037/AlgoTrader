"""Tests for the live-selection auto-demote filter (close-the-loop, step 2)."""

from __future__ import annotations

from types import SimpleNamespace

from app.screener.service import filter_out_demoted


def _spec(name: str):
    return SimpleNamespace(name=name, timeframe="5m")


class TestFilterOutDemoted:
    def test_drops_demoted_specs_when_enabled(self) -> None:
        specs = [_spec("good"), _spec("loser"), _spec("also_good")]
        kept = filter_out_demoted(specs, {"loser"}, enabled=True)
        assert [s.name for s in kept] == ["good", "also_good"]

    def test_is_inert_when_disabled(self) -> None:
        specs = [_spec("good"), _spec("loser")]
        kept = filter_out_demoted(specs, {"loser"}, enabled=False)
        assert [s.name for s in kept] == ["good", "loser"]

    def test_is_inert_when_no_demotions(self) -> None:
        specs = [_spec("good"), _spec("also_good")]
        kept = filter_out_demoted(specs, set(), enabled=True)
        assert kept is specs  # unchanged, same object

    def test_matches_case_insensitively(self) -> None:
        specs = [_spec("Loser"), _spec("Keep")]
        kept = filter_out_demoted(specs, {"loser"}, enabled=True)
        assert [s.name for s in kept] == ["Keep"]

    def test_empty_specs(self) -> None:
        assert filter_out_demoted([], {"loser"}, enabled=True) == []
