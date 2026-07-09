from __future__ import annotations

import asyncio
import base64
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
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
from urllib.request import urlopen

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
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")))
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
MAX_COOKIE_FILE_BYTES = max(
    1024, int(os.getenv("MAX_COOKIE_FILE_BYTES", str(2 * 1024 * 1024)))
)
COOKIE_FILE = DOWNLOAD_ROOT / "youtube-cookies.txt"
ADSENSE_CLIENT_ID = os.getenv(
    "ADSENSE_CLIENT_ID", "ca-pub-4820082513371524"
).strip()
ADSENSE_HEADER_SLOT = os.getenv("ADSENSE_HEADER_SLOT", "").strip()
ADSENSE_MIDDLE_SLOT = os.getenv("ADSENSE_MIDDLE_SLOT", "").strip()
ADSENSE_FOOTER_SLOT = os.getenv("ADSENSE_FOOTER_SLOT", "").strip()

PO_TOKEN_PROVIDER_ENABLED = os.getenv(
    "ENABLE_PO_TOKEN_PROVIDER", "true"
).strip().lower() not in {"0", "false", "no", "off"}
POT_PROVIDER_URL = os.getenv(
    "POT_PROVIDER_URL", "http://127.0.0.1:4416"
).strip().rstrip("/")
POT_PROVIDER_HOME = Path(
    os.getenv("POT_PROVIDER_HOME", "/opt/bgutil-provider")
).resolve()
POT_PROVIDER_STARTUP_TIMEOUT_SECONDS = max(
    3.0, float(os.getenv("POT_PROVIDER_STARTUP_TIMEOUT_SECONDS", "30"))
)
YOUTUBE_SLEEP_REQUESTS_SECONDS = max(
    0.0, float(os.getenv("YOUTUBE_SLEEP_REQUESTS_SECONDS", "1"))
)
YOUTUBE_JOB_SPACING_SECONDS = max(
    0.0, float(os.getenv("YOUTUBE_JOB_SPACING_SECONDS", "6"))
)

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
    cookie_file_path: str | None = None
    package_as_zip: bool = True
    is_archive: bool = False
    auth_mode: str | None = None
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

pot_provider_process: subprocess.Popen[bytes] | None = None
pot_provider_ready = False
pot_provider_error: str | None = None
youtube_spacing_lock = threading.Lock()
last_youtube_job_started_at = 0.0


def pot_provider_ping() -> bool:
    try:
        with urlopen(f"{POT_PROVIDER_URL}/ping", timeout=2.5) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def start_pot_provider() -> None:
    global pot_provider_process, pot_provider_ready, pot_provider_error

    pot_provider_ready = False
    pot_provider_error = None

    if not PO_TOKEN_PROVIDER_ENABLED:
        pot_provider_error = "Disabled by ENABLE_PO_TOKEN_PROVIDER."
        return

    if pot_provider_ping():
        pot_provider_ready = True
        return

    parsed_url = urlparse(POT_PROVIDER_URL)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if (parsed_url.hostname or "").lower() not in local_hosts:
        pot_provider_error = (
            f"The external PO-token provider at {POT_PROVIDER_URL} did not answer /ping."
        )
        return

    source_file = POT_PROVIDER_HOME / "src/main.ts"
    node_modules = POT_PROVIDER_HOME / "node_modules"
    if not source_file.is_file() or not node_modules.is_dir():
        pot_provider_error = (
            f"PO-token provider files were not found under {POT_PROVIDER_HOME}."
        )
        return

    port = parsed_url.port or 4416
    log_path = DOWNLOAD_ROOT / "pot-provider.log"
    command = [
        "deno",
        "run",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={node_modules}",
        f"--allow-read={node_modules}",
        str(source_file),
        "--port",
        str(port),
    ]

    try:
        with log_path.open("ab", buffering=0) as log_file:
            pot_provider_process = subprocess.Popen(
                command,
                cwd=POT_PROVIDER_HOME,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as exc:
        pot_provider_error = f"Could not start the PO-token provider: {exc}"
        return

    deadline = time.monotonic() + POT_PROVIDER_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pot_provider_process.poll() is not None:
            pot_provider_error = (
                f"The PO-token provider exited with code {pot_provider_process.returncode}."
            )
            return
        if pot_provider_ping():
            pot_provider_ready = True
            return
        time.sleep(0.25)

    pot_provider_error = "The PO-token provider did not become ready before timeout."


def stop_pot_provider() -> None:
    global pot_provider_process, pot_provider_ready

    process = pot_provider_process
    pot_provider_process = None
    pot_provider_ready = False

    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for_youtube_job_spacing(job_id: str) -> None:
    global last_youtube_job_started_at

    if YOUTUBE_JOB_SPACING_SECONDS <= 0:
        return

    with youtube_spacing_lock:
        elapsed = time.monotonic() - last_youtube_job_started_at
        delay = max(0.0, YOUTUBE_JOB_SPACING_SECONDS - elapsed)
        if delay > 0 and last_youtube_job_started_at > 0:
            update_job(
                job_id,
                status="queued",
                progress=1.0,
                message=f"Waiting {delay:.1f}s before contacting YouTube…",
            )
            time.sleep(delay)
        last_youtube_job_started_at = time.monotonic()


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


def parse_boolean(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def validate_cookie_upload(filename: str | None, data: bytes) -> bytes:
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded cookies.txt file is empty.")
    if len(data) > MAX_COOKIE_FILE_BYTES:
        limit_mb = MAX_COOKIE_FILE_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"The cookies.txt file is larger than the {limit_mb:g} MB limit.",
        )
    if b"\x00" in data:
        raise HTTPException(status_code=400, detail="The cookies file must be plain text.")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="The cookies file must be UTF-8 plain text in Netscape cookies.txt format.",
        ) from exc

    first_lines = "\n".join(text.splitlines()[:8])
    if "Netscape HTTP Cookie File" not in first_lines:
        raise HTTPException(
            status_code=400,
            detail=(
                "That file is not a Netscape-format cookies.txt export. "
                "Export cookies.txt from a browser where YouTube is already working."
            ),
        )

    lowered = text.lower()
    if "youtube.com" not in lowered and "google.com" not in lowered:
        raise HTTPException(
            status_code=400,
            detail="The cookies.txt file does not appear to contain YouTube or Google cookies.",
        )

    if filename and not filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="Upload a .txt cookies file.")

    return data


def authentication_error_message(message: str) -> str:
    cleaned = clean_error_message(message)
    lowered = cleaned.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        if pot_provider_ready:
            return (
                "Automatic PO-token protection was tried, but YouTube still challenged "
                "this Railway server. Open YouTube authentication, attach a fresh "
                "Netscape-format cookies.txt export, and retry."
            )
        return (
            "The automatic PO-token provider is unavailable, and YouTube challenged "
            "this Railway server. Open YouTube authentication, attach a fresh "
            "Netscape-format cookies.txt export, and retry."
        )
    return cleaned


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
    payload.pop("cookie_file_path", None)
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
        uploaded_cookie_path = job.cookie_file_path

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

    output_template = str(job_dir / "%(title).180B.%(ext)s")

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
        "concurrent_fragment_downloads": 1,
        "sleep_interval_requests": YOUTUBE_SLEEP_REQUESTS_SECONDS,
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

    cookie_path: Path | None = None
    if uploaded_cookie_path:
        candidate = Path(uploaded_cookie_path)
        if candidate.is_file():
            cookie_path = candidate
    if cookie_path is None and COOKIE_FILE.exists():
        cookie_path = COOKIE_FILE
    if cookie_path is not None:
        options["cookiefile"] = str(cookie_path)
        auth_mode = "cookies"
    elif PO_TOKEN_PROVIDER_ENABLED and pot_provider_ready:
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["mweb"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": [POT_PROVIDER_URL],
            },
        }
        auth_mode = "po_token"
    else:
        auth_mode = "guest"

    update_job(job_id, auth_mode=auth_mode)

    try:
        wait_for_youtube_job_spacing(job_id)

        if auth_mode == "po_token":
            analyzing_message = (
                "Generating automatic YouTube proof token and selecting up to 1080p60…"
            )
        elif auth_mode == "cookies":
            analyzing_message = (
                "Using YouTube authentication and selecting up to 1080p60…"
            )
        else:
            analyzing_message = (
                "Checking YouTube as a guest and selecting up to 1080p60…"
            )

        update_job(
            job_id,
            status="analyzing",
            progress=2.0,
            message=analyzing_message,
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
            error=authentication_error_message(str(exc)),
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
            error=authentication_error_message(str(exc)),
        )
        shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        if uploaded_cookie_path:
            Path(uploaded_cookie_path).unlink(missing_ok=True)
            update_job(job_id, cookie_file_path=None)


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
            if job.cookie_file_path:
                Path(job.cookie_file_path).unlink(missing_ok=True)

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
    await asyncio.to_thread(start_pot_provider)
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(stop_pot_provider)
        executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Railway YouTube Downloader",
    description="Downloads the best available YouTube quality up to 1080p and 60 FPS.",
    version="1.5.0",
    lifespan=lifespan,
)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.middleware("http")
async def prevent_stale_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/ads.txt", include_in_schema=False)
async def ads_txt() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "ads.txt",
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if (not PO_TOKEN_PROVIDER_ENABLED or pot_provider_ready) else "degraded",
        "po_token_provider": {
            "enabled": PO_TOKEN_PROVIDER_ENABLED,
            "ready": pot_provider_ready,
            "error": pot_provider_error,
        },
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "auth_required": bool(APP_PASSWORD),
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        "server_cookies_configured": COOKIE_FILE.exists(),
        "max_cookie_file_bytes": MAX_COOKIE_FILE_BYTES,
        "po_token_provider": {
            "enabled": PO_TOKEN_PROVIDER_ENABLED,
            "ready": pot_provider_ready,
            "error": pot_provider_error,
        },
        "adsense": {
            "client_id": ADSENSE_CLIENT_ID,
            "slots": {
                "header": ADSENSE_HEADER_SLOT,
                "middle": ADSENSE_MIDDLE_SLOT,
                "footer": ADSENSE_FOOTER_SLOT,
            },
        },
    }


@app.post(
    "/api/jobs",
    dependencies=[Depends(require_auth)],
    status_code=202,
)
async def create_job(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    uploaded_cookie_data: bytes | None = None
    uploaded_cookie_name: str | None = None

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_url = str(form.get("url") or "")
        package_as_zip = parse_boolean(form.get("package_as_zip"), default=True)
        cookies_file = form.get("cookies_file")

        if cookies_file is not None and hasattr(cookies_file, "read"):
            uploaded_cookie_name = getattr(cookies_file, "filename", None)
            uploaded_cookie_data = await cookies_file.read(MAX_COOKIE_FILE_BYTES + 1)
            uploaded_cookie_data = validate_cookie_upload(
                uploaded_cookie_name,
                uploaded_cookie_data,
            )
    else:
        try:
            payload = CreateJobRequest.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid download request.") from exc
        raw_url = payload.url
        package_as_zip = payload.package_as_zip

    url = normalize_and_validate_youtube_url(raw_url)
    enforce_rate_limit(client_ip(request))

    if active_job_count() >= MAX_QUEUED_JOBS:
        raise HTTPException(
            status_code=503,
            detail="The download queue is full. Try again after another job finishes.",
        )

    job_id = secrets.token_urlsafe(12)
    cookie_file_path: str | None = None

    if uploaded_cookie_data is not None:
        upload_path = DOWNLOAD_ROOT / f"{job_id}.cookies.txt"
        upload_path.write_bytes(uploaded_cookie_data)
        upload_path.chmod(0o600)
        cookie_file_path = str(upload_path)

    job = Job(
        id=job_id,
        url=url,
        token=secrets.token_urlsafe(24),
        package_as_zip=package_as_zip,
        cookie_file_path=cookie_file_path,
    )

    with jobs_lock:
        jobs[job_id] = job

    try:
        executor.submit(download_job, job_id)
    except Exception:
        if cookie_file_path:
            Path(cookie_file_path).unlink(missing_ok=True)
        with jobs_lock:
            jobs.pop(job_id, None)
        raise

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
