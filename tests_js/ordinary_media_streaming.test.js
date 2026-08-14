"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const protocol = require("../static/js/ordinary_media_streaming_protocol.js");
const streaming = require("../static/js/ordinary_media_streaming.js");
const ordinary = require("../static/js/ordinary_media_download.js");

const streamingPath = path.join(__dirname, "../static/js/ordinary_media_streaming.js");
const streamingSource = fs.readFileSync(streamingPath, "utf8");
const workerPath = path.join(__dirname, "../static/js/ordinary_media_download_sw.js");
const workerSource = fs.readFileSync(workerPath, "utf8");
const singleSource = fs.readFileSync(
    path.join(__dirname, "../static/js/xhs_download.js"),
    "utf8",
);
const batchSource = fs.readFileSync(
    path.join(__dirname, "../static/js/batch_parse.js"),
    "utf8",
);

function deterministicCrypto() {
    let value = 0;
    return {
        getRandomValues(bytes) {
            for (let index = 0; index < bytes.length; index += 1) {
                bytes[index] = value % 256;
                value += 1;
            }
            return bytes;
        },
    };
}

class FakeMessageChannel {
    constructor() {
        this.port1 = {onmessage: null};
        this.port2 = {
            postMessage: (data) => queueMicrotask(() => {
                if (this.port1.onmessage) {
                    this.port1.onmessage({data});
                }
            }),
        };
    }
}

function safeStatus(overrides) {
    return {
        downloadId: "000102030405060708090a0b0c0d0e0f",
        filename: "video.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 10,
        receivedBytes: 0,
        totalBytes: 10,
        percent: 0,
        bytesPerSecond: 0,
        etaSeconds: null,
        state: "prepared",
        streamingStarted: false,
        startedAt: 0,
        updatedAt: Date.now(),
        ...overrides,
    };
}

function createClientHarness(options) {
    const settings = options || {};
    const listeners = new Map();
    const statuses = new Map();
    const calls = [];
    const serviceWorker = {
        controller: null,
        addEventListener(name, callback) { listeners.set(name, callback); },
        removeEventListener(name, callback) {
            if (listeners.get(name) === callback) {
                listeners.delete(name);
            }
        },
        async register(url, registrationOptions) {
            calls.push({type: "register", url, options: registrationOptions});
            if (settings.registrationError) {
                throw new Error("registration failed");
            }
            return {active: worker};
        },
        emit(status) {
            statuses.set(status.downloadId, status);
            const callback = listeners.get("message");
            if (callback) {
                callback({data: {type: `DOWNLOAD_${status.state.toUpperCase()}`, status}});
            }
        },
    };
    const worker = {
        postMessage(message, ports) {
            calls.push({type: "message", message});
            const reply = ports[0];
            if (message.type === "REGISTER_DOWNLOAD") {
                const status = safeStatus({
                    downloadId: message.descriptor.downloadId,
                    filename: message.descriptor.filename,
                    provider: message.descriptor.provider,
                    expectedBytes: message.descriptor.expectedBytes,
                    totalBytes: message.descriptor.expectedBytes,
                });
                statuses.set(status.downloadId, status);
                reply.postMessage({ok: true, status});
                return;
            }
            if (message.type === "GET_DOWNLOAD_STATUS") {
                const status = statuses.get(message.downloadId);
                reply.postMessage(status
                    ? {ok: true, status}
                    : {ok: false, code: "DOWNLOAD_NOT_FOUND"});
                return;
            }
            if (message.type === "LIST_ACTIVE_DOWNLOADS") {
                reply.postMessage({
                    ok: true,
                    statuses: Array.from(statuses.values()).filter((status) => (
                        status.state === "prepared" || status.state === "fetching"
                    )),
                });
                return;
            }
            if (message.type === "CANCEL_DOWNLOAD") {
                const current = statuses.get(message.downloadId);
                const status = {...current, state: "cancelled", streamingStarted: true};
                statuses.set(status.downloadId, status);
                reply.postMessage({ok: true, status});
                serviceWorker.emit(status);
                return;
            }
            if (message.type === "DELETE_DOWNLOAD") {
                const current = statuses.get(message.downloadId);
                if (current && (current.streamingStarted || settings.deleteRaceToStarted)) {
                    const started = settings.deleteRaceToStarted
                        ? {...current, state: "fetching", streamingStarted: true}
                        : current;
                    statuses.set(message.downloadId, started);
                    reply.postMessage({
                        ok: false,
                        code: "DOWNLOAD_ALREADY_STARTED",
                        status: started,
                    });
                    if (settings.deleteRaceToStarted) {
                        queueMicrotask(() => serviceWorker.emit({
                            ...started,
                            state: "failed",
                            errorCode: "UPSTREAM_STREAM_FAILED",
                        }));
                    }
                    return;
                }
                const status = current
                    ? {...current, state: "cancelled", errorCode: "DESCRIPTOR_DELETED"}
                    : null;
                if (status) {
                    statuses.set(status.downloadId, status);
                }
                reply.postMessage({ok: true, deleted: Boolean(status), status});
                return;
            }
            reply.postMessage({ok: false, code: "UNKNOWN_MESSAGE"});
        },
    };
    serviceWorker.controller = worker;
    const clicked = [];
    const documentObject = {
        body: {
            appendChild() {},
        },
        createElement() {
            return {
                href: "",
                style: {},
                click() {
                    clicked.push(this.href);
                    if (typeof settings.onNavigate === "function") {
                        settings.onNavigate({serviceWorker, statuses, href: this.href});
                    }
                },
                remove() {},
            };
        },
    };
    const client = streaming.createStreamingClient({
        navigatorObject: {serviceWorker},
        documentObject,
        cryptoObject: deterministicCrypto(),
        MessageChannelConstructor: FakeMessageChannel,
        locationObject: settings.locationObject,
        startTimeoutMs: settings.startTimeoutMs || 100,
        messageTimeoutMs: 100,
        statusPollMs: 20,
        telemetryReporter: settings.telemetryReporter,
    });
    return {calls, clicked, client, serviceWorker, statuses};
}

function ordinaryItem(overrides) {
    return {
        type: "video",
        url: "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
        filename: "video.mp4",
        provider: "tiantianjiexi",
        size_bytes: 10,
        size_is_estimate: false,
        browser_direct_fetch_safe: true,
        requires_merge: false,
        requires_cookie: false,
        ...overrides,
    };
}

function responseFor(body) {
    return new Response(body, {
        status: 200,
        headers: {
            "Content-Length": String(Buffer.byteLength(body)),
            "Content-Type": "video/mp4",
        },
    });
}

function deferred() {
    let resolve;
    const promise = new Promise((done) => {
        resolve = done;
    });
    return {promise, resolve};
}

function createWorkerRuntime(fetchImpl, options) {
    const settings = options || {};
    const workerOrigin = settings.origin || "https://download.example.test";
    const workerUrl = new URL(workerOrigin);
    const listeners = new Map();
    const clientMessages = [];
    const context = {
        AbortController,
        DOMException,
        Headers,
        Promise,
        ReadableStream,
        Response,
        URL,
        console,
        encodeURIComponent,
        fetch: fetchImpl,
        performance,
        setTimeout,
        clearTimeout,
        importScripts() {
            context.self.AnimeImagesOrdinaryMediaStreamingProtocol = protocol;
        },
        self: {
            location: {origin: workerUrl.origin, hostname: workerUrl.hostname},
            clients: {
                async matchAll() {
                    return [{postMessage(message) { clientMessages.push(message); }}];
                },
            },
            addEventListener(name, callback) { listeners.set(name, callback); },
        },
    };
    vm.runInNewContext(workerSource, context, {filename: workerPath});

    async function send(message) {
        return new Promise((resolve) => {
            const waitUntil = [];
            listeners.get("message")({
                data: message,
                ports: [{postMessage: resolve}],
                waitUntil(promise) { waitUntil.push(promise); },
            });
        });
    }

    function startFetch(downloadId) {
        let responsePromise;
        const waitUntil = [];
        listeners.get("fetch")({
            request: {
                method: "GET",
                url: `${workerUrl.origin}/__animeimages-stream-download/${downloadId}`,
            },
            respondWith(promise) { responsePromise = Promise.resolve(promise); },
            waitUntil(promise) { waitUntil.push(Promise.resolve(promise)); },
        });
        return {response: responsePromise, waitUntil};
    }

    return {clientMessages, send, startFetch};
}

function chunkedUpstream(chunks, options) {
    const settings = options || {};
    let index = 0;
    let cancelled = false;
    const stream = new ReadableStream({
        pull(controller) {
            if (index >= chunks.length) {
                controller.close();
                return;
            }
            controller.enqueue(Uint8Array.from(chunks[index]));
            index += 1;
        },
        cancel() {
            cancelled = true;
            if (typeof settings.onCancel === "function") {
                settings.onCancel();
            }
        },
    });
    const response = {
        ok: true,
        url: settings.url || "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
        headers: new Headers({
            "Content-Type": "video/mp4",
            "Content-Length": String(chunks.reduce((total, chunk) => total + chunk.length, 0)),
        }),
        body: stream,
    };
    return {response, wasCancelled: () => cancelled};
}

test("protocol validates opaque IDs, bounded media hosts, and memory-safe snapshots", () => {
    const descriptor = protocol.createDescriptor({
        downloadId: "0123456789abcdef0123456789abcdef",
        url: "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
        filename: "../demo:name?.mp4",
        provider: "TianTianJieXi",
        expectedBytes: 104857600,
    }, {nowEpochMs: 1000});
    assert.equal(descriptor.filename, ".._demo_name_.mp4");
    assert.equal(descriptor.provider, "tiantianjiexi");
    assert.equal(descriptor.expiresAt, 121000);
    assert.equal(protocol.isAllowedStreamingUrl("https://v11-default.365yg.com/a"), true);
    assert.equal(protocol.isAllowedStreamingUrl("https://evil365yg.com/a"), false);
    assert.equal(protocol.isAllowedStreamingUrl("http://v11-default.365yg.com/a"), false);
    assert.equal(protocol.isAllowedStreamingUrl("http://127.0.0.1:8766/media"), false);
    assert.equal(protocol.isAllowedStreamingUrl(
        "http://127.0.0.1:8766/media",
        {allowLoopbackHttp: true},
    ), true);
    assert.equal(protocol.isAllowedStreamingUrl(
        "http://192.168.1.10:8766/media",
        {allowLoopbackHttp: true},
    ), false);
    assert.equal(protocol.isLoopbackHostname("localhost"), true);
    assert.equal(protocol.isLoopbackHostname("127.0.0.1"), true);
    assert.equal(protocol.isLoopbackHostname("download.example.test"), false);
    assert.throws(() => protocol.createDescriptor({...descriptor, downloadId: "bad"}));
    assert.equal(
        protocol.descriptorIdFromPath(
            "/__animeimages-stream-download/0123456789abcdef0123456789abcdef",
        ),
        "0123456789abcdef0123456789abcdef",
    );
    assert.equal(
        protocol.descriptorIdFromPath(
            "/nested/__animeimages-stream-download/0123456789abcdef0123456789abcdef",
        ),
        "",
    );
    assert.equal(
        protocol.descriptorIdFromPath(
            "/__animeimages-stream-download/0123456789abcdef0123456789abcdef/extra",
        ),
        "",
    );

    const snapshot = protocol.safeSnapshot({...descriptor, state: "fetching", streamingStarted: true});
    const serialized = JSON.stringify(snapshot);
    assert.equal(snapshot.streamingStarted, true);
    assert.equal(snapshot.mediaHost, "v11-default.365yg.com");
    assert.doesNotMatch(serialized, /media\/video\.mp4|fixture|signed|token|url/i);
});

test("fetch failures use fixed factual classes without inspecting raw messages", () => {
    const signedUrl = "https://v11-default.365yg.com/path/video.mp4?token=super-secret";
    const typeError = new TypeError(`failed to fetch ${signedUrl}`);
    const abortError = new Error(`aborted ${signedUrl}`);
    abortError.name = "AbortError";
    const unknownError = new Error(`unknown ${signedUrl}`);

    assert.equal(protocol.classifyFetchFailure(typeError), "FETCH_REJECTED_TYPE_ERROR");
    assert.equal(protocol.classifyFetchFailure(abortError), "FETCH_ABORTED");
    assert.equal(protocol.classifyFetchFailure(unknownError), "FETCH_REJECTED_OTHER");
    assert.equal(protocol.classifyFetchFailure({
        get name() { throw new Error("unsafe getter"); },
    }), "FETCH_REJECTED_OTHER");
    assert.equal(protocol.sanitizeMediaHost(signedUrl), "v11-default.365yg.com");
    assert.equal(protocol.sanitizeMediaHost("https://attacker.example/path"), "unknown");
    assert.equal(protocol.sanitizeMediaHost("https://bad..365yg.com/path"), "unknown");
    assert.equal(protocol.sanitizeMediaHost("https://-bad.365yg.com/path"), "unknown");
});

test("loopback benchmark sources require a loopback page and worker", async () => {
    const input = {
        downloadId: "33333333333333333333333333333333",
        url: "http://127.0.0.1:8766/media?fixture=benchmark",
        filename: "benchmark.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 4,
    };
    assert.throws(() => protocol.createDescriptor(input), /STREAMING_URL_UNSUPPORTED/);

    const remoteWorker = createWorkerRuntime(async () => chunkedUpstream([[1, 2, 3, 4]], {
        url: input.url,
    }).response);
    const remoteRegistration = await remoteWorker.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: input,
    });
    assert.equal(remoteRegistration.ok, false);
    assert.equal(remoteRegistration.code, "STREAMING_URL_UNSUPPORTED");

    const localUpstream = chunkedUpstream([[1, 2], [3, 4]], {url: input.url});
    const localWorker = createWorkerRuntime(
        async () => localUpstream.response,
        {origin: "http://127.0.0.1:8000"},
    );
    const localRegistration = await localWorker.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: input,
    });
    assert.equal(localRegistration.ok, true);
    const response = await localWorker.startFetch(input.downloadId).response;
    assert.equal(response.status, 200);
    assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [1, 2, 3, 4]);
});

test("first-install control waits for controllerchange before downloads are launched", async () => {
    const listeners = new Map();
    const controller = {postMessage() {}};
    const serviceWorker = {
        controller: null,
        addEventListener(name, callback) { listeners.set(name, callback); },
        removeEventListener(name, callback) {
            if (listeners.get(name) === callback) {
                listeners.delete(name);
            }
        },
    };
    const waiting = streaming.waitForWorkerControl({serviceWorker}, {timeoutMs: 50});
    serviceWorker.controller = controller;
    listeners.get("controllerchange")();
    assert.equal(await waiting, controller);
    assert.equal(listeners.has("controllerchange"), false);
});

test("progress estimator reports real bytes, stable speed, ETA, and unknown totals", () => {
    const estimator = protocol.createProgressEstimator({speedWindowMs: 3000, minEtaSampleMs: 1000});
    assert.equal(estimator.update(0, 1000, 0).etaSeconds, null);
    const known = estimator.update(400, 1000, 1000);
    assert.equal(Math.round(known.bytesPerSecond), 400);
    assert.equal(Math.round(known.etaSeconds * 10) / 10, 1.5);
    const unknown = estimator.update(700, 0, 2000);
    assert.equal(unknown.etaSeconds, null);
});

test("client registers the root-scoped worker and launches one opaque download path", async () => {
    const harness = createClientHarness({
        onNavigate({serviceWorker, statuses}) {
            const status = [...statuses.values()][0];
            serviceWorker.emit({...status, state: "fetching", streamingStarted: true});
        },
    });
    const active = await harness.client.start(ordinaryItem());
    assert.equal(active.transport, "browser_streaming");
    assert.deepEqual(harness.calls[0], {
        type: "register",
        url: "/ordinary-media-download-sw.js",
        options: {scope: "/", updateViaCache: "none"},
    });
    assert.match(harness.clicked[0], /^\/__animeimages-stream-download\/[a-f0-9]{32}$/);
    assert.doesNotMatch(streamingSource, /link\.download\s*=/);

    const current = harness.statuses.get(active.downloadId);
    harness.serviceWorker.emit({
        ...current,
        state: "completed",
        receivedBytes: 10,
        percent: 100,
        streamingStarted: true,
    });
    const completed = await active.completion;
    assert.equal(completed.receivedBytes, 10);
});

test("estimated display size is not sent as an exact stream length", async () => {
    const harness = createClientHarness({
        onNavigate({serviceWorker, statuses}) {
            const status = [...statuses.values()][0];
            serviceWorker.emit({...status, state: "fetching", streamingStarted: true});
        },
    });
    const active = await harness.client.start(ordinaryItem({size_is_estimate: true}));
    const registrationCall = harness.calls.find((call) => (
        call.type === "message" && call.message.type === "REGISTER_DOWNLOAD"
    ));
    assert.equal(registrationCall.message.descriptor.expectedBytes, 0);
    await active.cancel();
    await assert.rejects(active.completion, /下载已取消/);
});

test("active downloads can be listed after refresh without parsing or exposing a URL", async () => {
    const harness = createClientHarness();
    const status = safeStatus({state: "fetching", streamingStarted: true, receivedBytes: 4});
    harness.statuses.set(status.downloadId, status);
    const active = await harness.client.listActiveDownloads();
    assert.equal(active.length, 1);
    assert.equal(active[0].receivedBytes, 4);
    assert.doesNotMatch(JSON.stringify(active), /365yg|fixture|signed|token/i);
    assert.equal(harness.clicked.length, 0);
});

test("refresh list responses cannot regress newer progress or terminal state", async () => {
    const harness = createClientHarness();
    const observed = [];
    const unsubscribe = harness.client.subscribeAll((status) => observed.push(status));
    await harness.client.registration();
    const latest = safeStatus({
        state: "fetching",
        streamingStarted: true,
        receivedBytes: 8,
        updatedAt: 3000,
    });
    harness.serviceWorker.emit(latest);
    harness.statuses.set(latest.downloadId, safeStatus({
        state: "fetching",
        streamingStarted: true,
        receivedBytes: 4,
        updatedAt: 2000,
    }));

    const listed = await harness.client.listActiveDownloads();
    assert.equal(listed[0].receivedBytes, 8);
    assert.deepEqual(observed.map((status) => status.receivedBytes), [8]);

    const completed = safeStatus({
        state: "completed",
        streamingStarted: true,
        receivedBytes: 10,
        updatedAt: 4000,
    });
    harness.serviceWorker.emit(completed);
    harness.statuses.set(completed.downloadId, safeStatus({
        state: "fetching",
        streamingStarted: true,
        receivedBytes: 9,
        updatedAt: 5000,
    }));
    const afterCompletion = await harness.client.listActiveDownloads();
    assert.equal(afterCompletion[0].state, "completed");
    unsubscribe();
});

test("pre-start streaming failure falls back to Blob exactly once", async () => {
    let blobHandoffs = 0;
    let fallbacks = 0;
    const result = await ordinary.download(ordinaryItem({size_bytes: 5}), {
        streamingClient: {capabilityAvailable() { return false; }},
        fetchImpl: async () => responseFor("12345"),
        async handoffBlob() { blobHandoffs += 1; },
        onTransportFallback() { fallbacks += 1; },
    });
    assert.equal(result.transport, "browser_blob");
    assert.equal(blobHandoffs, 1);
    assert.equal(fallbacks, 1);
});

test("worker registration failure falls back to Blob exactly once", async () => {
    const harness = createClientHarness({registrationError: true});
    let mediaFetches = 0;
    let blobHandoffs = 0;
    const result = await ordinary.download(ordinaryItem({size_bytes: 5}), {
        streamingClient: harness.client,
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345");
        },
        async handoffBlob() { blobHandoffs += 1; },
    });
    assert.equal(result.transport, "browser_blob");
    assert.equal(mediaFetches, 1);
    assert.equal(blobHandoffs, 1);
    assert.equal(harness.clicked.length, 0);
});

test("only explicit pre-start capability failures permit Blob fallback", async () => {
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_REGISTRATION_FAILED",
        "registration failed",
    )), true);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_START_TIMEOUT",
        "start timed out after the prepared descriptor was deleted",
    )), true);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_URL_UNSUPPORTED",
        "unsafe URL",
    )), false);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_START_STATE_UNKNOWN",
        "start state unknown",
        {streamingStarted: true},
    )), false);
});

test("a confirmed unstarted descriptor timeout falls back to Blob once", async () => {
    const harness = createClientHarness({startTimeoutMs: 5});
    let mediaFetches = 0;
    let blobHandoffs = 0;
    const result = await ordinary.download(ordinaryItem({size_bytes: 5}), {
        streamingClient: harness.client,
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345");
        },
        async handoffBlob() { blobHandoffs += 1; },
    });
    assert.equal(result.transport, "browser_blob");
    assert.equal(harness.clicked.length, 1);
    assert.equal(mediaFetches, 1);
    assert.equal(blobHandoffs, 1);
});

test("navigation start race is treated as started and never falls back to Blob", async () => {
    const harness = createClientHarness({
        deleteRaceToStarted: true,
        startTimeoutMs: 5,
    });
    let mediaFetches = 0;
    let blobHandoffs = 0;
    await assert.rejects(
        ordinary.download(ordinaryItem(), {
            streamingClient: harness.client,
            fetchImpl: async () => {
                mediaFetches += 1;
                return responseFor("12345");
            },
            async handoffBlob() { blobHandoffs += 1; },
        }),
        (error) => error && error.streamingStarted === true,
    );
    assert.equal(harness.clicked.length, 1);
    assert.equal(mediaFetches, 0);
    assert.equal(blobHandoffs, 0);
});

test("post-start streaming failure never starts a Blob or second media fetch", async () => {
    let mediaFetches = 0;
    let blobHandoffs = 0;
    const error = streaming.streamingError(
        "UPSTREAM_STREAM_FAILED",
        "stream failed",
        {streamingStarted: true},
    );
    await assert.rejects(
        ordinary.download(ordinaryItem(), {
            streamingClient: {
                capabilityAvailable() { return true; },
                async start() { throw error; },
            },
            fetchImpl: async () => {
                mediaFetches += 1;
                return responseFor("12345");
            },
            async handoffBlob() { blobHandoffs += 1; },
        }),
        /stream failed/,
    );
    assert.equal(mediaFetches, 0);
    assert.equal(blobHandoffs, 0);
});

test("worker streams exact bytes without Blob, persistence, or AnimeImages relay", async () => {
    const upstream = chunkedUpstream([[1, 2], [3, 4, 5]]);
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "0123456789abcdef0123456789abcdef";
    const registered = await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
            filename: "demo.mp4",
            provider: "tiantianjiexi",
            expectedBytes: 5,
        },
    });
    assert.equal(registered.ok, true);
    const fetchRun = runtime.startFetch(downloadId);
    const response = await fetchRun.response;
    assert.equal(response.status, 200);
    assert.match(response.headers.get("Content-Disposition"), /attachment/);
    const bytes = new Uint8Array(await response.arrayBuffer());
    assert.deepEqual([...bytes], [1, 2, 3, 4, 5]);
    const finalStatus = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(finalStatus.status.state, "completed");
    assert.equal(finalStatus.status.receivedBytes, 5);
    assert.equal(upstream.wasCancelled(), false);

    const productSource = `${workerSource}\n${singleSource}\n${batchSource}`;
    assert.doesNotMatch(workerSource, /\.blob\s*\(|\.arrayBuffer\s*\(|new Blob|indexedDB|caches\./i);
    assert.doesNotMatch(workerSource, /localStorage|sessionStorage/i);
    assert.doesNotMatch(workerSource, /skipWaiting\s*\(/);
    assert.doesNotMatch(productSource, /ordinary_media.*\/media\/download-(prepare|stream|status)/i);
});

test("worker reports a URL-free before-response TypeError classification", async () => {
    const signedUrl = "https://v11-default.365yg.com/path/video.mp4?token=super-secret";
    const runtime = createWorkerRuntime(async () => {
        throw new TypeError(`Failed to fetch ${signedUrl}`);
    });
    const downloadId = "abababababababababababababababab";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId,
        url: signedUrl,
        filename: "failed.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 100,
    }});

    const response = await runtime.startFetch(downloadId).response;
    assert.equal(response.status, 502);
    const current = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(current.status.errorCode, "UPSTREAM_FETCH_FAILED");
    assert.equal(current.status.failureClass, "FETCH_REJECTED_TYPE_ERROR");
    assert.equal(current.status.failureStage, "FETCH_REJECTED_BEFORE_RESPONSE");
    assert.equal(current.status.mediaHost, "v11-default.365yg.com");
    assert.ok(current.status.fetchRejectMs >= 0);

    const serialized = JSON.stringify({status: current.status, messages: runtime.clientMessages});
    assert.doesNotMatch(serialized, /path\/video\.mp4|token|super-secret|https:\/\//i);
});

test("worker keeps AbortError factual and does not label it as CORS", async () => {
    const runtime = createWorkerRuntime(async () => {
        const error = new Error("sensitive text must not be copied");
        error.name = "AbortError";
        throw error;
    });
    const downloadId = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId,
        url: "https://v11-default.365yg.com/media/abort.mp4?fixture=signed",
        filename: "abort.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 10,
    }});
    await runtime.startFetch(downloadId).response;
    const current = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(current.status.failureClass, "FETCH_ABORTED");
    assert.doesNotMatch(JSON.stringify(current), /CORS|sensitive text|fixture|signed/i);
});

test("worker distinguishes invalid responses from stream failures after bytes", async () => {
    const invalidRuntime = createWorkerRuntime(async () => ({
        ok: false,
        status: 403,
        url: "https://v11-default.365yg.com/media/rejected.mp4?fixture=signed",
        headers: new Headers({"Content-Type": "text/html"}),
        body: new ReadableStream({start(controller) { controller.close(); }}),
    }));
    const invalidId = "dededededededededededededededede";
    await invalidRuntime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId: invalidId,
        url: "https://v11-default.365yg.com/media/rejected.mp4?fixture=signed",
        filename: "rejected.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 0,
    }});
    await invalidRuntime.startFetch(invalidId).response;
    const invalid = await invalidRuntime.send({type: "GET_DOWNLOAD_STATUS", downloadId: invalidId});
    assert.equal(invalid.status.errorCode, "UPSTREAM_MEDIA_INVALID");
    assert.equal(invalid.status.failureStage, "HTTP_RESPONSE_REJECTED");

    let pullCount = 0;
    const failingBody = new ReadableStream({
        pull(controller) {
            pullCount += 1;
            if (pullCount === 1) {
                controller.enqueue(Uint8Array.from([1, 2, 3]));
                return;
            }
            controller.error(new TypeError("stream failed at a signed URL"));
        },
    });
    const streamRuntime = createWorkerRuntime(async () => ({
        ok: true,
        status: 200,
        url: "https://v11-default.365yg.com/media/stream.mp4?fixture=signed",
        headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "6"}),
        body: failingBody,
    }));
    const streamId = "efefefefefefefefefefefefefefefef";
    await streamRuntime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId: streamId,
        url: "https://v11-default.365yg.com/media/stream.mp4?fixture=signed",
        filename: "stream.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 6,
    }});
    const streamResponse = await streamRuntime.startFetch(streamId).response;
    await assert.rejects(streamResponse.arrayBuffer());
    const failed = await streamRuntime.send({type: "GET_DOWNLOAD_STATUS", downloadId: streamId});
    assert.equal(failed.status.errorCode, "UPSTREAM_STREAM_FAILED");
    assert.equal(failed.status.failureStage, "STREAM_BODY_FAILURE");
    assert.equal(failed.status.receivedBytes, 3);
    assert.ok(failed.status.timeToFirstChunkMs >= 0);
});

test("terminal failure telemetry is sent once and remains best-effort", async () => {
    const calls = [];
    const reporter = streaming.createTerminalTelemetryReporter({
        csrfToken: "fake-csrf-token",
        cryptoObject: deterministicCrypto(),
        fetchImpl(endpoint, options) {
            calls.push({endpoint, options});
            return Promise.reject(new Error("telemetry unavailable"));
        },
    });
    const status = safeStatus({
        state: "failed",
        errorCode: "UPSTREAM_FETCH_FAILED",
        failureClass: "FETCH_REJECTED_TYPE_ERROR",
        failureStage: "FETCH_REJECTED_BEFORE_RESPONSE",
        mediaHost: "v11-default.365yg.com",
        streamingStarted: true,
    });
    const environment = {
        navigatorOnline: true,
        pageVisibility: "visible",
        workerControllerState: "activated",
        workerActiveState: "activated",
        browserFamily: "chromium",
    };
    assert.equal(reporter.report(status, environment), true);
    assert.equal(reporter.report(status, environment), false);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(calls.length, 1);
    assert.equal(calls[0].endpoint, "/media/browser-download-telemetry/");
    assert.equal(calls[0].options.keepalive, true);
    const payload = JSON.parse(calls[0].options.body);
    assert.equal(payload.failure_class, "FETCH_REJECTED_TYPE_ERROR");
    assert.equal(payload.media_host, "v11-default.365yg.com");
    assert.deepEqual(Object.keys(payload).sort(), [
        "browser_family",
        "download_id",
        "error_code",
        "expected_bytes",
        "failure_class",
        "failure_stage",
        "fetch_reject_ms",
        "media_host",
        "navigator_online",
        "page_visibility",
        "protocol_version",
        "provider",
        "received_bytes",
        "registration_to_fetch_ms",
        "schema_version",
        "state",
        "stream_duration_ms",
        "streaming_started",
        "telemetry_id",
        "time_to_first_chunk_ms",
        "total_bytes",
        "transport",
        "worker_active_state",
        "worker_controller_state",
        "worker_installing",
        "worker_waiting",
    ]);
    assert.doesNotMatch(JSON.stringify(payload), /media\/video\.mp4|token|signed|https:\/\//i);
});

test("a throwing telemetry reporter cannot change post-start failure semantics", async () => {
    const harness = createClientHarness({
        telemetryReporter: {report() { throw new Error("telemetry failed"); }},
        onNavigate({serviceWorker, statuses}) {
            const current = [...statuses.values()][0];
            serviceWorker.emit({...current, state: "fetching", streamingStarted: true});
            queueMicrotask(() => serviceWorker.emit({
                ...current,
                state: "failed",
                streamingStarted: true,
                errorCode: "UPSTREAM_FETCH_FAILED",
                failureClass: "FETCH_REJECTED_TYPE_ERROR",
                failureStage: "FETCH_REJECTED_BEFORE_RESPONSE",
            }));
        },
    });
    const active = await harness.client.start(ordinaryItem());
    await assert.rejects(active.completion, (error) => (
        error.errorCode === "UPSTREAM_FETCH_FAILED" && error.streamingStarted === true
    ));
});

test("downstream Download Manager cancel aborts the upstream stream and cleans state", async () => {
    let upstreamCancelled = false;
    const upstream = chunkedUpstream([[1, 2], [3, 4], [5, 6]], {
        onCancel() { upstreamCancelled = true; },
    });
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "fedcba9876543210fedcba9876543210";
    await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
            filename: "cancel.mp4",
            provider: "tiantianjiexi",
            expectedBytes: 6,
        },
    });
    const response = await runtime.startFetch(downloadId).response;
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.equal(first.value.byteLength, 2);
    await reader.cancel("browser manager cancelled");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const status = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(status.status.state, "cancelled");
    assert.equal(status.status.errorCode, "DOWNLOAD_MANAGER_CANCELLED");
    assert.equal(upstreamCancelled, true);
});

test("site cancel racing upstream response acquisition still cancels the body", async () => {
    const upstream = chunkedUpstream([[1, 2], [3, 4]]);
    const fetchResult = deferred();
    const runtime = createWorkerRuntime(() => fetchResult.promise);
    const downloadId = "abcdefabcdefabcdefabcdefabcdefab";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId,
        url: "https://v11-default.365yg.com/media/race.mp4?fixture=1",
        filename: "race.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 4,
    }});
    const fetchRun = runtime.startFetch(downloadId);
    const cancelled = await runtime.send({type: "CANCEL_DOWNLOAD", downloadId});
    assert.equal(cancelled.status.state, "cancelled");
    fetchResult.resolve(upstream.response);
    const response = await fetchRun.response;
    assert.equal(response.status, 499);
    assert.equal(upstream.wasCancelled(), true);
});

test("two worker downloads keep IDs, filenames, bytes, and cancellation isolated", async () => {
    const sources = [
        chunkedUpstream([[1], [2]]),
        chunkedUpstream([[3], [4], [5]]),
    ];
    let fetchIndex = 0;
    const runtime = createWorkerRuntime(async () => sources[fetchIndex++].response);
    const firstId = "11111111111111111111111111111111";
    const secondId = "22222222222222222222222222222222";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId: firstId,
        url: "https://v11-default.365yg.com/media/first.mp4?fixture=1",
        filename: "first.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 2,
    }});
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId: secondId,
        url: "https://v11-default.365yg.com/media/second.mp4?fixture=2",
        filename: "second.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 3,
    }});
    const firstResponse = await runtime.startFetch(firstId).response;
    const secondResponse = await runtime.startFetch(secondId).response;
    await firstResponse.body.cancel();
    const secondBytes = new Uint8Array(await secondResponse.arrayBuffer());
    assert.deepEqual([...secondBytes], [3, 4, 5]);
    const firstStatus = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId: firstId});
    const secondStatus = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId: secondId});
    assert.equal(firstStatus.status.state, "cancelled");
    assert.equal(secondStatus.status.state, "completed");
    assert.equal(secondStatus.status.filename, "second.mp4");
});

test("Single and Batch use one shared ordinary download primitive", () => {
    assert.match(singleSource, /ordinaryMediaDownloadApi\.download\(item/);
    assert.match(batchSource, /ordinaryMediaDownloadApi\.download\(item/);
    assert.doesNotMatch(batchSource, /response\.blob\s*\(|new Blob\(chunks/);
});
