from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.workflow import WorkflowBucketStatus, WorkflowTaskResponse
from tests.conftest import MockBroker, make_settings


class FakeWorkflowSchedule:
    def __init__(self) -> None:
        self.runs: list[str] = []

    def schedule_statuses(self):
        return [
            WorkflowBucketStatus(
                name="intraday_rotation",
                enabled=True,
                paused=False,
                last_status="ok",
                next_due_at="2026-05-04T10:15:00-04:00",
            )
        ]

    def run_bucket(self, bucket_name: str, *, notify: bool = True, force_refresh: bool = True):
        if bucket_name == "unknown_bucket":
            raise KeyError("Unknown workflow bucket: unknown_bucket")
        self.runs.append(bucket_name)
        return WorkflowTaskResponse(
            task=bucket_name,
            status="ok",
            detail=f"{bucket_name} completed.",
            candidates=1,
        )


def test_workflow_schedule_status_route(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    fake = FakeWorkflowSchedule()
    app.state.workflow_service = fake
    client = TestClient(app)

    response = client.get("/workflow/schedule")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["name"] == "intraday_rotation"
    assert payload[0]["enabled"] is True
    assert payload[0]["last_status"] == "ok"
    assert payload[0]["next_due_at"] == "2026-05-04T10:15:00-04:00"


def test_workflow_run_bucket_route(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    fake = FakeWorkflowSchedule()
    app.state.workflow_service = fake
    client = TestClient(app)

    response = client.post("/workflow/run/intraday_rotation")

    assert response.status_code == 200
    assert response.json()["task"] == "intraday_rotation"
    assert response.json()["status"] == "ok"
    assert fake.runs == ["intraday_rotation"]


def test_workflow_run_bucket_route_rejects_unknown_bucket(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    app.state.workflow_service = FakeWorkflowSchedule()
    client = TestClient(app)

    response = client.post("/workflow/run/unknown_bucket")

    assert response.status_code == 404
    assert "unknown_bucket" in response.json()["detail"]
