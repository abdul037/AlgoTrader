"""Tests for the typed config-section shim over the flat runtime settings."""

from __future__ import annotations

import pytest

from app.config_sections import (
    OPERATING_PROFILES,
    ConfigSections,
    build_config_sections,
    profile_overrides,
)
from tests.conftest import make_settings


def test_sections_mirror_flat_fields(tmp_path):
    settings = make_settings(tmp_path)
    sections = build_config_sections(settings)

    assert isinstance(sections, ConfigSections)
    # Grouped view reads the exact same values as flat access.
    assert sections.risk.max_daily_loss_usd == float(settings.max_daily_loss_usd)
    assert sections.risk.drawdown_governor_enabled == bool(settings.drawdown_governor_enabled)
    assert sections.execution.enable_real_trading == bool(settings.enable_real_trading)
    assert sections.data.primary_market_data_provider == str(settings.primary_market_data_provider)
    assert sections.strategy.regime_router_enabled == bool(settings.regime_router_enabled)
    assert sections.strategy.cross_sectional_momentum_top_pct == float(
        settings.cross_sectional_momentum_top_pct
    )


def test_settings_sections_property_is_available(tmp_path):
    settings = make_settings(tmp_path)
    # The back-compat shim: flat access still works AND grouped access works.
    assert settings.max_open_positions == settings.sections.risk.max_open_positions


def test_sections_are_frozen(tmp_path):
    settings = make_settings(tmp_path)
    sections = build_config_sections(settings)
    with pytest.raises(Exception):
        sections.risk.max_daily_loss_usd = 999.0  # type: ignore[misc]


def test_measurement_profile_disables_all_gated_features():
    overrides = profile_overrides("measurement")
    assert overrides == {
        "regime_router_enabled": False,
        "cross_sectional_momentum_enabled": False,
        "drawdown_governor_enabled": False,
    }


def test_profiles_only_touch_known_gated_flags():
    gated = {
        "regime_router_enabled",
        "cross_sectional_momentum_enabled",
        "drawdown_governor_enabled",
    }
    for name, overrides in OPERATING_PROFILES.items():
        assert set(overrides).issubset(gated), f"profile {name} touches non-gated flags"
        assert all(isinstance(v, bool) for v in overrides.values())


def test_unknown_profile_raises_with_valid_names():
    with pytest.raises(KeyError) as exc:
        profile_overrides("does-not-exist")
    assert "measurement" in str(exc.value)
