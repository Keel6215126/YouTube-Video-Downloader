const form = document.querySelector("#download-form");
const urlInput = document.querySelector("#youtube-url");
const passwordGroup = document.querySelector("#password-group");
const passwordInput = document.querySelector("#app-password");
const pasteButton = document.querySelector("#paste-button");
const collectionZipInput = document.querySelector("#collection-zip");
const submitButton = document.querySelector("#submit-button");
const submitLabel = submitButton.querySelector(".button-label");

const jobPanel = document.querySelector("#job-panel");
const jobStage = document.querySelector("#job-stage");
const jobTitle = document.querySelector("#job-title");
const qualityPill = document.querySelector("#quality-pill");
const progressBar = document.querySelector("#progress-bar");
const progressPercent = document.querySelector("#progress-percent");
const progressDetail = document.querySelector("#progress-detail");
const videoMeta = document.querySelector("#video-meta");
const uploaderValue = document.querySelector("#uploader-value");
const durationValue = document.querySelector("#duration-value");
const sizeValue = document.querySelector("#size-value");
const downloadLink = document.querySelector("#download-link");
const downloadLinkLabel = document.querySelector("#download-link-label");
const anotherButton = document.querySelector("#another-button");

let authRequired = false;
let currentJobId = null;
let pollTimer = null;

function authHeaders() {
    const headers = {
        "Content-Type": "application/json",
    };

    if (authRequired) {
        headers["X-App-Password"] = passwordInput.value;
    }

    return headers;
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) {
        return "—";
    }

    const total = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainingSeconds = total % 60;

    if (hours > 0) {
        return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
    }

    return `${minutes}:${String(remainingSeconds).padStart(2, "0")}`;
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) {
        return "—";
    }

    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unitIndex = 0;

    while (value >= 1024 && unitIndex < units.length - 1) {
        value /= 1024;
        unitIndex += 1;
    }

    const decimals = unitIndex === 0 ? 0 : 1;
    return `${value.toFixed(decimals)} ${units[unitIndex]}`;
}

function titleCaseStatus(status) {
    return status
        .replaceAll("_", " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function setBusy(isBusy) {
    submitButton.disabled = isBusy;
    submitLabel.textContent = isBusy ? "Starting…" : "Prepare download";
}

function showPanel() {
    jobPanel.hidden = false;
    jobPanel.scrollIntoView({ behavior: "smooth", block: "center" });
}

function resetPanel() {
    jobPanel.classList.remove("error");
    jobStage.textContent = "Queued";
    jobTitle.textContent = "Preparing your video…";
    progressBar.style.width = "0%";
    progressPercent.textContent = "0%";
    progressDetail.textContent = "Waiting for a download slot…";
    qualityPill.hidden = true;
    videoMeta.hidden = true;
    downloadLink.hidden = true;
    anotherButton.hidden = true;
}

function renderJob(job) {
    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));

    jobStage.textContent = titleCaseStatus(job.status);
    jobTitle.textContent = job.title || "Preparing your video…";
    progressBar.style.width = `${progress}%`;
    progressPercent.textContent = `${Math.round(progress)}%`;

    const detailParts = [job.message];
    if (job.speed) {
        detailParts.push(job.speed);
    }
    if (Number.isFinite(job.eta)) {
        detailParts.push(`${job.eta}s remaining`);
    }
    progressDetail.textContent = detailParts.filter(Boolean).join(" · ");

    if (job.quality) {
        qualityPill.textContent = job.quality;
        qualityPill.hidden = false;
    }

    if (job.status === "complete") {
        videoMeta.hidden = false;
        uploaderValue.textContent = job.uploader || "Unknown";
        durationValue.textContent = formatDuration(job.duration);
        sizeValue.textContent = formatBytes(job.file_size);

        downloadLink.href = job.download_url;
        downloadLink.setAttribute("download", job.filename || "");
        downloadLinkLabel.textContent = job.is_archive
            ? "Download collection ZIP"
            : "Download finished video";
        downloadLink.hidden = false;
        anotherButton.hidden = false;
        jobPanel.classList.remove("error");
        stopPolling();
        setBusy(false);
    }

    if (job.status === "error") {
        jobPanel.classList.add("error");
        jobStage.textContent = "Download failed";
        progressBar.style.width = "100%";
        progressPercent.textContent = "Error";
        progressDetail.textContent = job.error || "The download failed.";
        anotherButton.hidden = false;
        stopPolling();
        setBusy(false);
    }
}

function stopPolling() {
    if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
    }
}

async function apiError(response) {
    try {
        const payload = await response.json();
        return payload.detail || `Request failed with status ${response.status}.`;
    } catch {
        return `Request failed with status ${response.status}.`;
    }
}

async function pollJob() {
    if (!currentJobId) {
        return;
    }

    try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}`, {
            headers: authHeaders(),
            cache: "no-store",
        });

        if (!response.ok) {
            throw new Error(await apiError(response));
        }

        const job = await response.json();
        renderJob(job);

        if (!["complete", "error"].includes(job.status)) {
            pollTimer = window.setTimeout(pollJob, 900);
        }
    } catch (error) {
        jobPanel.classList.add("error");
        jobStage.textContent = "Connection problem";
        progressDetail.textContent = error.message;
        anotherButton.hidden = false;
        setBusy(false);
        stopPolling();
    }
}

async function startDownload(url, packageAsZip) {
    setBusy(true);
    resetPanel();
    showPanel();

    const response = await fetch("/api/jobs", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
            url,
            package_as_zip: packageAsZip,
        }),
    });

    if (!response.ok) {
        throw new Error(await apiError(response));
    }

    const job = await response.json();
    currentJobId = job.id;

    if (authRequired) {
        localStorage.setItem("framegrab-password", passwordInput.value);
    }
    localStorage.setItem(
        "framegrab-package-as-zip",
        packageAsZip ? "true" : "false",
    );

    renderJob(job);
    await pollJob();
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const url = urlInput.value.trim();
    if (!url) {
        urlInput.focus();
        return;
    }

    if (authRequired && !passwordInput.value) {
        passwordInput.focus();
        return;
    }

    try {
        await startDownload(url, collectionZipInput.checked);
    } catch (error) {
        jobPanel.classList.add("error");
        jobStage.textContent = "Could not start";
        jobTitle.textContent = "The download was not created.";
        progressBar.style.width = "100%";
        progressPercent.textContent = "Error";
        progressDetail.textContent = error.message;
        anotherButton.hidden = false;
        setBusy(false);
    }
});

pasteButton.addEventListener("click", async () => {
    try {
        const text = await navigator.clipboard.readText();
        urlInput.value = text.trim();
        urlInput.focus();
    } catch {
        urlInput.focus();
    }
});

anotherButton.addEventListener("click", () => {
    stopPolling();
    currentJobId = null;
    jobPanel.hidden = true;
    resetPanel();
    urlInput.value = "";
    urlInput.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
});

async function initialize() {
    try {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (!response.ok) {
            return;
        }

        const config = await response.json();
        authRequired = Boolean(config.auth_required);
        passwordGroup.hidden = !authRequired;

        const savedZipPreference = localStorage.getItem("framegrab-package-as-zip");
        collectionZipInput.checked = savedZipPreference === null
            ? true
            : savedZipPreference === "true";

        if (authRequired) {
            passwordInput.value = localStorage.getItem("framegrab-password") || "";
        }
    } catch {
        // The form will still surface any server-side error when submitted.
    }
}

initialize();
