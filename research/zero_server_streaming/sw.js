"use strict";

importScripts("./protocol.js");

const protocol = self.AnimeImagesStreamingProtocol;
const descriptors = new Map();
const PROGRESS_INTERVAL_MS = 400;

function postToClients(message) {
    return self.clients.matchAll({type: "window", includeUncontrolled: true})
        .then((clients) => Promise.all(clients.map((client) => client.postMessage(message))));
}

function attachmentHeader(filename) {
    const encoded = encodeURIComponent(filename);
    return `attachment; filename="download.mp4"; filename*=UTF-8''${encoded}`;
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

self.addEventListener("install", (event) => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("message", (event) => {
    const message = event.data && typeof event.data === "object" ? event.data : {};
    const reply = event.ports && event.ports[0];
    if (message.type === "REGISTER_DESCRIPTOR") {
        try {
            const descriptor = protocol.createDescriptor(message.descriptor);
            descriptors.set(descriptor.id, descriptor);
            if (reply) {
                reply.postMessage({ok: true, id: descriptor.id});
            }
        } catch (error) {
            if (reply) {
                reply.postMessage({ok: false, code: String(error && error.message || "INVALID")});
            }
        }
        return;
    }
    if (message.type === "DELETE_DESCRIPTOR") {
        descriptors.delete(String(message.id || "").toLowerCase());
        if (reply) {
            reply.postMessage({ok: true});
        }
    }
});

self.addEventListener("fetch", (event) => {
    const requestUrl = new URL(event.request.url);
    const id = protocol.descriptorIdFromPath(requestUrl.pathname);
    if (!id || event.request.method !== "GET") {
        return;
    }

    event.respondWith((async () => {
        const descriptor = descriptors.get(id);
        descriptors.delete(id);
        if (!descriptor) {
            return errorResponse(404, "DESCRIPTOR_NOT_FOUND");
        }

        let upstream;
        try {
            upstream = await fetch(descriptor.url, {
                method: "GET",
                credentials: "omit",
                cache: "no-store",
                referrerPolicy: "no-referrer",
            });
        } catch (error) {
            await postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_FETCH_FAILED"});
            return errorResponse(502, "UPSTREAM_FETCH_FAILED");
        }

        const contentType = protocol.normalizeContentType(upstream.headers.get("Content-Type"));
        const contentLength = protocol.positiveInteger(upstream.headers.get("Content-Length"));
        if (!upstream.ok || !upstream.body || !protocol.isAllowedMediaContentType(contentType)) {
            await postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_MEDIA_INVALID"});
            return errorResponse(502, "UPSTREAM_MEDIA_INVALID");
        }
        if (descriptor.expectedBytes && contentLength && descriptor.expectedBytes !== contentLength) {
            await postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_SIZE_MISMATCH"});
            return errorResponse(502, "UPSTREAM_SIZE_MISMATCH");
        }
        if (contentLength > protocol.MAX_EXPECTED_BYTES) {
            await postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_SIZE_LIMIT_EXCEEDED"});
            return errorResponse(502, "UPSTREAM_SIZE_LIMIT_EXCEEDED");
        }

        let receivedBytes = 0;
        let lastProgressAt = 0;
        const startedAt = performance.now();
        const streamByteLimit = descriptor.expectedBytes || protocol.MAX_EXPECTED_BYTES;
        const counter = new TransformStream({
            transform(chunk, controller) {
                receivedBytes += chunk.byteLength || 0;
                if (receivedBytes > streamByteLimit) {
                    postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_SIZE_LIMIT_EXCEEDED"});
                    throw new Error("UPSTREAM_SIZE_LIMIT_EXCEEDED");
                }
                const now = performance.now();
                if (now - lastProgressAt >= PROGRESS_INTERVAL_MS) {
                    lastProgressAt = now;
                    postToClients({
                        type: "STREAM_PROGRESS",
                        id,
                        receivedBytes,
                        totalBytes: contentLength,
                        elapsedMs: Math.round(now - startedAt),
                    });
                }
                controller.enqueue(chunk);
            },
            flush() {
                if (descriptor.expectedBytes && receivedBytes !== descriptor.expectedBytes) {
                    postToClients({type: "STREAM_FAILED", id, code: "UPSTREAM_SIZE_MISMATCH"});
                    throw new Error("UPSTREAM_SIZE_MISMATCH");
                }
                postToClients({
                    type: "STREAM_COMPLETED",
                    id,
                    receivedBytes,
                    totalBytes: contentLength,
                    elapsedMs: Math.round(performance.now() - startedAt),
                });
            },
        });

        const headers = new Headers({
            "Cache-Control": "no-store",
            "Content-Disposition": attachmentHeader(descriptor.filename),
            "Content-Type": contentType || "application/octet-stream",
            "X-Content-Type-Options": "nosniff",
        });
        if (contentLength) {
            headers.set("Content-Length", String(contentLength));
        }
        await postToClients({
            type: "STREAM_STARTED",
            id,
            totalBytes: contentLength,
        });
        return new Response(upstream.body.pipeThrough(counter), {
            status: 200,
            headers,
        });
    })());
});
