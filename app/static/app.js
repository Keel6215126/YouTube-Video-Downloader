const form = document.querySelector("#download-form");
const urlInput = document.querySelector("#youtube-url");
const passwordGroup = document.querySelector("#password-group");
const passwordInput = document.querySelector("#app-password");
const pasteButton = document.querySelector("#paste-button");
const collectionZipToggle = document.querySelector("#collection-zip-toggle");
const collectionPreview = document.querySelector("#collection-preview");
const cookieDetails = document.querySelector("#cookie-details");
const cookieSummaryText = document.querySelector("#cookie-summary-text");
const cookieFileInput = document.querySelector("#youtube-cookies-file");
const cookieFileLabel = document.querySelector("#cookie-file-label");
const clearCookieFileButton = document.querySelector("#clear-cookie-file");
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
const poTokenStatus = document.querySelector("#po-token-status");
const poTokenStatusText = document.querySelector("#po-token-status-text");

let authRequired = false;
let packageAsZip = true;
let currentJobId = null;
let pollTimer = null;
let maxCookieFileBytes = 2 * 1024 * 1024;
let serverCookiesConfigured = false;
let automaticProtectionSummary = "Automatic protection first; cookies only as a fallback";
let cookieFragmentCount = 4;

function authHeaders() {
    const headers = {};

    if (authRequired) {
        headers["X-App-Password"] = passwordInput.value;
    }

    return headers;
}

function safeStorageGet(key) {
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
}

function safeStorageSet(key, value) {
    try {
        localStorage.setItem(key, value);
    } catch {
        // Storage can be disabled in private or restricted browser modes.
    }
}

function setCollectionZip(enabled, remember = true) {
    packageAsZip = Boolean(enabled);
    collectionZipToggle.setAttribute("aria-checked", packageAsZip ? "true" : "false");
    collectionPreview.classList.toggle("is-disabled", !packageAsZip);

    if (remember) {
        safeStorageSet("framegrab-package-as-zip", packageAsZip ? "true" : "false");
    }
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

    return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
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
        resetCookieFile();
    }

    if (job.status === "error") {
        const errorMessage = job.error || "The download failed.";
        jobPanel.classList.add("error");
        jobStage.textContent = "Download failed";
        progressBar.style.width = "100%";
        progressPercent.textContent = "Error";
        progressDetail.textContent = errorMessage;
        anotherButton.hidden = false;
        stopPolling();
        setBusy(false);

        if (errorMessage.toLowerCase().includes("cookies.txt")) {
            cookieDetails.open = true;
        }
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

function updateCookieSummary() {
    if (selectedCookieFile()) {
        cookieSummaryText.textContent =
            `cookies.txt selected · fast ${cookieFragmentCount}-part mode`;
        return;
    }

    if (serverCookiesConfigured) {
        cookieSummaryText.textContent =
            `Railway cookies configured · fast ${cookieFragmentCount}-part mode`;
        return;
    }

    cookieSummaryText.textContent = automaticProtectionSummary;
}

function selectedCookieFile() {
    return cookieFileInput.files && cookieFileInput.files.length > 0
        ? cookieFileInput.files[0]
        : null;
}

function resetCookieFile() {
    cookieFileInput.value = "";
    cookieFileLabel.textContent = "Choose cookies.txt";
    clearCookieFileButton.hidden = true;
    updateCookieSummary();
}

async function startDownload(url) {
    setBusy(true);
    resetPanel();
    showPanel();

    const requestBody = new FormData();
    requestBody.append("url", url);
    requestBody.append("package_as_zip", packageAsZip ? "true" : "false");

    const cookiesFile = selectedCookieFile();
    if (cookiesFile) {
        requestBody.append("cookies_file", cookiesFile, cookiesFile.name);
    }

    const response = await fetch("/api/jobs", {
        method: "POST",
        headers: authHeaders(),
        body: requestBody,
    });

    if (!response.ok) {
        throw new Error(await apiError(response));
    }

    const job = await response.json();
    currentJobId = job.id;

    if (authRequired) {
        safeStorageSet("framegrab-password", passwordInput.value);
    }

    renderJob(job);
    await pollJob();
}

function mountAd(placementId, mountId, clientId, slotId) {
    const placement = document.querySelector(`#${placementId}`);
    const mount = document.querySelector(`#${mountId}`);

    if (!placement || !mount || !clientId || !slotId) {
        return;
    }

    const ad = document.createElement("ins");
    ad.className = "adsbygoogle";
    ad.style.display = "block";
    ad.dataset.adClient = clientId;
    ad.dataset.adSlot = slotId;
    ad.dataset.adFormat = "auto";
    ad.dataset.fullWidthResponsive = "true";

    const observer = new MutationObserver(() => {
        if (ad.dataset.adStatus === "unfilled") {
            placement.classList.add("is-unfilled");
        }
    });
    observer.observe(ad, { attributes: true, attributeFilter: ["data-ad-status"] });

    mount.replaceChildren(ad);
    placement.hidden = false;

    try {
        window.adsbygoogle = window.adsbygoogle || [];
        window.adsbygoogle.push({});
    } catch {
        placement.hidden = true;
    }
}

function initializeAds(adsense) {
    const clientId = adsense?.client_id || "";
    const slots = adsense?.slots || {};

    mountAd("ad-placement-header", "ad-header", clientId, slots.header || "");
    mountAd("ad-placement-middle", "ad-middle", clientId, slots.middle || "");
    mountAd("ad-placement-footer", "ad-footer", clientId, slots.footer || "");
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

    const cookiesFile = selectedCookieFile();
    if (cookiesFile && cookiesFile.size > maxCookieFileBytes) {
        const limitMb = maxCookieFileBytes / (1024 * 1024);
        cookieDetails.open = true;
        cookieFileLabel.textContent = `File exceeds ${limitMb.toFixed(1)} MB`;
        return;
    }

    try {
        await startDownload(url);
    } catch (error) {
        jobPanel.classList.add("error");
        jobStage.textContent = "Could not start";
        jobTitle.textContent = "The download was not created.";
        progressBar.style.width = "100%";
        progressPercent.textContent = "Error";
        progressDetail.textContent = error.message;
        anotherButton.hidden = false;
        setBusy(false);

        if ((error.message || "").toLowerCase().includes("cookies")) {
            cookieDetails.open = true;
        }
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

collectionZipToggle.addEventListener("click", () => {
    setCollectionZip(!packageAsZip);
});

collectionZipToggle.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setCollectionZip(!packageAsZip);
    }
});

cookieFileInput.addEventListener("change", () => {
    const file = selectedCookieFile();
    cookieFileLabel.textContent = file ? file.name : "Choose cookies.txt";
    clearCookieFileButton.hidden = !file;
    updateCookieSummary();
});

clearCookieFileButton.addEventListener("click", resetCookieFile);

anotherButton.addEventListener("click", () => {
    stopPolling();
    currentJobId = null;
    jobPanel.hidden = true;
    resetPanel();
    resetCookieFile();
    urlInput.value = "";
    urlInput.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
});

async function initialize() {
    const savedZipPreference = safeStorageGet("framegrab-package-as-zip");
    setCollectionZip(
        savedZipPreference === null ? true : savedZipPreference === "true",
        false,
    );

    try {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (!response.ok) {
            return;
        }

        const config = await response.json();
        authRequired = Boolean(config.auth_required);
        passwordGroup.hidden = !authRequired;

        const poTokenProvider = config.po_token_provider || {};
        if (poTokenProvider.ready) {
            poTokenStatus.className = "protection-status ready";
            poTokenStatusText.textContent =
                "Automatic PO-token protection is active on this server.";
            automaticProtectionSummary =
                "Automatic protection active; cookies only as a fallback";
        } else if (poTokenProvider.enabled) {
            poTokenStatus.className = "protection-status unavailable";
            poTokenStatusText.textContent =
                "Automatic protection did not start. Cookies may be required.";
            automaticProtectionSummary =
                "Automatic protection unavailable; cookies may be required";
        } else {
            poTokenStatus.className = "protection-status unavailable";
            poTokenStatusText.textContent =
                "Automatic PO-token protection is disabled.";
            automaticProtectionSummary =
                "Automatic protection disabled; cookies may be required";
        }
        maxCookieFileBytes = Number(config.max_cookie_file_bytes) || maxCookieFileBytes;
        serverCookiesConfigured = Boolean(config.server_cookies_configured);
        cookieFragmentCount =
            Number(config.download_modes?.cookies?.concurrent_fragments) || cookieFragmentCount;

        if (authRequired) {
            passwordInput.value = safeStorageGet("framegrab-password") || "";
        }

        updateCookieSummary();
        initializeAds(config.adsense);
    } catch {
        // The form will surface server-side errors when submitted.
    }
}

initialize();
