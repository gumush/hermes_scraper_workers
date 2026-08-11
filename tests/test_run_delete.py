"""
Deleting a run erases data that cannot be fetched again, so the two modes
are pinned down here: keeping the definition must leave the definition and
nothing else, and wiping must leave nothing. Everything runs against
temporary directories — a bug that ignores the monkeypatched roots would
delete the real outputs tree, so the paths are asserted, not assumed.

The endpoint functions are called directly rather than over a TestClient:
the installed starlette (0.27) builds its client with an `app=` keyword
that httpx 0.28 dropped, so TestClient cannot be constructed here.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
import coordinator  # noqa: E402


RUN_FILE = "demo.hermes-google-places-run.json"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A runs/state/outputs trio holding one finished run."""
    runs, state, outputs = (tmp_path / n for n in ("runs", "state", "outputs"))
    for d in (runs, state, outputs):
        d.mkdir()
    monkeypatch.setattr(coordinator, "RUNS_DIR", runs)
    monkeypatch.setattr(coordinator, "STATE_DIR", state)
    monkeypatch.setattr(coordinator, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(coordinator, "_current", None)

    (runs / RUN_FILE).write_text(json.dumps(
        {"payload": {"run": {"name": "demo-run"}, "place_ids": ["A", "B"]}}))
    exec_dir = state / "demo" / "exec-20260101-000000"
    exec_dir.mkdir(parents=True)
    (exec_dir / "exec.json").write_text('{"state": "done"}')
    place = outputs / "demo-run" / "A"
    place.mkdir(parents=True)
    (place / "info.json").write_text("x" * 4096)

    return {"runs": runs, "state": state, "outputs": outputs}


def status_of(fn, *a, **kw):
    """Status code an endpoint raises, or 200 when it returns normally."""
    try:
        fn(*a, **kw)
    except HTTPException as e:
        return e.status_code
    return 200


def test_footprint_reports_what_would_go(sandbox):
    f = coordinator.run_footprint(RUN_FILE)
    assert f["places"] == 2
    assert f["execs"] == 1
    assert f["delivered_places"] == 1
    assert f["outputs_bytes"] == 4096
    assert f["running"] is False


def test_keeping_json_leaves_only_the_definition(sandbox):
    assert coordinator.delete_run(RUN_FILE, keep_json=True)["json_removed"] is False
    assert (sandbox["runs"] / RUN_FILE).is_file()
    assert not (sandbox["state"] / "demo").exists()
    assert not (sandbox["outputs"] / "demo-run").exists()


def test_wiping_leaves_nothing(sandbox):
    assert coordinator.delete_run(RUN_FILE, keep_json=False)["json_removed"] is True
    assert not (sandbox["runs"] / RUN_FILE).exists()
    assert not (sandbox["state"] / "demo").exists()
    assert not (sandbox["outputs"] / "demo-run").exists()


def test_default_is_the_cautious_one(sandbox):
    """No flag must not silently erase the definition."""
    coordinator.delete_run(RUN_FILE)
    assert (sandbox["runs"] / RUN_FILE).is_file()


def test_running_execution_blocks_delete(sandbox, monkeypatch):
    class Live:
        run_stem = "demo"
        state = "running"
        id = "exec-live"

    monkeypatch.setattr(coordinator, "_current", Live())
    assert status_of(coordinator.delete_run, RUN_FILE) == 409
    assert (sandbox["runs"] / RUN_FILE).is_file()
    assert (sandbox["outputs"] / "demo-run").is_dir()


def test_a_different_run_is_not_blocked_by_a_live_one(sandbox, monkeypatch):
    class Live:
        run_stem = "baska"
        state = "running"
        id = "exec-live"

    monkeypatch.setattr(coordinator, "_current", Live())
    assert status_of(coordinator.delete_run, RUN_FILE) == 200


@pytest.mark.parametrize("name", ["../secrets.json", "a/b.json", "..", "x\\y.json"])
def test_path_like_names_are_refused(sandbox, name):
    assert status_of(coordinator.delete_run, name) == 400
    assert status_of(coordinator.run_footprint, name) == 400


def test_missing_run_is_404(sandbox):
    assert status_of(coordinator.delete_run, "yok.json") == 404


def test_delete_survives_a_run_that_never_ran(sandbox):
    """No exec dir and no outputs yet — deleting must still work."""
    (sandbox["runs"] / "bos.hermes-google-places-run.json").write_text(json.dumps(
        {"payload": {"run": {"name": "bos-run"}, "place_ids": []}}))
    assert coordinator.delete_run("bos.hermes-google-places-run.json",
                                  keep_json=False) == {
        "execs": 0, "outputs_bytes": 0, "state_bytes": 0, "json_removed": True}


def test_start_request_carries_the_attempt_limit(sandbox):
    """
    The per-run attempt count must survive the request, and a run that does
    not mention it must keep the old behaviour rather than silently changing
    how long every place is retried.
    """
    req = coordinator.StartRequest(
        run_file=RUN_FILE, profile="p", provider="local", vm_count=1, slots=1)
    assert req.max_attempts == coordinator.MAX_ATTEMPTS == 3
    assert coordinator.StartRequest(
        run_file=RUN_FILE, profile="p", provider="local", vm_count=1, slots=1,
        max_attempts=6).max_attempts == 6
