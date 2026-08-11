"""
Spot-VM oriented scrape API: two queues, JSON-only output packages.

Designed for ephemeral cloud instances (spot/preemptible VMs) where the
machine can disappear at any moment:

- Queue 1 (SCRAPE):   runs the browser scrape, builds a self-contained
                      package per job, tarred as
                      <place_id>/{info,reviews,images}.json plus
                      <place_id>/images/{ownerimages,reviewimages}/*.jpg
- Queue 2 (TRANSFER): ships the finished package off the VM (webhook POST
                      or S3/R2 upload) so results survive VM termination;
                      `none` mode leaves it for pull via /jobs/{id}/download

SQLite is used only as the scraper's internal working store inside a
per-job temp directory (dedup + pipeline engine) and is deleted after
packaging. Everything that leaves the VM is JSON.

Job state persists to {out_dir}/jobs.json after every transition, so a
restarted server reports prior jobs (running ones become "interrupted").

Auth: X-API-Key must equal the SPOT_API_KEY env var (auth disabled with a
warning when unset). Run:  python spot_server.py   (port: SPOT_PORT, 8100)
"""

import contextlib
import json
import logging
import os
import queue
import shutil
import tarfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests as _requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

from workers.engine.config import load_config
from workers.engine.log_manager import setup_logging

log = logging.getLogger("scraper")

OUT_DIR = Path(os.environ.get("SPOT_OUT_DIR", "spot_out"))
API_KEY = os.environ.get("SPOT_API_KEY", "")
SCRAPE_WORKERS = int(os.environ.get("SPOT_SCRAPE_WORKERS", "1"))
TRANSFER_RETRIES = 3

_scrape_q: "queue.Queue[str]" = queue.Queue()
_transfer_q: "queue.Queue[str]" = queue.Queue()
_jobs: Dict[str, Dict[str, Any]] = {}
_cancel_events: Dict[str, threading.Event] = {}
_jobs_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_jobs() -> None:
    """
    Write jobs.json atomically.

    The temp file has to be unique: this is called from the scrape worker,
    the transfer worker and API handlers, and with one shared "jobs.json.tmp"
    two threads write the same path, the first rename moves it away and the
    second fails with "No such file or directory: jobs.json.tmp".
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        snapshot = json.dumps(_jobs, ensure_ascii=False, indent=1)
    tmp = OUT_DIR / f"jobs.json.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp.write_text(snapshot, encoding="utf-8")
        os.replace(tmp, OUT_DIR / "jobs.json")
    except OSError as e:
        log.warning(f"could not persist jobs.json: {e}")
        tmp.unlink(missing_ok=True)


def _set_state(job_id: str, state: str, **extra) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job["state"] = state
        job["timeline"][state] = _now()
        job.update(extra)
    _persist_jobs()
    log.info(f"[{job_id}] -> {state}")


class _LogBuffer(logging.Handler):
    """Keeps the tail of a job's log in memory, capped so it can't run away."""

    def __init__(self, limit: int = 4000):
        super().__init__()
        self.lines: List[str] = []
        self.limit = limit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            return
        if len(self.lines) > self.limit:
            del self.lines[: len(self.lines) - self.limit]

    def text(self) -> str:
        return "\n".join(self.lines)


@contextlib.contextmanager
def _job_log_capture(limit: int = 4000):
    """Attach a buffer to the root logger for the duration of one job."""
    buf = _LogBuffer(limit)
    buf.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(buf)
    try:
        yield buf
    finally:
        root.removeHandler(buf)


def _progress_setter(job_id: str):
    """
    Build the scraper's progress callback for one job.

    Updates are hot (several per second while reviews stream in), so they are
    kept in memory only — jobs.json is written on state transitions, not here.
    """
    def report(update: Dict[str, Any]) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["progress"] = {**update, "at": _now()}
    return report


def _load_prior_jobs() -> None:
    path = OUT_DIR / "jobs.json"
    if not path.exists():
        return
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return
    for jid, job in prior.items():
        if job.get("state") in ("queued", "scraping", "packaging",
                                "transfer_queued", "transferring"):
            job["state"] = "interrupted"
        _jobs[jid] = job
    _persist_jobs()


# --- Package building -------------------------------------------------------

def _safe_name(value: str) -> str:
    """Filesystem-safe single path segment."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(value))
    return out.strip("._") or "unknown"


def _build_failure_bundle(job: Dict[str, Any], workdir: Path) -> Optional[Path]:
    """
    Tar up everything a failed job left behind, for a postmortem after the VM
    is gone: the captured screenshots and DOM, the job's log, and its request.

        <job_id>-failure/
            request.json
            scrape.log
            diagnostics/<time>_<code>/{screen.png,page.html,meta.json}
    """
    diag = workdir / "diagnostics"
    root = workdir / "failure"
    root.mkdir(exist_ok=True)
    (root / "request.json").write_text(
        json.dumps({"job_id": job["job_id"], "request": job.get("request"),
                    "state": job.get("state"), "timeline": job.get("timeline")},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    if job.get("scrape_log"):
        (root / "scrape.log").write_text(job["scrape_log"], encoding="utf-8")
    if diag.is_dir():
        shutil.copytree(diag, root / "diagnostics", dirs_exist_ok=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = OUT_DIR / f"{job['job_id']}-failure.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(root, arcname=f"{job['job_id']}-failure")
    log.info(f"[{job['job_id']}] failure bundle: {tar_path.name} "
             f"({tar_path.stat().st_size} bytes)")
    return tar_path


def _build_package(job: Dict[str, Any], workdir: Path) -> Path:
    """
    Assemble one tar.gz laid out exactly like the coordinator's outputs tree:

        <place_id>/
            info.json
            reviews.json
            images.json
            images/ownerimages/*.jpg
            images/reviewimages/*.jpg

    <place_id> is the caller's id (meta.place_id, e.g. ChIJ...) when given, so
    the folder matches the run file; otherwise Google's internal 0x...:N id.
    """
    from workers.engine.review_db import ReviewDB

    db = ReviewDB(str(workdir / "db.sqlite"))
    try:
        # A package without the business details is an incomplete package, and
        # an incomplete package is not worth delivering — fail it here so the
        # place is retried, now on a different VM.
        all_details = db.list_place_details()
        if not all_details:
            raise RuntimeError("scrape produced no place details")
        place_id, details = next(iter(all_details.items()))
        reviews = db.get_reviews(place_id, limit=1_000_000)
        place_row = db.get_place(place_id) or {}
    finally:
        db.close()

    pkg_dir = workdir / "package"
    pkg_dir.mkdir(exist_ok=True)

    owner_photos = details.pop("owner_photos", None) or {}
    info = {
        "place_id": place_id,
        "source_job": job["job_id"],
        "place": place_row,
        **details,
    }
    (pkg_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    (pkg_dir / "reviews.json").write_text(json.dumps({
        "place_id": place_id,
        "count": len(reviews),
        "scraped_at": details.get("scraped_at"),
        "reviews": reviews,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # owner photos: copy downloaded files into images/ownerimages/
    owner_dir = pkg_dir / "images" / "ownerimages"
    photo_items: List[Dict[str, Any]] = []
    for p in owner_photos.get("photos") or []:
        item = dict(p)
        local = p.get("local_path")
        if local:
            src = workdir / "images" / local
            if src.exists():
                owner_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, owner_dir / src.name)
                item["file"] = f"images/ownerimages/{src.name}"
        item.pop("local_path", None)
        photo_items.append(item)

    # review-attached images (downloaded when the job asked for them);
    # ImageHandler nests them as images/[{place_id}/]reviews/* — flatten those
    # into images/reviewimages/
    review_items: List[Dict[str, Any]] = []
    review_dir = pkg_dir / "images" / "reviewimages"
    images_root = workdir / "images"
    if images_root.is_dir():
        for rdir in images_root.glob("**/reviews"):
            if not rdir.is_dir():
                continue
            for src in sorted(rdir.rglob("*")):
                if not src.is_file():
                    continue
                review_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, review_dir / src.name)
                review_items.append({"file": f"images/reviewimages/{src.name}"})

    # Red flags travel with the package, and so does the log that produced
    # them — the VM is deleted minutes later, so this is the only chance.
    flags = job.get("flags") or []
    if flags:
        (pkg_dir / "flags.json").write_text(
            json.dumps({"place_id": place_id, "flags": flags},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        if job.get("debug_log"):
            (pkg_dir / "debug.log").write_text(job["debug_log"], encoding="utf-8")

    (pkg_dir / "images.json").write_text(json.dumps({
        "place_id": place_id,
        "owner": {
            "available": owner_photos.get("available", False),
            "count": sum(1 for i in photo_items if i.get("file")),
            "items": photo_items,
        },
        "review": {"count": len(review_items), "items": review_items},
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # folder (and tar root) name: caller's place_id when supplied
    meta_place = ((job.get("request") or {}).get("meta") or {}).get("place_id")
    folder = _safe_name(meta_place or place_id)
    tar_path = OUT_DIR / f"{folder}_{job['job_id']}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(pkg_dir, arcname=folder)

    with _jobs_lock:
        job["place_id"] = place_id
        job["package"] = {
            "file": tar_path.name,
            "folder": folder,
            "bytes": tar_path.stat().st_size,
            "reviews": len(reviews),
            "photos": sum(1 for i in photo_items if i.get("file")),
            "review_images": len(review_items),
            "flags": [f.get("code") for f in flags],
        }
    return tar_path


# --- Workers ----------------------------------------------------------------

def _scrape_worker() -> None:
    from workers.engine.scraper import GoogleReviewsScraper

    while True:
        job_id = _scrape_q.get()
        job = _jobs.get(job_id)
        if job is None or job["state"] != "queued":
            continue

        workdir = OUT_DIR / "work" / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            _set_state(job_id, "scraping")
            config = load_config()
            config.update({
                "url": job["request"]["url"],
                "businesses": [],
                "urls": [],
                "headless": True,
                "use_mongodb": False,
                "use_s3": False,
                "backup_to_json": True,
                "db_path": str(workdir / "db.sqlite"),
                "json_path": str(workdir / "reviews_raw.json"),
                "seen_ids_path": str(workdir / "reviews_raw.ids"),
                "image_dir": str(workdir / "images"),
                # Failure capture (screenshot + DOM) only when the caller asks:
                # it costs nothing on success but writes megabytes per failure.
                "diagnostics_dir": (str(workdir / "diagnostics")
                                    if job["request"].get("capture_failures", True)
                                    else ""),
                "place_details_json_path": "",
                "download_images": bool(job["request"].get("download_review_images", False)),
                "scrape_place_details": True,
                "scrape_place_photos": True,
                "download_place_photos": True,
            })
            for key in ("max_reviews", "max_reviews_cap",
                        "min_review_photos", "sort_by",
                        "language", "place_photos_limit", "scrape_mode",
                        "date_filter", "extended_warmup"):
                if job["request"].get(key) is not None:
                    config[key] = job["request"][key]

            cancel = _cancel_events.setdefault(job_id, threading.Event())
            scraper = GoogleReviewsScraper(config, cancel_event=cancel,
                                           progress_cb=_progress_setter(job_id))
            # Capture this job's log so a flagged package can carry it home;
            # the VM is usually gone by the time anyone looks.
            with _job_log_capture() as job_log:
                try:
                    scraper.scrape()
                finally:
                    # Keep the log on the job whatever happens. A place that
                    # fails to package produces no package, so shipping the
                    # log inside one loses exactly the cases worth reading;
                    # the coordinator pulls this from the job instead.
                    job["scrape_log"] = job_log.text()
            job["flags"] = list(scraper.flags)
            job["debug_log"] = job_log.text() if scraper.flags else None

            if cancel.is_set():
                _set_state(job_id, "cancelled")
                continue

            _set_state(job_id, "packaging")
            _build_package(job, workdir)
            _set_state(job_id, "transfer_queued")
            _transfer_q.put(job_id)
        except Exception as e:
            log.exception(f"[{job_id}] scrape failed")
            # Ship everything the failure left behind: screenshots, the DOM as
            # it stood, and the job's log. Guessing from a stack trace is how
            # the last fix addressed one cause and missed a second.
            try:
                bundle = _build_failure_bundle(job, workdir)
                _set_state(job_id, "error", error=str(e),
                           failure_bundle=bundle.name if bundle else None)
            except Exception:  # noqa: BLE001
                log.exception(f"[{job_id}] failure bundle could not be built")
                _set_state(job_id, "error", error=str(e))
        finally:
            if not os.environ.get("SPOT_KEEP_WORKDIR"):
                shutil.rmtree(workdir, ignore_errors=True)


def _transfer_worker() -> None:
    while True:
        job_id = _transfer_q.get()
        job = _jobs.get(job_id)
        if job is None or job["state"] != "transfer_queued":
            continue

        tar_path = OUT_DIR / job["package"]["file"]
        mode = job["request"].get("transfer") or (
            "webhook" if job["request"].get("webhook_url") else "none")
        _set_state(job_id, "transferring", transfer_mode=mode)

        try:
            if mode == "webhook":
                _transfer_webhook(job, tar_path)
            elif mode == "s3":
                _transfer_s3(job, tar_path)
            elif mode != "none":
                raise ValueError(f"unknown transfer mode: {mode}")
            _set_state(job_id, "done")
        except Exception as e:
            log.exception(f"[{job_id}] transfer failed")
            # package stays on disk for pull via /download
            _set_state(job_id, "transfer_error", error=str(e))


def _transfer_webhook(job: Dict[str, Any], tar_path: Path) -> None:
    url = job["request"]["webhook_url"]
    last_err: Optional[Exception] = None
    for attempt in range(1, TRANSFER_RETRIES + 1):
        try:
            with open(tar_path, "rb") as f:
                resp = _requests.post(
                    url,
                    files={"package": (tar_path.name, f, "application/gzip")},
                    data={
                        "job_id": job["job_id"],
                        "place_id": job.get("place_id", ""),
                        "reviews": str(job["package"]["reviews"]),
                        "photos": str(job["package"]["photos"]),
                    },
                    timeout=120,
                )
            resp.raise_for_status()
            with _jobs_lock:
                job["transfer_result"] = {
                    "status_code": resp.status_code, "attempt": attempt}
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"webhook transfer failed after {TRANSFER_RETRIES} attempts: {last_err}")


def _transfer_s3(job: Dict[str, Any], tar_path: Path) -> None:
    from workers.engine.s3_handler import S3Handler

    config = load_config()
    config["use_s3"] = True
    handler = S3Handler(config)
    key = f"packages/{tar_path.name}"
    url = handler.upload_file(tar_path, key)
    if not url:
        raise RuntimeError("S3 upload returned no URL (check s3 config)")
    with _jobs_lock:
        job["transfer_result"] = {"s3_key": key, "url": url}


# --- API --------------------------------------------------------------------

class SpotJobRequest(BaseModel):
    url: HttpUrl = Field(..., description="Google Maps place URL")
    max_reviews: Optional[int] = Field(None, description="0/None = all reviews")
    min_review_photos: Optional[int] = Field(
        None, description="keep scraping past max_reviews until this many review photos")
    capture_failures: bool = Field(
        True, description="on failure, save a screenshot + DOM + meta for postmortem")
    max_reviews_cap: Optional[int] = Field(
        None, description="hard ceiling on reviews when min_review_photos pushes past max_reviews")
    sort_by: Optional[str] = Field(None, description="newest|highest|lowest|relevance")
    language: Optional[str] = Field(None, description="UI language (default en)")
    place_photos_limit: Optional[int] = None
    download_review_images: bool = False
    date_filter: Optional[Dict[str, Any]] = Field(
        None, description="e.g. {'after': '2026-08-01', 'mode': 'early_stop'} for update runs")
    meta: Optional[Dict[str, Any]] = Field(
        None, description="opaque client metadata echoed back in job status")
    transfer: Optional[str] = Field(
        None, description="webhook | s3 | none (default: webhook if webhook_url set, else none)")
    webhook_url: Optional[HttpUrl] = Field(
        None, description="POST target for the finished package (multipart)")


def require_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


app = FastAPI(title="Google Reviews Scraper — Spot API", version="1.0",
              dependencies=[Depends(require_key)])


@app.get("/health")
def health():
    return {"ok": True, "queued": _scrape_q.qsize(),
            "transfer_queued": _transfer_q.qsize(),
            "jobs": len(_jobs)}


@app.post("/jobs", status_code=202)
def create_job(req: SpotJobRequest):
    job_id = uuid.uuid4().hex[:12]
    request = json.loads(req.model_dump_json(exclude_none=True))
    job = {
        "job_id": job_id,
        "state": "queued",
        "request": request,
        "timeline": {"queued": _now()},
    }
    with _jobs_lock:
        _jobs[job_id] = job
    _persist_jobs()
    _scrape_q.put(job_id)
    return {"job_id": job_id, "state": "queued"}


@app.get("/jobs")
def list_jobs(limit: int = 50):
    with _jobs_lock:
        items = sorted(_jobs.values(),
                       key=lambda j: j["timeline"].get("queued", ""),
                       reverse=True)[:limit]
        return [{k: j.get(k) for k in
                 ("job_id", "state", "place_id", "package", "error")}
                for j in items]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job["state"] in ("queued", "scraping"):
        _cancel_events.setdefault(job_id, threading.Event()).set()
        if job["state"] == "queued":
            _set_state(job_id, "cancelled")
        return {"job_id": job_id, "state": _jobs[job_id]["state"],
                "note": "cancel requested"}
    raise HTTPException(status_code=409,
                        detail=f"cannot cancel job in state {job['state']}")


@app.get("/jobs/{job_id}/failure")
def download_failure(job_id: str):
    """The postmortem bundle for a job that failed — screenshots, DOM, log."""
    job = _jobs.get(job_id)
    if not job or not job.get("failure_bundle"):
        raise HTTPException(status_code=404, detail="no failure bundle")
    path = OUT_DIR / job["failure_bundle"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="failure bundle gone")
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@app.get("/jobs/{job_id}/download")
def download_package(job_id: str):
    job = _jobs.get(job_id)
    if not job or not job.get("package"):
        raise HTTPException(status_code=404, detail="package not found")
    path = OUT_DIR / job["package"]["file"]
    if not path.exists():
        raise HTTPException(status_code=410, detail="package file gone")
    return FileResponse(path, media_type="application/gzip",
                        filename=path.name)


def main() -> None:
    import uvicorn

    setup_logging()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _load_prior_jobs()
    if not API_KEY:
        log.warning("SPOT_API_KEY not set — API is UNAUTHENTICATED")

    for _ in range(max(1, SCRAPE_WORKERS)):
        threading.Thread(target=_scrape_worker, daemon=True).start()
    threading.Thread(target=_transfer_worker, daemon=True).start()

    port = int(os.environ.get("SPOT_PORT", "8100"))
    log.info(f"Spot API listening on 0.0.0.0:{port} "
             f"(scrape workers: {SCRAPE_WORKERS}, out: {OUT_DIR})")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
