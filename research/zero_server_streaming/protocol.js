(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesStreamingProtocol = api;
    }
}(typeof self !== "undefined" ? self : globalThis, function () {
    "use strict";

    const DOWNLOAD_PATH_SEGMENT = "__animeimages-stream-download";
    const MAX_EXPECTED_BYTES = 2 * 1024 * 1024 * 1024;
    const SAFE_CONTENT_TYPES = Object.freeze([
        "application/octet-stream",
        "binary/octet-stream",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]);

    function positiveInteger(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 0;
    }

    function sanitizeFilename(value) {
        const filename = String(value || "video.mp4")
            .replace(/[\\/:*?"<>|\x00-\x1f\x7f]+/g, "_")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 180);
        return filename || "video.mp4";
    }

    function isAllowedResearchUrl(value) {
        try {
            const url = new URL(String(value || ""));
            if (url.protocol === "https:") {
                return url.hostname === "365yg.com" || url.hostname.endsWith(".365yg.com");
            }
            return url.protocol === "http:"
                && (url.hostname === "127.0.0.1" || url.hostname === "localhost");
        } catch (error) {
            return false;
        }
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

    function createDescriptor(input) {
        const value = input && typeof input === "object" ? input : {};
        const id = String(value.id || "").toLowerCase();
        const expectedBytes = positiveInteger(value.expectedBytes);
        if (!/^[a-f0-9]{32}$/.test(id)) {
            throw new Error("INVALID_DESCRIPTOR_ID");
        }
        if (!isAllowedResearchUrl(value.url)) {
            throw new Error("INVALID_MEDIA_URL");
        }
        if (expectedBytes > MAX_EXPECTED_BYTES) {
            throw new Error("EXPECTED_SIZE_TOO_LARGE");
        }
        return Object.freeze({
            id,
            url: String(value.url),
            filename: sanitizeFilename(value.filename),
            expectedBytes,
        });
    }

    function downloadPath(id) {
        const normalized = String(id || "").toLowerCase();
        if (!/^[a-f0-9]{32}$/.test(normalized)) {
            throw new Error("INVALID_DESCRIPTOR_ID");
        }
        return `./${DOWNLOAD_PATH_SEGMENT}/${normalized}`;
    }

    function descriptorIdFromPath(pathname) {
        const value = String(pathname || "");
        const marker = `/${DOWNLOAD_PATH_SEGMENT}/`;
        const markerIndex = value.lastIndexOf(marker);
        if (markerIndex < 0) {
            return "";
        }
        const id = value.slice(markerIndex + marker.length).toLowerCase();
        return /^[a-f0-9]{32}$/.test(id) ? id : "";
    }

    return Object.freeze({
        DOWNLOAD_PATH_SEGMENT,
        MAX_EXPECTED_BYTES,
        createDescriptor,
        descriptorIdFromPath,
        downloadPath,
        isAllowedMediaContentType,
        isAllowedResearchUrl,
        normalizeContentType,
        positiveInteger,
        sanitizeFilename,
    });
}));
