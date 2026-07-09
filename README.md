# FrameGrab — Railway YouTube Downloader

A Dockerized FastAPI website with Deno, automatic YouTube PO-token generation, that downloads the highest available YouTube
quality at or below **1080p and 60 FPS**.

It does not upscale resolution or generate frames. Examples:

- 2160p60 source → 1080p60
- 1080p30 source → 1080p30
- 720p60 source → 720p60
- 480p30 source → 480p30

The app prefers MP4/H.264 video plus M4A audio when YouTube offers that
combination, then falls back to the best compatible source formats. FFmpeg
merges separate video and audio streams.

By default, the website packages the result as a collection-ready ZIP. The ZIP
contains a top-level folder named after the video, with the downloaded video
inside that folder. Neither the direct file nor the ZIP contents include the YouTube video ID. The option can be disabled for a direct video download,
and the browser remembers the selected setting with local storage.

## Included

- Railway-compatible `Dockerfile`
- `railway.json` health check configuration
- Responsive one-page interface
- Default-on collection ZIP option remembered by the browser
- Clean filenames with no trailing YouTube video ID
- ZIP layout: `Video Name.zip` → `Video Name/Video Name.mp4`
- Download progress polling
- YouTube-only URL validation
- Single-video mode; playlists are disabled
- Automatic temporary-file cleanup
- Optional password protection
- Per-IP rate limiting
- Configurable queue, concurrency, duration, and expiry limits
- Optional per-download `cookies.txt` upload for YouTube bot checks
- Optional server-wide Netscape cookies through `YOUTUBE_COOKIES_B64`
- AdSense ownership code and `/ads.txt` for publisher `pub-4820082513371524`
- Cache-busted CSS/JavaScript so Railway deployments do not mix interface versions
- Compact custom Collection ZIP switch with no native browser checkbox
- Three optional responsive AdSense placements: header, middle, and footer

## Deploy to Railway

1. Create a new GitHub repository.
2. Upload every file from this project to the repository root.
3. In Railway, choose **New Project → Deploy from GitHub repo**.
4. Select the repository.
5. Open the Railway service's **Variables** section and add:

   ```text
   APP_PASSWORD=use-a-long-random-password
   ```

6. Open **Settings → Networking** and generate a public domain.
7. Keep the service at **one replica**. Jobs are intentionally stored in
   memory and on that replica's temporary filesystem.

Railway automatically detects the root `Dockerfile`. The included
`railway.json` checks `/health` during deployment.

## Recommended Railway variables

| Variable | Default | Purpose |
|---|---:|---|
| `APP_PASSWORD` | empty | Protects the downloader when set |
| `MAX_CONCURRENT_DOWNLOADS` | `1` | Simultaneous yt-dlp workers |
| `MAX_QUEUED_JOBS` | `10` | Queued and active jobs accepted |
| `MAX_JOBS_PER_HOUR` | `20` | Starts allowed per IP per hour |
| `MAX_DURATION_SECONDS` | `14400` | Maximum video duration |
| `COMPLETED_JOB_TTL_SECONDS` | `1800` | Finished-file lifetime |
| `FAILED_JOB_TTL_SECONDS` | `900` | Failed-job status lifetime |
| `ACTIVE_JOB_TIMEOUT_SECONDS` | `21600` | Maximum active-job age |
| `YOUTUBE_COOKIES_B64` | empty | Optional server-wide base64 cookies file |
| `MAX_COOKIE_FILE_BYTES` | `2097152` | Per-download cookies upload size limit |
| `ADSENSE_CLIENT_ID` | `ca-pub-4820082513371524` | AdSense publisher client ID |
| `ADSENSE_HEADER_SLOT` | empty | Responsive ad unit below the header |
| `ADSENSE_MIDDLE_SLOT` | empty | Responsive ad unit below the downloader |
| `ADSENSE_FOOTER_SLOT` | empty | Responsive ad unit above the footer |

## YouTube cookies and bot checks

Cloud-hosting IP addresses are sometimes challenged by YouTube. The website
now has a **YouTube authentication** panel directly under the Collection ZIP
option. Export a fresh Netscape-format `cookies.txt` file from a browser where
YouTube plays normally and attach it to the download. The upload is used only
for that job and deleted immediately after the job succeeds or fails.

Railway cannot use `--cookies-from-browser` to read Chrome or Firefox on your
personal computer because Railway runs on a separate remote machine.

For a persistent server-wide cookie file, convert `cookies.txt` to base64:

Convert it to one base64 line locally:

### Windows PowerShell

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes(".\cookies.txt")) |
    Set-Clipboard
```

### Linux or macOS

```bash
base64 -w 0 cookies.txt
```

Add the resulting value to Railway as:

```text
YOUTUBE_COOKIES_B64=PASTE_THE_BASE64_VALUE
```

Treat this value like a password. Do not commit it to GitHub. Fresh browser
exports may occasionally be required because YouTube rotates or invalidates
account cookies.

## AdSense verification and ad placements

The AdSense loader and account meta tag for `ca-pub-4820082513371524` are
included inside the page `<head>`. The site also serves this authorized-seller
record at `/ads.txt`:

```text
google.com, pub-4820082513371524, DIRECT, f08c47fec0942fa0
```

The layout contains three responsive manual ad locations. Create responsive
**Display ad** units in AdSense, then copy only the numeric `data-ad-slot` value
from each generated unit into Railway:

```text
ADSENSE_HEADER_SLOT=1234567890
ADSENSE_MIDDLE_SLOT=2345678901
ADSENSE_FOOTER_SLOT=3456789012
```

A location remains completely hidden when its slot variable is blank. If an ad
unit reports itself as unfilled, the wrapper is hidden automatically. You can
also enable Auto ads in AdSense; the verification loader is already installed.
AdSense review and ad delivery are controlled by Google and are not guaranteed
by the code alone.

## Run locally with Docker

```bash
docker build -t framegrab .
docker run --rm -p 8080:8080 -e APP_PASSWORD="change-me" framegrab
```

Open `http://localhost:8080`.

## Run locally without Docker

You need Python 3.10 or newer, FFmpeg, and Deno 2.0 or newer installed.

```bash
python -m venv .venv
```

### Windows

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

### Linux or macOS

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

## Storage behavior

Downloads are written to the container's temporary filesystem and removed
after their configured expiry. A Railway Volume is not needed unless you
change the app to preserve files permanently.

Because the queue and job records live in memory, do not run multiple Uvicorn
workers or multiple Railway replicas without first replacing the in-memory
job store with a shared database/queue.

## Important

Use the downloader only for videos you own or have permission to download.
You are responsible for complying with copyright law and YouTube's terms.
