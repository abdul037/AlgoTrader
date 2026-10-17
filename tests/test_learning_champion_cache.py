"""Learning champion cache (2026-10-17): the scan read the champion two or three times per
decision, from a table with 0 rows. Every test checks the cached repository against
``_UncachedRepository``, which keeps champion() exactly as it was before the cache."""

from __future__ import annotations

import itertools
import random
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.learning.artifacts import LocalArtifactStore
from app.learning.modeling import LearningModelService
from app.learning.repository import LearningRepository
from app.learning.service import LearningService
from app.models.learning import (
    DecisionSnapshot,
    MetaModelEvaluation,
    MetaModelVersion,
    OutcomeLabel,
)
from app.storage.db import Database
from tests.conftest import make_settings

_STATUSES = ("challenger", "champion", "champion", "retired", "rolled_back")
_MODES = ("shadow", "paper", "live", "retired")
_FEATURES = ("score", "rsi", "atr_pct", "volume_ratio", "trend")
_TABLES = (
    "learning_decision_snapshots",
    "learning_meta_model_versions",
    "learning_model_evaluations",
    "learning_model_promotions",
    "learning_drift_snapshots",
)


class _UncachedRepository(LearningRepository):
    """The reference oracle: champion() verbatim from before the cache, one SELECT per call."""

    def champion(self, *, force_refresh: bool = False) -> MetaModelVersion | None:
        return self._one(
            """
            SELECT * FROM learning_meta_model_versions
            WHERE status = 'champion' AND deployment_mode IN ('paper','live')
            ORDER BY created_at DESC LIMIT 1
            """,
            (),
            self._model,
        )


class _FakeBooster:
    def __init__(self, probability: bool):
        self.probability = probability

    def predict(self, rows):  # varied, deterministic: some candidates keep, some are blocked
        return [(sum(r) % 97) / 97 if self.probability else sum(r) / 100 - 0.5 for r in rows]


class _BoosterService(LearningModelService):  # inference without LightGBM artifacts
    def _load_champion(self, champion):
        return _FakeBooster(False), _FakeBooster(True), list(champion.feature_names)


class _FakeLightGBM:  # training stand-in: these tests are about which champion is the parent
    class Dataset:
        def __init__(self, *args, **kwargs):
            pass

    @staticmethod
    def train(params, dataset, num_boost_round):
        return SimpleNamespace(predict=lambda rows: [1.0] * len(rows), model_to_string=lambda: "")


def _repository(path, cls=LearningRepository, **settings):
    db = Database(make_settings(path, **settings))
    db.initialize()
    return cls(db)


def _oracle(repository: LearningRepository) -> MetaModelVersion | None:
    return _UncachedRepository.champion(repository)


def _dump(model):
    return None if model is None else model.model_dump()


def _age_cache(repository: LearningRepository) -> None:
    if repository._champion_cache is not None:  # what 61 s of waiting does
        stamp, row = repository._champion_cache
        repository._champion_cache = (stamp - 61.0, row)


def _model(rng: random.Random, step: int, **overrides) -> MetaModelVersion:
    fields = {
        "id": f"metamodel_{step}",
        "dataset_version_id": "dataset",
        "status": rng.choice(_STATUSES),
        "deployment_mode": rng.choice(_MODES),
        "feature_names": rng.sample(_FEATURES, k=rng.randint(0, 5)),
        "artifact_uri": "artifact",
        "artifact_hash": "hash",
        "metrics": {
            "expectancy": round(rng.uniform(-0.5, 1), 3),
            "profit_factor": round(rng.uniform(0.7, 2.5), 3),
            "max_drawdown": round(rng.uniform(0, 5), 3),
        },
        "created_at": f"2026-10-{rng.randint(1, 28):02d}T00:00:{step:02d}+00:00",
    }
    return MetaModelVersion(**{**fields, **overrides})


def _features(rng: random.Random) -> dict[str, float]:
    names = _FEATURES if rng.random() < 0.75 else rng.sample(_FEATURES, k=rng.randint(0, 5))
    return {name: round(rng.uniform(0, 100), 2) for name in names}


def _champion_reads(repository: LearningRepository, monkeypatch) -> list[str]:
    reads: list[str] = []
    real_one = repository._one

    def counted(query, params, factory):
        if "status = 'champion'" in query:
            reads.append(query)
        return real_one(query, params, factory)

    monkeypatch.setattr(repository, "_one", counted)
    return reads


def test_champion_matches_the_uncached_read_after_every_write(tmp_path) -> None:
    db = _repository(tmp_path).db
    for seed in range(15):
        with db.connect() as connection:
            connection.execute("DELETE FROM learning_meta_model_versions")
        repository = LearningRepository(db)  # and a new cache
        rng = random.Random(seed)
        ids: list[str] = []
        for step in range(40):
            if not ids or rng.random() < 0.4:
                ids.append(repository.record_model(_model(rng, step)).id)
            else:
                repository.update_model_status(
                    rng.choice(ids),
                    status=rng.choice(_STATUSES),
                    deployment_mode=rng.choice(_MODES),
                )
            for _ in range(rng.randint(1, 3)):  # cached reads between the writes
                assert _dump(repository.champion()) == _dump(_oracle(repository)), (seed, step)


def test_the_scan_reads_the_champion_once_per_ttl(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    reads = _champion_reads(repository, monkeypatch)
    assert all(repository.champion() is None for _ in range(50))
    assert len(reads) == 1
    _age_cache(repository)
    assert repository.champion() is None
    assert len(reads) == 2  # a cached "none" expires too
    assert repository.champion(force_refresh=True) is None
    assert len(reads) == 3


def test_each_call_gets_its_own_champion_object(tmp_path) -> None:
    repository = _repository(tmp_path)
    repository.record_model(
        _model(
            random.Random(1), 1, status="champion", deployment_mode="paper", feature_names=["rsi"]
        )
    )
    first = repository.champion()
    first.feature_names.append("mutated")
    first.metrics["expectancy"] = 99.0
    second = repository.champion()  # from the cache: rebuilt from the row, like the old SELECT
    assert second is not first
    assert _dump(second) == _dump(_oracle(repository))


def test_a_write_by_another_process_shows_within_the_ttl(tmp_path) -> None:
    repository = _repository(tmp_path)
    other = LearningRepository(Database(repository.db.settings))  # e.g. a separate learning worker
    assert repository.champion() is None  # cached
    other.record_model(_model(random.Random(3), 1, status="champion", deployment_mode="paper"))
    assert repository.champion() is None  # another process's write shows up to 60 s late...
    _age_cache(repository)
    assert _dump(repository.champion()) == _dump(_oracle(repository))  # ...then it shows
    other.update_model_status("metamodel_1", status="retired", deployment_mode="retired")
    assert repository.champion().id == "metamodel_1"  # a cached champion is kept up to 60 s too
    _age_cache(repository)
    assert repository.champion() is None
    other.update_model_status("metamodel_1", status="champion", deployment_mode="live")
    assert repository.champion() is None  # the cached "none"
    assert repository.status_counts()["active_model_version"] == "metamodel_1"  # reports: fresh
    other.update_model_status("metamodel_1", status="rolled_back", deployment_mode="retired")
    assert repository.champion(force_refresh=True) is None  # as is any forced read
    assert _oracle(repository) is None


def test_a_model_write_during_the_refresh_is_not_cached_away(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    repository.record_model(
        _model(random.Random(2), 1, status="challenger", deployment_mode="shadow")
    )
    real_one = repository._one

    def racing_one(query, params, factory):
        stale = real_one(query, params, factory)  # the SELECT ran before the promotion committed
        if "status = 'champion'" in query:
            monkeypatch.setattr(repository, "_one", real_one)
            repository.update_model_status(
                "metamodel_1", status="champion", deployment_mode="paper"
            )
        return stale

    monkeypatch.setattr(repository, "_one", racing_one)
    assert repository.champion() is None  # the stale read is used once...
    assert repository.champion().id == "metamodel_1"  # ...not kept


def test_a_read_before_the_write_commits_is_not_kept(tmp_path, monkeypatch) -> None:
    # The cache is dropped after the COMMIT: a read in between sees the old row and keeps it.
    repository = _repository(tmp_path)
    repository.record_model(
        _model(random.Random(4), 1, status="challenger", deployment_mode="shadow")
    )
    real_connect, armed, seen = repository.db.connect, [True], []

    @contextmanager
    def read_before_commit():
        with real_connect() as connection:
            yield connection
            if armed:  # the UPDATE has run; its COMMIT has not
                armed.clear()
                seen.append(repository.champion())

    monkeypatch.setattr(repository.db, "connect", read_before_commit)
    repository.update_model_status("metamodel_1", status="champion", deployment_mode="paper")
    assert seen == [None]
    assert repository.champion().id == "metamodel_1" == _oracle(repository).id


def test_a_write_whose_commit_reply_is_lost_still_drops_the_cache(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    repository.record_model(_model(random.Random(6), 1, status="champion", deployment_mode="paper"))
    assert repository.champion().id == "metamodel_1"  # cached
    real_connect = repository.db.connect

    @contextmanager
    def committed_then_lost():
        with real_connect() as connection:
            yield connection
        raise OSError("server closed the connection unexpectedly")  # after the COMMIT ran

    monkeypatch.setattr(repository.db, "connect", committed_then_lost)
    with pytest.raises(OSError):  # the caller sees the same error as before
        repository.update_model_status("metamodel_1", status="retired", deployment_mode="retired")
    monkeypatch.setattr(repository.db, "connect", real_connect)
    assert repository.champion() is None
    assert _oracle(repository) is None


def test_concurrent_readers_never_hide_a_write_that_returned(tmp_path) -> None:
    repository = _repository(tmp_path)
    rng = random.Random(7)
    for step in range(4):
        repository.record_model(_model(rng, step, status="challenger", deployment_mode="shadow"))
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                repository.champion()
                time.sleep(0.0005)  # let the writer have the GIL between cache hits
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for step in range(60):
            repository.update_model_status(
                f"metamodel_{step % 4}",
                status=rng.choice(_STATUSES),
                deployment_mode=rng.choice(_MODES),
            )
            assert _dump(repository.champion()) == _dump(_oracle(repository)), step
    finally:
        stop.set()
        for thread in readers:
            thread.join()
    assert errors == []


def _flows(tmp_path, cls, seed: int, monkeypatch) -> dict:
    """A random mix of every learning call that reads or changes the champion."""

    ids, clock = itertools.count(), itertools.count()
    start = datetime(2026, 10, 17, 14, tzinfo=UTC)
    monkeypatch.setattr("app.models.learning.generate_id", lambda prefix: f"{prefix}_{next(ids)}")
    for module in ("app.models.learning", "app.learning.repository", "app.learning.modeling"):
        monkeypatch.setattr(f"{module}.utc_now", lambda: start + timedelta(seconds=next(clock)))
    rng = random.Random(seed)
    repository = _repository(
        tmp_path,
        cls,
        model_deployment_mode="gating",
        learning_max_drift_score=rng.choice((0.4, 0.6, 0.8)),
        learning_min_probability_to_keep=0.6,
        learning_auto_promote_paper_enabled=rng.random() < 0.5,
        learning_live_promotion_enabled=True,
    )
    events: list[tuple[str, dict]] = []
    logs = SimpleNamespace(log=lambda event, payload: events.append((event, payload)))
    models = _BoosterService(
        settings=repository.db.settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        run_logs=logs,
        institutional_service=SimpleNamespace(readiness=lambda: {"ready": True}),
    )
    learning = LearningService(
        settings=repository.db.settings,
        repository=repository,
        executions=None,
        proposals=None,
        market_data=None,
        model_service=models,
        critic=None,
        run_logs=logs,
    )
    known: list[str] = []
    transcript: list[tuple] = []
    for step in range(50):
        action = rng.choice(
            ("model", "model", "evaluation", "evaluation", "promote", "promote", "rollback")
            + ("unsafe", "evaluate", "adjust", "adjust", "adjust", "drift", "drift", "capture")
            + ("capture", "capture", "status", "age")
        )
        target = rng.choice(known) if known else None
        try:
            if action == "model" or target is None:
                action, result = "model", _dump(repository.record_model(_model(rng, step)))
                known.append(result["id"])
            elif action == "evaluation":
                evaluation = MetaModelEvaluation(
                    model_version_id=target,
                    status="passed",
                    leakage_passed=True,
                    schema_passed=True,
                )
                result = _dump(repository.record_evaluation(evaluation))
            elif action == "promote":
                mode, signer = rng.choice(("paper", "live")), rng.choice(("", "operator"))
                result = _dump(models.promote(target, target_mode=mode, signed_by=signer))
            elif action == "rollback":
                result = _dump(models.rollback(target, signed_by="operator"))
            elif action == "unsafe":
                result = _dump(models.rollback_if_unsafe())
            elif action == "evaluate":
                result = _dump(models.evaluate_challenger(target))
            elif action == "adjust":
                result = models.safe_adjust(
                    deterministic_score=round(rng.uniform(40, 95), 2),
                    deterministic_eligible=rng.random() < 0.7,
                    features=_features(rng),
                )
            elif action == "drift":
                result = _dump(models.record_drift(_features(rng)))
            elif action == "capture":  # the scan path: safe_adjust, the decision row, record_drift
                record = SimpleNamespace(
                    id=f"scan_{step}",
                    payload={"measurements": _features(rng), "metadata": {"score": 70.0}},
                    alert_eligible=rng.random() < 0.7,
                    final_score=round(rng.uniform(40, 95), 2),
                    symbol=rng.choice(("NVDA", "AMD", "MU")),
                    strategy_name="swing",
                    timeframe="1d",
                    status=rng.choice(("candidate", "alerted", "watchlist", "rejected")),
                    scan_task="scan",
                    reason_codes=[],
                    rejection_reasons=[],
                    created_at=f"2026-10-17T15:00:{step:02d}+00:00",
                )
                result = _dump(learning.capture_scan_decision(record))
            elif action == "status":
                result = repository.status_counts()
            else:
                _age_cache(repository)
                result = None
        except Exception as exc:  # noqa: BLE001 - both repositories must fail alike
            result = ("raised", type(exc).__name__, str(exc))
        transcript.append((step, action, result))
    with repository.db.connect() as connection:
        rows = {
            table: [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in _TABLES
        }
    return {"transcript": transcript, "events": events, "rows": rows}


def test_learning_flows_match_the_uncached_champion(tmp_path, monkeypatch) -> None:
    seen: Counter[str] = Counter()
    for seed in range(8):
        old = _flows(tmp_path / f"old{seed}", _UncachedRepository, seed, monkeypatch)
        new = _flows(tmp_path / f"new{seed}", LearningRepository, seed, monkeypatch)
        assert new == old, seed
        for _step, action, result in new["transcript"]:
            if action == "promote" and isinstance(result, dict) and result["approved"]:
                seen["promoted"] += 1
            if action in {"adjust", "capture"} and result:
                adjustment = (
                    result if action == "adjust" else result["decision"]["model_adjustment"]
                )
                seen[
                    "scored_by_a_champion" if adjustment["model_version_id"] else "no_champion"
                ] += 1
                seen["blocked"] += bool(adjustment["model_blocked"])
        seen["automatic_rollbacks"] += len(new["events"])
    assert min(seen.values()) > 0, seen  # the mix really exercised every champion change


def _rival_promotion(tmp_path, cls) -> dict[str, tuple[str, str]]:
    """The API process has champion A cached; a separate worker retires A and promotes B."""

    repository = _repository(tmp_path, cls, learning_auto_promote_paper_enabled=True)
    other = LearningRepository(Database(repository.db.settings))
    rng = random.Random(5)
    for step, status in ((1, "champion"), (2, "challenger"), (3, "challenger")):
        mode = "paper" if status == "champion" else "shadow"
        repository.record_model(_model(rng, step, status=status, deployment_mode=mode))
    repository.record_evaluation(
        MetaModelEvaluation(
            model_version_id="metamodel_3", status="passed", leakage_passed=True, schema_passed=True
        )
    )
    service = LearningModelService(
        settings=repository.db.settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        run_logs=SimpleNamespace(log=lambda *_args: None),
    )
    assert repository.champion().id == "metamodel_1"
    other.update_model_status("metamodel_1", status="retired", deployment_mode="retired")
    other.update_model_status("metamodel_2", status="champion", deployment_mode="paper")
    assert service.promote("metamodel_3", target_mode="paper").approved is True
    return {model.id: (model.status, model.deployment_mode) for model in repository.list_models()}


def test_a_promotion_retires_the_champion_another_process_just_promoted(tmp_path) -> None:
    # A cached champion here would re-retire A and leave B and the new model both champion.
    statuses = _rival_promotion(tmp_path / "new", LearningRepository)
    assert statuses == _rival_promotion(tmp_path / "old", _UncachedRepository)
    assert statuses == {
        "metamodel_1": ("retired", "retired"),
        "metamodel_2": ("retired", "retired"),
        "metamodel_3": ("champion", "paper"),
    }


def _rival_lifecycle(tmp_path, cls, monkeypatch) -> dict:
    """Before each governed step the API process caches the champion and a worker swaps it."""

    monkeypatch.setitem(sys.modules, "lightgbm", _FakeLightGBM)
    repository = _repository(
        tmp_path,
        cls,
        learning_training_enabled=True,
        learning_min_training_rows=10,
        learning_embargo_rows=2,
    )
    other = LearningRepository(Database(repository.db.settings))
    rng = random.Random(9)
    for index in range(40):
        decision = repository.record_decision(
            DecisionSnapshot(
                decision_key=f"scan:{index}",
                symbol="NVDA",
                strategy_name="swing",
                timeframe="1d",
                stage="scan_decision",
                accepted=True,
                features={"score": float(index), "rsi": round(rng.uniform(20, 80), 2)},
                created_at=f"2026-09-{1 + index // 2:02d}T{index % 2:02d}:00:00+00:00",
            )
        )
        net_r = (1.0, -0.5)[index % 2]  # every holdout loss is a drawdown
        repository.record_label(
            OutcomeLabel(
                decision_snapshot_id=decision.id,
                label_type="real",
                status="closed",
                net_r=net_r,
                profitable=net_r > 0,
                source="broker_reconciliation",
            )
        )
    safe = {"expectancy": 1.0, "profit_factor": 2.0, "max_drawdown": 0.0}
    unsafe = {"expectancy": -1.0, "profit_factor": 0.5}  # no drawdown: never "worse than" it
    repository.record_model(
        _model(rng, 1, status="champion", deployment_mode="paper", metrics=safe)
    )
    repository.record_model(
        _model(rng, 2, status="challenger", deployment_mode="shadow", metrics=unsafe)
    )
    service = LearningModelService(
        settings=repository.db.settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        run_logs=SimpleNamespace(log=lambda *_args: None),
    )

    def swap(retire: str, promote: str) -> None:
        repository.champion()  # the scan caches the current champion...
        other.update_model_status(retire, status="retired", deployment_mode="retired")
        other.update_model_status(promote, status="champion", deployment_mode="paper")

    swap("metamodel_1", "metamodel_2")
    trained = service.train_challenger()
    swap("metamodel_2", "metamodel_1")
    evaluation = service.evaluate_challenger(trained.id)
    swap("metamodel_1", "metamodel_2")
    rollback = service.rollback_if_unsafe()
    assert trained.metrics["max_drawdown"] > 0  # so A's 0.0 drawdown blocks it, B's none does not
    return {
        "parent": trained.parent_version_id,
        "drawdown_blocked": "drawdown_worse_than_champion" in evaluation.blockers,
        "rolled_back": None if rollback is None else (rollback.model_version_id, rollback.blockers),
    }


def test_training_evaluation_and_safety_rollback_read_a_fresh_champion(
    tmp_path, monkeypatch
) -> None:
    new = _rival_lifecycle(tmp_path / "new", LearningRepository, monkeypatch)
    assert new == _rival_lifecycle(tmp_path / "old", _UncachedRepository, monkeypatch)
    assert new == {
        "parent": "metamodel_2",  # the champion the worker had just promoted
        "drawdown_blocked": True,  # judged against A, which the worker had just restored
        "rolled_back": (
            "metamodel_2",
            ["negative_champion_expectancy", "champion_profit_factor_below_one"],
        ),
    }
