"""
Local coordinator for distributed place scraping.

Reads a hermes-google-places-run JSON (place_ids), provisions N worker VMs
(local subprocess or GCP), keeps each VM's queue topped up (depth per VM),
pulls finished packages back immediately (transfer is separate from scrape
so a slow download never blocks dispatch), retries failures on other VMs,
and on shutdown deletes every VM in parallel and VERIFIES none remain.

State: orchestrator/state/<run-stem>/exec-<ts>/
    exec.json     job/vm state (persisted every transition)
    log.jsonl     per-run operation log (the "islem logu")
    packages/     pulled tar.gz packages, exactly as the worker built them

Delivered data (each package is unpacked here as it lands):
    outputs/<run name>/<place_id>/info.json
                                 /reviews.json
                                 /images.json
                                 /images/ownerimages/*.jpg
                                 /images/reviewimages/*.jpg
Profiles: orchestrator/profiles.json  (Standart - Google seeded; update mode
uses the newest review date per place from the latest completed exec of the
same run and stops at that boundary via the scraper's date-filter early stop.)

Run:  python orchestrator/coordinator.py   (port: ORCH_PORT, default 8140)
"""

import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from providers import GcpProvider, LocalProvider  # noqa: E402

log = logging.getLogger("orchestrator")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

ORCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = ORCH_DIR.parent
RUNS_DIR = ORCH_DIR / "runs"
STATE_DIR = ORCH_DIR / "state"
OUTPUTS_DIR = REPO_ROOT / "outputs"
PROFILES_PATH = ORCH_DIR / "profiles.json"

# max_reviews         kaç yorum indirilecek (0 = hepsi)
# min_review_photos   bu kadar yorum fotoğrafı görülene dek max_reviews aşılır
# max_reviews_cap     ama en fazla bu kadar yoruma kadar (foto hedefi sınırsız değil)
# place_photos_limit  kaç owner (işletme) fotoğrafı indirilecek
DEFAULT_PROFILES = {
    "Standart - Google": {
        "mode": "fresh",
        "max_reviews": 100,
        "max_reviews_cap": 250,
        "min_review_photos": 50,
        "place_photos_limit": 60,
        "download_review_images": True,
    },
    "Update": {
        "mode": "update",
        "place_photos_limit": 60,
        "download_review_images": True,
    },
}

MAX_ATTEMPTS = 3         # default VMs a place is tried on; per-run overridable
POLL_INTERVAL = 3
THROUGHPUT_WINDOW = 30   # deliveries averaged for the live rate / ETA
QUEUE_BUFFER = 1         # extra job held per VM so a freed slot restarts at once
DRAIN_SPARE = 2          # VMs kept beyond the remaining work, for stragglers
MAX_REPLACEMENTS = 24    # ceiling on VMs spun up to replace lost ones
VM_FAIL_LIMIT = 3        # consecutive failed jobs before a VM is retired
# Failures that describe the PAGE, not the machine. A run of these says the
# place did not render readably, which the next VM is no more likely to fix —
# scoring them against the worker retired three healthy VMs in one Antalya
# run, each with a working IP, while the retries were succeeding elsewhere.
# These are every code the scraper captures (modules/scraper.py); all four
# describe what the page did. "no_reviews_collected" stays on the list for
# the same reason: an empty list after a working tab is a page problem, and
# the retry lands on a different VM anyway.
PAGE_FAULT_CODES = {"reviews_undetermined", "reviews_tab_unreachable",
                    "wrong_place_on_details", "no_reviews_collected",
                    # Reviews exist but were not served to this client. Every
                    # address we have tried gets the same answer, so it says
                    # nothing about the machine that asked.
                    "reviews_withheld"}
DRAIN_TIMEOUT = 90       # seconds spent rescuing packages before VMs are killed
LIVE_STATES = ("starting", "provisioning", "running", "closing")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def place_url(place_id: str) -> str:
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def safe_name(value: str) -> str:
    """Filesystem-safe single path segment (same rule as the worker's)."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(value))
    return out.strip("._") or "unknown"


def untar_stripped(tar_path: Path, dst: Path) -> Path:
    """
    Extract a worker package into `dst`, dropping the tar's single top-level
    directory so `dst` itself becomes the place folder. Members escaping the
    destination are skipped.
    """
    dst.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 2 or ".." in parts:
                continue
            target = dst / Path(*parts[1:])
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as f:
                    shutil.copyfileobj(tar.extractfile(m), f)
    return dst


def unpack_to_outputs(tar_path: Path, run_name: str, place_id: str) -> Path:
    """
    Materialise a pulled package under the canonical delivery tree:

        outputs/<run name>/<place_id>/{info,reviews,images}.json
        outputs/<run name>/<place_id>/images/{ownerimages,reviewimages}/

    A re-run of the same run replaces that place's folder.
    """
    dst = OUTPUTS_DIR / safe_name(run_name) / safe_name(place_id)
    if dst.exists():
        shutil.rmtree(dst)
    return untar_stripped(tar_path, dst)


class Execution:
    """One run execution: VMs + jobs + log, driven by a background thread."""

    def __init__(self, run_file: str, profile_name: str, profile: Dict[str, Any],
                 provider_name: str, vm_count: int, slots: int,
                 machine_type: str = "e2-standard-2", spot: bool = False,
                 zones: Optional[List[str]] = None,
                 place_ids: Optional[List[str]] = None,
                 capture_failures: bool = True,
                 max_attempts: int = MAX_ATTEMPTS,
                 max_replacements: int = MAX_REPLACEMENTS,
                 vm_fail_limit: int = VM_FAIL_LIMIT,
                 drain_timeout: int = DRAIN_TIMEOUT,
                 keep_vms_after_run: bool = False):
        self.id = datetime.now(timezone.utc).strftime("exec-%Y%m%d-%H%M%S")
        self.run_file = run_file
        run_json = json.loads((RUNS_DIR / run_file).read_text())
        payload = run_json["payload"]
        self.run_name = payload["run"]["name"]
        # A retry run carries the same run name (so it lands in the same
        # outputs tree) but only the places it is meant to redo.
        self.place_ids: List[str] = list(place_ids or payload["place_ids"])
        self.profile_name = profile_name
        self.profile = profile
        self.provider_name = provider_name
        self.vm_count = vm_count
        self.slots = slots
        self.api_key = secrets.token_hex(16)

        stem = Path(run_file).stem.split(".")[0]
        self.dir = STATE_DIR / stem / self.id
        self.pkg_dir = self.dir / "packages"
        self.pkg_dir.mkdir(parents=True, exist_ok=True)
        self.run_stem = stem

        self.state = "starting"
        self.error: Optional[str] = None
        self.vms: List[Dict[str, Any]] = [
            {"index": i, "name": f"hermes-w{i:02d}-{uuid.uuid4().hex[:6]}",
             "state": "pending", "slots": slots, "active": [], "done": 0,
             "failed": 0, "consecutive_fails": 0, "egress_ip": None,
             "health_fails": 0, "state_reason": "sıraya alındı",
             "state_since": _now()}
            for i in range(vm_count)
        ]
        self.jobs: Dict[str, Dict[str, Any]] = {
            pid: {"place_id": pid, "state": "pending", "attempts": 0,
                  "vm": None, "remote_id": None, "package": None,
                  "latest_review_date": None, "error": None}
            for pid in self.place_ids
        }
        self.baseline: Dict[str, str] = {}   # place_id -> after-date (update mode)
        self.deliveries: List[datetime] = []  # when each package landed here
        self.replacements = 0                 # VMs spun up to replace losses
        # What shutdown is doing, so "closing" is not a silent state: the
        # transfer counter reads 0 during a drain because those jobs are not
        # in-flight scrapes, and a fleet that looks idle is the moment someone
        # wonders why the VMs are still up.
        self.drain: Optional[Dict[str, Any]] = None
        self._replacement_cap_logged = False
        self.shutdown_requested = False
        self.paused = False
        self.remaining_after_shutdown: Optional[List[str]] = None
        self._lock = threading.Lock()
        self._transfer_pool = ThreadPoolExecutor(max_workers=4)

        self.machine_type = machine_type
        self.spot = spot
        self.capture_failures = capture_failures
        # How many VMs a place is handed to before it is called failed. Worth
        # turning up when the failures are page-side rather than place-side: a
        # measured 7 of 15 flagged places were delivered in full by a later
        # attempt, and each attempt lands on a different machine and address.
        self.max_attempts = max(1, int(max_attempts))
        self.max_replacements = max(0, int(max_replacements))
        self.vm_fail_limit = max(1, int(vm_fail_limit))
        self.drain_timeout = max(10, int(drain_timeout))
        # Finishing used to leave the fleet up so the run could be continued.
        # It also left it billing, and the next run replaced the execution
        # object — the only handle on those machines. Three survived an hour
        # that way and ate the quota the next run then failed against. The
        # default is now to close, and keeping them is the deliberate choice.
        self.keep_vms_after_run = bool(keep_vms_after_run)
        self.zones = zones
        if provider_name == "gcp":
            self.provider = GcpProvider(self.api_key, self.dir / "vmout",
                                        machine_type=machine_type, spot=spot,
                                        zones=zones)
        else:
            self.provider = LocalProvider(self.api_key, self.dir / "vmout")

    # --- logging / persistence ---

    def log_event(self, event: str, **fields) -> None:
        rec = {"ts": _now(), "event": event, **fields}
        with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.info(f"[{self.id}] {event} {fields if fields else ''}")

    def persist(self) -> None:
        with self._lock:
            snap = self.snapshot()
        (self.dir / "exec.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")

    def snapshot(self) -> Dict[str, Any]:
        jobs = list(self.jobs.values())
        counts = {}
        for j in jobs:
            counts[j["state"]] = counts.get(j["state"], 0) + 1
        return {
            "exec_id": self.id, "run_file": self.run_file,
            "run_name": self.run_name, "state": self.state,
            "error": self.error,
            "profile": self.profile_name, "provider": self.provider_name,
            "vm_count": self.vm_count, "slots": self.slots,
            "machine_type": self.machine_type, "spot": self.spot,
            "total": len(jobs), "counts": counts,
            "max_attempts": self.max_attempts,
            "max_replacements": self.max_replacements,
            "replacements": self.replacements,
            "vm_fail_limit": self.vm_fail_limit,
            "keep_vms_after_run": self.keep_vms_after_run,
            "vms": [{k: v for k, v in vm.items() if k != "active"} |
                    {"active": len(vm["active"])} for vm in self.vms],
            "jobs": {p: {**{k: j[k] for k in
                            ("state", "attempts", "vm", "error",
                             "latest_review_date")},
                         "progress": j.get("progress")}
                     for p, j in self.jobs.items()},
            "shutdown_requested": self.shutdown_requested,
            "paused": self.paused,
            "throughput": self.throughput(),
            "drain": self.drain,
            "remaining_after_shutdown": self.remaining_after_shutdown,
        }

    def throughput(self, window: int = THROUGHPUT_WINDOW) -> Dict[str, Any]:
        """
        How fast finished work is actually landing, and what that implies for
        the rest of the queue.

        On a long run the useful question is not how long one place took —
        places vary from one minute to ten — but how often a package arrives
        with every VM and slot in play. That is the gap between deliveries,
        averaged over the last `window` of them so the figure tracks current
        conditions rather than the whole run.
        """
        stamps = [t for t in self.deliveries[-(window + 1):]]
        out: Dict[str, Any] = {"delivered": len(self.deliveries),
                               "window": max(0, len(stamps) - 1),
                               "seconds_per_job": None, "per_hour": None,
                               "remaining": None, "eta_seconds": None}
        remaining = sum(1 for j in self.jobs.values()
                        if j["state"] in ("pending", "scraping", "transferring"))
        out["remaining"] = remaining
        if len(stamps) < 2:
            return out
        span = (stamps[-1] - stamps[0]).total_seconds()
        gaps = len(stamps) - 1
        if span <= 0:
            return out
        per_job = span / gaps
        out["seconds_per_job"] = round(per_job, 1)
        out["per_hour"] = round(3600 / per_job, 1)
        if remaining:
            out["eta_seconds"] = round(per_job * remaining)
        return out

    # --- update-mode baseline ---

    def load_baseline(self) -> None:
        base = STATE_DIR / self.run_stem
        candidates = sorted(
            [d for d in base.glob("exec-*") if (d / "summary.json").exists()],
            reverse=True)
        for d in candidates:
            if d.name == self.id:
                continue
            data = json.loads((d / "summary.json").read_text())
            self.baseline = {p: v["latest_review_date"]
                             for p, v in data.items() if v.get("latest_review_date")}
            self.log_event("baseline_loaded", source=d.name,
                           places=len(self.baseline))
            return
        self.log_event("baseline_missing",
                       note="update mode but no completed exec found; "
                            "falling back to Standart limits")

    def save_summary(self) -> None:
        summary = {p: {"state": j["state"],
                       "latest_review_date": j["latest_review_date"]}
                   for p, j in self.jobs.items()}
        (self.dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- job payloads ---

    def job_request(self, place_id: str) -> Dict[str, Any]:
        prof = self.profile
        std = DEFAULT_PROFILES["Standart - Google"]
        req: Dict[str, Any] = {
            "url": place_url(place_id),
            "transfer": "none",
            "download_review_images": bool(prof.get("download_review_images")),
            "place_photos_limit": int(
                prof.get("place_photos_limit", std["place_photos_limit"]) or 0),
            "capture_failures": bool(self.capture_failures),
            "meta": {"place_id": place_id},
        }
        if prof.get("mode") == "update" and place_id in self.baseline:
            req["max_reviews"] = 0
            req["sort_by"] = "newest"
            req["date_filter"] = {"after": self.baseline[place_id][:10],
                                  "mode": "early_stop"}
        else:
            req["max_reviews"] = int(prof.get("max_reviews", std["max_reviews"]) or 0)
            req["min_review_photos"] = int(
                prof.get("min_review_photos", std["min_review_photos"]) or 0)
            req["max_reviews_cap"] = int(
                prof.get("max_reviews_cap", std["max_reviews_cap"]) or 0)
            req["sort_by"] = "newest"
        return req

    # --- lifecycle thread ---

    def start(self) -> None:
        threading.Thread(target=self._main, daemon=True).start()

    def _main(self) -> None:
        try:
            if self.profile.get("mode") == "update":
                self.load_baseline()
            self.state = "provisioning"
            self.log_event("provisioning_started", vms=self.vm_count,
                           provider=self.provider_name)
            self.persist()
            # Provisioning is spent waiting on gcloud, not on this machine, so
            # the whole fleet boots at once — a cap of 8 turned 16 VMs into two
            # rounds. And dispatch starts with the first VM that is ready
            # rather than the last: waiting for all of them left the earliest
            # machine idle for minutes while the slowest region caught up.
            # _drive only ever dispatches to VMs in state "ready", so the rest
            # join as they arrive.
            pool = ThreadPoolExecutor(max_workers=max(1, self.vm_count))
            futures = [pool.submit(self._provision_vm, vm) for vm in self.vms]
            pool.shutdown(wait=False)

            def _settled() -> bool:
                return all(f.done() for f in futures)

            while not any(vm["state"] == "ready" for vm in self.vms):
                if _settled():
                    raise RuntimeError("no VM became ready")
                if self.shutdown_requested:
                    return
                time.sleep(2)

            ready = [vm for vm in self.vms if vm["state"] == "ready"]
            self.state = "running"
            self.log_event("running", ready_vms=len(ready),
                           still_provisioning=sum(
                               1 for vm in self.vms if vm["state"] == "provisioning"))
            self.persist()
            self._drive()
            self.state = "completed"
            self.log_event("run_completed", **self.snapshot()["counts"])
            self.save_summary()
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = str(e)
            self.log_event("run_error", error=str(e))
        finally:
            self.persist()
            if self.shutdown_requested or not self.keep_vms_after_run:
                # Let what is already coming down arrive before the machines
                # holding it are deleted.
                left = self._await_transfers()
                if left:
                    log.warning("%d transfer still in flight at shutdown", left)
                self._shutdown_all()
            else:
                self.log_event("vms_kept", count=sum(
                    1 for v in self.vms if v["state"] != "deleted"))

    def _provision_vm(self, vm: Dict[str, Any]) -> None:
        self._set_vm_state(vm, "provisioning",
                           f"{self.machine_type} açılıyor"
                           + (" · spot" if self.spot else ""))
        self.log_event("vm_provisioning", vm=vm["name"])
        try:
            self.provider.provision(vm)
            vm["egress_ip"] = self.provider.egress_ip(vm)
            self._set_vm_state(
                vm, "ready",
                "worker /health cevap verdi · çıkış IP "
                + (vm["egress_ip"] or "okunamadı"))
            self.log_event("vm_ready", vm=vm["name"], port=vm.get("port"),
                           egress_ip=vm["egress_ip"])
            # Two VMs behind one egress IP is two VMs' cost for one VM's worth
            # of address diversity, which is the only reason to run several.
            dup = next((o for o in self.vms
                        if o is not vm and o["state"] == "ready"
                        and o.get("egress_ip")
                        and o["egress_ip"] == vm["egress_ip"]), None)
            if dup:
                self.log_event("vm_duplicate_ip", vm=vm["name"],
                               same_as=dup["name"], ip=vm["egress_ip"])
                raise RuntimeError(
                    f"egress IP {vm['egress_ip']} already used by {dup['name']}")
        except Exception as e:  # noqa: BLE001
            self._set_vm_state(vm, "failed", str(e).strip().splitlines()[-1][:120])
            vm["error"] = str(e)
            self.log_event("vm_failed", vm=vm["name"], error=str(e))
            # `create` may well have succeeded before the failure — a half
            # built VM bills exactly like a working one. Tear it down now
            # instead of leaving it for whoever remembers to hit shutdown.
            try:
                self.provider.delete(vm)
                self._set_vm_state(vm, "deleted", "açılamadı, silindi")
                self.log_event("vm_deleted", vm=vm["name"], reason="provision failed")
            except Exception as de:  # noqa: BLE001
                self.log_event("vm_delete_failed", vm=vm["name"], error=str(de))
        self.persist()

    def _http(self, vm: Dict[str, Any], method: str, path: str,
              **kwargs) -> requests.Response:
        url = self.provider.endpoint(vm) + path
        return requests.request(method, url, timeout=30,
                                headers={"X-API-Key": self.api_key}, **kwargs)

    def _await_transfers(self, timeout: float = 300.0) -> int:
        """
        Wait for packages already being pulled to land.

        The drive loop finishes when no VM is still scraping, but collection
        runs on its own pool — a run can reach "completed" with packages
        halfway down the wire. Deleting the VMs at that moment throws away
        work that was seconds from arriving, so shutdown waits here first.

        Returns how many were still in flight when the wait gave up.
        """
        deadline = time.time() + timeout
        while True:
            left = sum(1 for j in self.jobs.values()
                       if j["state"] == "transferring")
            if not left:
                return 0
            if time.time() >= deadline:
                self.log_event("transfer_wait_timeout", remaining=left)
                return left
            self.drain = {"vms": 0, "pulled": 0, "failed": 0,
                          "deadline": deadline,
                          "state": f"{left} paket iniyor"}
            self.persist()
            time.sleep(1.0)

    def _drive(self) -> None:
        while not self.shutdown_requested:
            pending = [p for p, j in self.jobs.items() if j["state"] == "pending"]
            active_total = sum(len(vm["active"]) for vm in self.vms)
            if not pending and active_total == 0 and not self.paused:
                break

            # Dispatch from one shared pool, never all at once: a job assigned
            # to a VM is stuck behind that VM's queue, so anything beyond what
            # keeps a slot busy is work another VM could have finished sooner.
            # Paused only stops NEW dispatches — in-flight jobs still finish
            # and get pulled, and the VMs stay up ready to resume.
            ready = [vm for vm in self.vms if vm["state"] == "ready"]
            depth = self._queue_depth(len(pending), len(ready))
            for vm in ready:
                if self.paused:
                    break
                while len(vm["active"]) < depth and pending:
                    pid = self._take_job(pending, vm)
                    if pid is None:
                        break
                    self._dispatch(vm, pid)

            # poll active jobs per VM
            for vm in self.vms:
                if vm["state"] != "ready" or not vm["active"]:
                    continue
                try:
                    self._poll_vm(vm)
                    vm["health_fails"] = 0
                except requests.RequestException as e:
                    vm["health_fails"] += 1
                    if vm["health_fails"] >= 3:
                        self._vm_lost(vm, str(e))

            self._replace_missing_vms()
            self._drain_surplus_vms()
            self.persist()
            time.sleep(POLL_INTERVAL)

    def _replace_missing_vms(self) -> None:
        """
        Put back capacity the run lost.

        A preempted spot VM, a dead tunnel, a zone with no capacity, a
        duplicate egress IP — all end with one fewer machine, and nothing
        previously replaced them, so a long run quietly finished on half the
        fleet it was given. Replacements take the next zone in the rotation
        rather than retrying the one that just failed, and are capped so a
        systematically failing setup cannot spin up VMs forever.
        """
        if self.shutdown_requested or self.paused:
            return
        remaining = sum(1 for j in self.jobs.values()
                        if j["state"] in ("pending", "scraping", "transferring"))
        if not remaining:
            return
        # "pending" counts as alive: with dispatch starting on the first ready
        # VM, the rest of the fleet is still coming up and must not be
        # mistaken for losses that need replacing.
        alive = [vm for vm in self.vms
                 if vm["state"] in ("pending", "provisioning", "ready")]
        want = max(1, min(self.vm_count, remaining))
        if len(alive) >= want:
            return
        budget = self.max_replacements - self.replacements
        if budget <= 0:
            if not self._replacement_cap_logged:
                self._replacement_cap_logged = True
                self.log_event("vm_replacement_capped", used=self.replacements)
            return
        for _ in range(min(want - len(alive), budget)):
            with self._lock:
                index = len(self.vms)
                self.replacements += 1
            vm = {"index": index,
                  "name": f"hermes-w{index:02d}-{uuid.uuid4().hex[:6]}",
                  "state": "pending", "slots": self.slots, "active": [],
                  "done": 0, "failed": 0, "consecutive_fails": 0,
                  "egress_ip": None, "health_fails": 0,
                  "state_reason": f"kayıp VM yerine ({want} hedef)",
                  "state_since": _now()}
            self.vms.append(vm)
            self.log_event("vm_replacing", vm=vm["name"], alive=len(alive),
                           want=want, remaining=remaining)
            threading.Thread(target=self._provision_vm, args=(vm,),
                             daemon=True).start()

    def _take_job(self, pending: List[str], vm: Dict[str, Any]) -> Optional[str]:
        """
        Pick a job for this VM, preferring one it has not already failed.

        A retry is only worth anything if something differs, and the most
        likely difference is the machine — a place that comes back empty may
        well be that VM's egress IP being throttled. Sending the retry to the
        same VM (which is what taking the head of the queue did) spends all
        three attempts on the same address. Falls back to the head of the
        queue when every candidate has been here, so a single-VM run still
        retries.
        """
        for i, pid in enumerate(pending):
            if self.jobs[pid].get("vm") != vm["name"]:
                return pending.pop(i)
        return pending.pop(0) if pending else None

    def _queue_depth(self, pending: int, ready_vms: int) -> int:
        """
        How many jobs to hold on one VM.

        Normally slots + 1: one buffered job means a finished slot restarts
        immediately instead of waiting for the next poll. But buffering only
        pays while there is more work than capacity — once the pool is nearly
        drained, a queued job sitting behind a slow VM is a job some idle VM
        could already have done, so the buffer is dropped and the remaining
        work spreads out.
        """
        base = max(1, self.slots)
        if ready_vms and pending <= ready_vms * base:
            return base
        return base + QUEUE_BUFFER

    def _drain_surplus_vms(self) -> None:
        """
        Shut down VMs the tail of the run no longer needs.

        The last few places do not need ten machines waiting on them, and an
        idle VM bills exactly like a busy one. Capacity is trimmed towards the
        work that is left, keeping DRAIN_SPARE VMs in hand so a straggler or a
        retry still has somewhere to go, and only ever retiring VMs that are
        genuinely idle.
        """
        if self.shutdown_requested or self.paused:
            return
        remaining = sum(1 for j in self.jobs.values()
                        if j["state"] in ("pending", "scraping", "transferring"))
        ready = [vm for vm in self.vms if vm["state"] == "ready"]
        keep = max(1, remaining + DRAIN_SPARE)
        if len(ready) <= keep:
            return
        idle = [vm for vm in ready if not vm["active"]]
        for vm in idle[: len(ready) - keep]:
            self.log_event("vm_drained", vm=vm["name"], remaining=remaining,
                           ready=len(ready), keep=keep)
            try:
                self.provider.delete(vm)
                self._set_vm_state(
                    vm, "deleted",
                    f"kalan iş {remaining} · fazla kapasite kapatıldı")
                self.log_event("vm_deleted", vm=vm["name"], reason="drain")
            except Exception as e:  # noqa: BLE001
                self.log_event("vm_delete_failed", vm=vm["name"], error=str(e))

    def _dispatch(self, vm: Dict[str, Any], place_id: str) -> None:
        job = self.jobs[place_id]
        try:
            r = self._http(vm, "POST", "/jobs", json=self.job_request(place_id))
            r.raise_for_status()
            job.update(state="scraping", vm=vm["name"],
                       remote_id=r.json()["job_id"])
            job["attempts"] += 1
            vm["active"].append(place_id)
            self.log_event("job_dispatched", place=place_id, vm=vm["name"],
                           attempt=job["attempts"], remote_id=job["remote_id"])
        except requests.RequestException as e:
            job["error"] = f"dispatch: {e}"
            self.log_event("job_dispatch_failed", place=place_id,
                           vm=vm["name"], error=str(e))

    def _poll_vm(self, vm: Dict[str, Any]) -> None:
        for place_id in list(vm["active"]):
            job = self.jobs[place_id]
            r = self._http(vm, "GET", f"/jobs/{job['remote_id']}")
            r.raise_for_status()
            remote = r.json()
            state = remote["state"]
            job["progress"] = remote.get("progress")
            if state in ("done", "transfer_error") and remote.get("package"):
                vm["active"].remove(place_id)
                job["state"] = "transferring"
                # The worker's own timeline separates queue wait from actual
                # scraping — dispatch->delivered lumps them together, and with
                # more jobs than slots, a job can sit queued for minutes.
                tl = remote.get("timeline") or {}
                self.log_event("job_packaged", place=place_id, vm=vm["name"],
                               queued_at=tl.get("queued"),
                               scrape_started=tl.get("scraping"),
                               scrape_ended=tl.get("packaging"),
                               **remote["package"])
                self._transfer_pool.submit(self._collect, vm, place_id, remote)
            elif state in ("error", "cancelled", "interrupted"):
                vm["active"].remove(place_id)
                vm["failed"] += 1
                codes = (self._pull_failure_bundle(vm, place_id, job)
                         if remote.get("failure_bundle") else [])
                self._vm_scored(vm, ok=False,
                                blame_vm=not any(c in PAGE_FAULT_CODES
                                                 for c in codes))
                # The worker's own message is a downstream symptom
                # ("scrape produced no place details") whatever went wrong
                # upstream; the captured code is the actual cause, so lead
                # with it when there is one.
                self._job_failed(place_id,
                                 f"{'/'.join(codes)}: {remote.get('error') or state}"
                                 if codes else (remote.get("error") or state))

    def _pull_failure_bundle(self, vm: Dict[str, Any], place_id: str,
                             job: Dict[str, Any]) -> List[str]:
        """
        Fetch a failed job's postmortem bundle before the VM is gone.

        The screenshots and DOM live on a machine that is deleted minutes
        later, so this is the only window — and these are exactly the cases
        where a log alone leaves you guessing.
        """
        dst_dir = self.dir / "failures"
        dst_dir.mkdir(exist_ok=True)
        dst = dst_dir / f"{safe_name(place_id)}-{job['attempts']}.tar.gz"
        try:
            r = self._http(vm, "GET", f"/jobs/{job['remote_id']}/failure",
                           stream=True)
            r.raise_for_status()
            with open(dst, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            codes = _bundle_codes(dst)
            self.log_event("failure_bundle", place=place_id, vm=vm["name"],
                           file=dst.name, bytes=dst.stat().st_size,
                           codes=codes)
            return codes
        except Exception as e:  # noqa: BLE001
            self.log_event("failure_bundle_missed", place=place_id,
                           vm=vm["name"], error=str(e)[:150])
        return []

    def _collect(self, vm: Dict[str, Any], place_id: str,
                 remote: Dict[str, Any]) -> None:
        job = self.jobs[place_id]
        try:
            r = self._http(vm, "GET", f"/jobs/{job['remote_id']}/download",
                           stream=True)
            r.raise_for_status()
            tar_path = self.pkg_dir / remote["package"]["file"]
            with open(tar_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            job["package"] = remote["package"]["file"]
            job["latest_review_date"] = self._latest_review_date(tar_path)
            out_dir = unpack_to_outputs(tar_path, self.run_name, place_id)
            job["output_dir"] = str(out_dir.relative_to(REPO_ROOT))

            # A package can build fine and still be empty: if Maps says the
            # place has reviews but we pulled none, the scrape silently landed
            # on the wrong page. Retry instead of calling it done.
            empty = self._empty_scrape(out_dir)
            if empty:
                vm["failed"] += 1          # already off vm["active"] (_poll_vm)
                self.log_event("job_empty", place=place_id, vm=vm["name"],
                               reason=empty)
                self._vm_scored(vm, ok=False)
                self._job_failed(place_id, empty)
                self.persist()
                return

            job["state"] = "done"
            vm["done"] += 1
            self._vm_scored(vm, ok=True)
            self.deliveries.append(datetime.now(timezone.utc))
            self.log_event("job_transferred", place=place_id, vm=vm["name"],
                           file=tar_path.name,
                           bytes=tar_path.stat().st_size,
                           output=job["output_dir"],
                           latest_review=job["latest_review_date"])
        except Exception as e:  # noqa: BLE001
            self._job_failed(place_id, f"transfer: {e}")
        self.persist()

    @staticmethod
    def _empty_scrape(out_dir: Path) -> Optional[str]:
        """Reason string when a package looks like a failed scrape, else None."""
        try:
            info = json.loads((out_dir / "info.json").read_text(encoding="utf-8"))
            reviews = json.loads((out_dir / "reviews.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None                      # can't tell — leave it alone

        # A hash: id means the browser never reached a canonical place page,
        # so nothing in the package can be trusted to be the right business.
        pid = str(info.get("place_id") or "")
        if pid.startswith("hash:"):
            return "mekan kimliği çözülemedi (hash) — sayfa gerçekten o mekan mı belirsiz"

        total = info.get("review_count")
        got = reviews.get("count", 0)
        if isinstance(total, int) and total > 0 and got == 0:
            return f"0 yorum indirildi ama Maps {total} yorum diyor"
        return None

    @staticmethod
    def _latest_review_date(tar_path: Path) -> Optional[str]:
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                member = next(m for m in tar.getmembers()
                              if m.name.endswith("/reviews.json"))
                data = json.load(tar.extractfile(member))
            dates = [rv.get("review_date") for rv in data.get("reviews", [])
                     if rv.get("review_date")]
            return max(dates) if dates else None
        except Exception:  # noqa: BLE001
            return None

    def _job_failed(self, place_id: str, error: str) -> None:
        job = self.jobs[place_id]
        job["error"] = error
        if job["attempts"] < self.max_attempts:
            job["state"] = "pending"     # retried, preferably on another VM
            self.log_event("job_retry", place=place_id, attempt=job["attempts"],
                           error=error[:200])
        else:
            job["state"] = "failed"
            self.log_event("job_failed_final", place=place_id, error=error[:200])

    def _set_vm_state(self, vm: Dict[str, Any], state: str, reason: str) -> None:
        """
        Move a VM to a new state and record why.

        "lost" alone does not say whether the machine stopped answering or was
        retired for failing three jobs in a row, and "ready" does not say what
        was actually checked. Whoever is watching a run needs the reason more
        than the label.
        """
        vm["state"] = state
        vm["state_reason"] = reason
        vm["state_since"] = _now()

    def _vm_scored(self, vm: Dict[str, Any], ok: bool,
                   blame_vm: bool = True) -> None:
        """
        Track how a VM is doing and retire one that has stopped working.

        A machine can be healthy — answering polls, accepting jobs — and still
        fail everything it is given, most likely because its egress IP is
        being throttled. Left alone it keeps drawing work from the queue and
        burning attempts on it, so a run of consecutive failures retires it.
        Its in-flight places go back to the queue and a replacement comes up
        in the next zone, which is the point: a different address.

        Only failures that could be the machine's doing count. A page that
        never rendered its reviews is not evidence against the VM, and
        treating it as such retires working machines while the same places
        succeed on retry — measured, not assumed: 14 of 18 such failures in
        one run were delivered in full by a later attempt.
        """
        if ok:
            vm["consecutive_fails"] = 0
            return
        if not blame_vm:
            return
        vm["consecutive_fails"] = vm.get("consecutive_fails", 0) + 1
        if vm["consecutive_fails"] >= self.vm_fail_limit and vm["state"] == "ready":
            self.log_event("vm_retired", vm=vm["name"],
                           consecutive_fails=vm["consecutive_fails"],
                           egress_ip=vm.get("egress_ip"))
            self._vm_lost(vm, f"{vm['consecutive_fails']} ardışık başarısız iş")

    def _vm_lost(self, vm: Dict[str, Any], error: str) -> None:
        self.log_event("vm_lost", vm=vm["name"], error=error[:200])
        self._set_vm_state(vm, "lost", error.strip().splitlines()[0][:120])
        for place_id in vm["active"]:
            self._job_failed(place_id, f"vm lost: {vm['name']}")
        vm["active"] = []
        try:
            self.provider.delete(vm)
            self.log_event("vm_deleted", vm=vm["name"])
        except Exception as e:  # noqa: BLE001
            self.log_event("vm_delete_failed", vm=vm["name"], error=str(e))

    # --- shutdown ---

    def set_paused(self, paused: bool) -> None:
        """
        Pause = stop handing out NEW jobs. Jobs already on a VM keep running
        and are pulled as they finish; the VMs stay up (use shutdown to kill
        them). Resume continues from the next pending place.
        """
        if self.paused == paused:
            return
        self.paused = paused
        self.log_event("paused" if paused else "resumed")
        self.persist()

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.log_event("shutdown_requested")
        if self.state in ("completed", "error"):
            threading.Thread(target=self._shutdown_all, daemon=True).start()

    def _drain_vm(self, vm: Dict[str, Any], deadline: float) -> None:
        """Pull whatever this VM has already packaged, until the deadline."""
        try:
            r = self._http(vm, "GET", "/jobs")
            for rj in r.json():
                if time.time() > deadline:
                    self.log_event("drain_deadline", vm=vm["name"])
                    return
                if not rj.get("package"):
                    continue
                pid = next((p for p, j in self.jobs.items()
                            if j.get("remote_id") == rj["job_id"]
                            and j["state"] != "done"), None)
                if pid:
                    self.log_event("drain_pull", vm=vm["name"], place=pid)
                    before = self.jobs[pid]["state"]
                    self._collect(vm, pid, {"package": rj["package"]})
                    if self.drain is not None:
                        key = ("pulled" if self.jobs[pid]["state"] == "done"
                               else "failed")
                        self.drain[key] += 1
        except requests.RequestException:
            pass

    def _shutdown_all(self) -> None:
        self.state = "closing"
        self.persist()
        # Drain finished packages before killing the VMs — but in parallel and
        # against a deadline. Doing it one VM at a time meant a fleet of
        # thirteen kept billing for as long as the slowest download took, and
        # "shut everything down" is a cost promise first: whatever has not
        # arrived by the deadline is a place that gets scraped again, which is
        # cheaper than the machines.
        drainable = [vm for vm in self.vms if vm["state"] == "ready"]
        if drainable:
            self.log_event("drain_started", vms=len(drainable),
                           deadline_s=self.drain_timeout)
            deadline = time.time() + self.drain_timeout
            self.drain = {"vms": len(drainable), "pulled": 0, "failed": 0,
                          "deadline": deadline, "state": "çekiliyor"}
            self.persist()
            with ThreadPoolExecutor(max_workers=min(8, len(drainable))) as pool:
                list(pool.map(lambda vm: self._drain_vm(vm, deadline), drainable))

        if self.drain is not None:
            self.drain["state"] = "bitti"
            self.persist()
        alive = [vm for vm in self.vms if vm["state"] != "deleted"]
        self.log_event("vm_shutdown_started", count=len(alive))
        with ThreadPoolExecutor(max_workers=8) as pool:
            def _del(vm):
                try:
                    self.provider.delete(vm)
                    self._set_vm_state(vm, "deleted", "run kapatıldı")
                    self.log_event("vm_deleted", vm=vm["name"])
                except Exception as e:  # noqa: BLE001
                    self.log_event("vm_delete_failed", vm=vm["name"], error=str(e))
            list(pool.map(_del, alive))
        # VERIFY: the cost guarantee
        try:
            remaining = self.provider.list_remaining()
        except Exception as e:  # noqa: BLE001
            remaining = [f"verify-error: {e}"]
        self.remaining_after_shutdown = remaining
        self.log_event("vm_shutdown_verified", remaining=remaining)
        self.state = "closed"
        self.save_summary()
        self.persist()


# --- API + UI ----------------------------------------------------------------

app = FastAPI(title="Hermes Scrape Orchestrator")
_current: Optional[Execution] = None


def _profiles() -> Dict[str, Any]:
    if not PROFILES_PATH.exists():
        PROFILES_PATH.write_text(json.dumps(DEFAULT_PROFILES, indent=1))
    return json.loads(PROFILES_PATH.read_text())


class StartRequest(BaseModel):
    run_file: str
    profile: str = "Standart - Google"
    provider: str = "local"
    vm_count: int = 2
    slots: int = 1
    machine_type: str = "e2-standard-2"
    spot: bool = False
    zones: Optional[List[str]] = None   # VMs are spread across these in order
    max_attempts: int = MAX_ATTEMPTS    # VMs a place is tried on before failing
    max_replacements: int = MAX_REPLACEMENTS  # budget for replacing lost VMs
    vm_fail_limit: int = VM_FAIL_LIMIT  # consecutive failures before retiring a VM
    drain_timeout: int = DRAIN_TIMEOUT  # seconds spent rescuing packages
    force: bool = False                 # start despite the pre-flight warnings
    keep_vms_after_run: bool = False    # leave the fleet up when the run ends
    place_ids: Optional[List[str]] = None  # subset to run (retry of a failed set)
    retry_of: Optional[str] = None         # exec_id this retry came from
    capture_failures: bool = True          # screenshot + DOM on a failed place


class ProfileRequest(BaseModel):
    name: str
    mode: str = "fresh"
    max_reviews: int = 100          # kaç yorum (0 = hepsi)
    min_review_photos: int = 50     # bu kadar yorum fotoğrafı görülene dek devam
    max_reviews_cap: int = 250      # ama en fazla bu kadar yorum
    place_photos_limit: int = 60    # kaç owner fotoğrafı
    download_review_images: bool = True
    rename_from: Optional[str] = None   # düzenlemede eski ad (ad değiştirmek için)


app.mount("/static", StaticFiles(directory=str(ORCH_DIR / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(ORCH_DIR / "static" / "index.html")


@app.get("/api/runs")
def list_runs():
    out = []
    for p in sorted(RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            run = data["payload"]["run"]
            execs = sorted((STATE_DIR / p.stem.split(".")[0]).glob("exec-*/summary.json"))
            out.append({"file": p.name, "name": run["name"],
                        "places": len(data["payload"]["place_ids"]),
                        "completed_execs": len(execs)})
        except Exception:  # noqa: BLE001
            continue
    return out


def _run_path(run_file: str) -> Path:
    """Resolve a run definition by file name, refusing anything path-like."""
    if "/" in run_file or "\\" in run_file or ".." in run_file:
        raise HTTPException(400, f"geçersiz dosya adı: {run_file}")
    path = RUNS_DIR / run_file
    if not path.is_file():
        raise HTTPException(404, f"run yok: {run_file}")
    return path


def _bundle_codes(tar_path: Path) -> List[str]:
    """
    Failure codes recorded inside a postmortem bundle.

    The worker reports whatever exception surfaced last, which for an aborted
    scrape is a packaging complaint rather than the reason it aborted. The
    capture meta written at the moment of failure holds the real code, so it
    is read here — the file is already on disk and a few kB.
    """
    codes: List[str] = []
    try:
        with tarfile.open(tar_path) as tf:
            for m in tf.getmembers():
                if m.name.endswith("/meta.json") and "/diagnostics/" in m.name:
                    f = tf.extractfile(m)
                    if not f:
                        continue
                    code = json.loads(f.read().decode("utf-8")).get("code")
                    if code and code not in codes:
                        codes.append(code)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read failure codes from %s: %s", tar_path.name, e)
    return codes


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def _dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) \
        if d.is_dir() else 0


@app.get("/api/runs/{run_file}/footprint")
def run_footprint(run_file: str):
    """
    What deleting this run would remove. Shown before the delete so the
    choice is made against real numbers rather than a guess at what is
    on disk — outputs are the expensive part and they do not come back.
    """
    path = _run_path(run_file)
    data = json.loads(path.read_text())
    stem = path.stem.split(".")[0]
    state_dir = STATE_DIR / stem
    out_dir = OUTPUTS_DIR / safe_name(data["payload"]["run"]["name"])
    execs = sorted(state_dir.glob("exec-*")) if state_dir.is_dir() else []
    return {
        "file": path.name, "run_stem": stem,
        "name": data["payload"]["run"]["name"],
        "places": len(data["payload"]["place_ids"]),
        "execs": len(execs),
        "delivered_places": sum(1 for x in out_dir.iterdir() if x.is_dir())
                            if out_dir.is_dir() else 0,
        "outputs_bytes": _dir_bytes(out_dir),
        "state_bytes": _dir_bytes(state_dir),
        "json_bytes": path.stat().st_size,
        "running": bool(_current and _current.run_stem == stem
                        and _current.state in LIVE_STATES),
    }


@app.delete("/api/runs/{run_file}")
def delete_run(run_file: str, keep_json: bool = True,
               keep_outputs: bool = False):
    """
    Erase a run's data. keep_json leaves the definition (the place list) so
    the run can be started again from zero; keep_json=false takes the
    definition too and the run leaves every list.

    The default keeps it: a missing or mistyped flag should cost the least,
    and the definition is the one piece here that a caller cannot rebuild
    from what is left on disk.

    keep_outputs is the opposite errand: drop just this definition and leave
    the delivered data alone. Several definitions can share an output tree,
    so removing one of them must not take the tree the others still use.

    Refuses while the run is executing: the background thread writes into
    the very directories this removes, and a half-deleted exec is worse
    than a live one.
    """
    path = _run_path(run_file)
    data = json.loads(path.read_text())
    stem = path.stem.split(".")[0]
    if _current and _current.run_stem == stem and _current.state in LIVE_STATES:
        raise HTTPException(409, f"bu run çalışıyor ({_current.id}); önce durdur")

    state_dir = STATE_DIR / stem
    out_dir = OUTPUTS_DIR / safe_name(data["payload"]["run"]["name"])
    removed = {"execs": len(list(state_dir.glob("exec-*"))) if state_dir.is_dir() else 0,
               "outputs_bytes": _dir_bytes(out_dir),
               "state_bytes": _dir_bytes(state_dir),
               "json_removed": not keep_json}
    if not keep_outputs:
        for d in (state_dir, out_dir):
            if d.is_dir():
                shutil.rmtree(d)
    removed["outputs_kept"] = keep_outputs
    if not keep_json:
        path.unlink()
    log.info("run silindi: %s (json %s, %d exec, %s)", stem,
             "korundu" if keep_json else "silindi", removed["execs"],
             _mb(removed["outputs_bytes"] + removed["state_bytes"]))
    return removed


# --- arşiv --------------------------------------------------------------------
# Biten bir run'ın verisi tek bir zip'e alınıp buradan kaldırılıyor. Birim run
# ADI: aynı adı taşıyan birden çok tanım dosyası bilerek aynı çıktı ağacına
# yazıyor, dolayısıyla tek dosyayı arşivleyip çıktıyı silmek diğerlerini
# yarım bırakırdı.
def _archive_root(archive_dir: str) -> Path:
    d = Path(archive_dir).expanduser()
    if not d.is_absolute():
        raise HTTPException(400, "arşiv klasörü mutlak yol olmalı")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_family(run_file: str) -> Dict[str, Any]:
    """Everything that shares this run's name: definitions, state, outputs."""
    path = _run_path(run_file)
    name = json.loads(path.read_text())["payload"]["run"]["name"]
    files, stems = [], []
    for p in sorted(RUNS_DIR.glob("*.json")):
        try:
            if json.loads(p.read_text())["payload"]["run"]["name"] == name:
                files.append(p)
                stems.append(p.stem.split(".")[0])
        except Exception:  # noqa: BLE001
            continue
    out_dir = OUTPUTS_DIR / safe_name(name)
    return {"name": name, "files": files, "stems": stems, "out_dir": out_dir}


@app.get("/api/archives")
def list_archives(archive_dir: str):
    d = Path(archive_dir).expanduser()
    if not d.is_dir():
        return {"dir": str(d), "exists": False, "items": []}
    items = []
    for z in sorted(d.glob("*.zip"), reverse=True):
        st = z.stat()
        meta = {}
        try:
            with zipfile.ZipFile(z) as zf:
                if "archive.json" in zf.namelist():
                    meta = json.loads(zf.read("archive.json").decode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
        items.append({"file": z.name, "bytes": st.st_size,
                      "at": datetime.fromtimestamp(st.st_mtime,
                                                   timezone.utc).isoformat(),
                      "run_name": meta.get("run_name"),
                      "places": meta.get("places"),
                      "execs": meta.get("execs")})
    return {"dir": str(d), "exists": True, "items": items}


@app.get("/api/runs/{run_file}/archive")
def archive_preview(run_file: str):
    """What archiving this run would take, before it takes it."""
    fam = _run_family(run_file)
    places = sum(1 for x in fam["out_dir"].iterdir()
                 if x.is_dir()) if fam["out_dir"].is_dir() else 0
    execs = sum(len(list((STATE_DIR / st).glob("exec-*")))
                for st in fam["stems"] if (STATE_DIR / st).is_dir())
    return {"run_name": fam["name"],
            "definitions": [p.name for p in fam["files"]],
            "places": places, "execs": execs,
            "outputs_bytes": _dir_bytes(fam["out_dir"]),
            "state_bytes": sum(_dir_bytes(STATE_DIR / st) for st in fam["stems"]),
            "running": bool(_current and _current.run_stem in fam["stems"]
                            and _current.state in LIVE_STATES)}


@app.post("/api/runs/{run_file}/export")
def export_run(run_file: str, archive_dir: str, stamp: str = ""):
    """
    Same zip as the archive, but nothing is removed.

    Taking a copy and clearing space are different intentions and one of
    them is irreversible; they get different buttons.
    """
    return _write_archive(run_file, archive_dir, stamp, remove=False)


@app.post("/api/runs/{run_file}/archive")
def archive_run(run_file: str, archive_dir: str, stamp: str = ""):
    """
    Zip the run's definitions, outputs and exec state, then remove them.

    Written to a temporary name and moved into place only once complete: a
    half-written archive that the source has already been deleted for is the
    one failure mode worth engineering against.
    """
    return _write_archive(run_file, archive_dir, stamp, remove=True)


def _write_archive(run_file: str, archive_dir: str, stamp: str,
                   remove: bool) -> Dict[str, Any]:
    fam = _run_family(run_file)
    if _current and _current.run_stem in fam["stems"] and _current.state in LIVE_STATES:
        raise HTTPException(409, f"bu run çalışıyor ({_current.id}); önce durdur")
    root = _archive_root(archive_dir)
    tag = stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = root / f"{safe_name(fam['name'])}-{tag}.zip"
    tmp = final.with_suffix(".zip.part")

    manifest = {
        "run_name": fam["name"],
        "definitions": [p.name for p in fam["files"]],
        "archived_at": _now(),
        "places": sum(1 for x in fam["out_dir"].iterdir() if x.is_dir())
                  if fam["out_dir"].is_dir() else 0,
        "execs": sum(len(list((STATE_DIR / st).glob("exec-*")))
                     for st in fam["stems"] if (STATE_DIR / st).is_dir()),
    }
    written = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            zf.writestr("archive.json",
                        json.dumps(manifest, ensure_ascii=False, indent=1))
            for f in fam["files"]:
                zf.write(f, f"runs/{f.name}")
                written += 1
            for st in fam["stems"]:
                d = STATE_DIR / st
                for f in d.rglob("*") if d.is_dir() else []:
                    if f.is_file():
                        zf.write(f, f"state/{st}/{f.relative_to(d)}")
                        written += 1
            od = fam["out_dir"]
            for f in (od.rglob("*") if od.is_dir() else []):
                if f.is_file():
                    zf.write(f, f"outputs/{od.name}/{f.relative_to(od)}")
                    written += 1
        os.replace(tmp, final)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"arşiv yazılamadı: {e}") from e

    if remove:
        for st in fam["stems"]:
            d = STATE_DIR / st
            if d.is_dir():
                shutil.rmtree(d)
        if fam["out_dir"].is_dir():
            shutil.rmtree(fam["out_dir"])
        for f in fam["files"]:
            f.unlink()

    size = final.stat().st_size
    log.info("%s: %s (%d dosya, %s)", "arşivlendi" if remove else "dışa aktarıldı",
             final.name, written, _mb(size))
    return {"file": final.name, "path": str(final), "bytes": size,
            "files": written, "removed": remove, **manifest}


@app.get("/api/profiles")
def get_profiles():
    return _profiles()


@app.post("/api/profiles")
def save_profile(req: ProfileRequest):
    """Create or update a profile (rename via rename_from)."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "profil adı boş olamaz")
    profiles = _profiles()
    if req.rename_from and req.rename_from != name:
        if req.rename_from not in profiles:
            raise HTTPException(404, f"profil yok: {req.rename_from}")
        profiles.pop(req.rename_from)
    profiles[name] = {
        "mode": req.mode, "max_reviews": req.max_reviews,
        "min_review_photos": req.min_review_photos,
        "max_reviews_cap": req.max_reviews_cap,
        "place_photos_limit": req.place_photos_limit,
        "download_review_images": req.download_review_images,
    }
    PROFILES_PATH.write_text(json.dumps(profiles, ensure_ascii=False, indent=1))
    return profiles


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    profiles = _profiles()
    if name not in profiles:
        raise HTTPException(404, f"profil yok: {name}")
    if len(profiles) == 1:
        raise HTTPException(400, "son profil silinemez")
    profiles.pop(name)
    PROFILES_PATH.write_text(json.dumps(profiles, ensure_ascii=False, indent=1))
    return profiles


@app.get("/api/gcp")
def gcp_status():
    return GcpProvider.check()


def _quota_headroom(vm_count: int, machine_type: str) -> Dict[str, Any]:
    """
    Whether the fleet fits before a single VM is created.

    CPUS_ALL_REGIONS is a project-wide ceiling; spreading across zones does
    nothing for it. One run asked for 13 machines against a 32-vCPU limit
    that three leftovers were already eating, and learned the answer 25
    failed create calls later.
    """
    per_vm = 2
    m = re.search(r"-(\d+)$", machine_type or "")
    if m:
        per_vm = int(m.group(1))
    try:
        out = subprocess.run(["gcloud", "compute", "project-info", "describe",
                              "--format=json(quotas)"],
                             capture_output=True, text=True, timeout=60)
        quotas = json.loads(out.stdout)["quotas"] if out.returncode == 0 else []
    except Exception as e:  # noqa: BLE001
        return {"known": False, "reason": str(e)[:120]}
    q = next((x for x in quotas if x["metric"] == "CPUS_ALL_REGIONS"), None)
    if not q:
        return {"known": False, "reason": "CPUS_ALL_REGIONS bulunamadı"}
    free = q["limit"] - q["usage"]
    need = vm_count * per_vm
    return {"known": True, "limit": q["limit"], "usage": q["usage"],
            "free": free, "need": need, "per_vm": per_vm,
            "fits_vms": int(free // per_vm), "ok": need <= free}


def _stranded_vms() -> List[str]:
    """VMs a finished execution still owns — nothing can reach them once a
    new run replaces it, and they keep billing."""
    if not _current or _current.state in LIVE_STATES:
        return []
    return [v["name"] for v in _current.vms if v["state"] != "deleted"]


_ZONE_CACHE: Dict[str, Any] = {}


@app.get("/api/gcpzones")
def gcp_zones():
    """
    Every zone the project can use, grouped by continent.

    Asked from gcloud rather than hard-coded: the list grows, and a picker
    built from a stale copy quietly hides the region that would have worked.
    Cached for the process — zones do not appear mid-run.
    """
    if _ZONE_CACHE.get("groups"):
        return _ZONE_CACHE
    try:
        out = subprocess.run(
            ["gcloud", "compute", "zones", "list",
             "--format=value(name,region.basename(),status)"],
            capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return {"groups": [], "error": _gcloud_reason(out.stderr)}
    except Exception as e:  # noqa: BLE001
        return {"groups": [], "error": str(e)[:150]}

    groups: Dict[str, Dict[str, Any]] = {}
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        zone, region = parts[0], parts[1]
        status = parts[2] if len(parts) > 2 else "UP"
        continent = re.sub(r"[-]?[a-z]*\d+$", "", region) or region
        g = groups.setdefault(continent, {"continent": continent, "zones": []})
        g["zones"].append({"zone": zone, "region": region, "up": status == "UP"})
    for g in groups.values():
        g["zones"].sort(key=lambda z: z["zone"])
    _ZONE_CACHE["groups"] = sorted(groups.values(), key=lambda g: g["continent"])
    return _ZONE_CACHE


@app.get("/api/preflight")
def preflight(vm_count: int = 13, machine_type: str = "e2-standard-2"):
    """What would go wrong if a run started right now."""
    return {"quota": _quota_headroom(vm_count, machine_type),
            "stranded": _stranded_vms()}


@app.post("/api/start")
def start(req: StartRequest):
    global _current
    if _current and _current.state in LIVE_STATES:
        raise HTTPException(409, f"execution {_current.id} is {_current.state}")
    # A finished execution can still own running VMs: the drive loop exits but
    # leaves them up so the run can be continued. Starting a new run replaces
    # the execution object, and with it the only handle on those machines —
    # three were found still billing an hour later, eating the quota that the
    # new run then failed to allocate against.
    stranded = _stranded_vms()
    if stranded and not req.force:
        raise HTTPException(409,
            f"önceki run ({_current.id}) hâlâ {len(stranded)} VM tutuyor: "
            f"{', '.join(n[-6:] for n in stranded[:5])}. Önce 'Tüm VM'leri kapat' "
            f"çalıştırın; yine de başlatmak için force.")
    profiles = _profiles()
    if req.profile not in profiles:
        raise HTTPException(404, f"profile not found: {req.profile}")
    if not (RUNS_DIR / req.run_file).exists():
        raise HTTPException(404, f"run file not found: {req.run_file}")
    _current = Execution(req.run_file, req.profile, profiles[req.profile],
                         req.provider, req.vm_count, req.slots,
                         machine_type=req.machine_type, spot=req.spot,
                         zones=req.zones, place_ids=req.place_ids,
                         max_attempts=req.max_attempts,
                         max_replacements=req.max_replacements,
                         vm_fail_limit=req.vm_fail_limit,
                         drain_timeout=req.drain_timeout,
                         keep_vms_after_run=req.keep_vms_after_run,
                         capture_failures=req.capture_failures)
    _current.log_event("execution_created", run=_current.run_name,
                       profile=req.profile, provider=req.provider,
                       vms=req.vm_count, slots=req.slots,
                       machine_type=req.machine_type, spot=req.spot,
                       zones=req.zones, places=len(_current.place_ids),
                       capture_failures=req.capture_failures,
                       retry_of=req.retry_of)
    _current.start()
    return {"exec_id": _current.id, "places": len(_current.place_ids)}


@app.get("/api/browse/{run_stem}/{exec_id}/failed")
def failed_places(run_stem: str, exec_id: str):
    """
    Places this exec did not deliver — the input for a retry run.

    Anything that never produced a package counts: outright failures, and
    places still pending or in flight when the run was stopped.
    """
    d = _exec_dir(run_stem, exec_id)
    snap = _read_json(d / "exec.json")
    delivered = set(_pkg_by_place(d))
    rows = [{"place_id": pid, "state": j.get("state"),
             "attempts": j.get("attempts"), "error": j.get("error")}
            for pid, j in (snap.get("jobs") or {}).items()
            if pid not in delivered]
    return {"run_file": snap.get("run_file"), "exec_id": exec_id,
            "count": len(rows), "places": rows}


@app.get("/api/status")
def status():
    if not _current:
        return {"state": "idle"}
    snap = _current.snapshot()
    log_path = _current.dir / "log.jsonl"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        snap["log_tail"] = [json.loads(ln) for ln in lines[-40:]]
    return snap


# --- servisin kendini kapatması ---------------------------------------------
# Süreci dışarıdan öldürmek VM'leri sahipsiz bırakıyor: tüneller bu süreçte
# yaşıyor, ölünce kimse onları silemiyor ve fatura işlemeye devam ediyor.
# Bu yüzden panelde durdurma yok; kapatma buradan, sırayla ve görünür yapılıyor.
_QUIT: Optional[Dict[str, Any]] = None


def _quit_steps() -> List[Dict[str, Any]]:
    return [{"key": k, "label": t, "state": "pending", "detail": ""}
            for k, t in (("drain", "Paketler VM'lerden çekiliyor"),
                         ("vms", "VM'ler siliniyor"),
                         ("verify", "gcloud ile doğrulanıyor"),
                         ("local", "Yerel worker/tarayıcı artıkları temizleniyor"),
                         ("exit", "Süreç kapanıyor"))]


def _sweep_local_leftovers() -> Dict[str, Any]:
    """
    Kill worker processes and browsers this checkout started and left behind.

    Scoped to this tree's own path: a pattern-based sweep is how a running
    coordinator got killed once. Anything outside this directory is not ours
    to touch.
    """
    root = str(REPO_ROOT)
    killed: List[str] = []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as e:  # noqa: BLE001
        return {"killed": 0, "error": str(e)[:120]}
    me = os.getpid()
    for line in out.splitlines():
        line = line.strip()
        pid_str, _, cmd = line.partition(" ")
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid == me or root not in cmd:
            continue
        if not any(t in cmd for t in ("workers.server", "uc_driver",
                                      "chromedriver", "Chrome")):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(f"{pid} {cmd.split()[0].rsplit('/', 1)[-1]}")
        except (ProcessLookupError, PermissionError):
            continue
    return {"killed": len(killed), "items": killed[:10]}


def _run_quit() -> None:
    global _QUIT
    assert _QUIT is not None
    steps = {s["key"]: s for s in _QUIT["steps"]}

    def mark(key: str, state: str, detail: str = "") -> None:
        steps[key]["state"] = state
        if detail:
            steps[key]["detail"] = detail

    exe = _current
    try:
        if exe and exe.state in LIVE_STATES:
            mark("drain", "running")
            exe.request_shutdown()
            waited = 0.0
            while exe.state not in ("closed", "error") and waited < DRAIN_TIMEOUT + 120:
                time.sleep(1.0)
                waited += 1.0
                d = exe.drain or {}
                mark("drain", "running",
                     f"{d.get('pulled', 0)} paket kurtarıldı"
                     if d else "kapanış bekleniyor")
            d = exe.drain or {}
            mark("drain", "done", f"{d.get('pulled', 0)} paket kurtarıldı, "
                                  f"{d.get('failed', 0)} alınamadı")
            mark("vms", "done",
                 f"{sum(1 for v in exe.vms if v['state'] == 'deleted')} VM silindi")
        else:
            mark("drain", "done", "çalışan run yok")
            mark("vms", "done", "silinecek VM yok")

        mark("verify", "running")
        left = GcpProvider.list_all_hermes()
        if left:
            mark("verify", "failed", f"{len(left)} VM hâlâ açık: {', '.join(left[:5])}")
        else:
            mark("verify", "done", "açık VM yok")

        mark("local", "running")
        swept = _sweep_local_leftovers()
        mark("local", "done", f"{swept.get('killed', 0)} süreç kapatıldı")

        mark("exit", "running", "3 sn içinde")
        _QUIT["finished"] = True
    except Exception as e:  # noqa: BLE001
        _QUIT["error"] = str(e)[:200]
        _QUIT["finished"] = True
        log.exception("quit failed")
        return

    def _die() -> None:
        time.sleep(3)
        log.info("shutting down on request")
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()


@app.post("/api/quit")
def quit_service():
    """Kapatma akışını başlat; ilerleme GET /api/quit ile okunur."""
    global _QUIT
    if _QUIT and not _QUIT.get("finished"):
        return _QUIT
    _QUIT = {"steps": _quit_steps(), "finished": False, "error": None,
             "started": _now()}
    threading.Thread(target=_run_quit, daemon=True).start()
    return _QUIT


@app.get("/api/quit")
def quit_status():
    return _QUIT or {"steps": [], "finished": False, "started": None}


@app.post("/api/restart")
def restart():
    """
    Re-exec the coordinator so edited code takes effect.

    Refused while a run is live: the VM registry lives in this process, so
    restarting mid-run would lose track of running VMs and leave them billing
    with nothing left to shut them down. Close the run first.
    """
    if _current and _current.state in LIVE_STATES:
        raise HTTPException(
            409, f"run çalışıyor ({_current.state}) — önce VM'leri kapat")

    def _reexec():
        time.sleep(0.5)          # let this response reach the browser
        log.info("restarting coordinator")
        os.execv(sys.executable, [sys.executable,
                                  str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])

    threading.Thread(target=_reexec, daemon=True).start()
    return {"ok": True}


@app.post("/api/pause")
def pause(paused: bool = True):
    """Duraklat/devam et: yeni iş dağıtımını durdurur, VM'ler ayakta kalır."""
    if not _current:
        raise HTTPException(404, "no execution")
    _current.set_paused(paused)
    return {"ok": True, "paused": _current.paused}


@app.post("/api/shutdown")
def shutdown():
    if not _current:
        raise HTTPException(404, "no execution")
    _current.request_shutdown()
    return {"ok": True}


# --- Run browser (read-only views over pulled packages) ----------------------

def _exec_dir(run_stem: str, exec_id: str) -> Path:
    d = STATE_DIR / run_stem / exec_id
    if not d.is_dir() or ".." in run_stem or ".." in exec_id:
        raise HTTPException(404, "exec not found")
    return d


def _place_dir(exec_dir: Path, run_name: str, place_id: str,
               tar_name: str) -> Path:
    """
    Folder holding one place's files. Current runs are already unpacked into
    outputs/<run>/<place_id>/ at collect time; older execs (packages pulled
    before that existed) are extracted on demand into the exec's own
    extracted/<pkg-stem>/.
    """
    out = OUTPUTS_DIR / safe_name(run_name) / safe_name(place_id)
    if out.is_dir():
        return out
    dst = exec_dir / "extracted" / tar_name.replace(".tar.gz", "")
    if dst.is_dir():
        return dst
    tar_path = exec_dir / "packages" / tar_name
    if not tar_path.exists():
        raise HTTPException(404, "package not found")
    return untar_stripped(tar_path, dst)


def _images_json(pkg: Path) -> Dict[str, Any]:
    """images.json, or the pre-rename photos.json mapped onto its shape."""
    data = _read_json(pkg / "images.json")
    if data:
        return data
    legacy = _read_json(pkg / "photos.json")
    if not legacy:
        return {}
    review_dir = pkg / "review_images"
    items = ([{"file": str(f.relative_to(pkg))}
              for f in sorted(review_dir.rglob("*")) if f.is_file()]
             if review_dir.is_dir() else [])
    photos = legacy.get("photos", [])
    return {
        "place_id": legacy.get("place_id"),
        "owner": {"available": legacy.get("available", False),
                  "count": sum(1 for p in photos if p.get("file")),
                  "items": photos},
        "review": {"count": len(items), "items": items},
    }


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/browse/runs")
def browse_runs():
    """Runs + their executions with headline counts."""
    out = []
    for p in sorted(RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except ValueError:
            continue
        stem = p.stem.split(".")[0]
        execs = []
        for d in sorted((STATE_DIR / stem).glob("exec-*"), reverse=True):
            snap = _read_json(d / "exec.json")
            pkgs = list((d / "packages").glob("*.tar.gz")) if (d / "packages").is_dir() else []
            t = _timing(d, snap.get("state"))
            execs.append({"exec_id": d.name, "state": snap.get("state"),
                          "profile": snap.get("profile"),
                          "counts": snap.get("counts", {}),
                          "packages": len(pkgs),
                          "bytes": sum(p.stat().st_size for p in pkgs),
                          "elapsed_s": t["elapsed_s"],
                          "per_place_s": t["per_place_s"],
                          "per_hour": t["per_hour"]})
        # Delivered size is what the outputs tree holds for this run, not the
        # sum of the execs: a retry replaces a place's folder rather than
        # adding to it, so summing packages would double-count.
        out_dir = OUTPUTS_DIR / safe_name(data["payload"]["run"]["name"])
        delivered_bytes = sum(f.stat().st_size
                              for f in out_dir.rglob("*") if f.is_file()) \
            if out_dir.is_dir() else 0
        # Delivery is counted against THIS run's list, not the whole outputs
        # tree. Several run definitions can share a run name on purpose so
        # their output lands together; counting the tree made a 9-place
        # top-up run report "delivered 384/9".
        want = data["payload"]["place_ids"]
        live = [e["exec_id"] for e in execs
                if e["state"] in LIVE_STATES]
        out.append({"file": p.name, "run_stem": stem, "running": live,
                    "name": data["payload"]["run"]["name"],
                    "places": len(data["payload"]["place_ids"]),
                    "delivered_places": sum(
                        1 for pid in want
                        if (out_dir / safe_name(pid) / "info.json").is_file()
                    ) if out_dir.is_dir() else 0,
                    "bytes": delivered_bytes,
                    # The output tree is shared by every definition with this
                    # run name, so the group header can show what is actually
                    # on disk instead of one member's slice of it.
                    "tree_places": sum(1 for x in out_dir.iterdir()
                                       if x.is_dir()) if out_dir.is_dir() else 0,
                    "execs": execs})
    # Newest activity first. Exec ids are timestamps, so the newest exec's id
    # sorts as the run's recency; a run that never ran goes last rather than
    # sitting among the ones you were just watching.
    out.sort(key=lambda r: (r["execs"][0]["exec_id"] if r["execs"] else ""),
             reverse=True)
    return out


def _failure_bundles(exec_dir: Path, place_id: str) -> List[Dict[str, Any]]:
    """Postmortem bundles pulled for a place, newest attempt first."""
    d = exec_dir / "failures"
    if not d.is_dir():
        return []
    out = []
    for f in d.glob(f"{safe_name(place_id)}-*.tar.gz"):
        # name is "<place>-<attempt>.tar.gz"; Path.stem only strips ".gz"
        try:
            attempt = int(f.name.rsplit("-", 1)[1].split(".")[0])
        except (IndexError, ValueError):
            attempt = 0
        out.append({"attempt": attempt, "file": f.name,
                    "bytes": f.stat().st_size})
    # Sorted on the attempt NUMBER, not the file name: with ten attempts the
    # name order puts "-10" between "-1" and "-2", so the attempt that
    # actually ended the job was buried in the middle of the list.
    out.sort(key=lambda x: x["attempt"], reverse=True)
    return out


def _failure_dir(exec_dir: Path, name: str) -> Path:
    """Extract a failure bundle once and return its folder."""
    dst = exec_dir / "failures" / "extracted" / name.replace(".tar.gz", "")
    if dst.is_dir():
        return dst
    tar_path = exec_dir / "failures" / name
    if not tar_path.exists():
        raise HTTPException(404, "failure bundle not found")
    return untar_stripped(tar_path, dst)


def _run_name_for(run_stem: str) -> str:
    for p in RUNS_DIR.glob(f"{run_stem}.*json"):
        data = _read_json(p)
        name = ((data.get("payload") or {}).get("run") or {}).get("name")
        if name:
            return name
    return run_stem


def _log_events(exec_dir: Path) -> List[Dict[str, Any]]:
    log_path = exec_dir / "log.jsonl"
    if not log_path.exists():
        return []
    out = []
    for ln in log_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _pkg_by_place(exec_dir: Path) -> Dict[str, str]:
    """place_id -> package file name, from the exec log's transfer events."""
    return {ev["place"]: ev["file"] for ev in _log_events(exec_dir)
            if ev.get("event") == "job_transferred" and ev.get("file")}


def _ts(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _timing(exec_dir: Path, state: Optional[str]) -> Dict[str, Any]:
    """
    Per-place durations and exec totals, derived from the log.

    Dispatch does not mean work started: a VM may hold a buffered job
    beyond the ones it is scraping, so a job can sit queued for minutes. The worker reports when it actually
    started and finished scraping, and those are used when present:

      queued     -> scraping     waiting for a slot   (queue_s)
      scraping   -> packaging    real scrape          (scrape_s)
      packaged   -> transferred  pull + unpack        (transfer_s)
      dispatched -> transferred  everything           (duration_s)
    """
    events = _log_events(exec_dir)
    per: Dict[str, Dict[str, Any]] = {}
    first = last = None
    for ev in events:
        t = _ts(ev.get("ts"))
        if t:
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
        place, name = ev.get("place"), ev.get("event")
        if not place or not name.startswith("job_"):
            continue
        row = per.setdefault(place, {})
        if name == "job_dispatched":
            row.clear()                 # retry: time the latest attempt
            row["dispatched_at"] = ev.get("ts")
        elif name == "job_packaged":
            row["packaged_at"] = ev.get("ts")
            row["queued_at"] = ev.get("queued_at")
            row["scrape_started"] = ev.get("scrape_started")
            row["scrape_ended"] = ev.get("scrape_ended")
        elif name == "job_transferred":
            row["done_at"] = ev.get("ts")

    live = state in LIVE_STATES
    now = datetime.now(timezone.utc)
    for row in per.values():
        d, p, f = (_ts(row.get(k)) for k in ("dispatched_at", "packaged_at", "done_at"))
        qs, ss, se = (_ts(row.get(k)) for k in
                      ("queued_at", "scrape_started", "scrape_ended"))
        end = f or (now if live else None)
        row["duration_s"] = round((end - d).total_seconds(), 1) if d and end else None
        row["transfer_s"] = round((f - p).total_seconds(), 1) if p and f else None
        if ss and se:
            row["scrape_s"] = round((se - ss).total_seconds(), 1)
            row["queue_s"] = round((ss - (qs or ss)).total_seconds(), 1)
        else:
            # older execs have no worker timeline — fall back to the old,
            # queue-inclusive figure rather than showing nothing
            row["scrape_s"] = round((p - d).total_seconds(), 1) if d and p else None
            row["queue_s"] = None
        row["running"] = f is None and live

    end = now if live else last
    elapsed = round((end - first).total_seconds(), 1) if first and end else None
    done = sum(1 for r in per.values() if r.get("done_at"))

    # Delivery rate over the tail of the run: the gap between the last N
    # packages landing, which is what "how fast is this going right now"
    # actually means once several VMs are in flight.
    stamps = sorted(t for t in (_ts(r.get("done_at")) for r in per.values()) if t)
    recent = stamps[-(THROUGHPUT_WINDOW + 1):]
    recent_s = None
    if len(recent) > 1:
        span = (recent[-1] - recent[0]).total_seconds()
        if span > 0:
            recent_s = round(span / (len(recent) - 1), 1)

    return {"started_at": first.isoformat() if first else None,
            "elapsed_s": elapsed, "done": done,
            "per_place_s": round(elapsed / done, 1) if elapsed and done else None,
            "per_hour": round(done / elapsed * 3600, 1) if elapsed and done else None,
            "recent_s": recent_s, "recent_window": max(0, len(recent) - 1),
            "recent_per_hour": round(3600 / recent_s, 1) if recent_s else None,
            "places": per}


@app.get("/api/zones")
def zone_stats():
    """
    Per-region history across every exec on disk.

    Where a VM runs turns out to matter: two Middle East regions delivered
    nothing at all while European ones ran at 65-85%, which is invisible
    without keeping score. This is the evidence for choosing zones — and for
    eventually weighting the good ones with more VMs.
    """
    zones: Dict[str, Dict[str, Any]] = {}

    def bucket(zone: str) -> Dict[str, Any]:
        return zones.setdefault(zone or "?", {
            "zone": zone or "?", "region": (zone or "?").rsplit("-", 1)[0],
            "delivered": 0, "empty": 0, "no_details": 0, "vm_lost": 0,
            "vm_retired": 0, "provision_failed": 0, "duplicate_ip": 0,
            "scrape_seconds": 0.0, "scrape_samples": 0, "runs": 0,
        })

    for exec_dir in sorted(STATE_DIR.glob("*/exec-*")):
        snap = _read_json(exec_dir / "exec.json")
        vm_zone = {vm["name"]: vm.get("zone")
                   for vm in (snap.get("vms") or []) if vm.get("name")}
        if not vm_zone:
            continue
        for z in {z for z in vm_zone.values() if z}:
            bucket(z)["runs"] += 1
        last_vm: Dict[str, str] = {}
        for ev in _log_events(exec_dir):
            name, vm = ev.get("event"), ev.get("vm")
            if name == "job_dispatched" and vm:
                last_vm[ev["place"]] = vm
            elif name == "job_transferred" and vm:
                bucket(vm_zone.get(vm))["delivered"] += 1
            elif name == "job_empty" and vm:
                bucket(vm_zone.get(vm))["empty"] += 1
            elif name == "job_packaged" and vm:
                s, e = _ts(ev.get("scrape_started")), _ts(ev.get("scrape_ended"))
                if s and e:
                    b = bucket(vm_zone.get(vm))
                    b["scrape_seconds"] += (e - s).total_seconds()
                    b["scrape_samples"] += 1
            elif name == "job_retry" and "no place details" in (ev.get("error") or ""):
                owner = last_vm.get(ev.get("place"))
                if owner:
                    bucket(vm_zone.get(owner))["no_details"] += 1
            elif name in ("vm_lost", "vm_retired", "vm_duplicate_ip") and vm:
                key = {"vm_lost": "vm_lost", "vm_retired": "vm_retired",
                       "vm_duplicate_ip": "duplicate_ip"}[name]
                bucket(vm_zone.get(vm))[key] += 1
            elif name == "vm_failed" and vm:
                bucket(vm_zone.get(vm))["provision_failed"] += 1

    out = []
    for z in zones.values():
        attempts = z["delivered"] + z["empty"] + z["no_details"]
        z["attempts"] = attempts
        z["success_rate"] = round(100 * z["delivered"] / attempts, 1) if attempts else None
        z["avg_scrape_s"] = (round(z["scrape_seconds"] / z["scrape_samples"], 1)
                             if z["scrape_samples"] else None)
        z.pop("scrape_seconds")
        out.append(z)
    out.sort(key=lambda r: (r["success_rate"] is None, -(r["success_rate"] or 0),
                            -r["delivered"]))
    return out


@app.get("/api/browse/{run_stem}/{exec_id}/places")
def browse_places(run_stem: str, exec_id: str):
    """Per-place rows for an exec: name, counts, package, status."""
    d = _exec_dir(run_stem, exec_id)
    snap = _read_json(d / "exec.json")
    jobs = snap.get("jobs", {})
    run_name = _run_name_for(run_stem)
    pkg_by_place = _pkg_by_place(d)
    timing = _timing(d, snap.get("state"))
    rows = []
    for place_id, job in jobs.items():
        t = timing["places"].get(place_id, {})
        row = {"place_id": place_id, "state": job.get("state"),
               "attempts": job.get("attempts"), "error": job.get("error"),
               "latest_review_date": job.get("latest_review_date"),
               "package": pkg_by_place.get(place_id),
               "maps_url": place_url(place_id), "resolved_url": None,
               "flags": [], "has_log": False,
               "failures": _failure_bundles(d, place_id),
               "duration_s": t.get("duration_s"), "scrape_s": t.get("scrape_s"),
               "queue_s": t.get("queue_s"), "transfer_s": t.get("transfer_s"),
               "running": t.get("running", False),
               "name": None, "rating": None, "review_count": None,
               "reviews_downloaded": 0, "owner_photos": 0,
               "review_images": 0, "bytes": None}
        tar_name = row["package"]
        if tar_name:
            tar_path = d / "packages" / tar_name
            row["bytes"] = tar_path.stat().st_size if tar_path.exists() else None
            try:
                pkg = _place_dir(d, run_name, place_id, tar_name)
                info = _read_json(pkg / "info.json")
                reviews = _read_json(pkg / "reviews.json")
                images = _images_json(pkg)
                row["name"] = info.get("name") or (info.get("place") or {}).get("place_name")
                row["resolved_url"] = (info.get("place") or {}).get("resolved_url")
                row["rating"] = info.get("rating")
                row["review_count"] = info.get("review_count")
                row["reviews_downloaded"] = reviews.get("count", 0)
                flags = _read_json(pkg / "flags.json").get("flags") or []
                row["flags"] = [{"code": f.get("code"), "message": f.get("message")}
                                for f in flags]
                row["has_log"] = (pkg / "debug.log").is_file()
                row["owner_photos"] = (images.get("owner") or {}).get("count", 0)
                row["review_images"] = (images.get("review") or {}).get("count", 0)
                if not row["review_images"]:
                    # not downloaded for this profile: count the remote URLs
                    row["review_images"] = sum(len(r.get("user_images") or [])
                                               for r in reviews.get("reviews", []))
            except HTTPException:
                pass
        rows.append(row)
    rows.sort(key=lambda r: (r["name"] is None, r["name"] or r["place_id"]))
    return {"exec": {"exec_id": exec_id, "state": snap.get("state"),
                     "profile": snap.get("profile"),
                     "elapsed_s": timing["elapsed_s"],
                     "per_place_s": timing["per_place_s"],
                     "per_hour": timing["per_hour"],
                     "recent_s": timing["recent_s"],
                     "recent_window": timing["recent_window"],
                     "recent_per_hour": timing["recent_per_hour"]},
            "places": rows}


@app.get("/api/browse/{run_stem}/{exec_id}/place/{place_id}")
def browse_place_detail(run_stem: str, exec_id: str, place_id: str):
    """Full detail for one place: info + reviews + photo list."""
    d = _exec_dir(run_stem, exec_id)
    data = browse_places(run_stem, exec_id)
    row = next((r for r in data["places"] if r["place_id"] == place_id), None)
    if not row:
        raise HTTPException(404, "place not in this exec")
    if not row.get("package"):
        # No package means it never succeeded — the postmortem captures are
        # the only thing to show, and the reason someone opened this row.
        if not row.get("failures"):
            raise HTTPException(404, "no package and no failure capture")
        return {"row": row, "info": {}, "reviews": [], "photos": [],
                "failures": _failure_detail(d, row["failures"], run_stem, exec_id,
                                            place_id)}
    pkg = _place_dir(d, _run_name_for(run_stem), place_id, row["package"])
    info = _read_json(pkg / "info.json")
    reviews = _read_json(pkg / "reviews.json")
    images = _images_json(pkg)
    base = f"/api/browse/{run_stem}/{exec_id}/file/{place_id}"
    photo_urls = []
    for x in (images.get("owner") or {}).get("items", []):
        if x.get("file"):
            photo_urls.append({"kind": "owner", "src": f"{base}/{x['file']}"})
        elif x.get("url_large"):
            photo_urls.append({"kind": "owner", "src": x["url_large"]})
    review_items = (images.get("review") or {}).get("items", [])
    if review_items:
        for x in review_items:
            photo_urls.append({"kind": "review", "src": f"{base}/{x['file']}"})
    else:
        # profile didn't download review images: link Google's copies directly
        for rv in reviews.get("reviews", []):
            for url in rv.get("user_images") or []:
                photo_urls.append({"kind": "review", "src": url})
    return {"row": row, "info": info, "output_dir": str(pkg),
            "reviews": reviews.get("reviews", []),
            "photos": photo_urls}


def _failure_detail(exec_dir: Path, bundles: List[Dict[str, Any]],
                    run_stem: str, exec_id: str, place_id: str) -> List[Dict[str, Any]]:
    """Unpack each bundle and describe what it holds, with URLs to serve it."""
    base = f"/api/browse/{run_stem}/{exec_id}/failure/{place_id}"
    out = []
    for b in bundles:
        try:
            root = _failure_dir(exec_dir, b["file"])
        except HTTPException:
            continue
        captures = []
        for meta_path in sorted((root / "diagnostics").glob("*/meta.json")):
            meta = _read_json(meta_path)
            folder = meta_path.parent.name
            captures.append({
                "code": meta.get("code"), "at": meta.get("at"),
                "url": meta.get("url"), "title": meta.get("title"),
                "tabs": meta.get("tabs"), "detail": meta.get("detail"),
                "controls": meta.get("review_controls"),
                "screenshot": f"{base}/{b['file']}/diagnostics/{folder}/screen.png"
                              if (meta_path.parent / "screen.png").exists() else None,
                "dom": f"{base}/{b['file']}/diagnostics/{folder}/page.html"
                       if (meta_path.parent / "page.html").exists() else None,
            })
        out.append({"attempt": b["attempt"], "bytes": b["bytes"],
                    "log": f"{base}/{b['file']}/scrape.log"
                           if (root / "scrape.log").exists() else None,
                    "captures": captures})
    return out


@app.get("/api/browse/{run_stem}/{exec_id}/failure/{place_id}/{name}/{path:path}")
def browse_failure_file(run_stem: str, exec_id: str, place_id: str,
                        name: str, path: str):
    """Serve one file out of a place's postmortem bundle."""
    d = _exec_dir(run_stem, exec_id)
    if not name.startswith(safe_name(place_id)):
        raise HTTPException(404, "bundle does not belong to this place")
    root = _failure_dir(d, name).resolve()
    f = (root / path).resolve()
    if not f.is_file() or not f.is_relative_to(root):
        raise HTTPException(404, "file not found")
    media = ("image/png" if f.suffix == ".png"
             else "text/html" if f.suffix == ".html" else "text/plain")
    return FileResponse(f, media_type=media)


@app.get("/api/browse/{run_stem}/{exec_id}/file/{place_id}/{path:path}")
def browse_file(run_stem: str, exec_id: str, place_id: str, path: str):
    """Serve one file (photo) out of a place's folder."""
    d = _exec_dir(run_stem, exec_id)
    tar_name = _pkg_by_place(d).get(place_id)
    if not tar_name:
        raise HTTPException(404, "no package for this place")
    pkg = _place_dir(d, _run_name_for(run_stem), place_id, tar_name).resolve()
    f = (pkg / path).resolve()
    if not f.is_file() or not f.is_relative_to(pkg):
        raise HTTPException(404, "file not found")
    return FileResponse(f)


@app.get("/api/verify")
def verify():
    """Independent leftover-VM check (the post-shutdown cost guarantee)."""
    if _current:
        try:
            return {"remaining": _current.provider.list_remaining()}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    return {"remaining": None, "note": "no execution; use gcloud directly"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ORCH_PORT", "8140"))
    log.info(f"Orchestrator on http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
