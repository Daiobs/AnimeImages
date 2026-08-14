(function (root, factory) {
    const streamingApi = root && root.AnimeImagesOrdinaryMediaStreaming
        ? root.AnimeImagesOrdinaryMediaStreaming
        : (typeof module === "object" && module.exports
            ? require("./ordinary_media_streaming.js")
            : null);
    const streamingProtocol = root && root.AnimeImagesOrdinaryMediaStreamingProtocol
        ? root.AnimeImagesOrdinaryMediaStreamingProtocol
        : (typeof module === "object" && module.exports
            ? require("./ordinary_media_streaming_protocol.js")
            : null);
    const api = factory(streamingApi, streamingProtocol);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesOrdinaryMediaDownload = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function (streamingApi, streamingProtocol) {
    "use strict";

    const MAX_BROWSER_BLOB_BYTES = 1024 * 1024 * 1024;
    const DEFAULT_BROWSER_FETCH_IDLE_TIMEOUT_MS = 30000;
    const DEFAULT_MEDIA_FETCH_PROBE_TIMEOUT_MS = 8000;
    const DEFAULT_PROGRESS_UPDATE_INTERVAL_MS = 300;
    const DEFAULT_SPEED_WINDOW_MS = 3000;
    const MIN_ETA_SAMPLE_MS = 1500;
    const ACTIVE_DOWNLOAD_STORAGE_KEY = "animeimages.ordinaryDownloadActive.v1";
    const SAFE_VIDEO_CONTENT_TYPES = Object.freeze([
        "application/octet-stream",
        "binary/octet-stream",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]);
    const STREAMING_BLOB_FALLBACK_ERROR_CODES = Object.freeze([
        "STREAMING_CAPABILITY_UNAVAILABLE",
        "STREAMING_REGISTRATION_FAILED",
        "STREAMING_WORKER_NOT_AVAILABLE",
        "STREAMING_WORKER_ACTIVATION_FAILED",
        "STREAMING_WORKER_ACTIVATION_TIMEOUT",
        "STREAMING_PAGE_NOT_CONTROLLED",
        "STREAMING_PAGE_CONTROL_TIMEOUT",
        "STREAMING_WORKER_NOT_ACTIVE",
        "STREAMING_MESSAGE_TIMEOUT",
        "STREAMING_NAVIGATION_UNAVAILABLE",
        "STREAMING_START_TIMEOUT",
    ]);
    const STAGE_MESSAGES = Object.freeze({
        preparing: "正在准备下载...",
        fetching: "正在下载源文件...",
        saving: "文件已获取完成，正在交给浏览器保存...",
        completed: "下载已完成。",
    });

    function booleanValue(value) {
        return value === true || String(value || "").toLowerCase() === "true";
    }

    function positiveInteger(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }

    function nonNegativeNumber(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function sanitizeFilename(filename) {
        const value = String(filename || "video.mp4")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, " ")
            .trim();
        return value || "video.mp4";
    }

    function sanitizeProvider(provider) {
        return String(provider || "unknown")
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, "_")
            .replace(/^_+|_+$/g, "")
            .slice(0, 64) || "unknown";
    }

    function formatBytes(value) {
        const bytes = nonNegativeNumber(value);
        if (bytes < 1024) {
            return `${Math.round(bytes)} B`;
        }
        const units = ["KiB", "MiB", "GiB"];
        let amount = bytes / 1024;
        let unit = units[0];
        for (let index = 1; index < units.length && amount >= 1024; index += 1) {
            amount /= 1024;
            unit = units[index];
        }
        const digits = amount >= 100 ? 0 : (amount >= 10 ? 1 : 2);
        return `${amount.toFixed(digits)} ${unit}`;
    }

    function formatRate(value) {
        const bytesPerSecond = nonNegativeNumber(value);
        return bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : "正在测速...";
    }

    function formatEta(value) {
        const seconds = Math.max(0, Math.ceil(nonNegativeNumber(value)));
        if (!seconds) {
            return "";
        }
        if (seconds < 60) {
            return `剩余约 ${seconds} 秒`;
        }
        const minutes = Math.floor(seconds / 60);
        const remainingSeconds = seconds % 60;
        return remainingSeconds
            ? `剩余约 ${minutes} 分 ${remainingSeconds} 秒`
            : `剩余约 ${minutes} 分钟`;
    }

    function createTransferProgressTracker(options) {
        const settings = options && typeof options === "object" ? options : {};
        const now = typeof settings.now === "function"
            ? settings.now
            : (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
        const updateIntervalMs = positiveInteger(
            settings.progressUpdateIntervalMs || settings.updateIntervalMs,
        )
            || DEFAULT_PROGRESS_UPDATE_INTERVAL_MS;
        const speedWindowMs = positiveInteger(
            settings.progressSpeedWindowMs || settings.speedWindowMs,
        )
            || DEFAULT_SPEED_WINDOW_MS;
        const minEtaSampleMs = positiveInteger(
            settings.progressMinEtaSampleMs || settings.minEtaSampleMs,
        )
            || MIN_ETA_SAMPLE_MS;
        const startedAt = nonNegativeNumber(now());
        const downloadId = String(settings.downloadId || "");
        const transport = String(settings.transport || "browser_blob");
        let totalBytes = positiveInteger(settings.totalBytes);
        let lastReceivedBytes = 0;
        let lastPercent = 0;
        let lastEmitAt = Number.NEGATIVE_INFINITY;
        const samples = [{at: startedAt, bytes: 0}];

        function setTotalBytes(value) {
            totalBytes = positiveInteger(value);
        }

        function update(receivedValue, stage, force) {
            const currentAt = nonNegativeNumber(now());
            const receivedBytes = Math.max(lastReceivedBytes, positiveInteger(receivedValue));
            lastReceivedBytes = receivedBytes;
            const previousSample = samples[samples.length - 1];
            if (previousSample && previousSample.at === currentAt) {
                previousSample.bytes = receivedBytes;
            } else {
                samples.push({at: currentAt, bytes: receivedBytes});
            }
            const cutoff = currentAt - speedWindowMs;
            while (samples.length > 2 && samples[1].at <= cutoff) {
                samples.shift();
            }

            const firstSample = samples[0];
            const sampleDurationMs = Math.max(0, currentAt - firstSample.at);
            const sampleBytes = Math.max(0, receivedBytes - firstSample.bytes);
            const bytesPerSecond = sampleDurationMs > 0
                ? (sampleBytes * 1000) / sampleDurationMs
                : 0;
            const completedStage = stage === "saving" || stage === "completed";
            let percent = totalBytes > 0
                ? Math.min(100, (receivedBytes / totalBytes) * 100)
                : null;
            if (completedStage) {
                percent = 100;
            }
            if (percent !== null) {
                percent = Math.max(lastPercent, percent);
                lastPercent = percent;
            }
            const elapsedMs = Math.max(0, currentAt - startedAt);
            const etaSeconds = (
                totalBytes > receivedBytes
                && sampleDurationMs >= minEtaSampleMs
                && bytesPerSecond > 0
            )
                ? (totalBytes - receivedBytes) / bytesPerSecond
                : null;
            const shouldEmit = Boolean(force)
                || currentAt - lastEmitAt >= updateIntervalMs
                || percent === 100;
            if (!shouldEmit) {
                return null;
            }
            lastEmitAt = currentAt;
            return Object.freeze({
                downloadId,
                transport,
                state: completedStage ? "completed" : "fetching",
                stage: String(stage || "fetching"),
                receivedBytes,
                totalBytes,
                percent,
                elapsedMs,
                bytesPerSecond,
                etaSeconds,
            });
        }

        return Object.freeze({setTotalBytes, update});
    }

    function activeDownloadStorage(settings) {
        if (settings && settings.sessionStorage) {
            return settings.sessionStorage;
        }
        try {
            return typeof window !== "undefined" ? window.sessionStorage : null;
        } catch (error) {
            return null;
        }
    }

    function createActiveDownloadMarker(item, filename, expectedBytes, startedAt) {
        return Object.freeze({
            version: 1,
            downloadActive: true,
            startedAt: positiveInteger(startedAt) || Date.now(),
            provider: sanitizeProvider(item && item.provider),
            filename: sanitizeFilename(filename),
            expectedBytes: positiveInteger(expectedBytes),
        });
    }

    function writeActiveDownloadMarker(storage, marker) {
        if (!storage || typeof storage.setItem !== "function") {
            return false;
        }
        try {
            storage.setItem(ACTIVE_DOWNLOAD_STORAGE_KEY, JSON.stringify(marker));
            return true;
        } catch (error) {
            return false;
        }
    }

    function removeActiveDownloadMarker(storage) {
        if (!storage || typeof storage.removeItem !== "function") {
            return false;
        }
        try {
            storage.removeItem(ACTIVE_DOWNLOAD_STORAGE_KEY);
            return true;
        } catch (error) {
            return false;
        }
    }

    function consumeInterruptedDownloadMarker(options) {
        const settings = options && typeof options === "object" ? options : {};
        const storage = activeDownloadStorage(settings);
        if (!storage || typeof storage.getItem !== "function") {
            return null;
        }
        let rawValue = "";
        try {
            rawValue = storage.getItem(ACTIVE_DOWNLOAD_STORAGE_KEY) || "";
        } catch (error) {
            return null;
        }
        removeActiveDownloadMarker(storage);
        if (!rawValue) {
            return null;
        }
        try {
            const parsed = JSON.parse(rawValue);
            if (!parsed || parsed.downloadActive !== true) {
                return null;
            }
            return Object.freeze({
                version: 1,
                downloadActive: true,
                startedAt: positiveInteger(parsed.startedAt),
                provider: sanitizeProvider(parsed.provider),
                filename: sanitizeFilename(parsed.filename),
                expectedBytes: positiveInteger(parsed.expectedBytes),
            });
        } catch (error) {
            return null;
        }
    }

    function createActiveDownloadProtection(item, filename, expectedBytes, options) {
        const settings = options && typeof options === "object" ? options : {};
        const storage = activeDownloadStorage(settings);
        const eventTarget = settings.eventTarget || (
            typeof window !== "undefined" ? window : null
        );
        const schedule = settings.defer || (
            typeof setTimeout === "function" ? setTimeout : null
        );
        let pageUnloading = false;
        let pageHidden = false;
        let cleared = false;
        let marker = createActiveDownloadMarker(
            item,
            filename,
            expectedBytes,
            settings.startedAtEpochMs,
        );
        writeActiveDownloadMarker(storage, marker);

        function handleBeforeUnload(event) {
            pageUnloading = true;
            if (event && typeof event.preventDefault === "function") {
                event.preventDefault();
            }
            if (event) {
                event.returnValue = "";
            }
            return "";
        }

        function handlePageHide() {
            pageHidden = true;
            pageUnloading = true;
            detachListeners();
        }

        function detachListeners() {
            if (eventTarget && typeof eventTarget.removeEventListener === "function") {
                eventTarget.removeEventListener("beforeunload", handleBeforeUnload);
                eventTarget.removeEventListener("pagehide", handlePageHide);
            }
        }

        if (eventTarget && typeof eventTarget.addEventListener === "function") {
            eventTarget.addEventListener("beforeunload", handleBeforeUnload);
            eventTarget.addEventListener("pagehide", handlePageHide);
        }

        return Object.freeze({
            updateExpectedBytes(value) {
                marker = createActiveDownloadMarker(
                    item,
                    filename,
                    value,
                    marker.startedAt,
                );
                writeActiveDownloadMarker(storage, marker);
            },
            clear(clearOptions) {
                if (cleared) {
                    return;
                }
                cleared = true;
                const preserveDuringUnload = Boolean(
                    clearOptions && clearOptions.preserveDuringUnload,
                );
                if (!preserveDuringUnload && !pageHidden) {
                    removeActiveDownloadMarker(storage);
                    detachListeners();
                } else if (pageHidden) {
                    detachListeners();
                } else if (!pageUnloading) {
                    removeActiveDownloadMarker(storage);
                    detachListeners();
                } else {
                    if (eventTarget && typeof eventTarget.removeEventListener === "function") {
                        eventTarget.removeEventListener("beforeunload", handleBeforeUnload);
                    }
                    if (schedule) {
                        schedule(() => {
                            if (!pageHidden) {
                                removeActiveDownloadMarker(storage);
                                detachListeners();
                            }
                        }, 0);
                    }
                }
            },
        });
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

    async function probeOrdinaryMediaFetch(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        const now = typeof settings.now === "function" ? settings.now : Date.now;
        const startedAt = nonNegativeNumber(now());
        const mediaHost = streamingProtocol && typeof streamingProtocol.sanitizeMediaHost === "function"
            ? streamingProtocol.sanitizeMediaHost(item && (item.url || item.video_url), {
                allowLoopback: Boolean(settings.allowLoopback),
            })
            : "unknown";
        const baseResult = {
            ok: false,
            mediaHost,
            httpStatus: 0,
            contentType: "",
            contentLength: 0,
            failureClass: "NOT_APPLICABLE",
            failureStage: "PRE_START_FAILURE",
            durationMs: 0,
        };
        const finish = (overrides) => Object.freeze({
            ...baseResult,
            ...(overrides || {}),
            durationMs: Math.max(0, Math.floor(nonNegativeNumber(now()) - startedAt)),
        });
        const sourceUrl = String(item && (item.url || item.video_url) || "");
        if (
            !isOrdinaryMediaItem(item)
            || !streamingProtocol
            || !streamingProtocol.isAllowedStreamingUrl(sourceUrl, {
                allowLoopbackHttp: Boolean(settings.allowLoopback),
            })
        ) {
            return finish({failureStage: "PRE_START_FAILURE"});
        }

        const fetchImpl = settings.fetchImpl || (
            typeof fetch === "function" ? fetch.bind(globalThis) : null
        );
        const Controller = settings.AbortControllerConstructor || (
            typeof AbortController === "function" ? AbortController : null
        );
        const schedule = settings.schedule || setTimeout;
        const clearSchedule = settings.clearSchedule || clearTimeout;
        if (!fetchImpl || !Controller || !schedule || !clearSchedule) {
            return finish({failureClass: "FETCH_REJECTED_OTHER"});
        }

        const controller = new Controller();
        const timeoutMs = positiveInteger(settings.timeoutMs)
            || DEFAULT_MEDIA_FETCH_PROBE_TIMEOUT_MS;
        let timedOut = false;
        let responseAcquired = false;
        let reader = null;
        const timer = schedule(() => {
            timedOut = true;
            controller.abort();
        }, timeoutMs);
        try {
            const response = await fetchImpl(
                sourceUrl,
                browserFetchOptions(controller.signal),
            );
            responseAcquired = true;
            const contentType = normalizedContentType(
                response && response.headers && response.headers.get
                    ? response.headers.get("Content-Type")
                    : "",
            );
            const contentLength = positiveInteger(
                response && response.headers && response.headers.get
                    ? response.headers.get("Content-Length")
                    : 0,
            );
            const responseMetadata = {
                httpStatus: positiveInteger(response && response.status),
                contentType,
                contentLength,
            };
            if (
                !response
                || !response.ok
                || !response.body
                || typeof response.body.getReader !== "function"
                || !contentTypeAllowsOrdinaryVideo(contentType)
                || !streamingProtocol.isAllowedStreamingUrl(response.url, {
                    allowLoopbackHttp: Boolean(settings.allowLoopback),
                })
            ) {
                return finish({...responseMetadata, failureStage: "HTTP_RESPONSE_REJECTED"});
            }

            reader = response.body.getReader();
            while (true) {
                const {done, value} = await reader.read();
                if (done) {
                    return finish({...responseMetadata, failureStage: "STREAM_BODY_FAILURE"});
                }
                if (value && value.byteLength) {
                    return finish({
                        ...responseMetadata,
                        ok: true,
                        failureStage: "",
                    });
                }
            }
        } catch (error) {
            const failureClass = timedOut
                ? "FETCH_TIMEOUT_ERROR"
                : (streamingProtocol && typeof streamingProtocol.classifyFetchFailure === "function"
                    ? streamingProtocol.classifyFetchFailure(error)
                    : "FETCH_REJECTED_OTHER");
            return finish({
                failureClass,
                failureStage: responseAcquired
                    ? "STREAM_BODY_FAILURE"
                    : "FETCH_REJECTED_BEFORE_RESPONSE",
            });
        } finally {
            clearSchedule(timer);
            if (reader && typeof reader.cancel === "function") {
                try {
                    const cancellation = reader.cancel("DIAGNOSTIC_PROBE_COMPLETE");
                    if (cancellation && typeof cancellation.catch === "function") {
                        cancellation.catch(() => {});
                    }
                } catch (error) {
                    // The bounded probe has already captured its safe result.
                }
            }
            if (reader && typeof reader.releaseLock === "function") {
                try {
                    reader.releaseLock();
                } catch (error) {
                    // Some streams release their lock during cancellation.
                }
            }
            controller.abort();
        }
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
        if (errorCode === "DOWNLOAD_MANAGER_CANCELLED" || errorCode === "USER_CANCELLED") {
            return "下载已取消。";
        }
        if (errorCode === "DESCRIPTOR_EXPIRED") {
            return "下载链接已失效，请重新解析后再试。";
        }
        if (error && error.streamingStarted) {
            return "浏览器下载未能完成，请重新点击下载；若仍失败，请重新解析。";
        }
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

    function canFallbackToBlob(error) {
        if (error && error.streamingStarted) {
            return false;
        }
        return STREAMING_BLOB_FALLBACK_ERROR_CODES.includes(
            String(error && error.errorCode || ""),
        );
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
            onChunk(positiveInteger(blob && blob.size));
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

    function createDownloadId(options) {
        const settings = options && typeof options === "object" ? options : {};
        if (settings.downloadId) {
            return String(settings.downloadId);
        }
        if (streamingProtocol && typeof streamingProtocol.randomDownloadId === "function") {
            try {
                return streamingProtocol.randomDownloadId(settings.cryptoObject);
            } catch (error) {
                // Blob fallback can proceed without a worker-visible identifier.
            }
        }
        return "";
    }

    function activeDownloadViewModel(status) {
        const value = status && typeof status === "object" ? status : {};
        const receivedBytes = positiveInteger(value.receivedBytes);
        const totalBytes = positiveInteger(value.totalBytes || value.expectedBytes);
        const percent = totalBytes > 0
            ? Math.max(0, Math.min(100, Number(value.percent || 0)))
            : null;
        return Object.freeze({
            downloadId: String(value.downloadId || ""),
            filename: sanitizeFilename(value.filename),
            provider: sanitizeProvider(value.provider),
            state: String(value.state || "failed"),
            receivedBytes,
            totalBytes,
            percent,
            bytesPerSecond: nonNegativeNumber(value.bytesPerSecond),
            etaSeconds: value.etaSeconds === null || value.etaSeconds === undefined
                ? null
                : nonNegativeNumber(value.etaSeconds),
        });
    }

    async function mountActiveDownloadPanel(container, options) {
        const settings = options && typeof options === "object" ? options : {};
        const client = settings.streamingClient || (
            streamingApi && typeof streamingApi.getSharedClient === "function"
                ? streamingApi.getSharedClient()
                : null
        );
        if (!container || !client || !client.capabilityAvailable()) {
            return Object.freeze({close() {}});
        }
        const documentObject = settings.documentObject || container.ownerDocument || document;
        const active = new Map();

        function render() {
            container.replaceChildren();
            const statuses = Array.from(active.values())
                .filter((status) => status.state === "prepared" || status.state === "fetching")
                .sort((left, right) => left.downloadId.localeCompare(right.downloadId));
            container.hidden = statuses.length === 0;
            if (!statuses.length) {
                return;
            }
            const heading = documentObject.createElement("div");
            heading.className = "ordinary-active-downloads-heading";
            heading.textContent = "正在下载";
            container.appendChild(heading);
            statuses.forEach((status) => {
                const row = documentObject.createElement("div");
                row.className = "ordinary-active-download-row";
                row.dataset.downloadId = status.downloadId;

                const identity = documentObject.createElement("div");
                identity.className = "ordinary-active-download-identity";
                const filename = documentObject.createElement("strong");
                filename.textContent = status.filename;
                const detail = documentObject.createElement("span");
                detail.textContent = status.totalBytes
                    ? `${formatBytes(status.receivedBytes)} / ${formatBytes(status.totalBytes)}`
                    : `已下载 ${formatBytes(status.receivedBytes)} · 总大小未知`;
                identity.append(filename, detail);

                const progress = documentObject.createElement("div");
                progress.className = "ordinary-active-download-progress";
                const bar = documentObject.createElement("div");
                bar.className = "ordinary-active-download-progress-bar";
                if (status.percent === null) {
                    progress.classList.add("is-pending");
                    bar.style.width = "100%";
                } else {
                    bar.style.width = `${status.percent}%`;
                }
                progress.appendChild(bar);

                const metrics = documentObject.createElement("div");
                metrics.className = "ordinary-active-download-metrics";
                const percentText = status.percent === null
                    ? "下载中"
                    : `${Math.round(status.percent)}%`;
                const etaText = status.etaSeconds ? ` · ${formatEta(status.etaSeconds)}` : "";
                metrics.textContent = `${percentText} · ${formatRate(status.bytesPerSecond)}${etaText}`;

                const cancel = documentObject.createElement("button");
                cancel.type = "button";
                cancel.className = "btn btn-outline-secondary btn-sm";
                cancel.dataset.cancelDownloadId = status.downloadId;
                cancel.textContent = "取消";

                row.append(identity, progress, metrics, cancel);
                container.appendChild(row);
            });
        }

        function applyStatus(rawStatus) {
            const status = activeDownloadViewModel(rawStatus);
            if (!status.downloadId) {
                return;
            }
            if (status.state === "prepared" || status.state === "fetching") {
                active.set(status.downloadId, status);
            } else {
                active.delete(status.downloadId);
            }
            render();
            if (typeof settings.onStatus === "function") {
                settings.onStatus(status);
            }
        }

        const unsubscribe = client.subscribeAll(applyStatus);
        const handleClick = async (event) => {
            const button = event.target.closest("[data-cancel-download-id]");
            if (!button || !container.contains(button)) {
                return;
            }
            button.disabled = true;
            try {
                await client.cancel(button.dataset.cancelDownloadId);
            } catch (error) {
                button.disabled = false;
            }
        };
        container.addEventListener("click", handleClick);
        (await client.listActiveDownloads()).forEach(applyStatus);
        return Object.freeze({
            close() {
                unsubscribe();
                container.removeEventListener("click", handleClick);
                active.clear();
                render();
            },
        });
    }

    async function downloadViaBlob(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        if (!isOrdinaryMediaItem(item)) {
            throw downloadError(
                "BROWSER_DOWNLOAD_NOT_ORDINARY_MEDIA",
                "当前资源不支持浏览器直接保存。",
            );
        }

        const declaredSize = positiveInteger(item.size_bytes || item.sizeBytes);
        const maxBytes = positiveInteger(settings.maxBytes) || MAX_BROWSER_BLOB_BYTES;
        const filename = sanitizeFilename(settings.filename || item.filename);
        const downloadId = createDownloadId(settings);
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
        const onProgress = typeof settings.onProgress === "function"
            ? settings.onProgress
            : (() => {});
        const progressTracker = createTransferProgressTracker({
            ...settings,
            downloadId,
            transport: "browser_blob",
        });
        const activeProtection = createActiveDownloadProtection(
            item,
            filename,
            declaredSize,
            settings,
        );
        let activeProtectionCleared = false;

        function clearActiveProtection(preserveDuringUnload) {
            if (!activeProtectionCleared) {
                activeProtectionCleared = true;
                activeProtection.clear({preserveDuringUnload});
            }
        }

        function emitProgress(stage, receivedBytes, force) {
            const progressEvent = progressTracker.update(receivedBytes, stage, force);
            if (progressEvent) {
                onProgress(progressEvent);
            }
        }

        onStage("preparing", STAGE_MESSAGES.preparing);
        emitProgress("preparing", 0, true);
        onStage("fetching", STAGE_MESSAGES.fetching);
        emitProgress("fetching", 0, true);

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
            clearActiveProtection(true);
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
            clearActiveProtection(true);
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
            clearActiveProtection(true);
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
        progressTracker.setTotalBytes(expectedBytes);
        activeProtection.updateExpectedBytes(expectedBytes || declaredSize);
        emitProgress("fetching", 0, true);
        if (expectedBytes > maxBytes) {
            fetchGuard.clear();
            clearActiveProtection(true);
            throw downloadError(
                "BROWSER_BLOB_TOO_LARGE",
                "当前资源超过浏览器保存上限。",
            );
        }

        let blob;
        try {
            fetchGuard.touch();
            blob = await readBlobWithinLimit(response, maxBytes, contentType, {
                onChunk: (receivedBytes) => {
                    fetchGuard.touch();
                    emitProgress("fetching", receivedBytes, false);
                },
            });
        } catch (error) {
            clearActiveProtection(true);
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
            clearActiveProtection(true);
            throw downloadError(
                "BROWSER_DOWNLOAD_EMPTY",
                "上游返回了空文件。",
            );
        }
        if (receivedBytes > maxBytes) {
            clearActiveProtection(true);
            throw downloadError(
                "BROWSER_BLOB_TOO_LARGE",
                "当前资源超过浏览器保存上限。",
            );
        }
        if (expectedBytes && receivedBytes !== expectedBytes) {
            clearActiveProtection(true);
            throw downloadError(
                "BROWSER_DOWNLOAD_SIZE_MISMATCH",
                "浏览器接收的文件大小不完整。",
            );
        }

        emitProgress("fetching", receivedBytes, true);
        clearActiveProtection();
        onStage("saving", STAGE_MESSAGES.saving);
        emitProgress("saving", receivedBytes, true);
        const handoffBlob = settings.handoffBlob || defaultBlobHandoff;
        await handoffBlob(blob, filename, settings);
        onStage("completed", STAGE_MESSAGES.completed);
        emitProgress("completed", receivedBytes, true);
        return {
            downloadId,
            transport: "browser_blob",
            filename,
            receivedBytes,
            expectedBytes,
            contentType,
        };
    }

    async function downloadViaStreaming(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        const client = settings.streamingClient || (
            streamingApi && typeof streamingApi.getSharedClient === "function"
                ? streamingApi.getSharedClient()
                : null
        );
        if (!client || typeof client.capabilityAvailable !== "function" || !client.capabilityAvailable()) {
            throw downloadError(
                "STREAMING_CAPABILITY_UNAVAILABLE",
                "当前浏览器不支持流式下载。",
            );
        }
        const onStage = typeof settings.onStage === "function"
            ? settings.onStage
            : (() => {});
        const onProgress = typeof settings.onProgress === "function"
            ? settings.onProgress
            : (() => {});
        onStage("preparing", STAGE_MESSAGES.preparing);
        const active = await client.start(item, {
            filename: sanitizeFilename(settings.filename || item.filename),
            onProgress(status) {
                if (status.state === "fetching") {
                    onStage("fetching", STAGE_MESSAGES.fetching);
                }
                onProgress(status);
            },
        });
        onStage("fetching", STAGE_MESSAGES.fetching);
        const completed = await active.completion;
        onStage("completed", STAGE_MESSAGES.completed);
        onProgress(completed);
        return {
            downloadId: active.downloadId,
            transport: "browser_streaming",
            filename: completed.filename,
            receivedBytes: completed.receivedBytes,
            expectedBytes: completed.totalBytes || completed.expectedBytes,
            contentType: "",
        };
    }

    async function download(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        if (!isOrdinaryMediaItem(item)) {
            throw downloadError(
                "BROWSER_DOWNLOAD_NOT_ORDINARY_MEDIA",
                "当前资源不支持浏览器直接保存。",
            );
        }
        if (settings.forceBlob === true) {
            return downloadViaBlob(item, settings);
        }
        try {
            return await downloadViaStreaming(item, settings);
        } catch (error) {
            if (!canFallbackToBlob(error)) {
                throw error;
            }
            if (typeof settings.onTransportFallback === "function") {
                settings.onTransportFallback({
                    from: "browser_streaming",
                    to: "browser_blob",
                    errorCode: String(error && error.errorCode || "STREAMING_UNAVAILABLE"),
                });
            }
            return downloadViaBlob(item, settings);
        }
    }

    return Object.freeze({
        MAX_BROWSER_BLOB_BYTES,
        DEFAULT_BROWSER_FETCH_IDLE_TIMEOUT_MS,
        DEFAULT_MEDIA_FETCH_PROBE_TIMEOUT_MS,
        DEFAULT_PROGRESS_UPDATE_INTERVAL_MS,
        DEFAULT_SPEED_WINDOW_MS,
        MIN_ETA_SAMPLE_MS,
        ACTIVE_DOWNLOAD_STORAGE_KEY,
        STREAMING_BLOB_FALLBACK_ERROR_CODES,
        STAGE_MESSAGES,
        activeDownloadViewModel,
        consumeInterruptedDownloadMarker,
        createActiveDownloadMarker,
        createActiveDownloadProtection,
        createTransferProgressTracker,
        browserFetchOptions,
        canFallbackToBlob,
        contentTypeAllowsOrdinaryVideo,
        createBrowserFetchGuard,
        defaultBlobHandoff,
        download,
        downloadViaBlob,
        downloadViaStreaming,
        isOrdinaryMediaItem,
        mountActiveDownloadPanel,
        probeOrdinaryMediaFetch,
        formatBytes,
        formatEta,
        formatRate,
        readBlobWithinLimit,
        safeErrorMessage,
        sanitizeFilename,
    });
}));
