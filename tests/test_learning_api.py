from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


def test_learning_read_apis_and_protected_mutations(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, control_api_token="control-secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    for path in (
        "/learning/status",
        "/learning/reviews",
        "/learning/models",
        "/learning/experiments",
        "/learning/drift",
    ):
        assert client.get(path).status_code == 200

    assert client.post("/learning/jobs/process").status_code == 403
    response = client.post(
        "/learning/jobs/process",
        headers={"X-Control-Token": "control-secret"},
    )
    assert response.status_code == 200
