from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_railway_deployment_keeps_single_non_overlapping_service() -> None:
    config = json.loads((ROOT / "railway.json").read_text())

    assert config["build"] == {
        "builder": "DOCKERFILE",
        "dockerfilePath": "Dockerfile",
    }
    assert config["deploy"]["preDeployCommand"] == "alembic upgrade head"
    assert config["deploy"]["numReplicas"] == 1
    assert config["deploy"]["healthcheckPath"] == "/health"
    assert config["deploy"]["sleepApplication"] is False
    assert config["deploy"]["restartPolicyType"] == "ALWAYS"
    assert config["deploy"]["overlapSeconds"] == 0


def test_dockerfile_uses_railway_port_with_local_fallback() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "--port ${PORT:-8011}" in dockerfile
