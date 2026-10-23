"use strict";

// Frozen policy consumers from 7ab8296777e42c6c003dd34c13a42fec66743ccb.
// Lifecycle and unchanged formatting/telemetry helpers come from the existing harness.
module.exports = function legacyResponsePolicy(protocol) {
    const {createDescriptor, normalizeDownloadId, positiveInteger, sanitizeFilename,
        classifyResponseRejection, isAllowedMediaContentType, normalizeHttpStatus,
        classifyContentType, classifyInitialMediaFamily, classifyObservedFinalHost} = protocol;
    const DEFAULT_STREAM_DESCRIPTOR_TTL_MS = 120000;
    const MAX_STREAM_BYTES = 2 * 1024 * 1024 * 1024;

    function hostnameMatchesSuffix(hostname, suffix) {
        const normalizedHost = String(hostname || "").trim().toLowerCase();
        const normalizedSuffix = String(suffix || "").toLowerCase().replace(/^\./, "");
        if (!normalizedHost || normalizedHost.endsWith(".")) {
            return false;
        }
        return normalizedHost === normalizedSuffix
            || normalizedHost.endsWith(`.${normalizedSuffix}`);
    }

    function hostnameIsStrictSubdomain(hostname, suffix) {
        const normalizedHost = String(hostname || "").trim().toLowerCase();
        const normalizedSuffix = String(suffix || "").toLowerCase().replace(/^\./, "");
        return Boolean(
            normalizedHost
            && !normalizedHost.endsWith(".")
            && normalizedHost !== normalizedSuffix
            && normalizedHost.endsWith(`.${normalizedSuffix}`)
        );
    }

    function isIpLiteral(hostname) {
        const value = String(hostname || "").trim().toLowerCase();
        return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(value)
            || value.includes(":")
            || value.startsWith("[");
    }

    function sanitizeObservedMediaHostname(value, options) {
        const settings = options && typeof options === "object" ? options : {};
        const hostname = String(value || "").trim().toLowerCase();
        if (hostname === "unknown") {
            return "unknown";
        }
        if (
            !hostname
            || hostname.length > 255
            || hostname.endsWith(".")
            || /[:/@?#\\]/.test(hostname)
        ) {
            return "unknown";
        }
        if (isLoopbackHostname(hostname)) {
            return settings.allowLoopback ? hostname : "unknown";
        }
        if (isIpLiteral(hostname) || !/^[a-z0-9.-]+$/.test(hostname)) {
            return "unknown";
        }
        if (hostname.split(".").some((label) => (
            !label
            || label.length > 63
            || label.startsWith("-")
            || label.endsWith("-")
        ))) {
            return "unknown";
        }
        return hostname;
    }

    function isSecureProviderUrl(url) {
        return Boolean(
            url
            && url.protocol === "https:"
            && !url.username
            && !url.password
            && (!url.port || url.port === "443")
            && sanitizeObservedMediaHostname(url.hostname) !== "unknown"
        );
    }

    function isLoopbackHostname(hostname) {
        const normalizedHost = String(hostname || "").toLowerCase().replace(/\.$/, "");
        return normalizedHost === "localhost"
            || normalizedHost === "127.0.0.1"
            || normalizedHost === "[::1]";
    }

    function observedFinalUrl(value, options) {
        const settings = options && typeof options === "object" ? options : {};
        let url;
        try {
            url = new URL(String(value || ""));
        } catch (error) {
            return Object.freeze({url: null, hostname: "unknown", secure: false});
        }
        const loopback = Boolean(settings.allowLoopbackHttp)
            && (url.protocol === "http:" || url.protocol === "https:")
            && !url.username
            && !url.password
            && isLoopbackHostname(url.hostname);
        const secure = isSecureProviderUrl(url) || loopback;
        const hostname = secure
            ? sanitizeObservedMediaHostname(url.hostname, {allowLoopback: loopback})
            : "unknown";
        return Object.freeze({url, hostname, secure});
    }

    function normalizeResponsePolicy(value, options) {
        const settings = options || {};
        if (!value || typeof value !== "object") throw new Error("INVALID_RESPONSE_POLICY");
        const policy = {};
        for (const key of ["hosts", "suffixes", "redirectSuffixes"]) {
            const entries = value[key];
            if (!Array.isArray(entries) || entries.length > 16) throw new Error("INVALID_RESPONSE_POLICY");
            policy[key] = Object.freeze(entries.map(entry => {
                const host = String(entry || "").toLowerCase();
                const loopback = key === "hosts" && settings.allowLoopbackHttp && isLoopbackHostname(host);
                if (!loopback && (sanitizeObservedMediaHostname(host) !== host || !host.includes(".")
                    || /(?:^|\.)(?:localhost|local|internal|test|invalid)$/.test(host))) {
                    throw new Error("INVALID_RESPONSE_POLICY");
                }
                return host;
            }));
        }
        if (!policy.hosts.length && !policy.suffixes.length) throw new Error("INVALID_RESPONSE_POLICY");
        return Object.freeze(policy);
    }

    function transportUrlAllowed(value, policy, options, redirected) {
        const observed = observedFinalUrl(value, options);
        if (!observed.secure || !observed.url || observed.url.hash
            || /\.(?:m4s|m3u8|mpd)$/i.test(observed.url.pathname)) return false;
        const host = observed.hostname;
        return policy.hosts.includes(host)
            || policy.suffixes.some(suffix => hostnameMatchesSuffix(host, suffix))
            || (Boolean(redirected) && policy.redirectSuffixes.some(suffix => hostnameIsStrictSubdomain(host, suffix)));
    }

    function createWorkerDescriptor(input, options) {
        const value = input || {};
        const settings = options || {};
        // Retain the already-deployed message boundary for open old pages (G1).
        // New requests always use the same generic transport path below.
        if (!value.responsePolicy) return createDescriptor(value, settings);
        const responsePolicy = normalizeResponsePolicy(value.responsePolicy, settings);
        const downloadId = normalizeDownloadId(value.downloadId);
        if (!downloadId) throw new Error("INVALID_DOWNLOAD_ID");
        if (!transportUrlAllowed(value.url, responsePolicy, settings, false)) throw new Error("STREAMING_URL_UNSUPPORTED");
        const now = positiveInteger(settings.nowEpochMs) || Date.now();
        const createdAt = positiveInteger(value.createdAt) || now;
        const expiresAt = Math.min(createdAt + DEFAULT_STREAM_DESCRIPTOR_TTL_MS,
            positiveInteger(value.expiresAt) || Infinity);
        if (createdAt > now || expiresAt <= now) throw new Error("DESCRIPTOR_EXPIRED");
        const expectedBytes = positiveInteger(value.expectedBytes);
        if (expectedBytes > MAX_STREAM_BYTES) throw new Error("EXPECTED_SIZE_TOO_LARGE");
        return Object.freeze({downloadId, url: String(value.url), filename: sanitizeFilename(value.filename),
            expectedBytes, createdAt, expiresAt, maxBytes: MAX_STREAM_BYTES, responsePolicy,
            mediaHost: new URL(value.url).hostname});
    }

    function classifyTransportResponse(response, descriptor, options) {
        if (!descriptor.responsePolicy) return classifyResponseRejection(response, {...options, initialUrl: descriptor.url});
        const contentType = response.headers && response.headers.get("Content-Type");
        const observed = observedFinalUrl(response.url, options);
        const allowed = transportUrlAllowed(response.url, descriptor.responsePolicy, options, response.redirected);
        let responseRejectReason = "";
        if (!response.ok) responseRejectReason = "HTTP_STATUS_REJECTED";
        else if (!response.body || response.type === "opaque" || response.type === "opaqueredirect") responseRejectReason = "BODY_UNAVAILABLE";
        else if (!isAllowedMediaContentType(contentType)) responseRejectReason = "CONTENT_TYPE_REJECTED";
        else if (!allowed) responseRejectReason = "FINAL_HOST_REJECTED";
        return Object.freeze({rejected: Boolean(responseRejectReason), responseRejectReason,
            httpStatus: normalizeHttpStatus(response.status), contentTypeClass: classifyContentType(contentType),
            // Classification is diagnostic only; it never changes admission.
            initialMediaFamily: classifyInitialMediaFamily(descriptor.url, options),
            finalMediaHost: allowed ? observed.hostname : "unknown", observedFinalHost: observed.hostname,
            observedFinalHostClass: classifyObservedFinalHost(observed.hostname, options),
            redirected: Boolean(response.redirected)});
    }

    return {...protocol, createWorkerDescriptor, classifyTransportResponse};
};
