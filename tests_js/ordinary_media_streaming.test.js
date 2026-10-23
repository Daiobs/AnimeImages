"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const protocol = require("../static/js/ordinary_media_streaming_protocol.js");
const streaming = require("../static/js/ordinary_media_streaming.js");
const ordinary = require("../static/js/ordinary_media_download.js");
const picker = require("../static/js/ytdlp_result_picker.js");
const {Node} = require("./helpers/lab_dom.js");

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
        protocolVersion: protocol.PROTOCOL_VERSION,
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
        addEventListener(name, callback) {
            if (!listeners.has(name)) listeners.set(name, new Set());
            listeners.get(name).add(callback);
        },
        removeEventListener(name, callback) {
            const callbacks = listeners.get(name);
            if (callbacks) callbacks.delete(callback);
            if (callbacks && !callbacks.size) listeners.delete(name);
        },
        async register(url, registrationOptions) {
            calls.push({type: "register", url, options: registrationOptions});
            if (settings.registrationError) {
                throw new Error("registration failed");
            }
            return registration;
        },
        emit(status, messageOptions, source = worker) {
            source.statuses.set(status.downloadId, status);
            for (const callback of listeners.get("message") || []) {
                callback({source, data: {
                    type: `DOWNLOAD_${status.state.toUpperCase()}`,
                    status,
                    ...(messageOptions || {}),
                }});
            }
        },
    };
    const worker = {
        name: "initial",
        state: "activated",
        settings,
        statuses,
        stateListeners: new Set(),
        addEventListener(name, callback) {
            if (name === 'statechange') this.stateListeners.add(callback);
        },
        removeEventListener(name, callback) {
            if (name === 'statechange') this.stateListeners.delete(callback);
        },
        changeState(state) {
            this.state = state;
            [...this.stateListeners].forEach(callback => callback());
        },
        postMessage(message, ports) {
            const settings = this.settings;
            const statuses = this.statuses;
            calls.push({type: "message", worker: this.name, message});
            const reply = ports && ports[0];
            if (!reply) return;
            if (settings.workerRuntime) {
                settings.workerRuntime.send(message).then(response => reply.postMessage(response));
                return;
            }
            if ((settings.dropMessages || []).includes(message.type)) return;
            if (message.type === "GET_STREAMING_CAPABILITIES") {
                reply.postMessage(settings.capabilitiesResponse || {
                    ok: true, protocolVersion: settings.protocolVersion || protocol.PROTOCOL_VERSION,
                });
                return;
            }
            if (message.type === "REGISTER_DOWNLOAD") {
                if (settings.registerDownloadErrorCode) {
                    reply.postMessage({
                        ok: false,
                        code: settings.registerDownloadErrorCode,
                    });
                    return;
                }
                const status = safeStatus({
                    downloadId: message.descriptor.downloadId,
                    filename: message.descriptor.filename,
                    provider: message.descriptor.provider,
                    expectedBytes: message.descriptor.expectedBytes,
                    totalBytes: message.descriptor.expectedBytes,
                });
                statuses.set(status.downloadId, status);
                if (settings.dropRegisterReply) return;
                reply.postMessage({ok: true, status});
                return;
            }
            if (message.type === "GET_DOWNLOAD_STATUS") {
                const status = statuses.get(message.downloadId);
                const response = status
                    ? {ok: true, status}
                    : {ok: false, code: "DOWNLOAD_NOT_FOUND"};
                if (Object.hasOwn(settings, "statusTelemetryOwner")) {
                    response.telemetryOwner = settings.statusTelemetryOwner;
                }
                reply.postMessage(response);
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
                serviceWorker.emit(status, {}, this);
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
    function createWorker(name, overrides) {
        return {...worker, name, statuses: new Map(), stateListeners: new Set(), settings: {...settings, ...overrides}};
    }
    const updateListeners = new Set();
    const registration = {
        active: worker,
        installing: null,
        waiting: settings.waitingWorker ? createWorker("waiting", settings.waitingWorker) : null,
        addEventListener(name, callback) {
            if (name === 'updatefound') updateListeners.add(callback);
        },
        removeEventListener(name, callback) {
            if (name === 'updatefound') updateListeners.delete(callback);
        },
        updateFound() { [...updateListeners].forEach(callback => callback()); },
        async update() {
            calls.push({type: 'update'});
            if (settings.onUpdate) await settings.onUpdate(registration);
            return registration;
        },
    };
    if (registration.waiting) registration.waiting.state = "installed";
    function changeController(nextWorker) {
        serviceWorker.controller = nextWorker;
        if (nextWorker) {
            nextWorker.state = "activated";
            registration.active = nextWorker;
            if (registration.waiting === nextWorker) registration.waiting = null;
            if (registration.installing === nextWorker) registration.installing = null;
            nextWorker.changeState('activated');
        }
        for (const callback of [...(listeners.get("controllerchange") || [])]) callback();
    }
    serviceWorker.controller = worker;
    const clicked = [];
    const frames = [];
    const targets = [];
    const documentObject = {
        body: {
            appendChild() {},
        },
        createElement(tagName) {
            if (tagName === "iframe") {
                const frame = {
                    hidden: false,
                    name: "",
                    removed: false,
                    style: {},
                    remove() {
                        this.removed = true;
                    },
                };
                frames.push(frame);
                return frame;
            }
            return {
                href: "",
                target: "",
                style: {},
                click() {
                    clicked.push(this.href);
                    targets.push(this.target);
                    if (typeof settings.onNavigate === "function") {
                        settings.onNavigate({
                            serviceWorker,
                            worker: registration.active,
                            statuses: registration.active.statuses,
                            href: this.href,
                            target: this.target,
                        });
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
        messageTimeoutMs: settings.messageTimeoutMs || 100,
        statusPollMs: 20,
        telemetryReporter: settings.telemetryReporter,
    });
    return {
        calls, clicked, client, frames, serviceWorker, statuses, targets,
        changeController, createWorker, listeners, registration, worker,
    };
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

function selectedItem(provider = "tiantianjiexi", overrides = {}) {
    const resource = "7669858827638845160";
    return ordinaryItem({
        provider, source_platform: "douyin", selection_schema: "service_selected_format_v1",
        candidate_id: "candidate-high", selected_format_id: "format-high",
        asset_id: `douyin:${resource}:video:${provider}:format:${"a".repeat(24)}`,
        provider_resource_id: resource, provider_cache_key: `douyin:${provider}:validated:${resource}`,
        provider_manifest_token: "signed-manifest-fixture", browser_upstream_bound: true,
        selection_expires_at: Date.now() + 60000, filename: "selected-high.bin", ...overrides,
    });
}

function selectedDescriptor(overrides = {}, provider = "tiantianjiexi") {
    return protocol.createDescriptor({...selectedItem(provider, overrides),
        downloadId: "0123456789abcdef0123456789abcdef"});
}

function bilibiliSelectedItem(overrides = {}) {
    return selectedItem("tiantianjiexi_generic", {
        source_platform: "bilibili", url: "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
        asset_id: `bilibili:service:BV1xx411c7mD:p1:video:${"c".repeat(24)}`,
        provider_resource_id: "b".repeat(64),
        provider_cache_key: `bilibili:service:tiantianjiexi_generic:${"b".repeat(64)}`,
        ...overrides,
    });
}

test("Bilibili complete-file admission keeps scheme, track and redirect boundaries", () => {
    const source = bilibiliSelectedItem().url;
    assert.equal(protocol.isAllowedInitialMediaUrl(source), true);
    for (const url of [source.replace("https:", "http:"), source.replace(".mp4", ".m4s"),
        source.replace(".mp4", ".m3u8"), source.replace(".mp4", ".mpd"),
        source.replace("bilivideo.com", "bilivideo.com.evil.example"),
        source.replace("bilivideo.com", "hdslb.com")]) {
        assert.equal(protocol.isAllowedInitialMediaUrl(url), false, url);
    }
    assert.equal(protocol.isAllowedFinalMediaUrl({initialUrl: source,
        finalUrl: "https://upos-other.bilivideo.cn/video.mp4", redirected: true}), true);
    for (const other of [ordinaryItem().url, "https://media.example/video.mp4", source.replace(".mp4", ".m4s")]) {
        assert.equal(protocol.isAllowedFinalMediaUrl({initialUrl: source, finalUrl: other, redirected: true}), false);
        assert.equal(protocol.isAllowedFinalMediaUrl({initialUrl: other, finalUrl: source, redirected: true}), false);
    }
    const item = bilibiliSelectedItem();
    assert.equal(ordinary.isOrdinaryMediaItem(item), true);
    const choice = {id: item.candidate_id, provider: item.provider, kind: "video", formatId: item.selected_format_id,
        schema: item.selection_schema, legacyAssetId: item.asset_id};
    assert.equal(picker.validateItem(item, choice), item);
    for (const changes of [{provider_manifest_token: ""}, {provider_cache_key: "other"},
        {asset_id: `bilibili:service:BV1xx411c7mD:p1:video:${"d".repeat(24)}`}]) {
        assert.throws(() => picker.validateItem({...item, ...changes}, choice));
    }
    const transfer = protocol.createTransportDescriptor({...item, downloadId: "f".repeat(32)});
    assert.deepEqual(Object.keys(transfer).sort(),
        ["createdAt", "downloadId", "expectedBytes", "expiresAt", "filename", "responsePolicy", "url"].sort());
});

async function registerSelected(runtime, descriptor) {
    return runtime.send({type: "REGISTER_DOWNLOAD", protocolVersion: protocol.PROTOCOL_VERSION, descriptor});
}

function integratedClient(fetchImpl, options = {}) {
    let harness;
    const navigations = [];
    const runtime = createWorkerRuntime(fetchImpl, {
        onMessage(message) { harness.serviceWorker.emit(message.status, {telemetryOwner: false}); },
    });
    harness = createClientHarness({
        ...options, workerRuntime: runtime,
        onNavigate({href}) {
            const navigation = runtime.startFetch(protocol.descriptorIdFromPath(href));
            navigations.push(navigation);
            if (options.onNavigate) options.onNavigate(navigation);
        },
    });
    return {...harness, runtime, navigations};
}

function responseFor(body, options) {
    const settings = options || {};
    const response = new Response(body, {
        status: 200,
        headers: {
            "Content-Length": String(Buffer.byteLength(body)),
            "Content-Type": "video/mp4",
        },
    });
    Object.defineProperty(response, "url", {
        configurable: true,
        value: settings.url || "https://v11-default.365yg.com/media/video.mp4",
    });
    return response;
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
    const clientDeliveries = [];
    const clientIds = settings.clientIds || ["client-a"];
    const messageLifetimes = [];
    const context = {
        Date: settings.DateConstructor || Date,
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
                    return clientIds.map((id) => ({
                        id,
                        postMessage(message) {
                            clientMessages.push(message);
                            clientDeliveries.push({id, message});
                            if (settings.onMessage) settings.onMessage(message, id);
                        },
                    }));
                },
            },
            addEventListener(name, callback) { listeners.set(name, callback); },
        },
    };
    vm.runInNewContext(settings.source || workerSource, context, {filename: workerPath});

    async function send(message, sourceId) {
        return new Promise((resolve) => {
            const waitUntil = [];
            listeners.get("message")({
                data: message,
                source: {id: sourceId || clientIds[0]},
                ports: [{postMessage: resolve}],
                waitUntil(promise) { waitUntil.push(promise); messageLifetimes.push(promise); },
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

    return {clientDeliveries, clientMessages, messageLifetimes, send, startFetch,
        inspectState: vm.runInNewContext("downloadId => downloads.get(downloadId)", context)};
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
        status: 200,
        url: settings.url || "https://v11-default.365yg.com/media/video.mp4?fixture=signed",
        headers: new Headers({
            "Content-Type": "video/mp4",
            "Content-Length": String(chunks.reduce((total, chunk) => total + chunk.length, 0)),
        }),
        body: stream,
    };
    return {response, wasCancelled: () => cancelled};
}

test("one generic Worker streams a newly admitted endpoint without platform rules or business fields", async () => {
    const url = "https://cdn.future.example.com/complete.mp4";
    assert.equal(protocol.isAllowedInitialMediaUrl(url), false, "the page's admission has not been widened");
    const runtime = createWorkerRuntime(async requested => {
        assert.equal(requested, url);
        return responseFor("12345", {url});
    });
    const descriptor = {downloadId: "e".repeat(32), url, filename: "selected.mp4", expectedBytes: 5,
        createdAt: Date.now(), expiresAt: Date.now() + 60000,
        responsePolicy: {hosts: ["cdn.future.example.com"], suffixes: [], redirectSuffixes: []}};
    const registration = await runtime.send({type: "REGISTER_DOWNLOAD", descriptor});
    assert.equal(registration.ok, true);
    const response = await runtime.startFetch(descriptor.downloadId).response;
    assert.equal(await response.text(), "12345");
    const terminal = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId: descriptor.downloadId});
    assert.equal(terminal.status.state, "completed");
    assert.equal(terminal.status.receivedBytes, 5);
});

test("generic transfer constraints preserve the existing resource-specific redirect boundary", () => {
    for (const source of ["https://a.365yg.com/v.mp4", "https://a.douyinvod.com/v.mp4",
        "https://v3-dy-a-x.ixigua.com/v.mp4", "https://sns-video-hw.xhscdn.com/v.mp4",
        "https://sns-video-bd.xhscdn.com/v.mp4", "https://sns-video-v7.xhscdn.com/v.mp4",
        "https://a.bilivideo.com/v.mp4"]) {
        const descriptor = {url: source, responsePolicy: protocol.createResponsePolicy(source)};
        for (const final of [source, "https://b.365yg.com/v.mp4", "https://b.douyinvod.com/v.mp4",
            "https://v3-dy-a-x.ixigua.com/v.mp4", "https://a.bdcgslb.com/v.mp4",
            "https://bdcgslb.com/v.mp4", "https://sns-video-hw.xhscdn.com/v.mp4",
            "https://sns-video-bd.xhscdn.com/v.mp4", "https://sns-video-v9.xhscdn.com/v.mp4",
            "https://b.bilivideo.cn/v.mp4", "https://attacker.example.com/v.mp4",
            "http://a.bilivideo.com/v.mp4", "https://127.0.0.1/v.mp4"]) {
            for (const redirected of [false, true]) {
                const response = {ok: true, status: 200, body: {}, url: final, redirected,
                    headers: new Headers({"Content-Type": "video/mp4"})};
                assert.equal(protocol.classifyTransportResponse(response, descriptor).rejected,
                    protocol.classifyResponseRejection(response, {initialUrl: source}).rejected,
                    `${source} -> ${final}, redirected=${redirected}`);
            }
        }
    }
});

test("generic worker input rejects unsafe endpoints, malformed constraints, expiry and oversized media", () => {
    const input = {downloadId: "c".repeat(32), url: "https://cdn.example.com/v.mp4", filename: "video.mp4",
        responsePolicy: {hosts: ["cdn.example.com"], suffixes: [], redirectSuffixes: []}};
    for (const url of ["http://cdn.example.com/v.mp4", "https://user@cdn.example.com/v.mp4",
        "https://cdn.example.com:444/v.mp4", "https://cdn.example.com/v.mp4#x",
        "https://cdn.example.com/v.m3u8", "https://127.0.0.1/v.mp4", "https://other.example.com/v.mp4"]) {
        assert.throws(() => protocol.createWorkerDescriptor({...input, url}), /STREAMING_URL_UNSUPPORTED/);
    }
    for (const host of ["*", "com", "localhost", "127.0.0.1", "internal", "host.local", "bad.example.com/"]) {
        assert.throws(() => protocol.createWorkerDescriptor({...input,
            responsePolicy: {hosts: [host], suffixes: [], redirectSuffixes: []}}), /INVALID_RESPONSE_POLICY/);
    }
    assert.throws(() => protocol.createWorkerDescriptor({...input, expiresAt: Date.now() - 1}), /DESCRIPTOR_EXPIRED/);
    assert.throws(() => protocol.createWorkerDescriptor({...input, expectedBytes: 2 ** 31 + 1}), /EXPECTED_SIZE_TOO_LARGE/);
});

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

test("reviewed ixigua admission is exact, HTTPS-only, and shared by descriptors", () => {
    const exactUrl = (
        "https://v3-dy-a-x.ixigua.com/media/video.mp4?fixture=fake"
    );
    const descriptor = protocol.createDescriptor({
        downloadId: "10101010101010101010101010101010",
        url: exactUrl,
        filename: "reviewed.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 1024,
    });

    assert.equal(descriptor.url, exactUrl);
    assert.equal(protocol.isAllowedStreamingUrl(exactUrl), true);
    assert.equal(
        protocol.isAllowedStreamingUrl(
            "https://V3-DY-A-X.IXIGUA.COM/media/video.mp4",
        ),
        true,
    );
    for (const rejected of [
        "https://ixigua.com/media/video.mp4",
        "https://www.ixigua.com/media/video.mp4",
        "https://evil.v3-dy-a-x.ixigua.com/media/video.mp4",
        "https://v3-dy-a-x.ixigua.com.evil.example/media/video.mp4",
        "https://evilixigua.com/media/video.mp4",
        "https://ixigua-example.com/media/video.mp4",
        "http://v3-dy-a-x.ixigua.com/media/video.mp4",
        "https://user@v3-dy-a-x.ixigua.com/media/video.mp4",
        "https://v3-dy-a-x.ixigua.com./media/video.mp4",
        "https://v3-dy-a-x.ixigua.com:444/media/video.mp4",
    ]) {
        assert.equal(protocol.isAllowedStreamingUrl(rejected), false, rejected);
        assert.equal(protocol.sanitizeMediaHost(rejected), "unknown", rejected);
    }
    assert.equal(
        protocol.sanitizeMediaHost(exactUrl),
        "v3-dy-a-x.ixigua.com",
    );
    assert.equal(
        protocol.sanitizeMediaHost("v3-dy-a-x.ixigua.com"),
        "v3-dy-a-x.ixigua.com",
    );
    assert.equal(protocol.isAllowedStreamingUrl("https://v11-default.365yg.com/a"), true);
    assert.equal(protocol.isAllowedStreamingUrl("https://v3-web.douyinvod.com/a"), true);
});

test("reviewed ixigua response keeps content and final-host gates", () => {
    const body = {cancel() { return Promise.resolve(); }};
    const initialUrl = "https://v3-dy-a-x.ixigua.com/media/video.mp4?fixture=fake";
    const accepted = protocol.classifyResponseRejection({
        ok: true,
        status: 200,
        url: "https://v3-dy-a-x.ixigua.com/media/video.mp4?fixture=fake",
        headers: new Headers({"Content-Type": "video/mp4"}),
        body,
    }, {initialUrl});
    assert.deepEqual(accepted, {
        rejected: false,
        responseRejectReason: "",
        httpStatus: 200,
        contentTypeClass: "VIDEO",
        finalMediaHost: "v3-dy-a-x.ixigua.com",
        initialMediaFamily: "DOUYIN_IXIGUA_EXACT",
        observedFinalHost: "v3-dy-a-x.ixigua.com",
        observedFinalHostClass: "INITIAL_SOURCE",
        redirected: false,
    });

    for (const contentType of ["text/html", "application/json"]) {
        const rejected = protocol.classifyResponseRejection({
            ok: true,
            status: 200,
            url: "https://v3-dy-a-x.ixigua.com/media/video.mp4",
            headers: new Headers({"Content-Type": contentType}),
            body,
        }, {initialUrl});
        assert.equal(rejected.responseRejectReason, "CONTENT_TYPE_REJECTED");
    }
    const untrustedRedirect = protocol.classifyResponseRejection({
        ok: true,
        status: 200,
        url: "https://attacker.example/video.mp4",
        headers: new Headers({"Content-Type": "video/mp4"}),
        body,
    }, {initialUrl});
    assert.equal(
        untrustedRedirect.responseRejectReason,
        "FINAL_HOST_REJECTED",
    );
});

test("reviewed XHS TianTian host is exact, HTTPS-only, and descriptor-safe", () => {
    const exactUrl = "https://sns-video-hw.xhscdn.com/media/video.mp4?fixture=fake";
    const descriptor = protocol.createDescriptor({
        downloadId: "45454545454545454545454545454545",
        url: exactUrl,
        filename: "xhs-video.mp4",
        provider: "tiantianjiexi_generic",
        expectedBytes: 2048,
    });

    assert.equal(descriptor.initialMediaFamily, "XHS_TIANTIAN_HW_EXACT");
    assert.equal(protocol.isAllowedStreamingUrl(exactUrl), true);
    for (const rejected of [
        "https://other.xhscdn.com/media/video.mp4",
        "https://evil.sns-video-hw.xhscdn.com/media/video.mp4",
        "https://sns-video-hw.xhscdn.com.evil.example/media/video.mp4",
        "http://sns-video-hw.xhscdn.com/media/video.mp4",
        "https://user@sns-video-hw.xhscdn.com/media/video.mp4",
        "https://sns-video-hw.xhscdn.com:444/media/video.mp4",
        "https://sns-video-hw.xhscdn.com./media/video.mp4",
    ]) {
        assert.equal(protocol.isAllowedStreamingUrl(rejected), false, rejected);
        assert.equal(protocol.sanitizeMediaHost(rejected), "unknown", rejected);
    }
});

test("XHS final-host policy cannot cross into Douyin or unreviewed XHS hosts", () => {
    const source = "https://sns-video-hw.xhscdn.com/media/source.mp4";
    assert.equal(protocol.isAllowedFinalMediaUrl({
        initialUrl: source,
        finalUrl: "https://sns-video-hw.xhscdn.com/media/final.mp4",
        redirected: true,
    }), true);
    for (const finalUrl of [
        "https://other.xhscdn.com/media/final.mp4",
        "https://v11-default.365yg.com/media/final.mp4",
        "https://v3-web.douyinvod.com/media/final.mp4",
        "https://v3-dy-a-x.ixigua.com/media/final.mp4",
        "https://attacker.example/media/final.mp4",
    ]) {
        assert.equal(protocol.isAllowedFinalMediaUrl({
            initialUrl: source,
            finalUrl,
            redirected: true,
        }), false, finalUrl);
    }
    assert.equal(protocol.isAllowedFinalMediaUrl({
        initialUrl: "https://v11-default.365yg.com/media/source.mp4",
        finalUrl: source,
        redirected: true,
    }), false);
});

test("reviewed XHS Native host is exact and isolated from TianTian", () => {
    const source = "https://sns-video-v28.xhscdn.com/native/source.mp4";
    const descriptor = protocol.createDescriptor({
        downloadId: "67676767676767676767676767676767",
        url: source,
        filename: "xhs-native.mp4",
        provider: "native",
        expectedBytes: 1024,
    });
    assert.equal(descriptor.initialMediaFamily, "XHS_NATIVE_REVIEWED");
    assert.equal(protocol.isAllowedStreamingUrl(source), true);
    assert.equal(protocol.isAllowedFinalMediaUrl({
        initialUrl: source,
        finalUrl: "https://sns-video-v28.xhscdn.com/native/final.mp4",
        redirected: true,
    }), true);
    for (const rejected of [
        "https://sns-video-hw.xhscdn.com/media/final.mp4",
        "https://sns-video-v27.xhscdn.com/native/final.mp4",
        "https://sns-video-vx.xhscdn.com/native/final.mp4",
        "https://evil-sns-video-v28.xhscdn.com/native/final.mp4",
        "https://other.xhscdn.com/media/final.mp4",
        "https://v11-default.365yg.com/media/final.mp4",
        "https://evil.sns-video-bd.xhscdn.com/native/final.mp4",
    ]) {
        assert.equal(protocol.isAllowedFinalMediaUrl({
            initialUrl: source,
            finalUrl: rejected,
            redirected: true,
        }), false, rejected);
    }
});

test("initial and source-bound final redirect policies reject bypasses", () => {
    const source365 = "https://v5-default.365yg.com/media/source.mp4";
    const sourceDouyinVod = "https://v3-web.douyinvod.com/media/source.mp4";
    const sourceIxigua = "https://v3-dy-a-x.ixigua.com/media/source.mp4";
    const finalBdcg = "https://random.edge.bdcgslb.com/media/final.mp4";

    assert.equal(protocol.isAllowedInitialMediaUrl(source365), true);
    assert.equal(protocol.isAllowedInitialMediaUrl(sourceDouyinVod), true);
    assert.equal(protocol.isAllowedInitialMediaUrl(sourceIxigua), true);
    assert.equal(protocol.isAllowedInitialMediaUrl(finalBdcg), false);
    assert.throws(() => protocol.createDescriptor({
        downloadId: "31313131313131313131313131313131",
        url: finalBdcg,
        filename: "blocked.mp4",
        provider: "tiantianjiexi",
    }), /STREAMING_URL_UNSUPPORTED/);

    for (const [initialUrl, finalUrl] of [
        [source365, "https://v11-default.365yg.com/media/final.mp4"],
        [sourceDouyinVod, "https://v6-web.douyinvod.com/media/final.mp4"],
        [sourceIxigua, sourceIxigua],
    ]) {
        assert.equal(protocol.isAllowedFinalMediaUrl({initialUrl, finalUrl}), true);
    }
    assert.equal(protocol.isAllowedFinalMediaUrl({
        initialUrl: source365,
        finalUrl: finalBdcg,
        redirected: true,
    }), true);
    assert.equal(protocol.isAllowedFinalMediaUrl({
        initialUrl: source365,
        finalUrl: finalBdcg,
        redirected: false,
    }), false);

    for (const [initialUrl, finalUrl] of [
        ["https://attacker.example/source.mp4", finalBdcg],
        [sourceIxigua, finalBdcg],
        [sourceDouyinVod, finalBdcg],
        [source365, "https://bdcgslb.com/media/final.mp4"],
        [source365, "https://bdcgslb.com.evil.example/media/final.mp4"],
        [source365, "https://evilbdcgslb.com/media/final.mp4"],
        [source365, "https://bdcgslb-example.com/media/final.mp4"],
        [source365, "http://random.bdcgslb.com/media/final.mp4"],
        [source365, "https://user@random.bdcgslb.com/media/final.mp4"],
        [source365, "https://random.bdcgslb.com:444/media/final.mp4"],
        [source365, "https://random.bdcgslb.com./media/final.mp4"],
        [source365, "https://127.0.0.1/media/final.mp4"],
        [source365, "https://localhost/media/final.mp4"],
    ]) {
        assert.equal(
            protocol.isAllowedFinalMediaUrl({initialUrl, finalUrl}),
            false,
            `${initialUrl} -> ${finalUrl}`,
        );
    }

    const unknown = protocol.classifyFinalRedirect({
        initialUrl: source365,
        finalUrl: "https://unreviewed.example/media/final.mp4?token=secret",
        redirected: true,
    });
    assert.equal(unknown.allowed, false);
    assert.equal(unknown.observedFinalHost, "unreviewed.example");
    assert.equal(unknown.observedFinalHostClass, "UNREVIEWED_PUBLIC");
    assert.equal(unknown.finalMediaHost, "unknown");
    assert.doesNotMatch(JSON.stringify(unknown), /media\/final|token|secret|https:\/\//i);
    for (const unsafeHost of [
        "https://unreviewed.example/path?token=secret",
        "user@unreviewed.example",
        "unreviewed.example/path",
        "unreviewed.example?token=secret",
        "127.0.0.1",
        "localhost",
        "unreviewed.example.",
    ]) {
        assert.equal(protocol.sanitizeObservedMediaHostname(unsafeHost), "unknown");
    }
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

test("client registers only the download scope and launches one opaque download path", async () => {
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
        options: {scope: "/__animeimages-stream-download/", updateViaCache: "none"},
    });
    assert.match(harness.clicked[0], /^\/__animeimages-stream-download\/[a-f0-9]{32}$/);
    assert.equal(harness.frames.length, 1);
    assert.equal(harness.frames[0].hidden, true);
    assert.match(harness.frames[0].name, /^animeimages-download-[a-f0-9]{32}$/);
    assert.equal(harness.targets[0], harness.frames[0].name);
    assert.equal(harness.frames[0].removed, false);
    assert.doesNotMatch(streamingSource, /link\.download\s*=/);
    assert.doesNotMatch(streamingSource, /link\.rel\s*=\s*["']noopener["']/);

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
    assert.equal(harness.frames[0].removed, true);
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

test("worker registration failure does not start a different transfer", async () => {
    const harness = createClientHarness({registrationError: true});
    let mediaFetches = 0;
    let blobHandoffs = 0;
    await assert.rejects(ordinary.download(ordinaryItem({size_bytes: 5}), {
        streamingClient: harness.client,
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345");
        },
        async handoffBlob() { blobHandoffs += 1; },
    }), error => error.errorCode === "STREAMING_REGISTRATION_FAILED");
    assert.equal(mediaFetches, 0);
    assert.equal(blobHandoffs, 0);
    assert.equal(harness.clicked.length, 0);
});

test("only explicit pre-start capability failures permit Blob fallback", async () => {
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_REGISTRATION_FAILED",
        "registration failed",
        {safeToFallback: true},
    )), false);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_START_TIMEOUT",
        "start timed out after the prepared descriptor was deleted",
        {safeToFallback: true},
    )), false);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_URL_UNSUPPORTED",
        "stale worker protocol",
        {safeToFallback: true},
    )), false);
    assert.equal(ordinary.canFallbackToBlob(streaming.streamingError(
        "STREAMING_START_STATE_UNKNOWN",
        "start state unknown",
        {streamingStarted: true},
    )), false);
});

test("an old worker URL rule never silently switches a selected transfer to Blob", async (t) => {
    const harness = createClientHarness({
        registerDownloadErrorCode: "STREAMING_URL_UNSUPPORTED",
    });
    t.after(() => harness.client.close());
    let mediaFetches = 0;
    let blobHandoffs = 0;
    let fallbacks = 0;
    await assert.rejects(ordinary.download(ordinaryItem({
        url: "https://sns-video-hw.xhscdn.com/media/video.mp4?fixture=fake",
        provider: "tiantianjiexi_generic",
        size_bytes: 5,
    }), {
        streamingClient: harness.client,
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345", {
                url: "https://sns-video-hw.xhscdn.com/media/video.mp4?fixture=fake",
            });
        },
        async handoffBlob() { blobHandoffs += 1; },
        onTransportFallback() { fallbacks += 1; },
    }), error => error.errorCode === "STREAMING_URL_UNSUPPORTED");

    assert.equal(mediaFetches, 0);
    assert.equal(blobHandoffs, 0);
    assert.equal(fallbacks, 0);
    assert.equal(harness.clicked.length, 0);
    assert.equal(workerMessages(harness, "REGISTER_DOWNLOAD").length, 1);
    assert.equal(workerMessages(harness, "SKIP_WAITING").length, 0);
});

test("a page-rejected URL never fetches", async () => {
    let mediaFetches = 0;
    await assert.rejects(ordinary.download(ordinaryItem({
        url: "https://attacker.example/video.mp4",
        size_bytes: 5,
    }), {
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345");
        },
        async handoffBlob() {},
    }), (error) => error && error.errorCode === "UPSTREAM_MEDIA_INVALID");
    assert.equal(mediaFetches, 0);
});

test("the worker asset address does not impose a protocol version gate", () => {
    assert.equal(protocol.PROTOCOL_VERSION, 4);
    assert.equal(
        streaming.DEFAULT_WORKER_URL,
        "/ordinary-media-download-sw.js",
    );
    assert.doesNotMatch(workerSource, /STREAMING_PROTOCOL_ASSET_VERSION/);
    assert.match(workerSource, /if \(!self\.AnimeImagesOrdinaryMediaStreamingProtocol\)/);
});

test("an unstarted descriptor timeout is not missing browser capability", async () => {
    const harness = createClientHarness({startTimeoutMs: 5});
    let mediaFetches = 0;
    let blobHandoffs = 0;
    await assert.rejects(ordinary.download(ordinaryItem({size_bytes: 5}), {
        streamingClient: harness.client,
        fetchImpl: async () => {
            mediaFetches += 1;
            return responseFor("12345");
        },
        async handoffBlob() { blobHandoffs += 1; },
    }), error => error.errorCode === "STREAMING_START_TIMEOUT");
    assert.equal(harness.clicked.length, 1);
    assert.equal(mediaFetches, 0);
    assert.equal(blobHandoffs, 0);
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

test("worker streams a reviewed ixigua descriptor through the same zero-relay path", async () => {
    const exactUrl = "https://v3-dy-a-x.ixigua.com/media/video.mp4?fixture=fake";
    const upstream = chunkedUpstream([[1, 2, 3], [4, 5, 6]], {url: exactUrl});
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "12121212121212121212121212121212";
    const registered = await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: exactUrl,
            filename: "ixigua.mp4",
            provider: "tiantianjiexi",
            expectedBytes: 6,
        },
    });

    assert.equal(registered.ok, true);
    const response = await runtime.startFetch(downloadId).response;
    assert.deepEqual(
        [...new Uint8Array(await response.arrayBuffer())],
        [1, 2, 3, 4, 5, 6],
    );
    const finalStatus = await runtime.send({
        type: "GET_DOWNLOAD_STATUS",
        downloadId,
    });
    assert.equal(finalStatus.status.state, "completed");
    assert.equal(finalStatus.status.receivedBytes, 6);
    assert.equal(finalStatus.status.mediaHost, "v3-dy-a-x.ixigua.com");
});

test("worker streams reviewed XHS TianTian bytes without server relay", async () => {
    const exactUrl = "https://sns-video-hw.xhscdn.com/media/video.mp4?fixture=fake";
    const upstream = chunkedUpstream([[1, 2, 3], [4, 5, 6]], {url: exactUrl});
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "56565656565656565656565656565656";
    const registered = await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: exactUrl,
            filename: "xhs-video.mp4",
            provider: "tiantianjiexi_generic",
            expectedBytes: 6,
        },
    });

    assert.equal(registered.ok, true);
    const response = await runtime.startFetch(downloadId).response;
    assert.deepEqual(
        [...new Uint8Array(await response.arrayBuffer())],
        [1, 2, 3, 4, 5, 6],
    );
    const finalStatus = await runtime.send({
        type: "GET_DOWNLOAD_STATUS",
        downloadId,
    });
    assert.equal(finalStatus.status.state, "completed");
    assert.equal(finalStatus.status.receivedBytes, 6);
    assert.equal(finalStatus.status.mediaHost, "sns-video-hw.xhscdn.com");
});

test("worker streams reviewed XHS Native bytes without server relay", async () => {
    const exactUrl = "https://sns-video-v28.xhscdn.com/native/video.mp4?fixture=fake";
    const upstream = chunkedUpstream([[1, 2, 3], [4, 5, 6]], {url: exactUrl});
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "68686868686868686868686868686868";
    const registered = await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: exactUrl,
            filename: "xhs-native.mp4",
            provider: "native",
            expectedBytes: 6,
        },
    });

    assert.equal(registered.ok, true);
    const response = await runtime.startFetch(downloadId).response;
    assert.deepEqual(
        [...new Uint8Array(await response.arrayBuffer())],
        [1, 2, 3, 4, 5, 6],
    );
    const finalStatus = await runtime.send({
        type: "GET_DOWNLOAD_STATUS",
        downloadId,
    });
    assert.equal(finalStatus.status.state, "completed");
    assert.equal(finalStatus.status.receivedBytes, 6);
    assert.equal(finalStatus.status.mediaHost, "sns-video-v28.xhscdn.com");
});

test("worker admits a reviewed 365yg to bdcgslb final redirect and streams exact bytes", async () => {
    const initialUrl = "https://v5-default.365yg.com/media/source.mp4?fixture=fake";
    const finalUrl = "https://random.edge.bdcgslb.com/media/final.mp4?fixture=fake";
    const upstream = chunkedUpstream([[1, 2, 3], [4, 5]], {url: finalUrl});
    upstream.response.redirected = true;
    const runtime = createWorkerRuntime(async () => upstream.response);
    const downloadId = "34343434343434343434343434343434";
    const registered = await runtime.send({
        type: "REGISTER_DOWNLOAD",
        descriptor: {
            downloadId,
            url: initialUrl,
            filename: "redirected.mp4",
            provider: "tiantianjiexi",
            expectedBytes: 5,
        },
    });

    assert.equal(registered.ok, true);
    const response = await runtime.startFetch(downloadId).response;
    assert.equal(response.status, 200);
    assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [1, 2, 3, 4, 5]);
    const finalStatus = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(finalStatus.status.state, "completed");
    assert.equal(finalStatus.status.receivedBytes, 5);
    assert.equal(finalStatus.status.initialMediaFamily, "DOUYIN_365YG");
    assert.equal(finalStatus.status.finalMediaHost, "random.edge.bdcgslb.com");
    assert.equal(finalStatus.status.observedFinalHost, "random.edge.bdcgslb.com");
    assert.equal(finalStatus.status.observedFinalHostClass, "REVIEWED_FINAL_ONLY");
    assert.equal(finalStatus.status.redirected, true);
    assert.doesNotMatch(JSON.stringify(finalStatus), /media\/(?:source|final)\.mp4|fixture|https:\/\//i);
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

test("response rejection predicates have stable status-body-type-host order", () => {
    const body = {cancel() { return Promise.resolve(); }};
    const initialUrl = "https://v11-default.365yg.com/media/source.mp4";
    const allInvalid = protocol.classifyResponseRejection({
        ok: false,
        status: 403,
        url: "https://attacker.example/redirected.mp4?token=secret",
        headers: new Headers({"Content-Type": "text/html"}),
        body: null,
    }, {initialUrl});
    assert.deepEqual(allInvalid, {
        rejected: true,
        responseRejectReason: "HTTP_STATUS_REJECTED",
        httpStatus: 403,
        contentTypeClass: "OTHER",
        finalMediaHost: "unknown",
        initialMediaFamily: "DOUYIN_365YG",
        observedFinalHost: "attacker.example",
        observedFinalHostClass: "UNREVIEWED_PUBLIC",
        redirected: false,
    }, {initialUrl});

    const missingBody = protocol.classifyResponseRejection({
        ok: true,
        status: 200,
        url: "https://v11-default.365yg.com/media/video.mp4",
        headers: new Headers({"Content-Type": "text/html"}),
        body: null,
    }, {initialUrl});
    assert.equal(missingBody.responseRejectReason, "BODY_UNAVAILABLE");

    const badType = protocol.classifyResponseRejection({
        ok: true,
        status: 200,
        url: "https://attacker.example/redirected.mp4",
        headers: new Headers({"Content-Type": "text/html"}),
        body,
    }, {initialUrl});
    assert.equal(badType.responseRejectReason, "CONTENT_TYPE_REJECTED");

    const badHost = protocol.classifyResponseRejection({
        ok: true,
        status: 200,
        url: "https://attacker.example/redirected.mp4",
        headers: new Headers({"Content-Type": "video/mp4"}),
        body,
    });
    assert.equal(badHost.responseRejectReason, "FINAL_HOST_REJECTED");
    assert.equal(badHost.finalMediaHost, "unknown");
    assert.doesNotMatch(JSON.stringify(badHost), /\/redirected\.mp4|token|secret|https:\/\//i);
});

test("worker reports each response rejection reason without response URLs", async () => {
    const cases = [
        {
            id: "11111111111111111111111111111111",
            expected: "BODY_UNAVAILABLE",
            response: {
                ok: true,
                status: 200,
                url: "https://v11-default.365yg.com/media/body.mp4?token=secret",
                headers: new Headers({"Content-Type": "video/mp4"}),
                body: null,
            },
        },
        {
            id: "22222222222222222222222222222222",
            expected: "CONTENT_TYPE_REJECTED",
            response: {
                ok: true,
                status: 200,
                url: "https://v11-default.365yg.com/media/html.mp4?token=secret",
                headers: new Headers({"Content-Type": "text/html"}),
                body: new ReadableStream({start(controller) { controller.close(); }}),
            },
        },
        {
            id: "33333333333333333333333333333333",
            expected: "FINAL_HOST_REJECTED",
            response: {
                ok: true,
                status: 200,
                url: "https://attacker.example/redirected.mp4?token=secret",
                headers: new Headers({"Content-Type": "video/mp4"}),
                body: new ReadableStream({start(controller) { controller.close(); }}),
            },
        },
    ];

    for (const current of cases) {
        const runtime = createWorkerRuntime(async () => current.response);
        await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
            downloadId: current.id,
            url: "https://v11-default.365yg.com/media/source.mp4?token=secret",
            filename: "source.mp4",
            provider: "tiantianjiexi",
            expectedBytes: 0,
        }});
        await runtime.startFetch(current.id).response;
        const status = await runtime.send({
            type: "GET_DOWNLOAD_STATUS",
            downloadId: current.id,
        });
        assert.equal(status.status.responseRejectReason, current.expected);
        assert.equal(status.status.errorCode, "UPSTREAM_MEDIA_INVALID");
        assert.doesNotMatch(
            JSON.stringify(status),
            /media\/(?:body|html|source)\.mp4|\/redirected\.mp4|token|secret|https:\/\//i,
        );
    }
});

test("worker keeps size mismatch in the existing size-validation class", async () => {
    const runtime = createWorkerRuntime(async () => ({
        ok: true,
        status: 200,
        url: "https://v11-default.365yg.com/media/size.mp4",
        headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "5"}),
        body: new ReadableStream({start(controller) { controller.close(); }}),
    }));
    const downloadId = "44444444444444444444444444444444";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId,
        url: "https://v11-default.365yg.com/media/size.mp4",
        filename: "size.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 6,
    }});
    await runtime.startFetch(downloadId).response;
    const current = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId});
    assert.equal(current.status.errorCode, "UPSTREAM_SIZE_MISMATCH");
    assert.equal(current.status.failureStage, "SIZE_VALIDATION_FAILURE");
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
    assert.equal(invalid.status.responseRejectReason, "HTTP_STATUS_REJECTED");
    assert.equal(invalid.status.httpStatus, 403);
    assert.equal(invalid.status.contentTypeClass, "OTHER");
    assert.equal(invalid.status.finalMediaHost, "v11-default.365yg.com");

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

test("non-owner tabs observe terminal state without duplicating telemetry", async () => {
    let telemetryCalls = 0;
    const harness = createClientHarness({
        telemetryReporter: {report() { telemetryCalls += 1; }},
    });
    await harness.client.registration();
    harness.serviceWorker.emit(safeStatus({
        state: "failed",
        errorCode: "UPSTREAM_FETCH_FAILED",
        failureClass: "FETCH_REJECTED_TYPE_ERROR",
        failureStage: "FETCH_REJECTED_BEFORE_RESPONSE",
        streamingStarted: true,
    }), {telemetryOwner: false});

    assert.equal(telemetryCalls, 0);
});

test("worker assigns terminal telemetry ownership to exactly one client", async () => {
    const runtime = createWorkerRuntime(async () => {
        throw new TypeError("controlled fetch failure");
    }, {clientIds: ["client-a", "client-b"]});
    const downloadId = "99999999999999999999999999999999";
    await runtime.send({type: "REGISTER_DOWNLOAD", descriptor: {
        downloadId,
        url: "https://v11-default.365yg.com/media/video.mp4",
        filename: "failed.mp4",
        provider: "tiantianjiexi",
        expectedBytes: 10,
    }}, "client-b");
    await runtime.startFetch(downloadId).response;

    const failed = runtime.clientDeliveries.filter(({message}) => (
        message.type === "DOWNLOAD_FAILED"
    ));
    assert.equal(failed.length, 2);
    assert.equal(
        failed.filter(({message}) => message.telemetryOwner).length,
        1,
    );
    assert.equal(
        failed.find(({message}) => message.telemetryOwner).id,
        "client-b",
    );

    const nonOwnerPoll = await runtime.send({
        type: "GET_DOWNLOAD_STATUS",
        downloadId,
    }, "client-a");
    const ownerPoll = await runtime.send({
        type: "GET_DOWNLOAD_STATUS",
        downloadId,
    }, "client-b");
    assert.equal(nonOwnerPoll.telemetryOwner, false);
    assert.equal(ownerPoll.telemetryOwner, true);
});

test("non-owner status polling cannot bypass terminal telemetry ownership", async () => {
    let telemetryCalls = 0;
    const harness = createClientHarness({
        statusTelemetryOwner: false,
        telemetryReporter: {report() { telemetryCalls += 1; }},
    });
    await harness.client.registration();
    const status = safeStatus({
        state: "failed",
        errorCode: "UPSTREAM_FETCH_FAILED",
        failureClass: "FETCH_REJECTED_TYPE_ERROR",
        failureStage: "FETCH_REJECTED_BEFORE_RESPONSE",
        streamingStarted: true,
    });
    harness.statuses.set(status.downloadId, status);

    const observed = await harness.client.getStatus(status.downloadId);
    assert.equal(observed.state, "failed");
    assert.equal(telemetryCalls, 0);
});

test("response-gate telemetry adds the fixed rejection field group", () => {
    const payload = streaming.createTerminalTelemetryPayload(safeStatus({
        state: "failed",
        errorCode: "UPSTREAM_MEDIA_INVALID",
        failureClass: "NOT_APPLICABLE",
        failureStage: "HTTP_RESPONSE_REJECTED",
        responseRejectReason: "HTTP_STATUS_REJECTED",
        httpStatus: 403,
        contentTypeClass: "OTHER",
        finalMediaHost: "v11-default.365yg.com",
        mediaHost: "v11-default.365yg.com",
        initialMediaFamily: "DOUYIN_365YG",
        observedFinalHost: "v11-default.365yg.com",
        observedFinalHostClass: "INITIAL_SOURCE",
        redirected: false,
    }), {}, {telemetryId: "11111111111111111111111111111111"});

    assert.deepEqual({
        response_reject_reason: payload.response_reject_reason,
        http_status: payload.http_status,
        content_type_class: payload.content_type_class,
        final_media_host: payload.final_media_host,
        initial_media_host: payload.initial_media_host,
        initial_media_family: payload.initial_media_family,
        observed_final_host: payload.observed_final_host,
        observed_final_host_class: payload.observed_final_host_class,
        redirected: payload.redirected,
    }, {
        response_reject_reason: "HTTP_STATUS_REJECTED",
        http_status: 403,
        content_type_class: "OTHER",
        final_media_host: "v11-default.365yg.com",
        initial_media_host: "v11-default.365yg.com",
        initial_media_family: "DOUYIN_365YG",
        observed_final_host: "v11-default.365yg.com",
        observed_final_host_class: "INITIAL_SOURCE",
        redirected: false,
    });
});

test("new page keeps the legacy telemetry shape for an old worker status", () => {
    const legacyStatus = safeStatus({
        state: "failed",
        errorCode: "UPSTREAM_MEDIA_INVALID",
        failureClass: "NOT_APPLICABLE",
        failureStage: "HTTP_RESPONSE_REJECTED",
        responseRejectReason: "FINAL_HOST_REJECTED",
        httpStatus: 200,
        contentTypeClass: "VIDEO",
        finalMediaHost: "unknown",
        mediaHost: "v11-default.365yg.com",
    });
    const payload = streaming.createTerminalTelemetryPayload(
        legacyStatus,
        {},
        {telemetryId: "11111111111111111111111111111111"},
    );

    assert.equal(payload.response_reject_reason, "FINAL_HOST_REJECTED");
    assert.equal(payload.final_media_host, "unknown");
    for (const field of [
        "initial_media_host",
        "initial_media_family",
        "observed_final_host",
        "observed_final_host_class",
        "redirected",
    ]) {
        assert.equal(Object.prototype.hasOwnProperty.call(payload, field), false, field);
    }
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
    assert.equal(status.status.httpStatus, 200);
    assert.equal(status.status.contentTypeClass, "VIDEO");
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
    const ordinaryVideoHandler = batchSource.slice(
        batchSource.indexOf('const directDownloadButton = event.target.closest("[data-batch-direct-download]")'),
        batchSource.indexOf('const imageDownloadButton = event.target.closest("[data-batch-image-download]")'),
    );
    const sharedVideoHandler = batchSource.slice(
        batchSource.indexOf('async function downloadBatchVideoWithUi('),
        batchSource.indexOf('function snapshotLegacyMedia('),
    );
    assert.match(singleSource, /ordinaryMediaDownloadApi\.download\(item/);
    assert.match(ordinaryVideoHandler, /await downloadBatchVideoWithUi\(directDownloadButton\)/);
    assert.match(batchSource, /await downloadBatchVideoWithUi\(button, \{isCurrent: applicable\}\)/);
    assert.match(
        sharedVideoHandler,
        /const authorizedItems = await authorizeBatchUpstreamItems\(page, \[item\]\);\s*if \(options\.isCurrent && !options\.isCurrent\(\)\) return;\s*await ordinaryMediaDownloadApi\.download\(authorizedItems\[0\],/,
    );
    assert.doesNotMatch(ordinaryVideoHandler, /response\.blob\s*\(|new Blob\(chunks/);
    assert.doesNotMatch(sharedVideoHandler, /response\.blob\s*\(|new Blob\(chunks/);
    assert.match(batchSource, /async function readBatchPhotoArchiveResponse\(/);
});

test("legacy bound messages retain their original validation for already-open pages", () => {
    for (const provider of ["tiantianjiexi", "kukutool"]) {
        const input = selectedItem(provider);
        const descriptor = selectedDescriptor(input, provider);
        assert.equal(descriptor.selectionBinding.candidate_id, input.candidate_id);
        assert.equal(descriptor.selectionBinding.provider_manifest_token, input.provider_manifest_token);
        assert.equal(descriptor.expiresAt, input.selection_expires_at);
        assert.equal(descriptor.maxBytes, 1024 ** 3);
        const registered = protocol.createDescriptor(descriptor, {nowEpochMs: descriptor.createdAt + 1000});
        assert.equal(registered.expiresAt, descriptor.expiresAt);
        assert.ok(Object.isFrozen(descriptor.selectionBinding));
        const snapshot = JSON.stringify(protocol.safeSnapshot({...descriptor, state: "prepared"}));
        assert.ok(!snapshot.includes(input.provider_manifest_token));
        assert.ok(!snapshot.includes(input.url));
        assert.equal(protocol.createDescriptor({...descriptor, maxBytes: 2 * 1024 ** 3}).maxBytes, 1024 ** 3);
        assert.throws(() => selectedDescriptor({size_bytes: 1024 ** 3 + 1}), /EXPECTED_SIZE_TOO_LARGE/);
        assert.equal(selectedDescriptor({size_bytes: 1024 ** 3 + 1, size_is_estimate: true}).expectedBytes, 0);
        assert.throws(() => protocol.createDescriptor(descriptor, {nowEpochMs: descriptor.expiresAt}), /DESCRIPTOR_EXPIRED/);
    }
    assert.equal(protocol.createDescriptor({...ordinaryItem(), downloadId: "1".repeat(32)}).maxBytes, 2 * 1024 ** 3);
});

test("new transfer projection removes business fields but retains the earlier authorization deadline", () => {
    const now = Date.now();
    const input = {...selectedItem("tiantianjiexi", {selection_expires_at: now + 1000}),
        downloadId: "1".repeat(32), expectedBytes: 1024 ** 3 + 1};
    const transfer = protocol.createTransportDescriptor(input, {nowEpochMs: now});
    assert.equal(transfer.expectedBytes, input.expectedBytes);
    assert.equal(transfer.expiresAt, input.selection_expires_at);
    assert.equal(input.selection_expires_at, now + 1000);
    for (const key of ["provider", "candidate_id", "provider_manifest_token", "selectionBinding", "protocolVersion"])
        assert.equal(transfer[key], undefined);
    assert.ok(Object.isFrozen(transfer));
    assert.throws(() => protocol.createTransportDescriptor(input, {nowEpochMs: now + 1000}), /DESCRIPTOR_EXPIRED/);
    assert.throws(() => protocol.createTransportDescriptor({...input, expectedBytes: 2 * 1024 ** 3 + 1}), /EXPECTED_SIZE_TOO_LARGE/);
    assert.equal(protocol.createTransportDescriptor({...input, size_is_estimate: true}).expectedBytes, 0);
    for (const changes of [{requires_cookie: true}, {requires_context: true}, {has_drm: true},
        {type: "audio"}, {url: "https://unreviewed.example/video.mp4"}, {provider_manifest_token: ""}])
        assert.throws(() => protocol.createTransportDescriptor({...input, ...changes}));
});

test("bound video variants retain strict admission without new domains, audio or context grants", () => {
    for (const provider of ["yt_dlp_generic", "tiantianjiexi_generic", "kukutool_generic"]) {
        const item = selectedItem(provider, {source_platform: "upstream", asset_id: "upstream:selection:candidate-high",
            selection_schema: provider === "yt_dlp_generic" ? "ytdlp_selected_format_v1" : "service_selected_format_v1"});
        assert.equal(selectedDescriptor(item, provider).selectionBinding.candidate_id, "candidate-high");
    }
    for (const changes of [
        {type: "audio"}, {requires_merge: true}, {requires_cookie: true}, {requires_context: true},
        {download_context_required: true}, {has_drm: true}, {video_tracks: "absent"},
        {browser_direct_fetch_safe: false}, {selection_delivery: "legacy_manifest"},
        {provider_cache_key: "douyin:kukutool:validated:other"}, {asset_id: "upstream:selection:other"},
        {selection_expires_at: undefined}, {selection_expires_at: Date.now() - 1},
        {url: "https://unreviewed.example/video.mp4"}, {url: "http://127.0.0.1/video.mp4"},
    ]) assert.throws(() => selectedDescriptor(changes), undefined, JSON.stringify(changes));
});

test("selected worker validates MIME before naming the response and never permits old redirects", async () => {
    for (const [mime, extension] of [["video/mp4", "mp4"], ["video/webm", "webm"], ["video/quicktime", "mov"],
        ["application/octet-stream", "bin"]]) {
        const source = selectedItem();
        let fetches = 0;
        const runtime = createWorkerRuntime(async (url, options) => {
            fetches += 1;
            assert.equal(url, source.url);
            assert.equal(options.redirect, "error"); assert.equal(options.mode, "cors");
            assert.equal(options.credentials, "omit"); assert.equal(options.referrerPolicy, "no-referrer");
            const upstream = chunkedUpstream([[1, 2]], {url});
            upstream.response.headers.set("Content-Type", mime);
            return upstream.response;
        });
        const descriptor = selectedDescriptor({...source, size_bytes: 2});
        assert.equal((await registerSelected(runtime, descriptor)).ok, true);
        const response = await runtime.startFetch(descriptor.downloadId).response;
        assert.match(response.headers.get("Content-Disposition"), new RegExp(`selected-high\\.${extension}`));
        const status = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId: descriptor.downloadId});
        assert.equal(status.status.filename, `selected-high.${extension}`);
        await response.arrayBuffer();
        assert.equal(fetches, 1);
    }
    for (const changes of [{redirected: true}, {type: "opaque"}, {type: "opaqueredirect"},
        {url: "https://v.example.bdcgslb.com/video.mp4"}, {url: selectedItem().url + "&different=1"}]) {
        let fetches = 0;
        const runtime = createWorkerRuntime(async () => {
            fetches += 1;
            return {...chunkedUpstream([[1, 2]]).response, ...changes};
        });
        const descriptor = selectedDescriptor({size_bytes: 2});
        await registerSelected(runtime, descriptor);
        assert.equal((await runtime.startFetch(descriptor.downloadId).response).status, 502);
        assert.equal(fetches, 1);
    }
});

test("selected worker enforces declared, response and actual-byte 1 GiB limits without allocating GiB", async () => {
    for (const mode of ["header", "chunks"]) {
        const runtime = createWorkerRuntime(async () => ({
            ok: true, status: 200, url: selectedItem().url,
            headers: new Headers({"Content-Type": "video/mp4", ...(mode === "header"
                ? {"Content-Length": String(1024 ** 3 + 1)} : {})}),
            body: new ReadableStream({pull(controller) {
                // A synthetic oversized chunk exercises accounting before enqueue, without allocation.
                controller.enqueue({byteLength: 1024 ** 3 + 1});
            }}),
        }));
        const descriptor = selectedDescriptor({size_bytes: 0});
        await registerSelected(runtime, descriptor);
        const response = await runtime.startFetch(descriptor.downloadId).response;
        if (mode === "header") assert.equal(response.status, 502);
        else await assert.rejects(response.body.getReader().read(), /UPSTREAM_SIZE_LIMIT_EXCEEDED/);
        const status = await runtime.send({type: "GET_DOWNLOAD_STATUS", downloadId: descriptor.downloadId});
        assert.equal(status.status.errorCode, "UPSTREAM_SIZE_LIMIT_EXCEEDED");
    }
});

test("real client and worker deliver selected bytes before EOF with bounded read-ahead and frozen identity", async () => {
    const sources = [selectedItem("tiantianjiexi"), selectedItem("kukutool"), bilibiliSelectedItem(),
        ordinaryItem({provider: "", source_platform: "bilibili", url: bilibiliSelectedItem().url,
            filename: "selected-high.bin"}),
        ...["yt_dlp_generic", "parsehub_generic"].map(provider => ordinaryItem({provider,
            source_platform: "upstream", selection_schema: "upstream_resource_v1", browser_upstream_bound: true,
            asset_id: `upstream:${"a".repeat(32)}:video:1`, provider_resource_id: "b".repeat(64),
            provider_cache_key: `upstream:${"a".repeat(32)}`, provider_manifest_token: "signed-resource-manifest",
            selection_expires_at: Date.now() + 60000, filename: "selected-high.bin"}))];
    for (const item of sources) {
        const end = deferred();
        let pulls = 0, eof = false, fetches = 0;
        const source = {...item, size_bytes: 4};
        const app = integratedClient(async url => {
            fetches += 1;
            assert.equal(url, source.url);
            return {ok: true, status: 200, url, headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "4"}),
                body: new ReadableStream({async pull(controller) {
                    pulls += 1;
                    if (pulls <= 4) controller.enqueue(Uint8Array.of(pulls));
                    else { await end.promise; eof = true; controller.close(); }
                }})};
        });
        const active = await app.client.start(source);
        source.filename = "later-choice.bin";
        const response = await app.navigations[0].response;
        await new Promise(resolve => setImmediate(resolve));
        assert.ok(pulls <= 2, `read ahead ${pulls} chunks without downstream consumption`);
        const reader = response.body.getReader();
        assert.deepEqual(Array.from((await reader.read()).value), [1]);
        assert.equal(eof, false);
        assert.match(response.headers.get("Content-Disposition"), /selected-high.mp4/);
        for (let index = 2; index <= 4; index += 1) assert.deepEqual(Array.from((await reader.read()).value), [index]);
        end.resolve();
        assert.equal((await reader.read()).done, true);
        assert.equal((await active.completion).filename, "selected-high.mp4");
        assert.equal(fetches, 1);
        await app.client.close();
    }
});

for (const size of ["unknown", "estimated"]) {
    for (const chunks of [[], [[], []]]) {
        test(`real client and worker reject ${size} empty EOF after ${chunks.length} zero-byte chunks`, async () => {
            let fetches = 0;
            const navigation = deferred();
            const app = integratedClient(async url => {
                fetches += 1;
                const upstream = chunkedUpstream(chunks, {url});
                upstream.response.headers.delete("Content-Length");
                return upstream.response;
            }, {onNavigate: navigation.resolve});
            try {
                const item = selectedItem("tiantianjiexi", {size_bytes: size === "estimated" ? 1024 : 0,
                    size_is_estimate: size === "estimated"});
                const pending = ordinary.download(item, {streamingClient: app.client,
                    fetchImpl: () => assert.fail("no fallback fetch"), handoffBlob: () => assert.fail("no Blob handoff")});
                const outcome = pending.then(result => ({result}), error => ({error}));
                const started = await navigation.promise;
                const response = await started.response;
                assert.equal(response.status, 200);
                await assert.rejects(response.arrayBuffer(), /UPSTREAM_STREAM_FAILED/);
                const {error} = await outcome;
                assert.equal(error.errorCode, "UPSTREAM_STREAM_FAILED");
                assert.equal(error.streamingStarted, true);
                assert.notEqual(error.safeToFallback, true);
                await Promise.all(started.waitUntil);
                const terminal = app.runtime.clientMessages.filter(message => message.type === "DOWNLOAD_FAILED");
                assert.equal(terminal.length, 1);
                assert.equal(terminal[0].status.receivedBytes, 0);
                assert.equal(terminal[0].status.failureStage, "STREAM_BODY_FAILURE");
                assert.equal(app.runtime.clientMessages.some(message => message.type === "DOWNLOAD_COMPLETED"), false);
                assert.equal(fetches, 1);
                const state = app.runtime.inspectState(terminal[0].status.downloadId);
                for (const key of ["settled", "expiryTimer", "selectionBinding", "abortController", "upstreamReader", "downstreamController"]) {
                    assert.equal(state[key], null, `${key} is released`);
                }
                assert.equal(state.url, "");
                assert.equal((await app.client.listActiveDownloads()).length, 0);
            } finally { await app.client.close(); }
        });
    }
}

for (const size of ["unknown", "estimated", "exact", "mismatch"]) {
    test(`real client and worker preserve ${size} nonempty incremental consumption`, async () => {
        const end = deferred();
        let pulls = 0, fetches = 0, eof = false;
        const app = integratedClient(async url => {
            fetches += 1;
            return {ok: true, status: 200, url, headers: new Headers({"Content-Type": "video/mp4"}),
                body: new ReadableStream({async pull(controller) {
                    pulls += 1;
                    if (pulls <= 4) controller.enqueue(Uint8Array.of(pulls));
                    else { await end.promise; eof = true; controller.close(); }
                }})};
        });
        try {
            const active = await app.client.start(selectedItem("tiantianjiexi", {
                size_bytes: {unknown: 0, estimated: 1024, exact: 4, mismatch: 5}[size],
                size_is_estimate: size === "estimated",
            }));
            const completion = active.completion.then(result => ({result}), error => ({error}));
            const response = await app.navigations[0].response;
            await new Promise(resolve => setImmediate(resolve));
            assert.ok(pulls <= 2, `bounded read-ahead: ${pulls}`);
            const reader = response.body.getReader();
            for (let byte = 1; byte <= 4; byte += 1) {
                assert.deepEqual(Array.from((await reader.read()).value), [byte]);
                assert.equal(eof, false, "bytes arrive before EOF");
            }
            end.resolve();
            if (size === "mismatch") {
                await assert.rejects(reader.read(), /UPSTREAM_SIZE_MISMATCH/);
                assert.equal((await completion).error.errorCode, "UPSTREAM_SIZE_MISMATCH");
            } else {
                assert.equal((await reader.read()).done, true);
                assert.equal((await completion).result.receivedBytes, 4);
            }
            await Promise.all(app.navigations[0].waitUntil);
            assert.equal(fetches, 1);
        } finally { end.resolve(); await app.client.close(); }
    });
}

test("old resource binding keeps its own manifest and expiry without fabricating candidate identity", () => {
    const input = ordinaryItem({provider: "parsehub_generic", source_platform: "upstream",
        selection_schema: "upstream_resource_v1", browser_upstream_bound: true,
        asset_id: `upstream:${"a".repeat(32)}:video:1`, provider_resource_id: "b".repeat(64),
        provider_cache_key: `upstream:${"a".repeat(32)}`, provider_manifest_token: "signed-resource-manifest",
        selection_expires_at: Date.now() + 10000, downloadId: "1".repeat(32)});
    const descriptor = protocol.createDescriptor(input);
    assert.equal(descriptor.selectionBinding.asset_id, input.asset_id);
    assert.equal(descriptor.selectionBinding.provider_manifest_token, input.provider_manifest_token);
    assert.equal(descriptor.selectionBinding.candidate_id, undefined);
    assert.equal(descriptor.expiresAt, input.selection_expires_at);
    for (const changes of [{selection_schema: undefined}, {selection_expires_at: undefined},
        {provider_manifest_token: ""}, {provider_cache_key: `upstream:${"b".repeat(32)}`},
        {provider_resource_id: "another-work"}, {asset_id: `upstream:${"a".repeat(32)}:image:1`},
        {requires_cookie: true}, {requires_merge: true}, {source_platform: "douyin"}]) {
        assert.throws(() => protocol.createDescriptor({...input, ...changes}), /INVALID_SELECTION_BINDING/);
    }
});

test("real selected page signal cancels its worker task and waits for upstream cancellation confirmation", async () => {
    const cancelled = deferred(), release = deferred();
    let options, fetches = 0;
    const app = integratedClient(async (url, init) => {
        options = init; fetches += 1;
        return {ok: true, status: 200, url, headers: new Headers({"Content-Type": "video/mp4"}),
            body: new ReadableStream({pull() {}, async cancel() { cancelled.resolve(); await release.promise; }})};
    });
    const controller = new AbortController();
    const active = await app.client.start(selectedItem("tiantianjiexi", {size_bytes: 0}), {signal: controller.signal});
    await app.navigations[0].response;
    let settled = false;
    const completion = active.completion.catch(error => { settled = true; return error; });
    controller.abort();
    await cancelled.promise;
    assert.equal(options.signal.aborted, true);
    const pending = await app.client.getStatus(active.downloadId);
    assert.equal(pending.cancellationPending, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(settled, false);
    release.resolve();
    const error = await completion;
    assert.equal(error.errorCode, "USER_CANCELLED");
    assert.equal(error.safeToFallback, false);
    assert.equal(error.status.cancellationPending, false);
    assert.equal(fetches, 1);
    await app.client.close();
});

for (const entry of ["client", "handle", "signal"]) {
    test(`legacy ${entry} cancellation shares one ACK and leaves parallel work independent`, async (t) => {
        const app = createClientHarness({protocolVersion: 2, onNavigate: emitNavigationStarted});
        t.after(() => app.client.close());
        const controller = new AbortController();
        const active = await app.client.start(selectedItem(), {signal: controller.signal});
        const other = await app.client.start(ordinaryItem());
        const notices = [];
        app.client.subscribeAll(status => notices.push(status));
        let settled = false;
        const completion = active.completion.catch(error => { settled = true; return error; });
        const dispatched = deferred();
        const original = app.worker.postMessage.bind(app.worker);
        let acknowledge;
        app.worker.postMessage = (message, ports) => {
            if (message.type !== "CANCEL_DOWNLOAD") return original(message, ports);
            app.calls.push({type: "message", worker: "initial", message});
            const status = {...app.statuses.get(message.downloadId), state: "cancelled", errorCode: "USER_CANCELLED"};
            app.serviceWorker.emit(status);
            acknowledge = () => ports[0].postMessage({ok: true, status});
            dispatched.resolve();
        };
        if (entry === "client") app.client.cancel(active.downloadId);
        else if (entry === "handle") active.cancel();
        else controller.abort();
        const confirmation = app.client.cancel(active.downloadId);
        assert.equal(active.cancel(), confirmation);
        controller.abort();
        await dispatched.promise;
        const pending = await app.client.getStatus(active.downloadId);
        assert.equal(pending.state, "fetching");
        assert.equal(pending.cancellationPending, true);
        assert.equal(settled, false);
        assert.equal(app.frames[0].removed, false);
        assert.equal(workerMessages(app, "CANCEL_DOWNLOAD").length, 1);
        assert.equal(notices.filter(status => status.downloadId === active.downloadId
            && protocol.TERMINAL_STATES.includes(status.state)).length, 0);
        await completeDownload(app, other);
        assert.equal(app.frames[1].removed, true);
        assert.equal(app.frames[0].removed, false);
        acknowledge();
        assert.equal((await confirmation).state, "cancelled");
        assert.equal((await completion).errorCode, "USER_CANCELLED");
        assert.equal(app.frames[0].removed, true);
        assert.equal(notices.filter(status => status.downloadId === active.downloadId
            && protocol.TERMINAL_STATES.includes(status.state)).length, 1);
    });
}

test("unconfirmed legacy cancellation never publishes its early terminal status", async (t) => {
    const app = createClientHarness({protocolVersion: 2, onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const active = await app.client.start(selectedItem());
    const notices = [];
    app.client.subscribeAll(status => notices.push(status));
    const completion = active.completion.catch(error => error);
    const original = app.worker.postMessage.bind(app.worker);
    app.worker.postMessage = (message, ports) => {
        if (message.type !== "CANCEL_DOWNLOAD") return original(message, ports);
        const status = {...app.statuses.get(message.downloadId), state: "cancelled", errorCode: "USER_CANCELLED"};
        app.serviceWorker.emit(status);
        ports[0].postMessage({ok: false, status});
    };
    await assert.rejects(app.client.cancel(active.downloadId), error => error.errorCode === "STREAMING_CANCEL_STATE_UNKNOWN");
    assert.equal((await completion).errorCode, "STREAMING_CANCEL_STATE_UNKNOWN");
    assert.equal((await app.client.getStatus(active.downloadId)).state, "fetching");
    assert.equal(app.frames[0].removed, false);
    assert.equal(notices.some(status => protocol.TERMINAL_STATES.includes(status.state)), false);
});

test("unknown registration is cleaned but cannot fall back, and unknown cleanup never permits a retry", async () => {
    for (const dropMessages of [[], ["DELETE_DOWNLOAD"]]) {
        const app = createClientHarness({dropRegisterReply: true, dropMessages, messageTimeoutMs: 5});
        await assert.rejects(app.client.start(selectedItem()), error =>
            error.errorCode === "STREAMING_START_STATE_UNKNOWN" && error.safeToFallback === false);
        assert.equal(app.clicked.length, 0);
        assert.equal(app.calls.filter(call => call.message && call.message.type === "REGISTER_DOWNLOAD").length, 1);
        await app.client.close();
    }
});

function workerMessages(app, type) {
    return app.calls.filter(call => call.message && (!type || call.message.type === type));
}

function assertNoUpdateSideEffects(app) {
    assert.deepEqual(workerMessages(app).filter(call => ![
        "GET_STREAMING_CAPABILITIES", "GET_DOWNLOAD_STATUS", "LIST_ACTIVE_DOWNLOADS",
    ].includes(call.message.type)), []);
    assert.equal(app.clicked.length, 0);
    assert.equal(app.frames.length, 0);
}

function emitNavigationStarted({serviceWorker, statuses, href, worker}) {
    const status = statuses.get(protocol.descriptorIdFromPath(href));
    serviceWorker.emit({...status, state: "fetching", streamingStarted: true}, {}, worker);
}

function completeDownload(app, active) {
    const owner = [app.worker, app.registration.active].find(w => w.statuses.has(active.downloadId));
    app.serviceWorker.emit({...owner.statuses.get(active.downloadId), state: "completed",
        receivedBytes: 10, streamingStarted: true}, {}, owner);
    return active.completion;
}

test("unchanged active code keeps legacy wire support without a handshake or repeated update", async (t) => {
    for (const capabilitiesResponse of [{ok: true, protocolVersion: 2}, {ok: false, code: "UNKNOWN_MESSAGE"}]) {
        const app = createClientHarness({capabilitiesResponse, onNavigate: emitNavigationStarted});
        t.after(() => app.client.close());
        const existing = await app.client.start(ordinaryItem());
        const oldFrame = app.frames[0];
        const oldStatus = {...app.statuses.get(existing.downloadId)};
        const input = selectedItem("tiantianjiexi", {filename: "selected-720p.mp4"});
        const selected = await app.client.start(input);
        input.url = "https://wrong.example/changed.mp4";
        const registrations = workerMessages(app, "REGISTER_DOWNLOAD");
        assert.equal(registrations.length, 2);
        assert.equal(registrations[1].worker, "initial");
        const transfer = registrations[1].message.descriptor;
        assert.equal(transfer.filename, "selected-720p.mp4");
        assert.equal(transfer.url, selectedItem().url);
        assert.deepEqual(Object.keys(transfer).sort(),
            ["createdAt", "downloadId", "expectedBytes", "expiresAt", "filename", "responsePolicy", "url"].sort());
        assert.equal(registrations[1].message.protocolVersion, undefined);
        assert.equal(workerMessages(app, "GET_STREAMING_CAPABILITIES").length, 0);
        assert.equal(workerMessages(app, "LIST_ACTIVE_DOWNLOADS").length, 0);
        assert.equal(workerMessages(app).some(call => call.worker === "waiting"), false);
        assert.equal(app.calls.filter(call => call.type === 'update').length, 1);
        assert.equal(oldFrame.removed, false);
        assert.deepEqual(app.statuses.get(existing.downloadId), oldStatus);
        assert.equal(app.frames.length, 2);
        assert.equal(workerMessages(app, "CANCEL_DOWNLOAD").length, 0);
        await completeDownload(app, selected);
        assert.equal(oldFrame.removed, false);
        await completeDownload(app, existing);
    }
});

test('first registration waits for normal old-code to new-code activation before registering media', async (t) => {
    let app;
    let replacement;
    let mediaRequests = 0;
    let navigation;
    const updateReturned = deferred();
    // Replay the pre-fix registration lifetime, with its own actual Worker Map.
    assert.ok(workerSource.includes('event.waitUntil(state.preparation.promise);'));
    const oldRuntime = createWorkerRuntime(() => assert.fail('Old Worker must not fetch media'), {
        source: workerSource.replace('event.waitUntil(state.preparation.promise);', ''),
    });
    const newRuntime = createWorkerRuntime(() => {
        mediaRequests++;
        return responseFor('first-last');
    }, {
        onMessage(message) { app.serviceWorker.emit(message.status, {}, replacement); },
    });
    app = createClientHarness({workerRuntime: oldRuntime,
        onUpdate(registration) {
            replacement = app.createWorker('updated', {workerRuntime: newRuntime});
            replacement.state = 'installing';
            registration.installing = replacement;
            registration.updateFound();
            updateReturned.resolve();
        },
        onNavigate({href, worker}) {
            assert.equal(worker, replacement);
            navigation = newRuntime.startFetch(protocol.descriptorIdFromPath(href));
        },
    });
    t.after(() => app.client.close());
    const root = {state: 'activated', postMessage() { assert.fail('Root Worker is unrelated'); }};
    app.serviceWorker.controller = root;
    const starting = app.client.start(ordinaryItem());
    await updateReturned.promise;
    await new Promise(resolve => setImmediate(resolve));
    assertNoUpdateSideEffects(app);
    app.registration.installing = null;
    app.registration.waiting = replacement;
    replacement.changeState('installed');
    await new Promise(resolve => setImmediate(resolve));
    assertNoUpdateSideEffects(app);
    app.registration.waiting = null;
    app.registration.active = replacement;
    replacement.changeState('activating');
    await new Promise(resolve => setImmediate(resolve));
    assertNoUpdateSideEffects(app);
    replacement.changeState('activated');
    const active = await starting;
    assert.equal(await (await navigation.response).text(), 'first-last');
    await active.completion;
    assert.equal(oldRuntime.inspectState(active.downloadId), undefined);
    assert.equal(oldRuntime.messageLifetimes.length, 0);
    assert.equal(newRuntime.inspectState(active.downloadId).state, 'completed');
    assert.deepEqual(workerMessages(app, 'REGISTER_DOWNLOAD').map(call => call.worker), ['updated']);
    assert.equal(workerMessages(app, 'GET_STREAMING_CAPABILITIES').length, 0);
    assert.equal(app.clicked.length, 1);
    assert.equal(mediaRequests, 1);
    assert.equal(app.serviceWorker.controller, root);
});

test('unsafe update paths end with no registration, navigation, media or Blob fallback', async (t) => {
    for (const failure of ['network', 'stalled-register', 'stalled-update', 'waiting', 'install-failed']) {
        const delayed = deferred();
        let app;
        app = createClientHarness({startTimeoutMs: 25,
            waitingWorker: failure === 'waiting' ? {} : null,
            onUpdate(registration) {
                if (failure === 'network') throw new TypeError('Update fetch failed');
                if (failure === 'stalled-update') return delayed.promise;
                if (failure === 'install-failed') {
                    const candidate = app.createWorker('failed');
                    candidate.state = 'installing';
                    registration.installing = candidate;
                    registration.updateFound();
                    queueMicrotask(() => {
                        registration.installing = null;
                        candidate.changeState('redundant');
                    });
                }
            },
        });
        t.after(() => app.client.close());
        if (failure === 'stalled-register') {
            app.serviceWorker.register = () => delayed.promise.then(() => app.registration);
        }
        const oldTask = safeStatus({state: 'fetching', streamingStarted: true});
        app.worker.statuses.set(oldTask.downloadId, oldTask);
        await assert.rejects(ordinary.download(ordinaryItem(), {
            streamingClient: app.client,
            fetchImpl() { assert.fail('No media fallback'); },
            handoffBlob() { assert.fail('No Blob fallback'); },
        }), error => {
            assert.equal(error.errorCode, failure === 'network' ? 'STREAMING_WORKER_UPDATE_FAILED'
                : failure === 'install-failed' ? 'STREAMING_WORKER_ACTIVATION_FAILED'
                    : 'STREAMING_WORKER_ACTIVATION_TIMEOUT');
            assert.equal(error.safeToFallback, false);
            assert.equal(ordinary.canFallbackToBlob(error), false);
            return true;
        });
        delayed.resolve();
        if (app.registration.waiting) app.changeController(app.registration.waiting);
        await new Promise(resolve => setImmediate(resolve));
        assertNoUpdateSideEffects(app);
        assert.equal(app.calls.filter(call => call.type === 'update').length,
            failure === 'stalled-register' ? 0 : 1);
        assert.deepEqual(app.worker.statuses.get(oldTask.downloadId), oldTask);
        assert.equal(app.worker.stateListeners.size, 0);
    }
});

test('a failed update is not retried automatically and does not poison a later user request', async (t) => {
    let failUpdate = true;
    const app = createClientHarness({onNavigate: emitNavigationStarted,
        onUpdate() { if (failUpdate) throw new TypeError('Offline update'); }});
    t.after(() => app.client.close());
    await assert.rejects(app.client.start(ordinaryItem()), error =>
        error.errorCode === 'STREAMING_WORKER_UPDATE_FAILED' && error.safeToFallback === false);
    await new Promise(resolve => setImmediate(resolve));
    assertNoUpdateSideEffects(app);
    assert.equal(app.calls.filter(call => call.type === 'update').length, 1);
    failUpdate = false;
    const active = await app.client.start(ordinaryItem());
    assert.equal(app.calls.filter(call => call.type === 'update').length, 2);
    assert.equal(workerMessages(app, 'REGISTER_DOWNLOAD').length, 1);
    await completeDownload(app, active);
});

test('cancellation interrupts registration/update/activation waits without late registration', async (t) => {
    for (const phase of ['register', 'update', 'activation']) {
        const entered = deferred();
        const release = deferred();
        const app = createClientHarness({
            waitingWorker: phase === 'activation' ? {} : null,
            onUpdate() {
                if (phase !== 'register') entered.resolve();
                if (phase === 'update') return release.promise;
            },
        });
        t.after(() => app.client.close());
        if (phase === 'register') {
            const original = app.serviceWorker.register;
            app.serviceWorker.register = async (...args) => {
                entered.resolve();
                await release.promise;
                return original(...args);
            };
        }
        const controller = new AbortController();
        const starting = app.client.start(selectedItem(), {signal: controller.signal});
        await entered.promise;
        controller.abort();
        await assert.rejects(starting, error => error.errorCode === 'USER_CANCELLED'
            && error.safeToFallback === false);
        release.resolve();
        if (app.registration.waiting) app.changeController(app.registration.waiting);
        await new Promise(resolve => setImmediate(resolve));
        assertNoUpdateSideEffects(app);
    }
});

test('one cancelled caller does not cancel another caller sharing the normal update', async (t) => {
    const entered = deferred();
    const release = deferred();
    const app = createClientHarness({onNavigate: emitNavigationStarted,
        onUpdate() { entered.resolve(); return release.promise; }});
    t.after(() => app.client.close());
    const controller = new AbortController();
    const cancelled = app.client.start(ordinaryItem(), {signal: controller.signal});
    const retained = app.client.start(ordinaryItem());
    await entered.promise;
    controller.abort();
    await assert.rejects(cancelled, error => error.errorCode === 'USER_CANCELLED');
    release.resolve();
    const active = await retained;
    assert.equal(app.calls.filter(call => call.type === 'update').length, 1);
    assert.equal(workerMessages(app, 'REGISTER_DOWNLOAD').length, 1);
    await completeDownload(app, active);
});

test("cancellation before transfer registration never starts later after a controller change", async (t) => {
    const app = createClientHarness({protocolVersion: 2, waitingWorker: {protocolVersion: 4}});
    t.after(() => app.client.close());
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(app.client.start(selectedItem(), {signal: controller.signal}),
        error => error.errorCode === "USER_CANCELLED" && error.safeToFallback === false);
    app.changeController(app.registration.waiting);
    await new Promise(resolve => setImmediate(resolve));
    assertNoUpdateSideEffects(app);
});

test("authorization expires during browser registration without creating a transfer or renewing the candidate", async (t) => {
    let now = Date.now();
    t.mock.method(Date, "now", () => now);
    const input = selectedItem("tiantianjiexi", {selection_expires_at: now + 1000});
    const app = createClientHarness({protocolVersion: 2});
    t.after(() => app.client.close());
    const register = app.serviceWorker.register;
    app.serviceWorker.register = async (...args) => {
        now += 2000;
        return register(...args);
    };
    await assert.rejects(app.client.start(input), error =>
        error.errorCode === "DESCRIPTOR_EXPIRED" && error.safeToFallback === false);
    assertNoUpdateSideEffects(app);
});

test("cached registration uses the current download-scope active Worker without replacing frames", async (t) => {
    const app = createClientHarness({onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const existing = await app.client.start(ordinaryItem());
    const oldFrame = app.frames[0];
    const current = app.createWorker("current", {protocolVersion: 4});
    app.changeController(current);
    const selected = await app.client.start(selectedItem());
    assert.equal(oldFrame.removed, false);
    assert.equal(workerMessages(app, "REGISTER_DOWNLOAD").at(-1).worker, "current");
    assert.equal(workerMessages(app, "GET_STREAMING_CAPABILITIES").length, 0);
    await completeDownload(app, existing);
    await completeDownload(app, selected);
});

test('root controller with an active old download is untouched by the scoped fallback', async (t) => {
    const app = createClientHarness({onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const rootWorker = {state: 'activated', postMessage() { assert.fail('Do not message or replace the root Worker'); }};
    app.serviceWorker.controller = rootWorker;
    const first = await app.client.start(bilibiliSelectedItem());
    assert.equal(app.serviceWorker.controller, rootWorker);
    assert.equal(app.calls[0].options.scope, '/__animeimages-stream-download/');
    assert.equal(workerMessages(app, 'REGISTER_DOWNLOAD').length, 1);
    assert.equal(app.clicked.length, 1);
    await completeDownload(app, first);
    assert.equal(app.serviceWorker.controller, rootWorker);
});

test('task status and cancellation stay with the owner across active replacement', async (t) => {
    const app = createClientHarness({onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const active = await app.client.start(ordinaryItem());
    const completion = active.completion.catch(error => error);
    const replacement = app.createWorker('replacement');
    app.changeController(replacement);
    assert.equal(replacement.statuses.size, 0);
    app.serviceWorker.emit({...app.worker.statuses.get(active.downloadId), state: 'completed', receivedBytes: 10}, {}, replacement);
    assert.equal((await app.client.getStatus(active.downloadId)).state, 'fetching');
    assert.equal((await active.cancel()).state, 'cancelled');
    assert.equal((await completion).errorCode, 'STREAMING_CANCELLED');
    assert.equal(workerMessages(app, 'CANCEL_DOWNLOAD').at(-1).worker, 'initial');
    assert.equal(app.clicked.length, 1);
});

test('replacement between registration ACK and navigation ends before any navigation', async (t) => {
    const app = createClientHarness();
    t.after(() => app.client.close());
    const original = app.worker.postMessage;
    app.worker.postMessage = function(message, ports) {
        original.call(this, message, ports);
        if (message.type === 'REGISTER_DOWNLOAD') app.changeController(app.createWorker('replacement'));
    };
    await assert.rejects(app.client.start(ordinaryItem()), error => error.errorCode === 'STREAMING_WORKER_CHANGED');
    assert.equal(app.clicked.length, 0);
    assert.equal(workerMessages(app, 'REGISTER_DOWNLOAD').length, 1);
    assert.equal(workerMessages(app, 'DELETE_DOWNLOAD').at(-1).worker, 'initial');
    assert.equal([...app.worker.statuses.values()][0].state, 'cancelled');
    assert.equal(app.registration.active.statuses.size, 0);
});

test('registration lifetime hands off to fetch without retaining the long stream on message', async () => {
    const body = new ReadableStream({start(controller) { controller.enqueue(new Uint8Array([1])); }});
    const descriptor = selectedDescriptor({size_bytes: 0});
    // Use an actual streaming response: EOF remains pending until cancellation.
    const streamRuntime = createWorkerRuntime(() => {
        const response = new Response(body, {headers: {'Content-Type': 'video/mp4'}});
        Object.defineProperty(response, 'url', {value: descriptor.url});
        return response;
    });
    await registerSelected(streamRuntime, descriptor);
    let preparationDone = false;
    streamRuntime.messageLifetimes[0].then(() => { preparationDone = true; });
    await Promise.resolve();
    assert.equal(preparationDone, false);
    const transfer = streamRuntime.startFetch(descriptor.downloadId);
    const response = await transfer.response;
    await streamRuntime.messageLifetimes[0];
    assert.equal(preparationDone, true);
    assert.equal(streamRuntime.inspectState(descriptor.downloadId).state, 'fetching');
    await response.body.cancel();
    await Promise.all(transfer.waitUntil);
    assert.equal(streamRuntime.inspectState(descriptor.downloadId).state, 'cancelled');
});

test('preparation cancellation and expiry release message lifetime and never fetch media', async () => {
    for (const operation of ['cancel', 'expire']) {
        const runtime = createWorkerRuntime(() => { assert.fail('No media before navigation'); });
        const descriptor = selectedDescriptor({selection_expires_at: Date.now() + 40});
        await registerSelected(runtime, descriptor);
        if (operation === 'cancel') await runtime.send({type: 'CANCEL_DOWNLOAD', downloadId: descriptor.downloadId});
        await runtime.messageLifetimes[0];
        const state = runtime.inspectState(descriptor.downloadId);
        assert.equal(state.state, operation === 'cancel' ? 'cancelled' : 'failed');
        assert.equal(state.preparation, null);
        assert.equal(state.url, '');
        assert.equal((await runtime.startFetch(descriptor.downloadId).response).status, 404);
    }
});

test('generic transfer preserves the original candidate deadline without business fields', () => {
    const expiry = Date.now() + 1000;
    const descriptor = protocol.createTransportDescriptor({...selectedItem('tiantianjiexi', {
        selection_expires_at: expiry,
    }), downloadId: 'f'.repeat(32)});
    assert.equal(descriptor.expiresAt, expiry);
    assert.equal(descriptor.selectionBinding, undefined);
});

test("ordinary download uses the compatible active download Worker without a Blob fallback", async (t) => {
    const app = createClientHarness({capabilitiesResponse: {ok: false, code: "UNKNOWN_MESSAGE"},
        onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const pending = ordinary.download(selectedItem(), {
        streamingClient: app.client,
        fetchImpl() { throw new Error("no page media fetch"); },
        handoffBlob() { throw new Error("no Blob handoff"); },
        onStage(stage) { assert.notEqual(stage, "updating"); },
    });
    await new Promise(resolve => setImmediate(resolve));
    const status = Array.from(app.statuses.values())[0];
    assert.ok(status);
    app.serviceWorker.emit({...status, state: "completed", receivedBytes: 10, streamingStarted: true});
    await pending;
    assert.equal(workerMessages(app, "REGISTER_DOWNLOAD").length, 1);
    assert.equal(app.clicked.length, 1);
});

test("legacy worker zero-byte completion is not reported as a successful file", async (t) => {
    const app = createClientHarness({protocolVersion: 2, onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const active = await app.client.start(selectedItem("tiantianjiexi", {size_bytes: 0}));
    const rejected = assert.rejects(active.completion, error => error.errorCode === "UPSTREAM_STREAM_FAILED"
        && error.streamingStarted === true);
    app.serviceWorker.emit({...app.statuses.get(active.downloadId), state: "completed", receivedBytes: 0});
    await rejected;
    assert.equal(workerMessages(app, "REGISTER_DOWNLOAD").length, 1);
});

test("legacy cancellation broadcast waits for the reader cancellation acknowledgement", async (t) => {
    const app = createClientHarness({protocolVersion: 2, onNavigate: emitNavigationStarted});
    t.after(() => app.client.close());
    const abort = new AbortController();
    const active = await app.client.start(selectedItem(), {signal: abort.signal});
    const postMessage = app.worker.postMessage;
    let acknowledge;
    app.worker.postMessage = function (message, ports) {
        if (message.type !== "CANCEL_DOWNLOAD") return postMessage.call(this, message, ports);
        const status = {...app.statuses.get(active.downloadId), state: "cancelled", errorCode: "USER_CANCELLED"};
        app.serviceWorker.emit(status);
        acknowledge = () => ports[0].postMessage({ok: true, status});
    };
    let finished = false;
    const rejected = assert.rejects(active.completion, error => error.errorCode === "USER_CANCELLED")
        .then(() => { finished = true; });
    abort.abort();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(finished, false);
    acknowledge();
    await rejected;
});

test("selected worker rejects old protocol registration and enforces task ownership", async () => {
    const runtime = createWorkerRuntime(async () => { throw new Error("no network"); });
    const descriptor = selectedDescriptor();
    assert.equal((await runtime.send({type: "REGISTER_DOWNLOAD", protocolVersion: 2, descriptor})).ok, false);
    assert.equal((await registerSelected(runtime, descriptor)).ok, true);
    assert.equal((await runtime.send({type: "CANCEL_DOWNLOAD", downloadId: descriptor.downloadId}, "other-client")).code,
        "DOWNLOAD_OWNER_MISMATCH");
    await runtime.send({type: "DELETE_DOWNLOAD", downloadId: descriptor.downloadId});
});

test("unknown cancellation fails honestly without a Blob-safe result", async () => {
    const app = createClientHarness({dropMessages: ["CANCEL_DOWNLOAD"], messageTimeoutMs: 5,
        onNavigate({serviceWorker, statuses}) {
            serviceWorker.emit({...Array.from(statuses.values())[0], state: "fetching", streamingStarted: true});
        }});
    const controller = new AbortController();
    const active = await app.client.start(selectedItem(), {signal: controller.signal});
    controller.abort();
    await assert.rejects(active.completion, error => error.errorCode === "STREAMING_CANCEL_STATE_UNKNOWN"
        && error.safeToFallback === false);
    await app.client.close();
});

test("real picker action validation through ordinary, client and worker streams two selected cards before EOF", async () => {
    class PickerNode extends Node {
        get isConnected() { return !this.parentNode ? this.tagName === "DOCUMENT" : this.parentNode.isConnected; }
        matches(selector) {
            if (selector.endsWith(":checked")) return this.checked && super.matches(selector.slice(0, -8));
            return super.matches(selector);
        }
        contains(node) { return this === node || this.children.some(child => child.contains(node)); }
        removeAttribute(name) { delete this.attributes[name]; }
    }
    const doc = new PickerNode("document");
    const win = {AbortController, URL, location: {href: "https://download.example.test/", origin: "https://download.example.test"}};
    const actions = [], media = [], results = [], endings = [deferred(), deferred()];
    const navigated = [deferred(), deferred()];
    let navigationCount = 0, blobCalls = 0;
    const app = integratedClient(async url => {
        const index = media.length; media.push(url);
        let sent = false;
        return {ok: true, status: 200, url, headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "2"}),
            body: new ReadableStream({async pull(controller) {
                if (!sent) { sent = true; controller.enqueue(Uint8Array.of(index, 7)); }
                else { await endings[index].promise; controller.close(); }
            }})};
    }, {onNavigate(navigation) { navigated[navigationCount++].resolve(navigation); }});
    const roots = [], controllers = [];
    for (const [index, provider] of ["tiantianjiexi", "kukutool"].entries()) {
        const root = new PickerNode("section", {"data-ytdlp-picker": "", "data-selection-url": "/media/service-selected-format/",
            "data-provider": provider, "data-selection-schema": "service_selected_format_v1"});
        for (const quality of ["low", "high"]) {
            const row = new PickerNode("label", {"data-picker-option": ""});
            const radio = new PickerNode("input", {"data-picker-radio": "", "data-kind": "video", value: `candidate-${quality}`,
                "data-token": `token-${quality}`, "data-format-id": `format-${quality}`, "data-can-download": "true",
                "data-legacy-asset-id": `douyin:7669858827638845160:video:${provider}:format:${(quality === "low" ? "b" : "a").repeat(24)}`});
            radio.checked = quality === (index ? "high" : "low"); row.append(radio); root.append(row);
        }
        root.append(new PickerNode("p", {"data-picker-status": ""})); doc.append(root); roots.push(root);
        controllers.push(picker.mount(root, doc, win, {
            fetchImpl: async (url, options) => {
                const body = JSON.parse(options.body); actions.push(body);
                assert.equal(body.action, "download");
                assert.equal(url, "https://download.example.test/media/service-selected-format/");
                const quality = body.candidate_id === "candidate-low" ? "low" : "high";
                const item = selectedItem(provider, {candidate_id: body.candidate_id, selected_format_id: `format-${quality}`,
                    asset_id: `douyin:7669858827638845160:video:${provider}:format:${(quality === "low" ? "b" : "a").repeat(24)}`,
                    filename: `${provider}-${quality}.bin`, url: `https://v11-default.365yg.com/${provider}/${quality}.mp4`,
                    size_bytes: 999, size_is_estimate: true});
                return {ok: true, json: async () => ({status: "success", item, server_media_relay: false})};
            },
            ordinary: {async download(item, options) {
                const result = await ordinary.download(item, {...options, streamingClient: app.client,
                    fetchImpl: async () => { blobCalls += 1; throw new Error("unexpected Blob fetch"); },
                    handoffBlob: async () => { blobCalls += 1; throw new Error("unexpected Blob handoff"); }});
                results.push(result); return result;
            }},
        }));
    }
    const saves = controllers.map(controller => controller.save());
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(navigationCount, 2, roots.map(root => root.querySelector("[data-picker-status]").textContent).join("; "));
    const responses = await Promise.all(navigated.map(async ready => (await ready.promise).response));
    assert.equal(media.length, 2); assert.equal(actions.length, 2); assert.equal(results.length, 0);
    // A later UI choice cannot replace either already-authorized stream.
    roots.forEach(root => root.querySelectorAll("[data-picker-radio]").forEach(radio => { radio.checked = !radio.checked; }));
    const readers = responses.map(response => response.body.getReader());
    for (let index = 0; index < 2; index += 1) {
        assert.deepEqual(Array.from((await readers[index].read()).value), [index, 7]);
        assert.equal(results.length, 0);
        assert.match(responses[index].headers.get("Content-Disposition"), index ? /kukutool-high.mp4/ : /tiantianjiexi-low.mp4/);
        endings[index].resolve();
        assert.equal((await readers[index].read()).done, true);
    }
    await Promise.all(saves);
    assert.equal(results.length, 2); assert.equal(blobCalls, 0);
    assert.ok(results.every(result => result.transport === "browser_streaming" && result.receivedBytes === 2));
    assert.deepEqual(media, ["https://v11-default.365yg.com/tiantianjiexi/low.mp4", "https://v11-default.365yg.com/kukutool/high.mp4"]);
    controllers.forEach(controller => controller.destroy()); await app.client.close();
});

test("worker refuses a new expired selected task but does not expire an already-started stream", async () => {
    let now = Date.now();
    class Clock extends Date { static now() { return now; } }
    let fetches = 0;
    const runtime = createWorkerRuntime(async url => { fetches += 1; return chunkedUpstream([[1, 2]], {url}).response; },
        {DateConstructor: Clock});
    const descriptor = selectedDescriptor({size_bytes: 2});
    await registerSelected(runtime, descriptor);
    const response = await runtime.startFetch(descriptor.downloadId).response;
    now = descriptor.expiresAt + 1;
    assert.equal((await response.arrayBuffer()).byteLength, 2);
    assert.equal(fetches, 1);
    const later = selectedDescriptor({size_bytes: 2});
    const second = {...later, downloadId: "f".repeat(32)};
    await registerSelected(runtime, second);
    now = second.expiresAt + 1;
    assert.equal((await runtime.startFetch(second.downloadId).response).status, 410);
    assert.equal(fetches, 1);
});
