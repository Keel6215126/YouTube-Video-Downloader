from __future__ import annotations

import asyncio
import base64
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import zipfile
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp.utils import DownloadError


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_ROOT", "/tmp/railway-youtube-downloader")
).resolve()

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_QUEUED_JOBS = max(1, int(os.getenv("MAX_QUEUED_JOBS", "10")))
MAX_JOBS_PER_HOUR = max(1, int(os.getenv("MAX_JOBS_PER_HOUR", "20")))
MAX_DURATION_SECONDS = max(60, int(os.getenv("MAX_DURATION_SECONDS", "14400")))
COMPLETED_JOB_TTL_SECONDS = max(
    300, int(os.getenv("COMPLETED_JOB_TTL_SECONDS", "1800"))
)
FAILED_JOB_TTL_SECONDS = max(
    300, int(os.getenv("FAILED_JOB_TTL_SECONDS", "900"))
)
ACTIVE_JOB_TIMEOUT_SECONDS = max(
    1800, int(os.getenv("ACTIVE_JOB_TIMEOUT_SECONDS", "21600"))
)
COOKIE_FILE = DOWNLOAD_ROOT / "youtube-cookies.txt"

ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
}
MEDIA_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


@dataclass
class Job:
    id: str
    url: str
    token: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "Waiting for an available download slot…"
    title: str | None = None
    uploader: str | None = None
    duration: int | None = None
    quality: str | None = None
    speed: str | None = None
    eta: int | None = None
    filename: str | None = None
    file_size: int | None = None
    file_path: str | None = None
    package_as_zip: bool = True
    is_archive: bool = False
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    package_as_zip: bool = True


jobs: dict[str, Job] = {}
jobs_lock = threading.RLock()
rate_limit_lock = threading.Lock()
rate_events: dict[str, deque[float]] = defaultdict(deque)
executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_DOWNLOADS,
    thread_name_prefix="youtube-download",
)


def decode_cookie_file() -> None:
    encoded = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if not encoded:
        return

    try:
        cookie_bytes = base64.b64decode(encoded, validate=True)
        if not cookie_bytes:
            raise ValueError("decoded cookie file is empty")
        COOKIE_FILE.write_bytes(cookie_bytes)
        COOKIE_FILE.chmod(0o600)
    except Exception as exc:
        raise RuntimeError(
            "YOUTUBE_COOKIES_B64 is not valid base64-encoded cookie-file data"
        ) from exc


def normalize_and_validate_youtube_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)

    if parsed.scheme.lower() not in {"http", "https"}:
        raise HTTPException(
            status_code=400,
            detail="Enter a full YouTube URL beginning with https://",
        )

    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(
        host == allowed_host or host.endswith(f".{allowed_host}")
        for allowed_host in ALLOWED_YOUTUBE_HOSTS
    )
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail="Only YouTube and youtu.be links are supported.",
        )

    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

    return value


def require_auth(
    x_app_password: str | None = Header(default=None, alias="X-App-Password"),
) -> None:
    if not APP_PASSWORD:
        return

    supplied = x_app_password or ""
    if not hmac.compare_digest(supplied, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect app password.")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def enforce_rate_limit(ip: str) -> None:
    now = time.time()
    cutoff = now - 3600

    with rate_limit_lock:
        events = rate_events[ip]
        while events and events[0] < cutoff:
            events.popleft()

        if len(events) >= MAX_JOBS_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="Too many downloads from this address. Try again later.",
            )
        events.append(now)


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        for key, value in changes.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = time.time()


def public_job(job: Job) -> dict[str, Any]:
    payload = asdict(job)
    payload.pop("file_path", None)
    payload.pop("token", None)

    if job.status == "complete":
        payload["download_url"] = f"/api/jobs/{job.id}/file?token={job.token}"
        payload["expires_in"] = max(
            0,
            round(
                COMPLETED_JOB_TTL_SECONDS - (time.time() - job.updated_at)
            ),
        )
    else:
        payload["download_url"] = None
        payload["expires_in"] = None

    return payload


def active_job_count() -> int:
    active_statuses = {"queued", "analyzing", "downloading", "processing"}
    with jobs_lock:
        return sum(job.status in active_statuses for job in jobs.values())


def clean_error_message(message: str) -> str:
    cleaned = ANSI_ESCAPE.sub("", message)
    cleaned = cleaned.replace(str(COOKIE_FILE), "[cookie file]")
    cleaned = cleaned.replace(str(DOWNLOAD_ROOT), "[temporary storage]")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.removeprefix("ERROR: ")
    return cleaned[:1000] or "The download failed."


def format_bytes(value: float | int | None) -> str | None:
    if value is None:
        return None
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return None


def format_speed(value: float | int | None) -> str | None:
    formatted = format_bytes(value)
    return f"{formatted}/s" if formatted else None


def quality_label(info: dict[str, Any]) -> str | None:
    requested = info.get("requested_formats") or [info]
    video_streams = [
        stream
        for stream in requested
        if stream and stream.get("vcodec") not in {None, "none"}
    ]
    if not video_streams:
        return None

    heights = [stream.get("height") for stream in video_streams if stream.get("height")]
    frame_rates = [stream.get("fps") for stream in video_streams if stream.get("fps")]

    height = max(heights) if heights else info.get("height")
    fps = max(frame_rates) if frame_rates else info.get("fps")

    if not height:
        return None

    label = f"{int(height)}p"
    if fps:
        rounded_fps = int(round(float(fps)))
        label += str(rounded_fps)
    return label


def find_final_media_file(job_dir: Path) -> Path:
    candidates: list[Path] = []

    for path in job_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if path.name.endswith(".part"):
            continue
        candidates.append(path)

    if not candidates:
        raise RuntimeError("yt-dlp finished, but no completed video file was found.")

    return max(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_collection_name(title: str | None, video_id: str | None = None) -> str:
    name = (title or "YouTube Video").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")

    if not name:
        name = "YouTube Video"
    if name.upper() in WINDOWS_RESERVED_NAMES:
        name = f"{name}_video"

    name = name[:120].rstrip(" .")
    if not name:
        name = f"YouTube Video {video_id or ''}".strip()
    return name


def package_video_as_collection_zip(
    job_dir: Path,
    final_file: Path,
    title: str | None,
    video_id: str | None,
) -> Path:
    folder_name = safe_collection_name(title, video_id)
    archive_path = job_dir / f"{folder_name}.zip"
    archive_name = f"{folder_name}/{final_file.name}"

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(final_file, arcname=archive_name)

    return archive_path


class JobLogger:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.last_warning: str | None = None

    def debug(self, message: str) -> None:
        if not message.startswith("[debug] "):
            self.info(message)

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        warning = clean_error_message(message)
        self.last_warning = warning
        if "cookies" in warning.lower() or "sign in" in warning.lower():
            update_job(
                self.job_id,
                message="YouTube requested authentication; checking configured cookies…",
            )

    def error(self, message: str) -> None:
        return


def download_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        source_url = job.url
        package_as_zip = job.package_as_zip

    job_dir = Path(
        tempfile.mkdtemp(prefix=f"{job_id}-", dir=str(DOWNLOAD_ROOT))
    )
    temp_dir = job_dir / "working"
    temp_dir.mkdir(parents=True, exist_ok=True)

    logger = JobLogger(job_id)

    def progress_hook(data: dict[str, Any]) -> None:
        status = data.get("status")

        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            raw_progress = (downloaded / total * 100) if total else 0.0
            visible_progress = min(90.0, max(1.0, raw_progress * 0.9))
            update_job(
                job_id,
                status="downloading",
                progress=round(visible_progress, 1),
                message="Downloading the best available video and audio streams…",
                speed=format_speed(data.get("speed")),
                eta=data.get("eta"),
            )
        elif status == "finished":
            update_job(
                job_id,
                status="processing",
                progress=93.0,
                message="Merging video and audio with FFmpeg…",
                speed=None,
                eta=None,
            )

    def postprocessor_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "started":
            update_job(
                job_id,
                status="processing",
                progress=95.0,
                message="Finalizing the downloadable file…",
                speed=None,
                eta=None,
            )
        elif status == "finished":
            update_job(job_id, progress=98.0)

    def match_filter(
        info_dict: dict[str, Any],
        *,
        incomplete: bool = False,
    ) -> str | None:
        if incomplete:
            return None

        if info_dict.get("is_live") or info_dict.get("live_status") == "is_live":
            return "Live streams are not supported by this website."

        duration = info_dict.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            hours = MAX_DURATION_SECONDS / 3600
            return f"This video is longer than the configured {hours:g}-hour limit."

        return None

    output_template = str(job_dir / "%(title).180B [%(id)s].%(ext)s")

    options: dict[str, Any] = {
        "format": (
            "(bv*[height<=1080][fps<=60][ext=mp4]+ba[ext=m4a])/"
            "(bv*[height<=1080][fps<=60]+ba)/"
            "b[height<=1080][fps<=60]"
        ),
        "format_sort": ["res:1080", "fps:60", "codec:h264"],
        "outtmpl": output_template,
        "paths": {
            "home": str(job_dir),
            "temp": str(temp_dir),
        },
        "merge_output_format": "mp4",
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "continuedl": True,
        "overwrites": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": False,
        "logger": logger,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "match_filter": match_filter,
    }

    if COOKIE_FILE.exists():
        options["cookiefile"] = str(COOKIE_FILE)

    try:
        update_job(
            job_id,
            status="analyzing",
            progress=2.0,
            message="Checking the video and selecting the best quality up to 1080p60…",
        )

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=True)

        if not info:
            raise RuntimeError("YouTube returned no video information.")

        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise RuntimeError("No downloadable video was found.")
            info = entries[0]

        final_file = find_final_media_file(job_dir)
        title = info.get("title") or final_file.stem
        downloadable_file = final_file
        is_archive = False

        if package_as_zip:
            update_job(
                job_id,
                status="processing",
                progress=99.0,
                message="Creating a collection ZIP with a named folder…",
                speed=None,
                eta=None,
            )
            downloadable_file = package_video_as_collection_zip(
                job_dir=job_dir,
                final_file=final_file,
                title=title,
                video_id=info.get("id"),
            )
            is_archive = True

        stat = downloadable_file.stat()

        update_job(
            job_id,
            status="complete",
            progress=100.0,
            message=(
                "Collection ZIP ready to download."
                if is_archive
                else "Video ready to download."
            ),
            title=title,
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            quality=quality_label(info),
            speed=None,
            eta=None,
            filename=downloadable_file.name,
            file_size=stat.st_size,
            file_path=str(downloadable_file),
            is_archive=is_archive,
            error=None,
        )

    except DownloadError as exc:
        update_job(
            job_id,
            status="error",
            progress=0.0,
            message="The download failed.",
            speed=None,
            eta=None,
            error=clean_error_message(str(exc)),
        )
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            progress=0.0,
            message="The download failed.",
            speed=None,
            eta=None,
            error=clean_error_message(str(exc)),
        )
        shutil.rmtree(job_dir, ignore_errors=True)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        remove_ids: list[str] = []

        with jobs_lock:
            for job_id, job in jobs.items():
                age = now - job.updated_at

                if job.status == "complete" and age > COMPLETED_JOB_TTL_SECONDS:
                    remove_ids.append(job_id)
                elif job.status == "error" and age > FAILED_JOB_TTL_SECONDS:
                    remove_ids.append(job_id)
                elif (
                    job.status in {"queued", "analyzing", "downloading", "processing"}
                    and age > ACTIVE_JOB_TIMEOUT_SECONDS
                ):
                    job.status = "error"
                    job.error = "The job timed out."
                    job.message = "The download timed out."
                    job.updated_at = now

            removed_jobs = [jobs.pop(job_id) for job_id in remove_ids]

        for job in removed_jobs:
            if job.file_path:
                shutil.rmtree(Path(job.file_path).parent, ignore_errors=True)

        with rate_limit_lock:
            cutoff = now - 3600
            empty_keys: list[str] = []
            for ip, events in rate_events.items():
                while events and events[0] < cutoff:
                    events.popleft()
                if not events:
                    empty_keys.append(ip)
            for ip in empty_keys:
                rate_events.pop(ip, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    decode_cookie_file()
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Railway YouTube Downloader",
    description="Downloads the best available YouTube quality up to 1080p and 60 FPS.",
    version="1.1.0",
    lifespan=lifespan,
)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "auth_required": bool(APP_PASSWORD),
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
    }


@app.post(
    "/api/jobs",
    dependencies=[Depends(require_auth)],
    status_code=202,
)
async def create_job(payload: CreateJobRequest, request: Request) -> dict[str, Any]:
    url = normalize_and_validate_youtube_url(payload.url)
    enforce_rate_limit(client_ip(request))

    if active_job_count() >= MAX_QUEUED_JOBS:
        raise HTTPException(
            status_code=503,
            detail="The download queue is full. Try again after another job finishes.",
        )

    job_id = secrets.token_urlsafe(12)
    job = Job(
        id=job_id,
        url=url,
        token=secrets.token_urlsafe(24),
        package_as_zip=payload.package_as_zip,
    )

    with jobs_lock:
        jobs[job_id] = job

    executor.submit(download_job, job_id)
    return public_job(job)


@app.get(
    "/api/jobs/{job_id}",
    dependencies=[Depends(require_auth)],
)
async def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail="This download job no longer exists.",
            )
        return public_job(job)


@app.get("/api/jobs/{job_id}/file")
async def download_file(job_id: str, token: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail="This download has expired.",
            )

        if not hmac.compare_digest(token, job.token):
            raise HTTPException(status_code=403, detail="Invalid download token.")

        if job.status != "complete" or not job.file_path:
            raise HTTPException(status_code=409, detail="The file is not ready yet.")

        file_path = Path(job.file_path)
        if not file_path.is_file():
            raise HTTPException(
                status_code=404,
                detail="The completed file is no longer available.",
            )

        job.updated_at = time.time()
        filename = job.filename or file_path.name

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type,
    )
