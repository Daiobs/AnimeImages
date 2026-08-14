"use strict";

importScripts("/static/js/ordinary_media_streaming_protocol.js");

const protocol = self.AnimeImagesOrdinaryMediaStreamingProtocol;
const downloads = new Map();
const PROGRESS_INTERVAL_MS = 350;
const TERMINAL_RETENTION_MS = 60000;
const ALLOW_LOOPBACK_HTTP = protocol.isLoopbackHostname(self.location.hostname);

function deferred() {
    let resolve;
    const promise = new Promise((done) => {
        resolve = done;
    });
    return {promise, resolve};
}

function attachmentHeader(filename) {
    return `attachment; filename="download.mp4"; filename*=UTF-8''${encodeURIComponent(filename)}`;
}

function errorResponse(status, code) {
    return new Response(code, {
        status,
        headers: {
            "Cache-Control": "no-store",
            "Content-Type": "text/plain; charset=utf-8",
            "X-Content-Type-Options": "nosniff",
        },
    });
}

function safeStatus(state) {
    return protocol.safeSnapshot(state);
}

async function postToClients(type, state) {
    const message = {type, status: safeStatus(state)};
    const clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    clients.forEach((client) => client.postMessage(message));
}

function settle(state) {
    if (state.settled) {
        state.settled.resolve();
        state.settled = null;
    }
}

function clearSensitiveState(state) {
    if (state.expiryTimer !== null) {
        clearTimeout(state.expiryTimer);
        state.expiryTimer = null;
    }
    state.url = "";
    state.abortController = null;
    state.upstreamReader = null;
    state.downstreamController = null;
}

function markTerminal(state, nextState, errorCode, options) {
    const settings = options && typeof options === "object" ? options : {};
    if (protocol.TERMINAL_STATES.includes(state.state)) {
        return Promise.resolve();
    }
    state.state = nextState;
    state.errorCode = String(errorCode || "");
    state.failureClass = protocol.normalizeFailureClass(
        settings.failureClass || state.failureClass,
    );
    state.failureStage = protocol.normalizeFailureStage(
        settings.failureStage || state.failureStage,
    );
    state.updatedAt = Date.now();
    state.terminalAt = state.updatedAt;
    const notification = postToClients(`DOWNLOAD_${nextState.toUpperCase()}`, state)
        .catch(() => {});
    if (settings.deferCleanup) {
        return notification;
    }
    clearSensitiveState(state);
    return notification.finally(() => {
        settle(state);
    });
}

function pruneDownloads() {
    const now = Date.now();
    for (const [downloadId, state] of downloads.entries()) {
        if (
            protocol.TERMINAL_STATES.includes(state.state)
            && now - state.terminalAt >= TERMINAL_RETENTION_MS
        ) {
            downloads.delete(downloadId);
        } else if (state.state === "prepared" && now > state.expiresAt) {
            markTerminal(state, "failed", "DESCRIPTOR_EXPIRED", {
                failureStage: "DESCRIPTOR_FAILURE",
            });
        }
    }
}

async function cancelDownload(state, code) {
    if (!state || protocol.TERMINAL_STATES.includes(state.state)) {
        return;
    }
    const abortController = state.abortController;
    const upstreamReader = state.upstreamReader;
    const downstreamController = state.downstreamController;
    const notification = markTerminal(
        state,
        "cancelled",
        code || "DOWNLOAD_CANCELLED",
        {
            deferCleanup: true,
            failureStage: code === "USER_CANCELLED"
                ? "USER_CANCELLED"
                : (code === "DOWNLOAD_MANAGER_CANCELLED"
                    ? "DOWNLOAD_MANAGER_CANCELLED"
                    : "DESCRIPTOR_FAILURE"),
        },
    );
    try {
        if (abortController) {
            abortController.abort();
        }
        if (upstreamReader && typeof upstreamReader.cancel === "function") {
            await upstreamReader.cancel(code || "DOWNLOAD_CANCELLED");
        }
    } catch (error) {
        // Abort already closes some upstream readers.
    } finally {
        if (downstreamController) {
            try {
                downstreamController.error(new DOMException("Download cancelled", "AbortError"));
            } catch (error) {
                // The browser may already have closed the downstream stream.
            }
        }
        await notification;
        clearSensitiveState(state);
        settle(state);
    }
}

function createDownloadState(descriptor) {
    const state = {
        ...descriptor,
        totalBytes: descriptor.expectedBytes,
        receivedBytes: 0,
        bytesPerSecond: 0,
        etaSeconds: null,
        state: "prepared",
        startedAt: 0,
        updatedAt: Date.now(),
        errorCode: "",
        failureClass: "NOT_APPLICABLE",
        failureStage: "",
        streamingStarted: false,
        terminalAt: 0,
        registeredAt: Date.now(),
        fetchStartedAt: 0,
        fetchFailedAt: 0,
        firstResponseAt: 0,
        firstChunkAt: 0,
        abortController: null,
        upstreamReader: null,
        downstreamController: null,
        expiryTimer: null,
        settled: deferred(),
        progressEstimator: protocol.createProgressEstimator(),
        lastProgressAt: 0,
    };
    state.expiryTimer = setTimeout(() => {
        if (state.state === "prepared") {
            markTerminal(state, "failed", "DESCRIPTOR_EXPIRED", {
                failureStage: "DESCRIPTOR_FAILURE",
            });
        }
    }, Math.max(0, descriptor.expiresAt - Date.now()));
    return state;
}

function reply(port, payload) {
    if (port) {
        port.postMessage(payload);
    }
}

self.addEventListener("install", () => {
    // Active workers finish existing downloads. New workers wait for normal activation.
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("message", (event) => {
    pruneDownloads();
    const message = event.data && typeof event.data === "object" ? event.data : {};
    const port = event.ports && event.ports[0];
    if (message.type === "REGISTER_DOWNLOAD") {
        try {
            const descriptor = protocol.createDescriptor(message.descriptor, {
                allowLoopbackHttp: ALLOW_LOOPBACK_HTTP,
            });
            if (downloads.has(descriptor.downloadId)) {
                throw new Error("DOWNLOAD_ID_ALREADY_EXISTS");
            }
            const state = createDownloadState(descriptor);
            downloads.set(descriptor.downloadId, state);
            reply(port, {ok: true, status: safeStatus(state)});
        } catch (error) {
            reply(port, {ok: false, code: String(error && error.message || "INVALID_DESCRIPTOR")});
        }
        return;
    }
    if (message.type === "DELETE_DOWNLOAD") {
        const downloadId = protocol.normalizeDownloadId(message.downloadId);
        const state = downloads.get(downloadId);
        if (state && state.state === "prepared") {
            event.waitUntil(markTerminal(state, "cancelled", "DESCRIPTOR_DELETED").then(() => {
                reply(port, {ok: true, deleted: true, status: safeStatus(state)});
            }));
            return;
        }
        if (state && state.streamingStarted) {
            reply(port, {
                ok: false,
                code: "DOWNLOAD_ALREADY_STARTED",
                status: safeStatus(state),
            });
            return;
        }
        reply(port, {
            ok: true,
            deleted: false,
            status: state ? safeStatus(state) : null,
        });
        return;
    }
    if (message.type === "LIST_ACTIVE_DOWNLOADS") {
        const statuses = Array.from(downloads.values())
            .filter((state) => !protocol.TERMINAL_STATES.includes(state.state))
            .map(safeStatus);
        reply(port, {ok: true, protocolVersion: protocol.PROTOCOL_VERSION, statuses});
        return;
    }
    if (message.type === "GET_DOWNLOAD_STATUS") {
        const state = downloads.get(protocol.normalizeDownloadId(message.downloadId));
        reply(port, state
            ? {ok: true, status: safeStatus(state)}
            : {ok: false, code: "DOWNLOAD_NOT_FOUND"});
        return;
    }
    if (message.type === "CANCEL_DOWNLOAD") {
        const state = downloads.get(protocol.normalizeDownloadId(message.downloadId));
        if (!state) {
            reply(port, {ok: false, code: "DOWNLOAD_NOT_FOUND"});
            return;
        }
        event.waitUntil(cancelDownload(state, "USER_CANCELLED").then(() => {
            reply(port, {ok: true, status: safeStatus(state)});
        }));
        return;
    }
    reply(port, {ok: false, code: "UNKNOWN_MESSAGE"});
});

async function streamDownload(state) {
    if (Date.now() > state.expiresAt) {
        markTerminal(state, "failed", "DESCRIPTOR_EXPIRED", {
            failureStage: "DESCRIPTOR_FAILURE",
        });
        return errorResponse(410, "DESCRIPTOR_EXPIRED");
    }
    state.state = "fetching";
    state.streamingStarted = true;
    state.startedAt = Date.now();
    state.fetchStartedAt = state.startedAt;
    state.updatedAt = state.startedAt;
    state.abortController = new AbortController();
    await postToClients("DOWNLOAD_FETCH_STARTED", state);

    let upstream;
    try {
        upstream = await fetch(state.url, {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
            referrerPolicy: "no-referrer",
            signal: state.abortController.signal,
        });
    } catch (error) {
        if (state.state === "cancelled") {
            return errorResponse(499, "DOWNLOAD_CANCELLED");
        }
        state.fetchFailedAt = Date.now();
        markTerminal(state, "failed", "UPSTREAM_FETCH_FAILED", {
            failureClass: protocol.classifyFetchFailure(error),
            failureStage: "FETCH_REJECTED_BEFORE_RESPONSE",
        });
        return errorResponse(502, "UPSTREAM_FETCH_FAILED");
    }
    if (state.state === "cancelled") {
        if (upstream.body && typeof upstream.body.cancel === "function") {
            await upstream.body.cancel().catch(() => {});
        }
        return errorResponse(499, "DOWNLOAD_CANCELLED");
    }
    state.firstResponseAt = Date.now();
    state.url = "";

    const contentType = protocol.normalizeContentType(upstream.headers.get("Content-Type"));
    const contentLength = protocol.positiveInteger(upstream.headers.get("Content-Length"));
    if (
        !upstream.ok
        || !upstream.body
        || !protocol.isAllowedMediaContentType(contentType)
        || !protocol.isAllowedStreamingUrl(upstream.url, {
            allowLoopbackHttp: ALLOW_LOOPBACK_HTTP,
        })
    ) {
        if (upstream.body && typeof upstream.body.cancel === "function") {
            await upstream.body.cancel().catch(() => {});
        }
        markTerminal(state, "failed", "UPSTREAM_MEDIA_INVALID", {
            failureStage: "HTTP_RESPONSE_REJECTED",
        });
        return errorResponse(502, "UPSTREAM_MEDIA_INVALID");
    }
    if (contentLength > protocol.MAX_STREAM_BYTES) {
        await upstream.body.cancel().catch(() => {});
        markTerminal(state, "failed", "UPSTREAM_SIZE_LIMIT_EXCEEDED", {
            failureStage: "SIZE_VALIDATION_FAILURE",
        });
        return errorResponse(502, "UPSTREAM_SIZE_LIMIT_EXCEEDED");
    }
    if (state.expectedBytes && contentLength && state.expectedBytes !== contentLength) {
        await upstream.body.cancel().catch(() => {});
        markTerminal(state, "failed", "UPSTREAM_SIZE_MISMATCH", {
            failureStage: "SIZE_VALIDATION_FAILURE",
        });
        return errorResponse(502, "UPSTREAM_SIZE_MISMATCH");
    }

    state.totalBytes = contentLength || state.expectedBytes;
    state.upstreamReader = upstream.body.getReader();
    const byteLimit = state.totalBytes || protocol.MAX_STREAM_BYTES;

    const downstream = new ReadableStream({
        start(controller) {
            state.downstreamController = controller;
        },
        async pull(controller) {
            try {
                const {done, value} = await state.upstreamReader.read();
                if (state.state === "cancelled") {
                    return;
                }
                if (done) {
                    if (state.totalBytes && state.receivedBytes !== state.totalBytes) {
                        throw new Error("UPSTREAM_SIZE_MISMATCH");
                    }
                    const metrics = state.progressEstimator.update(
                        state.receivedBytes,
                        state.totalBytes,
                        performance.now(),
                    );
                    state.bytesPerSecond = metrics.bytesPerSecond;
                    state.etaSeconds = null;
                    controller.close();
                    await markTerminal(state, "completed", "");
                    return;
                }
                const chunkBytes = value && value.byteLength ? value.byteLength : 0;
                if (chunkBytes && !state.firstChunkAt) {
                    state.firstChunkAt = Date.now();
                }
                state.receivedBytes += chunkBytes;
                if (state.receivedBytes > byteLimit) {
                    throw new Error("UPSTREAM_SIZE_LIMIT_EXCEEDED");
                }
                state.updatedAt = Date.now();
                const metrics = state.progressEstimator.update(
                    state.receivedBytes,
                    state.totalBytes,
                    performance.now(),
                );
                state.bytesPerSecond = metrics.bytesPerSecond;
                state.etaSeconds = metrics.etaSeconds;
                const now = performance.now();
                if (now - state.lastProgressAt >= PROGRESS_INTERVAL_MS) {
                    state.lastProgressAt = now;
                    postToClients("DOWNLOAD_PROGRESS", state);
                }
                controller.enqueue(value);
            } catch (error) {
                if (state.state === "cancelled") {
                    return;
                }
                const code = String(error && error.message || "UPSTREAM_STREAM_FAILED");
                if (state.abortController) {
                    state.abortController.abort();
                }
                const terminalCode = (
                    code === "UPSTREAM_SIZE_MISMATCH"
                    || code === "UPSTREAM_SIZE_LIMIT_EXCEEDED"
                ) ? code : "UPSTREAM_STREAM_FAILED";
                markTerminal(state, "failed", terminalCode, {
                    failureStage: terminalCode === "UPSTREAM_STREAM_FAILED"
                        ? "STREAM_BODY_FAILURE"
                        : "SIZE_VALIDATION_FAILURE",
                });
                controller.error(new Error(state.errorCode));
            }
        },
        async cancel() {
            await cancelDownload(state, "DOWNLOAD_MANAGER_CANCELLED");
        },
    });

    state.updatedAt = Date.now();
    await postToClients("DOWNLOAD_STARTED", state);
    const headers = new Headers({
        "Cache-Control": "no-store",
        "Content-Disposition": attachmentHeader(state.filename),
        "Content-Type": contentType || "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
    });
    if (contentLength) {
        headers.set("Content-Length", String(contentLength));
    }
    return new Response(downstream, {status: 200, headers});
}

self.addEventListener("fetch", (event) => {
    const requestUrl = new URL(event.request.url);
    if (requestUrl.origin !== self.location.origin) {
        return;
    }
    const downloadId = protocol.descriptorIdFromPath(requestUrl.pathname);
    if (!downloadId || event.request.method !== "GET") {
        return;
    }
    const state = downloads.get(downloadId);
    if (!state || state.state !== "prepared") {
        event.respondWith(Promise.resolve(errorResponse(404, "DOWNLOAD_NOT_FOUND")));
        return;
    }
    const lifecyclePromise = state.settled.promise;
    event.respondWith(streamDownload(state));
    event.waitUntil(lifecyclePromise);
});
