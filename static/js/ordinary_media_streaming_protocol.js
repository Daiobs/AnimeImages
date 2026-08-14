(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesOrdinaryMediaStreamingProtocol = api;
    }
}(typeof self !== "undefined" ? self : globalThis, function () {
    "use strict";

    const PROTOCOL_VERSION = 1;
    const DOWNLOAD_PATH_SEGMENT = "__animeimages-stream-download";
    const DEFAULT_STREAM_DESCRIPTOR_TTL_MS = 120000;
    const MAX_STREAM_BYTES = 2 * 1024 * 1024 * 1024;
    const DOWNLOAD_ID_PATTERN = /^[a-f0-9]{32}$/;
    const ALLOWED_MEDIA_HOST_SUFFIXES = Object.freeze([
        "365yg.com",
        "douyinvod.com",
    ]);
    const SAFE_CONTENT_TYPES = Object.freeze([
        "application/octet-stream",
        "binary/octet-stream",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]);
    const SAFE_STATES = Object.freeze([
        "prepared",
        "fetching",
        "completed",
        "failed",
        "cancelled",
    ]);
    const TERMINAL_STATES = Object.freeze(["completed", "failed", "cancelled"]);
    const FAILURE_CLASSES = Object.freeze([
        "NOT_APPLICABLE",
        "FETCH_REJECTED_TYPE_ERROR",
        "FETCH_ABORTED",
        "FETCH_NETWORK_ERROR",
        "FETCH_SECURITY_ERROR",
        "FETCH_TIMEOUT_ERROR",
        "FETCH_REJECTED_OTHER",
    ]);
    const FAILURE_STAGES = Object.freeze([
        "PRE_START_FAILURE",
        "FETCH_REJECTED_BEFORE_RESPONSE",
        "HTTP_RESPONSE_REJECTED",
        "STREAM_BODY_FAILURE",
        "SIZE_VALIDATION_FAILURE",
        "USER_CANCELLED",
        "DOWNLOAD_MANAGER_CANCELLED",
        "DESCRIPTOR_FAILURE",
    ]);
    const MAX_SAFE_TIMING_MS = 24 * 60 * 60 * 1000;

    function positiveInteger(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 0;
    }

    function nonNegativeNumber(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function sanitizeFilename(value) {
        const filename = String(value || "video.mp4")
            .replace(/[\\/:*?"<>|\x00-\x1f\x7f]+/g, "_")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 180);
        return filename || "video.mp4";
    }

    function sanitizeProvider(value) {
        return String(value || "unknown")
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, "_")
            .replace(/^_+|_+$/g, "")
            .slice(0, 64) || "unknown";
    }

    function normalizeContentType(value) {
        return String(value || "")
            .split(";", 1)[0]
            .trim()
            .toLowerCase();
    }

    function isAllowedMediaContentType(value) {
        const contentType = normalizeContentType(value);
        return SAFE_CONTENT_TYPES.includes(contentType) || contentType.startsWith("video/");
    }

    function hostnameMatchesSuffix(hostname, suffix) {
        const normalizedHost = String(hostname || "").toLowerCase().replace(/\.$/, "");
        const normalizedSuffix = String(suffix || "").toLowerCase().replace(/^\./, "");
        return normalizedHost === normalizedSuffix
            || normalizedHost.endsWith(`.${normalizedSuffix}`);
    }

    function isLoopbackHostname(hostname) {
        const normalizedHost = String(hostname || "").toLowerCase().replace(/\.$/, "");
        return normalizedHost === "localhost"
            || normalizedHost === "127.0.0.1"
            || normalizedHost === "[::1]";
    }

    function isAllowedStreamingUrl(value, options) {
        const settings = options && typeof options === "object" ? options : {};
        try {
            const url = new URL(String(value || ""));
            const providerMediaUrl = url.protocol === "https:"
                && ALLOWED_MEDIA_HOST_SUFFIXES.some((suffix) => (
                    hostnameMatchesSuffix(url.hostname, suffix)
                ));
            const localBenchmarkUrl = Boolean(settings.allowLoopbackHttp)
                && (url.protocol === "http:" || url.protocol === "https:")
                && isLoopbackHostname(url.hostname);
            return providerMediaUrl || localBenchmarkUrl;
        } catch (error) {
            return false;
        }
    }

    function sanitizeMediaHost(value, options) {
        const settings = options && typeof options === "object" ? options : {};
        let hostname = "";
        try {
            const rawValue = String(value || "").trim();
            hostname = rawValue.includes("://")
                ? new URL(rawValue).hostname
                : rawValue;
        } catch (error) {
            return "unknown";
        }
        hostname = hostname.toLowerCase().replace(/\.$/, "");
        const allowedProviderHost = ALLOWED_MEDIA_HOST_SUFFIXES.some((suffix) => (
            hostnameMatchesSuffix(hostname, suffix)
        ));
        const allowedLoopback = Boolean(settings.allowLoopback)
            && isLoopbackHostname(hostname);
        if (!allowedProviderHost && !allowedLoopback) {
            return "unknown";
        }
        if (
            hostname.length > 255
            || (!isLoopbackHostname(hostname) && !/^[a-z0-9.-]+$/.test(hostname))
        ) {
            return "unknown";
        }
        if (!isLoopbackHostname(hostname) && hostname.split(".").some((label) => (
            !label
            || label.length > 63
            || label.startsWith("-")
            || label.endsWith("-")
        ))) {
            return "unknown";
        }
        return hostname;
    }

    function classifyFetchFailure(error) {
        let name = "";
        try {
            name = String(error && error.name || "");
        } catch (classificationError) {
            return "FETCH_REJECTED_OTHER";
        }
        const classifications = Object.freeze({
            TypeError: "FETCH_REJECTED_TYPE_ERROR",
            AbortError: "FETCH_ABORTED",
            NetworkError: "FETCH_NETWORK_ERROR",
            SecurityError: "FETCH_SECURITY_ERROR",
            TimeoutError: "FETCH_TIMEOUT_ERROR",
        });
        return classifications[name] || "FETCH_REJECTED_OTHER";
    }

    function normalizeFailureClass(value) {
        const failureClass = String(value || "").toUpperCase();
        return FAILURE_CLASSES.includes(failureClass) ? failureClass : "NOT_APPLICABLE";
    }

    function normalizeFailureStage(value) {
        const failureStage = String(value || "").toUpperCase();
        return FAILURE_STAGES.includes(failureStage) ? failureStage : "";
    }

    function safeTiming(value) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed) || parsed < 0) {
            return 0;
        }
        return Math.min(MAX_SAFE_TIMING_MS, Math.floor(parsed));
    }

    function elapsedTiming(startValue, endValue) {
        const start = positiveInteger(startValue);
        const end = positiveInteger(endValue);
        return start && end && end >= start ? safeTiming(end - start) : 0;
    }

    function normalizeDownloadId(value) {
        const id = String(value || "").toLowerCase();
        return DOWNLOAD_ID_PATTERN.test(id) ? id : "";
    }

    function randomDownloadId(cryptoObject) {
        const cryptoApi = cryptoObject || (
            typeof crypto !== "undefined" ? crypto : null
        );
        if (!cryptoApi || typeof cryptoApi.getRandomValues !== "function") {
            throw new Error("SECURE_DOWNLOAD_ID_UNAVAILABLE");
        }
        const bytes = new Uint8Array(16);
        cryptoApi.getRandomValues(bytes);
        return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    }

    function createDescriptor(input, options) {
        const value = input && typeof input === "object" ? input : {};
        const settings = options && typeof options === "object" ? options : {};
        const id = normalizeDownloadId(value.downloadId || value.id);
        const now = positiveInteger(settings.nowEpochMs) || Date.now();
        const ttlMs = positiveInteger(settings.ttlMs) || DEFAULT_STREAM_DESCRIPTOR_TTL_MS;
        const expectedBytes = positiveInteger(value.expectedBytes || value.size_bytes);
        if (!id) {
            throw new Error("INVALID_DOWNLOAD_ID");
        }
        if (!isAllowedStreamingUrl(value.url, settings)) {
            throw new Error("STREAMING_URL_UNSUPPORTED");
        }
        if (expectedBytes > MAX_STREAM_BYTES) {
            throw new Error("EXPECTED_SIZE_TOO_LARGE");
        }
        return Object.freeze({
            downloadId: id,
            url: String(value.url),
            mediaHost: sanitizeMediaHost(value.url, {
                allowLoopback: Boolean(settings.allowLoopbackHttp),
            }),
            filename: sanitizeFilename(value.filename),
            provider: sanitizeProvider(value.provider),
            expectedBytes,
            createdAt: now,
            expiresAt: now + ttlMs,
        });
    }

    function downloadPath(downloadId, scopePath) {
        const id = normalizeDownloadId(downloadId);
        if (!id) {
            throw new Error("INVALID_DOWNLOAD_ID");
        }
        const base = String(scopePath || "/static/js/").replace(/\/*$/, "/");
        return `${base}${DOWNLOAD_PATH_SEGMENT}/${id}`;
    }

    function descriptorIdFromPath(pathname) {
        const value = String(pathname || "");
        const marker = `/${DOWNLOAD_PATH_SEGMENT}/`;
        if (!value.startsWith(marker)) {
            return "";
        }
        const id = value.slice(marker.length).toLowerCase();
        return normalizeDownloadId(id);
    }

    function normalizeState(value) {
        const state = String(value || "").toLowerCase();
        return SAFE_STATES.includes(state) ? state : "failed";
    }

    function safeSnapshot(value) {
        const status = value && typeof value === "object" ? value : {};
        const totalBytes = positiveInteger(status.totalBytes || status.expectedBytes);
        const receivedBytes = positiveInteger(status.receivedBytes);
        const rawPercent = totalBytes > 0
            ? Math.min(100, (receivedBytes / totalBytes) * 100)
            : null;
        const state = normalizeState(status.state);
        return Object.freeze({
            protocolVersion: PROTOCOL_VERSION,
            downloadId: normalizeDownloadId(status.downloadId),
            filename: sanitizeFilename(status.filename),
            provider: sanitizeProvider(status.provider),
            expectedBytes: positiveInteger(status.expectedBytes),
            receivedBytes,
            totalBytes,
            percent: state === "completed" ? 100 : rawPercent,
            bytesPerSecond: nonNegativeNumber(status.bytesPerSecond),
            etaSeconds: status.etaSeconds === null || status.etaSeconds === undefined
                ? null
                : nonNegativeNumber(status.etaSeconds),
            transport: "browser_streaming",
            state,
            streamingStarted: Boolean(status.streamingStarted),
            stage: state === "prepared" ? "preparing" : (
                state === "fetching" ? "fetching" : state
            ),
            startedAt: positiveInteger(status.startedAt),
            updatedAt: positiveInteger(status.updatedAt),
            errorCode: String(status.errorCode || "").replace(/[^A-Z0-9_]+/g, "").slice(0, 80),
            failureClass: normalizeFailureClass(status.failureClass),
            failureStage: normalizeFailureStage(status.failureStage),
            mediaHost: sanitizeMediaHost(status.mediaHost, {allowLoopback: true}),
            registrationToFetchMs: safeTiming(
                status.registrationToFetchMs
                || elapsedTiming(status.registeredAt, status.fetchStartedAt),
            ),
            fetchRejectMs: safeTiming(
                status.fetchRejectMs
                || elapsedTiming(status.fetchStartedAt, status.fetchFailedAt),
            ),
            timeToFirstChunkMs: safeTiming(
                status.timeToFirstChunkMs
                || elapsedTiming(status.fetchStartedAt, status.firstChunkAt),
            ),
            streamDurationMs: safeTiming(
                status.streamDurationMs
                || elapsedTiming(status.fetchStartedAt, status.terminalAt),
            ),
        });
    }

    function createProgressEstimator(options) {
        const settings = options && typeof options === "object" ? options : {};
        const speedWindowMs = positiveInteger(settings.speedWindowMs) || 3000;
        const minEtaSampleMs = positiveInteger(settings.minEtaSampleMs) || 1500;
        const samples = [];

        function update(receivedValue, totalValue, nowValue) {
            const now = nonNegativeNumber(nowValue);
            const receivedBytes = positiveInteger(receivedValue);
            const totalBytes = positiveInteger(totalValue);
            const previous = samples[samples.length - 1];
            if (previous && previous.at === now) {
                previous.bytes = Math.max(previous.bytes, receivedBytes);
            } else {
                samples.push({at: now, bytes: receivedBytes});
            }
            const cutoff = now - speedWindowMs;
            while (samples.length > 2 && samples[1].at <= cutoff) {
                samples.shift();
            }
            const first = samples[0] || {at: now, bytes: receivedBytes};
            const durationMs = Math.max(0, now - first.at);
            const transferred = Math.max(0, receivedBytes - first.bytes);
            const bytesPerSecond = durationMs > 0 ? (transferred * 1000) / durationMs : 0;
            const etaSeconds = (
                totalBytes > receivedBytes
                && durationMs >= minEtaSampleMs
                && bytesPerSecond > 0
            ) ? (totalBytes - receivedBytes) / bytesPerSecond : null;
            return Object.freeze({bytesPerSecond, etaSeconds});
        }

        return Object.freeze({update});
    }

    return Object.freeze({
        ALLOWED_MEDIA_HOST_SUFFIXES,
        DEFAULT_STREAM_DESCRIPTOR_TTL_MS,
        DOWNLOAD_PATH_SEGMENT,
        FAILURE_CLASSES,
        FAILURE_STAGES,
        MAX_STREAM_BYTES,
        PROTOCOL_VERSION,
        TERMINAL_STATES,
        classifyFetchFailure,
        createDescriptor,
        createProgressEstimator,
        descriptorIdFromPath,
        downloadPath,
        isAllowedMediaContentType,
        isAllowedStreamingUrl,
        isLoopbackHostname,
        normalizeFailureClass,
        normalizeFailureStage,
        normalizeContentType,
        normalizeDownloadId,
        positiveInteger,
        randomDownloadId,
        safeSnapshot,
        sanitizeMediaHost,
        sanitizeFilename,
        sanitizeProvider,
    });
}));
