(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesOrdinaryMediaDownload = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    const MAX_BROWSER_BLOB_BYTES = 1024 * 1024 * 1024;
    const DEFAULT_BROWSER_FETCH_IDLE_TIMEOUT_MS = 30000;
    const SAFE_VIDEO_CONTENT_TYPES = Object.freeze([
        "application/octet-stream",
        "binary/octet-stream",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]);
    const STAGE_MESSAGES = Object.freeze({
        preparing: "正在准备下载...",
        fetching: "正在获取文件...",
        saving: "已开始保存",
        completed: "已开始保存",
    });

    function booleanValue(value) {
        return value === true || String(value || "").toLowerCase() === "true";
    }

    function positiveInteger(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }

    function sanitizeFilename(filename) {
        const value = String(filename || "video.mp4")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, " ")
            .trim();
        return value || "video.mp4";
    }

    function isHttpUrl(value) {
        try {
            const url = new URL(String(value || ""));
            return url.protocol === "http:" || url.protocol === "https:";
        } catch (error) {
            return false;
        }
    }

    function normalizedContentType(value) {
        return String(value || "").split(";", 1)[0].trim().toLowerCase();
    }

    function contentTypeAllowsOrdinaryVideo(value) {
        const contentType = normalizedContentType(value);
        return SAFE_VIDEO_CONTENT_TYPES.includes(contentType)
            || contentType.startsWith("video/");
    }

    function isOrdinaryMediaItem(item) {
        return Boolean(
            item
            && typeof item === "object"
            && String(item.type || item.media_type || "").toLowerCase() === "video"
            && isHttpUrl(item.url || item.video_url)
            && booleanValue(item.browser_direct_fetch_safe)
            && !booleanValue(item.requires_merge)
            && !booleanValue(item.requires_cookie)
        );
    }

    function browserFetchOptions(signal) {
        return {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
            referrerPolicy: "no-referrer",
            signal,
        };
    }

    function downloadError(errorCode, message) {
        const error = new Error(message);
        error.errorCode = errorCode;
        return error;
    }

    function createBrowserFetchGuard(options) {
        const settings = options && typeof options === "object" ? options : {};
        const externalSignal = settings.signal;
        const idleTimeoutMs = positiveInteger(settings.idleTimeoutMs)
            || DEFAULT_BROWSER_FETCH_IDLE_TIMEOUT_MS;
        const schedule = settings.schedule || (
            typeof setTimeout === "function" ? setTimeout : null
        );
        const clearSchedule = settings.clearSchedule || (
            typeof clearTimeout === "function" ? clearTimeout : null
        );
        const Controller = typeof AbortController === "function" ? AbortController : null;
        if (!Controller || !schedule || !clearSchedule) {
            return {
                signal: externalSignal,
                touch() {},
                clear() {},
                didTimeout() { return false; },
            };
        }

        const controller = new Controller();
        let timer = null;
        let timedOut = false;
        const externalAbort = () => controller.abort();
        if (externalSignal) {
            if (externalSignal.aborted) {
                externalAbort();
            } else if (typeof externalSignal.addEventListener === "function") {
                externalSignal.addEventListener("abort", externalAbort, {once: true});
            }
        }

        function clearTimer() {
            if (timer !== null) {
                clearSchedule(timer);
                timer = null;
            }
        }

        return {
            signal: controller.signal,
            touch() {
                clearTimer();
                timer = schedule(() => {
                    timedOut = true;
                    controller.abort();
                }, idleTimeoutMs);
            },
            clear() {
                clearTimer();
                if (externalSignal && typeof externalSignal.removeEventListener === "function") {
                    externalSignal.removeEventListener("abort", externalAbort);
                }
            },
            didTimeout() {
                return timedOut;
            },
        };
    }

    function safeErrorMessage(error) {
        const errorCode = String(error && error.errorCode || "");
        if (errorCode === "BROWSER_DOWNLOAD_SIZE_MISMATCH") {
            return "文件接收不完整，请重试下载。";
        }
        if (errorCode === "BROWSER_BLOB_TOO_LARGE") {
            return "文件较大，当前浏览器暂时无法直接保存，请重新解析后再试。";
        }
        if (errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT") {
            return "上游没有返回可保存的视频文件，请重新解析后再试。";
        }
        if (errorCode === "BROWSER_DOWNLOAD_STALLED") {
            return "资源连接长时间没有进展，请重试下载；若仍失败，请重新解析。";
        }
        return "资源获取失败，请重试下载；若仍失败，请重新解析。";
    }

    function defaultBlobHandoff(blob, filename, options) {
        const settings = options && typeof options === "object" ? options : {};
        const documentObject = settings.documentObject || (
            typeof document !== "undefined" ? document : null
        );
        const urlApi = settings.urlApi || (
            typeof URL !== "undefined" ? URL : null
        );
        if (!documentObject || !urlApi || typeof urlApi.createObjectURL !== "function") {
            throw downloadError(
                "BROWSER_DOWNLOAD_API_UNAVAILABLE",
                "当前浏览器无法保存文件。",
            );
        }

        const objectUrl = urlApi.createObjectURL(blob);
        const link = documentObject.createElement("a");
        link.href = objectUrl;
        link.download = filename;
        link.rel = "noopener";
        link.style.display = "none";
        documentObject.body.appendChild(link);
        link.click();
        link.remove();

        const schedule = settings.schedule || (
            typeof setTimeout === "function" ? setTimeout : null
        );
        if (schedule && typeof urlApi.revokeObjectURL === "function") {
            schedule(() => urlApi.revokeObjectURL(objectUrl), 60000);
        }
    }

    async function readBlobWithinLimit(response, maxBytes, contentType, options) {
        const settings = options && typeof options === "object" ? options : {};
        const onChunk = typeof settings.onChunk === "function"
            ? settings.onChunk
            : (() => {});
        if (!response.body || typeof response.body.getReader !== "function") {
            const blob = await response.blob();
            if (positiveInteger(blob && blob.size) > maxBytes) {
                throw downloadError(
                    "BROWSER_BLOB_TOO_LARGE",
                    "当前资源超过浏览器保存上限。",
                );
            }
            return blob;
        }

        const reader = response.body.getReader();
        const chunks = [];
        let receivedBytes = 0;
        try {
            while (true) {
                const {done, value} = await reader.read();
                if (done) {
                    break;
                }
                if (!value || !value.byteLength) {
                    continue;
                }
                receivedBytes += value.byteLength;
                onChunk(receivedBytes);
                if (receivedBytes > maxBytes) {
                    if (typeof reader.cancel === "function") {
                        try {
                            await reader.cancel();
                        } catch (error) {
                            // The bounded read has already failed; cancellation is best-effort.
                        }
                    }
                    throw downloadError(
                        "BROWSER_BLOB_TOO_LARGE",
                        "当前资源超过浏览器保存上限。",
                    );
                }
                chunks.push(value);
            }
        } finally {
            if (typeof reader.releaseLock === "function") {
                reader.releaseLock();
            }
        }
        return new Blob(chunks, {type: contentType || "application/octet-stream"});
    }

    async function download(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        if (!isOrdinaryMediaItem(item)) {
            throw downloadError(
                "BROWSER_DOWNLOAD_NOT_ORDINARY_MEDIA",
                "当前资源不支持浏览器直接保存。",
            );
        }

        const declaredSize = positiveInteger(item.size_bytes || item.sizeBytes);
        const maxBytes = positiveInteger(settings.maxBytes) || MAX_BROWSER_BLOB_BYTES;
        if (declaredSize > maxBytes) {
            throw downloadError(
                "BROWSER_BLOB_TOO_LARGE",
                "当前资源超过浏览器保存上限。",
            );
        }

        const fetchImpl = settings.fetchImpl || (
            typeof fetch === "function" ? fetch.bind(globalThis) : null
        );
        if (!fetchImpl) {
            throw downloadError(
                "BROWSER_FETCH_UNAVAILABLE",
                "当前浏览器不支持资源获取。",
            );
        }

        const onStage = typeof settings.onStage === "function"
            ? settings.onStage
            : (() => {});
        onStage("preparing", STAGE_MESSAGES.preparing);
        onStage("fetching", STAGE_MESSAGES.fetching);

        const fetchGuard = createBrowserFetchGuard(settings);
        let response;
        try {
            fetchGuard.touch();
            response = await fetchImpl(
                String(item.url || item.video_url),
                browserFetchOptions(fetchGuard.signal),
            );
        } catch (error) {
            fetchGuard.clear();
            if (fetchGuard.didTimeout()) {
                throw downloadError(
                    "BROWSER_DOWNLOAD_STALLED",
                    "资源连接长时间没有进展。",
                );
            }
            if (error && error.name === "AbortError") {
                throw error;
            }
            throw downloadError(
                "BROWSER_FETCH_FAILED",
                "浏览器无法获取资源。",
            );
        }
        if (!response || !response.ok) {
            fetchGuard.clear();
            throw downloadError(
                "BROWSER_FETCH_HTTP_ERROR",
                "上游资源请求失败。",
            );
        }

        const contentType = normalizedContentType(
            response.headers && response.headers.get
                ? response.headers.get("Content-Type")
                : "",
        );
        if (!contentTypeAllowsOrdinaryVideo(contentType)) {
            fetchGuard.clear();
            throw downloadError(
                "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT",
                "上游没有返回视频文件。",
            );
        }

        const expectedBytes = positiveInteger(
            response.headers && response.headers.get
                ? response.headers.get("Content-Length")
                : 0,
        );
        if (expectedBytes > maxBytes) {
            fetchGuard.clear();
            throw downloadError(
                "BROWSER_BLOB_TOO_LARGE",
                "当前资源超过浏览器保存上限。",
            );
        }

        let blob;
        try {
            fetchGuard.touch();
            blob = await readBlobWithinLimit(response, maxBytes, contentType, {
                onChunk: () => fetchGuard.touch(),
            });
        } catch (error) {
            if (fetchGuard.didTimeout()) {
                throw downloadError(
                    "BROWSER_DOWNLOAD_STALLED",
                    "资源连接长时间没有进展。",
                );
            }
            if (error && error.name === "AbortError") {
                throw error;
            }
            throw error;
        } finally {
            fetchGuard.clear();
        }
        const receivedBytes = positiveInteger(blob && blob.size);
        if (!receivedBytes) {
            throw downloadError(
                "BROWSER_DOWNLOAD_EMPTY",
                "上游返回了空文件。",
            );
        }
        if (receivedBytes > maxBytes) {
            throw downloadError(
                "BROWSER_BLOB_TOO_LARGE",
                "当前资源超过浏览器保存上限。",
            );
        }
        if (expectedBytes && receivedBytes !== expectedBytes) {
            throw downloadError(
                "BROWSER_DOWNLOAD_SIZE_MISMATCH",
                "浏览器接收的文件大小不完整。",
            );
        }

        const filename = sanitizeFilename(settings.filename || item.filename);
        onStage("saving", STAGE_MESSAGES.saving);
        const handoffBlob = settings.handoffBlob || defaultBlobHandoff;
        await handoffBlob(blob, filename, settings);
        onStage("completed", STAGE_MESSAGES.completed);
        return {
            filename,
            receivedBytes,
            expectedBytes,
            contentType,
        };
    }

    return Object.freeze({
        MAX_BROWSER_BLOB_BYTES,
        DEFAULT_BROWSER_FETCH_IDLE_TIMEOUT_MS,
        STAGE_MESSAGES,
        browserFetchOptions,
        contentTypeAllowsOrdinaryVideo,
        createBrowserFetchGuard,
        defaultBlobHandoff,
        download,
        isOrdinaryMediaItem,
        readBlobWithinLimit,
        safeErrorMessage,
        sanitizeFilename,
    });
}));
