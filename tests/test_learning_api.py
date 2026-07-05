from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.learning import LearningJob
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


def test_learning_job_inspect_retry_and_resolve(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, control_api_token="control-secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    repository = app.state.learning_repository
    failed = repository.enqueue_job(
        LearningJob(
            id="job_failed",
            idempotency_key="failed-key",
            job_type="trade_review",
            status="failed",
            payload={"execution_id": "exec_1", "secret": "redacted"},
            error="temporary model outage",
            attempts=2,
        )
    )
    resolvable = repository.enqueue_job(
        LearningJob(
            id="job_resolve",
            idempotency_key="resolve-key",
            job_type="nightly_training",
            status="failed",
            payload={"dataset": "v1"},
            error="artifact bucket missing",
            attempts=1,
        )
    )
    client = TestClient(app)

    assert client.get("/learning/jobs?status=failed").status_code == 403

    list_response = client.get(
        "/learning/jobs?status=failed",
        headers={"X-Control-Token": "control-secret"},
    )
    detail_response = client.get(
        f"/learning/jobs/{failed.id}",
        headers={"X-Control-Token": "control-secret"},
    )
    retry_response = client.post(
        f"/learning/jobs/{failed.id}/retry",
        headers={"X-Control-Token": "control-secret"},
    )
    resolve_response = client.post(
        f"/learning/jobs/{resolvable.id}/resolve",
        headers={"X-Control-Token": "control-secret"},
        json={"signed_by": "qa", "evidence": {"reason": "legacy setup failure manually reviewed"}},
    )

    assert list_response.status_code == 200
    assert {item["id"] for item in list_response.json()} == {"job_failed", "job_resolve"}
    assert detail_response.status_code == 200
    assert detail_response.json()["next_action"] == "retry_or_resolve"
    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == "pending"
    assert retry_response.json()["attempts"] == 2
    assert resolve_response.status_code == 200
    assert resolve_response.json()["status"] == "resolved"

    status_response = client.get("/learning/status")
    assert status_response.json()["failed_jobs"] == 0
