"""
VM providers for the orchestrator.

- LocalProvider: each "VM" is a local worker subprocess on its own port.
  Used to exercise the full coordinator flow (provision -> ready -> dispatch
  -> collect -> shutdown -> verify) without cloud cost.
- GcpProvider: real VMs via the gcloud CLI. One worker (workers/server.py) per VM,
  reached through an SSH tunnel (no public firewall opening). VMs are labeled
  `hermes=1` so leftover-instance verification is a simple filtered list.

Provider contract (all methods synchronous; coordinator threads them):
    provision(vm) -> None      boots + installs + starts worker; sets vm fields
    endpoint(vm) -> str        base URL the coordinator should call
    egress_ip(vm) -> str|None  what the VM's outbound traffic looks like
    delete(vm) -> None
    list_remaining() -> list   names of instances still alive (MUST be [] after
                               a full shutdown — this is the cost guarantee)
"""

import json
import logging
import os
import signal
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("orchestrator")

REPO_ROOT = Path(__file__).resolve().parent.parent


class LocalProvider:
    name = "local"

    def __init__(self, api_key: str, out_root: Path, base_port: int = 9151):
        self.api_key = api_key
        self.out_root = out_root
        self.base_port = base_port
        self._procs: Dict[str, subprocess.Popen] = {}

    def provision(self, vm: Dict[str, Any]) -> None:
        port = self.base_port + vm["index"]
        out_dir = self.out_root / vm["name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ,
                   SPOT_API_KEY=self.api_key,
                   SPOT_PORT=str(port),
                   SPOT_OUT_DIR=str(out_dir),
                   SPOT_SCRAPE_WORKERS=str(vm.get("slots", 1)))
        # Its own process group, so the whole tree can be taken down later.
        # A worker spawns Chrome and a uc_driver; terminating only the worker
        # left those running — three of them were found still alive hours
        # after the run that started them had finished.
        proc = subprocess.Popen(
            [sys.executable, "-m", "workers.server"],
            cwd=str(REPO_ROOT), env=env,
            stdout=open(out_dir / "worker.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True)
        self._procs[vm["name"]] = proc
        vm["port"] = port
        # wait for health
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/health",
                                 headers={"X-API-Key": self.api_key}, timeout=3)
                if r.ok:
                    return
            except requests.RequestException:
                pass
            if proc.poll() is not None:
                raise RuntimeError(f"worker process died (see {out_dir}/worker.log)")
            time.sleep(1)
        raise RuntimeError("worker did not become healthy in 60s")

    def endpoint(self, vm: Dict[str, Any]) -> str:
        return f"http://127.0.0.1:{vm['port']}"

    def egress_ip(self, vm: Dict[str, Any]) -> Optional[str]:
        try:
            return requests.get("https://api.ipify.org", timeout=10).text.strip()
        except requests.RequestException:
            return None

    def delete(self, vm: Dict[str, Any]) -> None:
        """
        Stop the worker and everything it started.

        Signalling the process group rather than the process: the browser and
        its driver are children, and killing only the parent orphans them.
        """
        proc = self._procs.pop(vm["name"], None)
        if not proc or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    def list_remaining(self) -> List[str]:
        return [name for name, p in self._procs.items() if p.poll() is None]


# --- GCP ---------------------------------------------------------------------

STARTUP_SCRIPT = """#!/bin/bash
set -e
apt-get update -y
apt-get install -y python3-venv python3-pip curl wget gnupg unzip
wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y /tmp/chrome.deb || apt-get -f install -y
"""


def _gcloud_reason(stderr: str) -> str:
    """
    The part of a gcloud failure worth reading.

    gcloud prefixes a WARNING about boot disks under 200GB to every create,
    so an unfiltered stderr leads with a note about I/O performance and
    buries the actual cause — a zone out of spot capacity was reported to us
    as a disk-size problem. Known-benign noise is dropped, the ERROR line is
    promoted, and its code is put first because that is the actionable part:
    ZONE_RESOURCE_POOL_EXHAUSTED means try another zone, not fix the disk.
    """
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    noise = ("WARNING: You have selected a disk size",
             "For more information, see",
             "Troubleshooting documentation")
    kept = [l for l in lines if not any(l.startswith(n) for n in noise)]
    text = " ".join(kept) or " ".join(lines) or "(no output)"

    for code, plain in (
        ("ZONE_RESOURCE_POOL_EXHAUSTED", "bölgede kapasite yok"),
        ("QUOTA_EXCEEDED", "kota doldu"),
        ("Permission denied", "bölge/izin kapalı"),
        ("Reauthentication", "gcloud oturumu süresi doldu"),
    ):
        if code in text:
            return f"{plain} ({code})"
    idx = text.find("ERROR:")
    return (text[idx:] if idx >= 0 else text)[:300]


class GcpProvider:
    """
    Real VMs through the gcloud CLI.

    Flow per VM: create (labeled) -> wait ssh -> scp repo tarball -> install
    venv + deps -> launch the worker -> open ssh tunnel (local port ->
    VM-local 8100) -> health check -> egress IP via ssh curl.
    """
    name = "gcp"

    def __init__(self, api_key: str, out_root: Path,
                 machine_type: str = "e2-standard-2",
                 zones: Optional[List[str]] = None,
                 base_port: int = 9251,
                 spot: bool = False):
        self.api_key = api_key
        self.out_root = out_root
        self.machine_type = machine_type
        self.spot = spot
        self.zones = zones or ["europe-west1-b", "europe-west4-a",
                               "us-central1-a", "us-east1-b"]
        self.base_port = base_port
        self._tunnels: Dict[str, subprocess.Popen] = {}
        self._tarball: Optional[Path] = None

    def _run(self, args: List[str], timeout: int = 300) -> str:
        res = subprocess.run(["gcloud"] + args, capture_output=True,
                             text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(
                f"gcloud {' '.join(args[:3])}: {_gcloud_reason(res.stderr)}")
        return res.stdout

    @staticmethod
    def check() -> Dict[str, Any]:
        """
        Report gcloud availability/auth/project for the UI.

        An ACTIVE account in `gcloud auth list` is not proof of a usable
        session — the refresh token expires while the account stays listed,
        and every API call then dies with "Reauthentication failed. cannot
        prompt during non-interactive execution". So mint an access token as
        well: that is the cheapest call that actually exercises the refresh.
        """
        if not shutil.which("gcloud"):
            return {"ok": False, "reason": "gcloud not installed"}
        try:
            acct = subprocess.run(
                ["gcloud", "auth", "list", "--filter=status:ACTIVE",
                 "--format=value(account)"],
                capture_output=True, text=True, timeout=20).stdout.strip()
            proj = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True, text=True, timeout=20).stdout.strip()
            if not (acct and proj):
                return {"ok": False, "account": acct, "project": proj,
                        "reason": "no active account or project"}
            token = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, timeout=30)
            if token.returncode != 0:
                reason = "oturum süresi dolmuş — `gcloud auth login` çalıştır"
                if "Reauthentication" not in token.stderr:
                    reason = token.stderr.strip().splitlines()[0][:160] or reason
                return {"ok": False, "account": acct, "project": proj,
                        "reason": reason}
            return {"ok": True, "account": acct, "project": proj}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)}

    def _ensure_tarball(self) -> Path:
        if self._tarball and self._tarball.exists():
            return self._tarball
        tar = self.out_root / "hermes-scraper.tar.gz"
        tar.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "archive", "--format=tar.gz",
                        "-o", str(tar), "HEAD"],
                       cwd=str(REPO_ROOT), check=True)
        self._tarball = tar
        return tar

    def zone_for(self, index: int) -> str:
        return self.zones[index % len(self.zones)]

    def provision(self, vm: Dict[str, Any]) -> None:
        # A replacement can name the zone it wants. Index rotation alone sends
        # it back where it came from — with thirteen zones the replacement for
        # index 0 is index 13, which is zone 0 again, so a VM rejected for
        # sharing an egress address was rebuilt in the address range that
        # produced it.
        zone = vm.get("zone_hint") or self.zone_for(vm["index"])
        vm["zone"] = zone
        name = vm["name"]
        create = [
            "compute", "instances", "create", name,
            "--zone", zone,
            "--machine-type", self.machine_type,
            "--image-family", "debian-12",
            "--image-project", "debian-cloud",
            "--boot-disk-size", "20GB",
            "--labels", "hermes=1",
            "--metadata", f"startup-script={STARTUP_SCRIPT}",
        ]
        if self.spot:
            # Preemption is survivable here: the coordinator returns a lost
            # VM's in-flight jobs to the queue. DELETE on termination keeps a
            # preempted VM from lingering as a stopped instance.
            create += ["--provisioning-model=SPOT",
                       "--instance-termination-action=DELETE"]
        self._run(create, timeout=300)

        # wait for SSH + chrome install (startup script)
        deadline = time.time() + 420
        while time.time() < deadline:
            try:
                out = self._ssh(name, zone,
                                "test -x /usr/bin/google-chrome && echo READY || echo WAIT")
                if "READY" in out:
                    break
            except RuntimeError:
                pass
            time.sleep(15)
        else:
            raise RuntimeError("VM did not become ssh/chrome-ready in 7min")

        # ship + install the scraper
        tar = self._ensure_tarball()
        self._run(["compute", "scp", str(tar), f"{name}:/tmp/scraper.tar.gz",
                   "--zone", zone], timeout=300)
        install = (
            "mkdir -p ~/scraper && tar xzf /tmp/scraper.tar.gz -C ~/scraper && "
            "cd ~/scraper && python3 -m venv .venv && "
            ".venv/bin/pip install -q -r requirements.txt -c constraints.txt && "
            "cp config.sample.yaml config.yaml"
        )
        self._ssh(name, zone, install, timeout=600)

        # Hand the worker to systemd rather than backgrounding it in the ssh
        # session. A long-lived process started with `nohup ... &` holds the
        # ssh channel open even with setsid and all three streams redirected
        # (measured: the command never returns, and gcloud times out while the
        # worker is in fact running). systemd-run returns as soon as systemd
        # has taken ownership.
        launch = (
            "sudo systemd-run --unit=hermes-worker --collect "
            "--working-directory=$HOME/scraper "
            f"--setenv=SPOT_API_KEY={shlex.quote(self.api_key)} "
            "--setenv=SPOT_PORT=8100 "
            f"--setenv=SPOT_SCRAPE_WORKERS={vm.get('slots', 1)} "
            "-p StandardOutput=append:$HOME/scraper/worker.log "
            "-p StandardError=append:$HOME/scraper/worker.log "
            # systemd-run execs a command, it does not run a shell, so no
            # "cd X && ..." here — the working directory is set above, which
            # is also what puts $HOME/scraper on sys.path for `-m`.
            "$HOME/scraper/.venv/bin/python -m workers.server"
        )
        self._ssh(name, zone, launch, timeout=120)

        # ssh tunnel: local port -> vm:8100 (no public firewall needed)
        port = self.base_port + vm["index"]
        tunnel = subprocess.Popen(
            ["gcloud", "compute", "ssh", name, "--zone", zone, "--",
             "-N", "-L", f"{port}:localhost:8100"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._tunnels[name] = tunnel
        vm["port"] = port

        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/health",
                                 headers={"X-API-Key": self.api_key}, timeout=3)
                if r.ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError("worker not healthy through tunnel in 90s")

    def _ssh(self, name: str, zone: str, command: str, timeout: int = 120) -> str:
        return self._run(["compute", "ssh", name, "--zone", zone,
                          "--command", command], timeout=timeout)

    def endpoint(self, vm: Dict[str, Any]) -> str:
        return f"http://127.0.0.1:{vm['port']}"

    def egress_ip(self, vm: Dict[str, Any]) -> Optional[str]:
        try:
            return self._ssh(vm["name"], vm["zone"],
                             "curl -s https://api.ipify.org", timeout=30).strip()
        except RuntimeError:
            return None

    def delete(self, vm: Dict[str, Any]) -> None:
        tunnel = self._tunnels.pop(vm["name"], None)
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
        zone = vm.get("zone")
        if not zone:
            return                      # never reached `create`, nothing exists
        try:
            self._run(["compute", "instances", "delete", vm["name"],
                       "--zone", zone, "--quiet"], timeout=300)
        except RuntimeError as e:
            # Already gone is the desired end state, not a failure.
            if "was not found" in str(e) or "notFound" in str(e):
                log.info(f"{vm['name']} already gone")
                return
            raise

    def list_remaining(self) -> List[str]:
        out = self._run(["compute", "instances", "list",
                         "--filter=labels.hermes=1",
                         "--format=value(name)"], timeout=60)
        return [ln for ln in out.strip().splitlines() if ln]

    @staticmethod
    def list_all_hermes() -> List[str]:
        """
        Every hermes VM in the project, asked without an execution.

        Shutdown has to be able to check after the execution object is gone —
        and the whole point of the check is to trust gcloud rather than our
        own record of what we deleted.
        """
        try:
            out = subprocess.run(
                ["gcloud", "compute", "instances", "list",
                 "--filter=labels.hermes=1", "--format=value(name)"],
                capture_output=True, text=True, timeout=90)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"gcloud sorgulanamadı: {e}") from e
        if out.returncode != 0:
            raise RuntimeError(_gcloud_reason(out.stderr))
        return [ln for ln in out.stdout.strip().splitlines() if ln]
