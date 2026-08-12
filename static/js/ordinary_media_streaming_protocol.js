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

    function isAllowedStreamingUrl(value) {
        try {
            const url = new URL(String(value || ""));
            return url.protocol === "https:"
                && ALLOWED_MEDIA_HOST_SUFFIXES.some((suffix) => (
                    hostnameMatchesSuffix(url.hostname, suffix)
                ));
        } catch (error) {
            return false;
        }
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
        if (!isAllowedStreamingUrl(value.url)) {
            throw new Error("STREAMING_URL_UNSUPPORTED");
        }
        if (expectedBytes > MAX_STREAM_BYTES) {
            throw new Error("EXPECTED_SIZE_TOO_LARGE");
        }
        return Object.freeze({
            downloadId: id,
            url: String(value.url),
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
        MAX_STREAM_BYTES,
        PROTOCOL_VERSION,
        TERMINAL_STATES,
        createDescriptor,
        createProgressEstimator,
        descriptorIdFromPath,
        downloadPath,
        isAllowedMediaContentType,
        isAllowedStreamingUrl,
        normalizeContentType,
        normalizeDownloadId,
        positiveInteger,
        randomDownloadId,
        safeSnapshot,
        sanitizeFilename,
        sanitizeProvider,
    });
}));
