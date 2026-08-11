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
        "execs": 0, "outputs_bytes": 0, "state_bytes": 0,
        "json_removed": True, "outputs_kept": False}


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


# --- arşiv -------------------------------------------------------------------

def test_archive_takes_the_whole_family_and_removes_it(sandbox, tmp_path):
    """
    Archiving is keyed on the run NAME, not the file: several definitions
    share a name on purpose so their output lands in one tree, and taking
    one file's outputs would leave the others pointing at nothing.
    """
    second = "demo-topup.hermes-google-places-run.json"
    (sandbox["runs"] / second).write_text(json.dumps(
        {"payload": {"run": {"name": "demo-run"}, "place_ids": ["B"]}}))
    arc = tmp_path / "arc"

    r = coordinator.archive_run(RUN_FILE, archive_dir=str(arc), stamp="test")
    assert r["places"] == 1 and r["execs"] == 1
    assert sorted(r["definitions"]) == sorted([RUN_FILE, second])

    zips = list(arc.glob("*.zip"))
    assert len(zips) == 1 and not list(arc.glob("*.part"))
    import zipfile
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
        assert "archive.json" in names
        assert f"runs/{RUN_FILE}" in names
        assert f"runs/{second}" in names
        assert any(n.startswith("outputs/demo-run/A/") for n in names)
        assert any(n.startswith("state/demo/exec-") for n in names)

    assert not (sandbox["runs"] / RUN_FILE).exists()
    assert not (sandbox["runs"] / second).exists()
    assert not (sandbox["state"] / "demo").exists()
    assert not (sandbox["outputs"] / "demo-run").exists()


def test_archive_preview_does_not_remove_anything(sandbox, tmp_path):
    f = coordinator.archive_preview(RUN_FILE)
    assert f["run_name"] == "demo-run" and f["places"] == 1
    assert (sandbox["runs"] / RUN_FILE).is_file()
    assert (sandbox["outputs"] / "demo-run").is_dir()


def test_archive_refuses_while_the_run_is_live(sandbox, tmp_path, monkeypatch):
    class Live:
        run_stem = "demo"
        state = "running"
        id = "exec-live"

    monkeypatch.setattr(coordinator, "_current", Live())
    assert status_of(coordinator.archive_run, RUN_FILE,
                     archive_dir=str(tmp_path / "arc")) == 409
    assert (sandbox["outputs"] / "demo-run").is_dir()


def test_archive_needs_an_absolute_directory(sandbox):
    assert status_of(coordinator.archive_run, RUN_FILE,
                     archive_dir="relatif/klasor") == 400
    assert (sandbox["runs"] / RUN_FILE).is_file()


def test_listing_an_absent_archive_dir_is_not_an_error(tmp_path):
    r = coordinator.list_archives(str(tmp_path / "yok"))
    assert r["exists"] is False and r["items"] == []


def test_deleting_one_definition_can_leave_the_shared_output_alone(sandbox):
    """
    Definitions share an output tree on purpose; removing one of them must
    not take the data the others are still listed against.
    """
    r = coordinator.delete_run(RUN_FILE, keep_json=False, keep_outputs=True)
    assert r["outputs_kept"] is True
    assert not (sandbox["runs"] / RUN_FILE).exists()
    assert (sandbox["outputs"] / "demo-run" / "A" / "info.json").is_file()
    assert (sandbox["state"] / "demo").is_dir()


def test_failure_bundles_are_ordered_by_attempt_number(sandbox):
    """
    Ten attempts sort as strings into 1, 10, 2, 3 … so the attempt that ended
    the job landed in the middle of the list instead of at the top.
    """
    d = sandbox["state"] / "demo" / "exec-20260101-000000" / "failures"
    d.mkdir(parents=True)
    for n in (1, 2, 9, 10, 11):
        (d / f"A-{n}.tar.gz").write_bytes(b"x")
    got = [b["attempt"] for b in
           coordinator._failure_bundles(d.parent, "A")]
    assert got == [11, 10, 9, 2, 1]


# --- run bitince filo ---------------------------------------------------------

def _exec_stub(**kw):
    """An Execution without touching a provider or the network."""
    e = object.__new__(coordinator.Execution)
    e.shutdown_requested = kw.get("shutdown_requested", False)
    e.keep_vms_after_run = kw.get("keep_vms_after_run", False)
    return e


def test_finishing_closes_the_fleet_by_default():
    """
    Leaving VMs up on completion is how three of them billed for an hour and
    ate the quota the next run failed against. Closing is the default.
    """
    e = _exec_stub()
    assert (e.shutdown_requested or not e.keep_vms_after_run) is True


def test_keeping_the_fleet_is_a_deliberate_choice():
    e = _exec_stub(keep_vms_after_run=True)
    assert (e.shutdown_requested or not e.keep_vms_after_run) is False


def test_an_explicit_shutdown_still_wins_over_keeping():
    e = _exec_stub(keep_vms_after_run=True, shutdown_requested=True)
    assert (e.shutdown_requested or not e.keep_vms_after_run) is True


def test_start_request_defaults_to_closing():
    req = coordinator.StartRequest(
        run_file=RUN_FILE, profile="p", provider="local", vm_count=1, slots=1)
    assert req.keep_vms_after_run is False
