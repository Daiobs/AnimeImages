"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ordinary = require("../static/js/ordinary_media_download.js");

function headers(values) {
    const normalized = Object.fromEntries(
        Object.entries(values || {}).map(([key, value]) => [key.toLowerCase(), String(value)]),
    );
    return {
        get(name) {
            return normalized[String(name || "").toLowerCase()] || null;
        },
    };
}

function responseFor(content, options) {
    const settings = options || {};
    const blob = new Blob([content], {type: settings.contentType || "video/mp4"});
    return {
        ok: settings.ok !== false,
        status: settings.status || 200,
        redirected: Boolean(settings.redirected),
        url: settings.url || "https://v11-default.365yg.com/media/video.mp4",
        headers: headers({
            "Content-Type": settings.contentType || "video/mp4",
            "Content-Length": settings.contentLength === undefined
                ? blob.size
                : settings.contentLength,
        }),
        async blob() {
            return blob;
        },
        body: settings.body === undefined ? {} : settings.body,
    };
}

function ordinaryItem(overrides) {
    return {
        type: "video",
        url: "https://v11-default.365yg.com/media/video.mp4",
        filename: "demo.mp4",
        provider: "tiantianjiexi",
        size_bytes: 5,
        browser_direct_fetch_safe: true,
        requires_merge: false,
        requires_cookie: false,
        ...(overrides || {}),
    };
}

function memoryStorage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
        serialized() { return JSON.stringify(Object.fromEntries(values)); },
    };
}

function eventTarget() {
    const listeners = new Map();
    return {
        addEventListener(name, listener) { listeners.set(name, listener); },
        removeEventListener(name, listener) {
            if (listeners.get(name) === listener) {
                listeners.delete(name);
            }
        },
        listener(name) { return listeners.get(name); },
    };
}

test("ordinary media classification requires a safe plain video", () => {
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem()), true);
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem({browser_direct_fetch_safe: false})), false);
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem({requires_merge: true})), false);
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem({requires_cookie: true})), false);
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem({type: "image"})), false);
    assert.equal(ordinary.isOrdinaryMediaItem(ordinaryItem({url: "javascript:alert(1)"})), false);
});

test("browser fetch options omit credentials, cache, and page referrer", () => {
    const options = ordinary.browserFetchOptions(undefined);
    assert.equal(options.method, "GET");
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
    assert.equal(options.referrerPolicy, "no-referrer");
});

test("bounded diagnostic probe reads only the first non-empty chunk and returns no URL", async () => {
    const signedUrl = "https://v11-default.365yg.com/path/video.mp4?token=super-secret";
    let reads = 0;
    let cancelled = false;
    let requestedUrl = "";
    let requestedOptions = null;
    const result = await ordinary.probeOrdinaryMediaFetch(ordinaryItem({url: signedUrl}), {
        fetchImpl: async (url, options) => {
            requestedUrl = url;
            requestedOptions = options;
            return {
                ok: true,
                status: 200,
                url: signedUrl,
                headers: headers({"Content-Type": "video/mp4", "Content-Length": 104857600}),
                body: {
                    getReader() {
                        return {
                            async read() {
                                reads += 1;
                                if (reads === 1) {
                                    return {done: false, value: new Uint8Array(0)};
                                }
                                if (reads === 2) {
                                    return {done: false, value: new Uint8Array(1024)};
                                }
                                assert.fail("probe must not read the complete media body");
                            },
                            cancel() {
                                cancelled = true;
                                return Promise.resolve();
                            },
                            releaseLock() {},
                        };
                    },
                },
            };
        },
    });

    assert.equal(requestedUrl, signedUrl);
    assert.equal(requestedOptions.credentials, "omit");
    assert.equal(requestedOptions.cache, "no-store");
    assert.equal(requestedOptions.referrerPolicy, "no-referrer");
    assert.equal(result.ok, true);
    assert.equal(result.mediaHost, "v11-default.365yg.com");
    assert.equal(result.contentLength, 104857600);
    assert.equal(reads, 2);
    assert.equal(cancelled, true);
    assert.doesNotMatch(JSON.stringify(result), /path\/video\.mp4|token|super-secret|https:\/\//i);
});

test("diagnostic probe safely distinguishes before-response and after-response failures", async () => {
    const signedUrl = "https://v11-default.365yg.com/path/video.mp4?token=super-secret";
    const beforeResponse = await ordinary.probeOrdinaryMediaFetch(
        ordinaryItem({url: signedUrl}),
        {
            fetchImpl: async () => {
                throw new TypeError(`failed ${signedUrl}`);
            },
        },
    );
    assert.equal(beforeResponse.ok, false);
    assert.equal(beforeResponse.failureClass, "FETCH_REJECTED_TYPE_ERROR");
    assert.equal(beforeResponse.failureStage, "FETCH_REJECTED_BEFORE_RESPONSE");

    const afterResponse = await ordinary.probeOrdinaryMediaFetch(
        ordinaryItem({url: signedUrl}),
        {
            fetchImpl: async () => ({
                ok: true,
                status: 200,
                url: signedUrl,
                headers: headers({"Content-Type": "video/mp4", "Content-Length": 10}),
                body: {
                    getReader() {
                        return {
                            async read() {
                                throw new TypeError(`stream failed ${signedUrl}`);
                            },
                            cancel() { return Promise.resolve(); },
                            releaseLock() {},
                        };
                    },
                },
            }),
        },
    );
    assert.equal(afterResponse.ok, false);
    assert.equal(afterResponse.failureClass, "FETCH_REJECTED_TYPE_ERROR");
    assert.equal(afterResponse.failureStage, "STREAM_BODY_FAILURE");
    assert.doesNotMatch(JSON.stringify({beforeResponse, afterResponse}), /token|super-secret|https:\/\//i);
});

test("diagnostic probe rejects a final response outside the Worker media allowlist", async () => {
    const result = await ordinary.probeOrdinaryMediaFetch(ordinaryItem({
        url: "https://v11-default.365yg.com/media/source.mp4?fixture=fake",
    }), {
        fetchImpl: async () => ({
            ok: true,
            status: 200,
            url: "https://attacker.example/redirected.mp4",
            headers: headers({"Content-Type": "video/mp4", "Content-Length": 10}),
            body: {
                getReader() {
                    return {
                        async read() { assert.fail("rejected response body must not be read"); },
                        cancel() { return Promise.resolve(); },
                        releaseLock() {},
                    };
                },
            },
        }),
    });
    assert.equal(result.ok, false);
    assert.equal(result.failureStage, "HTTP_RESPONSE_REJECTED");
    assert.equal(result.mediaHost, "v11-default.365yg.com");
    assert.doesNotMatch(JSON.stringify(result), /attacker|redirected/i);
});

test("diagnostic probe accepts the reviewed 365yg to bdcgslb final relation", async () => {
    let cancelled = false;
    const result = await ordinary.probeOrdinaryMediaFetch(ordinaryItem({
        url: "https://v5-default.365yg.com/media/source.mp4?fixture=fake",
    }), {
        fetchImpl: async () => ({
            ok: true,
            status: 200,
            redirected: true,
            url: "https://random.edge.bdcgslb.com/media/final.mp4?fixture=fake",
            headers: headers({"Content-Type": "video/mp4", "Content-Length": 10}),
            body: {
                getReader() {
                    return {
                        async read() { return {done: false, value: new Uint8Array(1024)}; },
                        cancel() {
                            cancelled = true;
                            return Promise.resolve();
                        },
                        releaseLock() {},
                    };
                },
            },
        }),
    });

    assert.equal(result.ok, true);
    assert.equal(result.mediaHost, "v5-default.365yg.com");
    assert.equal(cancelled, true);
    assert.doesNotMatch(JSON.stringify(result), /bdcgslb|media\/(?:source|final)|fixture/i);
});

test("diagnostic probe has a deterministic timeout and never invokes server relay", async () => {
    let relayCalls = 0;
    const result = await ordinary.probeOrdinaryMediaFetch(ordinaryItem({
        url: "https://v11-default.365yg.com/media/stalled.mp4?fixture=fake",
    }), {
        timeoutMs: 5,
        fetchImpl: async (_url, options) => new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => {
                const error = new Error("aborted");
                error.name = "AbortError";
                reject(error);
            }, {once: true});
        }),
        serverRelay() { relayCalls += 1; },
    });
    assert.equal(result.ok, false);
    assert.equal(result.failureClass, "FETCH_TIMEOUT_ERROR");
    assert.equal(result.failureStage, "FETCH_REJECTED_BEFORE_RESPONSE");
    assert.equal(relayCalls, 0);
});

test("Blob fallback applies the same source-bound final redirect policy", async () => {
    const initialUrl = "https://v5-default.365yg.com/media/source.mp4?fixture=fake";
    const finalUrl = "https://random.edge.bdcgslb.com/media/final.mp4?fixture=fake";
    let handoffBytes = 0;
    const result = await ordinary.downloadViaBlob(ordinaryItem({url: initialUrl}), {
        fetchImpl: async () => responseFor("12345", {
            redirected: true,
            url: finalUrl,
        }),
        async handoffBlob(blob) {
            handoffBytes = blob.size;
        },
    });

    assert.equal(result.receivedBytes, 5);
    assert.equal(handoffBytes, 5);

    let cancelled = false;
    await assert.rejects(
        ordinary.downloadViaBlob(ordinaryItem({url: initialUrl}), {
            fetchImpl: async () => responseFor("12345", {
                redirected: true,
                url: "https://unreviewed.example/media/final.mp4?fixture=fake",
                body: {
                    cancel() {
                        cancelled = true;
                        return Promise.resolve();
                    },
                },
            }),
            async handoffBlob() {
                assert.fail("unreviewed final hosts must fail before browser handoff");
            },
        }),
        (error) => {
            assert.equal(error.errorCode, "UPSTREAM_MEDIA_INVALID");
            assert.equal(error.status.responseRejectReason, "FINAL_HOST_REJECTED");
            assert.equal(error.status.mediaHost, "v5-default.365yg.com");
            assert.equal(error.status.observedFinalHost, "unreviewed.example");
            assert.equal(error.status.finalMediaHost, "unknown");
            assert.doesNotMatch(JSON.stringify(error.status), /media\/final|fixture|https:\/\//i);
            return true;
        },
    );
    assert.equal(cancelled, true);

    let directFinalOnlyFetches = 0;
    await assert.rejects(
        ordinary.downloadViaBlob(ordinaryItem({url: finalUrl}), {
            fetchImpl: async () => {
                directFinalOnlyFetches += 1;
                return responseFor("12345", {url: finalUrl});
            },
        }),
        (error) => {
            assert.equal(error.errorCode, "UPSTREAM_MEDIA_INVALID");
            assert.equal(error.status.responseRejectReason, "FINAL_HOST_REJECTED");
            assert.equal(error.status.mediaHost, "unknown");
            assert.doesNotMatch(JSON.stringify(error.status), /media\/final|fixture|https:\/\//i);
            return true;
        },
    );
    assert.equal(directFinalOnlyFetches, 0);
});

test("complete browser response is handed off once with exact bytes", async () => {
    const stages = [];
    const handoffs = [];
    let fetchOptions = null;
    const result = await ordinary.download(ordinaryItem(), {
        fetchImpl: async (_url, options) => {
            fetchOptions = options;
            return responseFor("12345");
        },
        onStage(stage) {
            stages.push(stage);
        },
        async handoffBlob(blob, filename) {
            handoffs.push({bytes: blob.size, filename});
        },
    });

    assert.equal(fetchOptions.referrerPolicy, "no-referrer");
    assert.deepEqual(stages, ["preparing", "fetching", "saving", "completed"]);
    assert.deepEqual(handoffs, [{bytes: 5, filename: "demo.mp4"}]);
    assert.equal(result.receivedBytes, 5);
    assert.equal(result.expectedBytes, 5);
});

test("progress events use received chunks and finish at exact bytes", async () => {
    const events = [];
    let readCount = 0;
    let now = 0;
    const response = {
        ok: true,
        status: 200,
        url: "https://v11-default.365yg.com/media/video.mp4",
        headers: headers({"Content-Type": "video/mp4", "Content-Length": 5}),
        body: {
            getReader() {
                return {
                    async read() {
                        readCount += 1;
                        if (readCount === 1) {
                            return {done: false, value: new Uint8Array(2)};
                        }
                        if (readCount === 2) {
                            return {done: false, value: new Uint8Array(3)};
                        }
                        return {done: true};
                    },
                    releaseLock() {},
                };
            },
        },
    };
    await ordinary.download(ordinaryItem(), {
        fetchImpl: async () => response,
        progressUpdateIntervalMs: 1,
        now() {
            now += 10;
            return now;
        },
        onProgress(event) { events.push(event); },
        async handoffBlob() {},
    });

    const received = events.map((event) => event.receivedBytes);
    assert.ok(received.includes(2));
    assert.ok(received.includes(5));
    assert.deepEqual([...received].sort((left, right) => left - right), received);
    assert.equal(events.at(-1).stage, "completed");
    assert.equal(events.at(-1).receivedBytes, 5);
    assert.equal(events.at(-1).totalBytes, 5);
    assert.equal(events.at(-1).percent, 100);
});

test("progress tracker is monotonic and calculates smoothed speed and ETA", () => {
    let now = 0;
    const tracker = ordinary.createTransferProgressTracker({
        totalBytes: 1000,
        now: () => now,
        updateIntervalMs: 750,
        speedWindowMs: 3000,
        minEtaSampleMs: 1000,
    });
    const events = [];
    events.push(tracker.update(0, "fetching", true));
    now = 500;
    assert.equal(tracker.update(200, "fetching", false), null);
    now = 1000;
    events.push(tracker.update(400, "fetching", false));
    now = 2000;
    events.push(tracker.update(700, "fetching", false));
    now = 2100;
    events.push(tracker.update(650, "fetching", true));

    assert.deepEqual(events.map((event) => Math.round(event.percent)), [0, 40, 70, 70]);
    assert.equal(Math.round(events[1].bytesPerSecond), 400);
    assert.equal(Math.round(events[1].etaSeconds * 10) / 10, 1.5);
    assert.ok(events[2].bytesPerSecond > 0);
});

test("unknown Content-Length reports bytes and speed without fake percent or ETA", () => {
    let now = 0;
    const tracker = ordinary.createTransferProgressTracker({
        now: () => now,
        updateIntervalMs: 1,
        minEtaSampleMs: 1,
    });
    now = 1000;
    const progress = tracker.update(2048, "fetching", true);
    assert.equal(progress.receivedBytes, 2048);
    assert.equal(progress.totalBytes, 0);
    assert.equal(progress.percent, null);
    assert.equal(progress.etaSeconds, null);
    assert.equal(ordinary.formatBytes(progress.receivedBytes), "2.00 KiB");
});

test("progress helper formats stable rate and ETA copy", () => {
    assert.equal(ordinary.formatRate(4 * 1024 * 1024), "4.00 MiB/s");
    assert.equal(ordinary.formatRate(0), "正在测速...");
    assert.equal(ordinary.formatEta(17), "剩余约 17 秒");
    assert.equal(ordinary.formatEta(83), "剩余约 1 分 23 秒");
});

test("Content-Length mismatch fails before browser save handoff", async () => {
    let handedOff = false;
    await assert.rejects(
        ordinary.download(ordinaryItem(), {
            fetchImpl: async () => responseFor("12345", {contentLength: 6}),
            async handoffBlob() {
                handedOff = true;
            },
        }),
        (error) => error.errorCode === "BROWSER_DOWNLOAD_SIZE_MISMATCH",
    );
    assert.equal(handedOff, false);
});

test("active marker is cleared after completion and stores no URL or token", async () => {
    const storage = memoryStorage();
    const target = eventTarget();
    let markerDuringFetch = "";
    await ordinary.download(ordinaryItem({
        url: "https://v11-default.365yg.com/video.mp4?token=secret",
        provider_manifest_token: "manifest-secret",
    }), {
        sessionStorage: storage,
        eventTarget: target,
        fetchImpl: async () => {
            markerDuringFetch = storage.serialized();
            return responseFor("12345");
        },
        async handoffBlob() {},
    });
    assert.match(markerDuringFetch, /downloadActive/);
    assert.match(markerDuringFetch, /tiantianjiexi/);
    assert.doesNotMatch(markerDuringFetch, /365yg|token|secret|manifest/);
    assert.equal(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
    assert.equal(target.listener("beforeunload"), undefined);
});

test("errors and aborts clear active marker and unload protection", async () => {
    for (const failure of ["error", "abort"]) {
        const storage = memoryStorage();
        const target = eventTarget();
        await assert.rejects(
            ordinary.download(ordinaryItem(), {
                sessionStorage: storage,
                eventTarget: target,
                fetchImpl: async () => {
                    const error = new Error(failure);
                    if (failure === "abort") {
                        error.name = "AbortError";
                    }
                    throw error;
                },
            }),
        );
        assert.equal(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
        assert.equal(target.listener("beforeunload"), undefined);
    }
});

test("cancelled beforeunload keeps the download active and completion clears its marker", () => {
    const storage = memoryStorage();
    const target = eventTarget();
    const protection = ordinary.createActiveDownloadProtection(
        ordinaryItem(),
        "demo.mp4",
        100,
        {sessionStorage: storage, eventTarget: target},
    );
    const event = {prevented: false, returnValue: null, preventDefault() { this.prevented = true; }};
    target.listener("beforeunload")(event);
    assert.equal(event.prevented, true);
    assert.equal(event.returnValue, "");
    protection.clear();
    assert.equal(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
});

test("confirmed beforeunload preserves the marker even when cleanup follows pagehide", () => {
    const storage = memoryStorage();
    const target = eventTarget();
    const protection = ordinary.createActiveDownloadProtection(
        ordinaryItem(),
        "demo.mp4",
        100,
        {sessionStorage: storage, eventTarget: target},
    );
    target.listener("beforeunload")({preventDefault() {}});
    target.listener("pagehide")();
    protection.clear({preserveDuringUnload: true});
    assert.notEqual(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
    const interrupted = ordinary.consumeInterruptedDownloadMarker({sessionStorage: storage});
    assert.equal(interrupted.downloadActive, true);
    assert.equal(interrupted.filename, "demo.mp4");
    assert.equal(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
});

test("failed cleanup before pagehide preserves the marker when navigation is confirmed", () => {
    const storage = memoryStorage();
    const target = eventTarget();
    const deferred = [];
    const protection = ordinary.createActiveDownloadProtection(
        ordinaryItem(),
        "demo.mp4",
        100,
        {
            sessionStorage: storage,
            eventTarget: target,
            defer(callback) { deferred.push(callback); },
        },
    );
    target.listener("beforeunload")({preventDefault() {}});
    protection.clear({preserveDuringUnload: true});
    assert.notEqual(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
    assert.equal(target.listener("beforeunload"), undefined);
    target.listener("pagehide")();
    deferred.shift()();
    assert.notEqual(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
});

test("failed cleanup clears the marker when the unload prompt is cancelled", () => {
    const storage = memoryStorage();
    const target = eventTarget();
    const deferred = [];
    const protection = ordinary.createActiveDownloadProtection(
        ordinaryItem(),
        "demo.mp4",
        100,
        {
            sessionStorage: storage,
            eventTarget: target,
            defer(callback) { deferred.push(callback); },
        },
    );
    target.listener("beforeunload")({preventDefault() {}});
    protection.clear({preserveDuringUnload: true});
    deferred.shift()();
    assert.equal(storage.getItem(ordinary.ACTIVE_DOWNLOAD_STORAGE_KEY), null);
    assert.equal(target.listener("pagehide"), undefined);
});

test("empty response fails before browser save handoff", async () => {
    let handedOff = false;
    await assert.rejects(
        ordinary.download(ordinaryItem({size_bytes: 0}), {
            fetchImpl: async () => responseFor("", {contentLength: 0}),
            async handoffBlob() {
                handedOff = true;
            },
        }),
        (error) => error.errorCode === "BROWSER_DOWNLOAD_EMPTY",
    );
    assert.equal(handedOff, false);
});

test("HTML, JSON, XML, and SVG responses are rejected", async () => {
    for (const contentType of [
        "text/html",
        "application/json",
        "application/xml",
        "image/svg+xml",
    ]) {
        await assert.rejects(
            ordinary.download(ordinaryItem(), {
                fetchImpl: async () => responseFor("12345", {contentType}),
                async handoffBlob() {
                    assert.fail("unsafe content must not be handed off");
                },
            }),
            (error) => (
                error.errorCode === "UPSTREAM_MEDIA_INVALID"
                && error.status.responseRejectReason === "CONTENT_TYPE_REJECTED"
            ),
        );
    }
});

test("video and binary media content types remain supported", () => {
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("video/mp4; charset=binary"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("video/webm"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("application/octet-stream"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("text/html"), false);
});

function unknownServiceFile(provider, type = "video", overrides = {}) {
    return ordinaryItem({provider, type, selection_schema: "service_selected_format_v1",
        candidate_id: "service-selected-candidate", selected_format_id: "",
        asset_id: "upstream:selection:service-selected-candidate", source_platform: "upstream",
        browser_upstream_bound: true, service_audio_file: type === "audio",
        selection_expires_at: Date.now() + 60000,
        url: "https://cdn.example.org/opaque-resource", filename: "upstream-quality.bin",
        audio_tracks: "unknown", video_tracks: "unknown", ...overrides});
}

function dedicatedServiceFile(provider, overrides = {}) {
    return ordinaryItem({provider, selection_schema: "service_selected_format_v1",
        candidate_id: "signed-candidate-low", selected_format_id: "", source_platform: "douyin",
        asset_id: `douyin:7123456789012345678:video:${provider}:format:0123456789abcdef01234567`,
        provider_resource_id: "7123456789012345678",
        provider_cache_key: `douyin:${provider}:validated:7123456789012345678`,
        provider_manifest_token: "signed-low-manifest:timestamp:signature",
        selection_expires_at: Date.now() + 60000,
        browser_upstream_bound: true, url: "https://v11-default.365yg.com/media/low.mp4?token=low-exact",
        filename: "low.mp4", ...overrides});
}

test("equal authorized full-video access conditions give old and selected cards the same streaming priority", async () => {
    const url = "https://v11-default.365yg.com/media/same.mp4";
    const oldUpstream = provider => ordinaryItem({provider, url, source_platform: "upstream",
        selection_schema: "upstream_resource_v1", browser_upstream_bound: true,
        asset_id: `upstream:${"a".repeat(32)}:video:1`, provider_resource_id: "b".repeat(64),
        provider_cache_key: `upstream:${"a".repeat(32)}`, provider_manifest_token: "signed-original-manifest",
        selection_expires_at: Date.now() + 60000});
    const cases = [
        ordinaryItem({provider: "", url}), ordinaryItem({provider: "tiantianjiexi", url}),
        ordinaryItem({provider: "kukutool", url}),
        ...["tiantianjiexi", "kukutool"].map(provider => dedicatedServiceFile(provider, {url})),
        ...["tiantianjiexi_generic", "kukutool_generic"].map(provider => unknownServiceFile(provider, "video", {
            url, selection_expires_at: Date.now() + 60000})),
        unknownServiceFile("yt_dlp_generic", "video", {url, selection_schema: "ytdlp_selected_format_v1",
            selection_expires_at: Date.now() + 60000}),
        ...["yt_dlp_generic", "parsehub_generic"].map(oldUpstream),
    ];
    for (const item of cases) {
        let starts = 0;
        const result = await ordinary.download(item, {forceBlob: true,
            fetchImpl: () => assert.fail("card identity must not force a page fetch"),
            handoffBlob: () => assert.fail("card identity must not force a Blob"),
            streamingClient: {capabilityAvailable: () => true, async start(actual) {
                starts += 1;
                assert.equal(actual, item);
                assert.equal(actual.url, url);
                return {downloadId: "same-resource", completion: Promise.resolve({filename: item.filename,
                    receivedBytes: 5, totalBytes: 5})};
            }},
        });
        assert.equal(result.transport, "browser_streaming", `${item.provider}/${item.selection_schema}`);
        assert.equal(starts, 1);
    }
});

test("non-SW compatibility cannot bypass a declared resource binding or its original expiry", async () => {
    const resource = ordinaryItem({provider: "parsehub_generic", url: "https://cdn.example.org/video.mp4",
        source_platform: "upstream", selection_schema: "upstream_resource_v1", browser_upstream_bound: true,
        asset_id: `upstream:${"a".repeat(32)}:video:1`, provider_resource_id: "b".repeat(64),
        provider_cache_key: `upstream:${"a".repeat(32)}`, provider_manifest_token: "signed-resource",
        selection_expires_at: Date.now() + 60000});
    for (const changes of [{provider_manifest_token: undefined}, {selection_expires_at: undefined},
        {selection_expires_at: Date.now() - 1}, {provider_cache_key: "other"}]) {
        for (const run of [ordinary.download, ordinary.downloadViaBlob]) {
            let fetches = 0, saves = 0;
            await assert.rejects(run({...resource, ...changes}, {
                fetchImpl: () => { fetches += 1; throw new Error("unexpected-fetch"); },
                handoffBlob: () => { saves += 1; },
            }), error => error.message === "INVALID_SELECTION_BINDING" || error.errorCode === "DESCRIPTOR_EXPIRED");
            assert.equal(fetches, 0);
            assert.equal(saves, 0);
        }
    }
});

for (const provider of ["tiantianjiexi", "kukutool"]) {
    test(`${provider} bound selected format uses streaming when available without full Blob buffering`, async () => {
        for (const suffix of ["", ":format:0123456789abcdef01234567"]) {
            const descriptor = dedicatedServiceFile(provider, {asset_id: `douyin:7123456789012345678:video:${provider}${suffix}`});
            const before = JSON.stringify(descriptor);
            const streams = [];
            assert.equal(ordinary.isOrdinaryMediaItem(descriptor), true);
            const result = await ordinary.download(descriptor, {
                forceBlob: true,
                fetchImpl: () => assert.fail("streaming selection must not fetch a page Blob"),
                streamingClient: {capabilityAvailable: () => true,
                    async start(item, options) {
                        streams.push({...item});
                        assert.equal(options.filename, descriptor.filename);
                        return {downloadId: "selected-stream", completion: Promise.resolve({
                            filename: item.filename, receivedBytes: 5, totalBytes: 5,
                        })};
                    }},
                handoffBlob: () => assert.fail("streaming selection must not hand off a Blob"),
            });
            assert.equal(result.transport, "browser_streaming");
            assert.deepEqual(streams, [descriptor]);
            assert.equal(JSON.stringify(descriptor), before);
        }
    });

    test(`${provider} dedicated selected URL cannot redirect or return HTML before save`, async () => {
        const descriptor = dedicatedServiceFile(provider);
        for (const change of [{redirected: true}, {url: "https://v11-default.365yg.com/media/primary.mp4"},
            {contentType: "text/html"}, {type: "opaque"}, {type: "opaqueredirect"}]) {
            await assert.rejects(ordinary.download(descriptor, {
                fetchImpl: async () => ({...responseFor("12345", {url: descriptor.url, ...change}),
                    ...(change.type ? {type: change.type} : {})}),
                handoffBlob: () => assert.fail("invalid response must not save"),
            }));
        }
    });

    test(`${provider} dedicated selection rejects incomplete or inconsistent manifest identity before media fetch`, async () => {
        const descriptor = dedicatedServiceFile(provider);
        const changes = [
            {provider: "unknown"}, {provider: `${provider}_custom`}, {provider: `${provider}_generic`},
            {provider: provider === "kukutool" ? "tiantianjiexi" : "kukutool"},
            {source_platform: "upstream"}, {source_platform: "third_party"}, {type: "audio", service_audio_file: true},
            {asset_id: "upstream:selection:signed-candidate-low"}, {asset_id: `douyin:8123456789012345678:video:${provider}`},
            {asset_id: `${descriptor.asset_id}extra`}, {asset_id: `douyin:7123456789012345678:video:${provider}:format:xyz`},
            {provider_resource_id: "8123456789012345678"}, {provider_resource_id: 7123456789012345678},
            {provider_cache_key: "cache-for-another-work"}, {selected_format_id: undefined},
            {browser_upstream_bound: false}, {browser_upstream_bound: "true"}, {browser_direct_fetch_safe: "true"},
            {requires_merge: undefined}, {requires_cookie: undefined}, {selection_delivery: "legacy_manifest"},
            {url: "http://v11-default.365yg.com/media/low.mp4"},
            {url: "https://user:password@v11-default.365yg.com/media/low.mp4"},
            {url: `${descriptor.url}#fragment`}, {url: "https://v11-default.365yg.com:8443/media/low.mp4"},
        ];
        for (const field of ["candidate_id", "provider_resource_id", "provider_manifest_token", "asset_id", "provider_cache_key"]) {
            for (const value of [undefined, "", " ", "bad\nvalue"]) changes.push({[field]: value});
        }
        for (const change of changes) {
            const invalid = {...descriptor, ...change};
            assert.equal(ordinary.isOrdinaryMediaItem(invalid), false, JSON.stringify(change));
            await assert.rejects(ordinary.download(invalid, {
                fetchImpl: () => assert.fail("invalid identity must not fetch"),
                handoffBlob: () => assert.fail("invalid identity must not save"),
            }), error => error.errorCode === "BROWSER_DOWNLOAD_NOT_ORDINARY_MEDIA");
        }
    });
}

test("selected stream expiry and malformed binding fail before even a compatibility fetch", async () => {
    for (const changes of [{selection_expires_at: Date.now() - 1}, {selection_expires_at: undefined},
        {selection_expires_at: "9999999999999"}]) {
        await assert.rejects(ordinary.download(dedicatedServiceFile("tiantianjiexi", changes), {
            streamingClient: {capabilityAvailable: () => false},
            fetchImpl: () => assert.fail("expired selection must not fetch"),
            handoffBlob: () => assert.fail("expired selection must not save"),
        }));
    }
});

test("selected stream capability absence is an explicit one-fetch Blob compatibility path", async () => {
    const item = dedicatedServiceFile("tiantianjiexi");
    let fetches = 0, handoffs = 0;
    const result = await ordinary.download(item, {
        streamingClient: {capabilityAvailable: () => false},
        fetchImpl: async (url, options) => {
            fetches += 1;
            assert.equal(url, item.url);
            assert.equal(options.redirect, "error");
            return responseFor("12345", {url});
        },
        handoffBlob: async (blob, filename) => {
            handoffs += 1;
            assert.equal(await blob.text(), "12345");
            assert.equal(filename, item.filename);
        },
    });
    assert.equal(result.transport, "browser_blob");
    assert.equal(fetches, 1);
    assert.equal(handoffs, 1);
});

test("a plausible error name without explicit unstarted proof never retries selected media", async () => {
    for (const evidence of [{}, {streamingStarted: false}, {safeToFallback: true},
        {safeToFallback: true, streamingStarted: true}]) {
        const error = Object.assign(new Error("start state unavailable"), {
            errorCode: "STREAMING_MESSAGE_TIMEOUT", ...evidence,
        });
        await assert.rejects(ordinary.download(dedicatedServiceFile("kukutool"), {
            streamingClient: {capabilityAvailable: () => true, start: async () => { throw error; }},
            fetchImpl: () => assert.fail("unknown start must not resend"),
            handoffBlob: () => assert.fail("unknown start must not save a Blob"),
        }), actual => actual === error);
    }
});

test("failed SW setup never renews an expiring selection or starts a Blob request", async t => {
    const now = Date.now();
    t.mock.method(Date, "now", () => now);
    const item = dedicatedServiceFile("tiantianjiexi", {selection_expires_at: now + 1000});
    await assert.rejects(ordinary.download(item, {
        streamingClient: {capabilityAvailable: () => true, async start() {
            t.mock.method(Date, "now", () => now + 1001);
            throw Object.assign(new Error("registration failed"), {
                errorCode: "STREAMING_REGISTRATION_FAILED", streamingStarted: false, safeToFallback: true,
            });
        }},
        fetchImpl: () => assert.fail("expired authorization cannot start a fallback request"),
    }), error => error.errorCode === "STREAMING_REGISTRATION_FAILED");
});

test("worker upgrade is not browser incapability and never starts a Blob request", async () => {
    let fetches = 0;
    const item = dedicatedServiceFile("kukutool");
    for (const code of ["STREAMING_PROTOCOL_UNSUPPORTED", "STREAMING_UPDATE_REQUIRED"]) {
        await assert.rejects(ordinary.download(item, {
            streamingClient: {capabilityAvailable: () => true, async start() {
                throw Object.assign(new Error("old protocol"), {
                    errorCode: code, streamingStarted: false, safeToFallback: true,
                });
            }},
            fetchImpl: async url => { fetches += 1; return responseFor("12345", {url}); },
            handoffBlob: async () => {},
        }), error => error.errorCode === code);
        assert.equal(fetches, 0);
        assert.match(ordinary.safeErrorMessage({errorCode: code}), /尚未开始下载/);
        assert.doesNotMatch(ordinary.safeErrorMessage({errorCode: code}), /其他下载|关闭所有/);
    }
});

test("service schema cannot grant arbitrary non-upstream providers ordinary download permission", () => {
    for (const provider of ["unknown", "yt_dlp_generic", "gallery_dl_generic", "parsehub_generic"]) {
        for (const source of [{}, {asset_id: "upstream:selection:service-selected-candidate", source_platform: "upstream"}]) {
            assert.equal(ordinary.isOrdinaryMediaItem(dedicatedServiceFile(provider, source)), false);
        }
    }
});

for (const provider of ["tiantianjiexi_generic", "kukutool_generic"]) {
    test(`${provider} unknown suffix uses validated HTTP media MIME without changing its descriptor`, async () => {
        for (const [type, contentType, suffix] of [
            ["video", "video/mp4; codecs=avc1", "mp4"], ["video", "video/webm", "webm"],
            ["audio", "audio/mp4", "m4a"], ["audio", "audio/mpeg", "mp3"],
            ["audio", "audio/webm", "webm"], ["audio", "audio/ogg", "ogg"],
        ]) {
            const descriptor = unknownServiceFile(provider, type);
            const before = JSON.stringify(descriptor);
            const handoffs = [];
            const result = await ordinary.download(descriptor, {
                fetchImpl: async url => {
                    assert.equal(url, descriptor.url);
                    return responseFor("12345", {contentType, url});
                },
                handoffBlob: async (blob, filename) => handoffs.push({filename, body: await blob.text()}),
            });
            assert.deepEqual(handoffs, [{filename: `upstream-quality.${suffix}`, body: "12345"}]);
            assert.equal(result.filename, `upstream-quality.${suffix}`);
            assert.equal(result.receivedBytes, 5);
            assert.equal(JSON.stringify(descriptor), before, "MIME improves only the save filename, not format or track facts");
            assert.equal(descriptor.filename, "upstream-quality.bin");
            assert.equal(descriptor.selected_format_id, "");
        }
    });

    test(`${provider} MIME suffix inference never overwrites known or nonterminal suffixes`, async () => {
        for (const [type, filename, contentType] of [
            ["video", "original.mov", "video/mp4"], ["video", "original.webm", "video/mp4"],
            ["audio", "original.aac", "audio/mp4"], ["audio", "original.ogg", "audio/mpeg"],
            ["video", "original.bin.backup", "video/mp4"],
        ]) {
            const descriptor = unknownServiceFile(provider, type, {filename});
            const result = await ordinary.download(descriptor, {
                fetchImpl: async () => responseFor("12345", {contentType, url: descriptor.url}),
                handoffBlob: async (_blob, actualName) => assert.equal(actualName, filename),
            });
            assert.equal(result.filename, filename);
            assert.equal(descriptor.filename, filename);
        }
        const descriptor = unknownServiceFile(provider);
        const result = await ordinary.download(descriptor, {filename: "user-choice.mp4",
            fetchImpl: async () => responseFor("12345", {contentType: "video/webm", url: descriptor.url}),
            handoffBlob: async (_blob, filename) => assert.equal(filename, "user-choice.mp4"),
        });
        assert.equal(result.filename, "user-choice.mp4");
    });

    test(`${provider} generic MIME leaves unknown suffix honest instead of guessing from URL or label`, async () => {
        for (const type of ["video", "audio"]) {
            const descriptor = unknownServiceFile(provider, type, {url: "https://cdn.example.org/looks-like-1080p.mp4",
                filename: "upstream-1080P.BIN", quality_label: "1080P"});
            const result = await ordinary.download(descriptor, {
                fetchImpl: async () => responseFor("12345", {contentType: "application/octet-stream", url: descriptor.url}),
                handoffBlob: async (_blob, filename) => assert.equal(filename, "upstream-1080P.BIN"),
            });
            assert.equal(result.filename, "upstream-1080P.BIN");
        }
    });

    test(`${provider} HTML and invalid blobs cannot reach MIME-based rename or save`, async () => {
        for (const type of ["video", "audio"]) {
            for (const [content, options, errorCode] of [
                ["<html>login</html>", {contentType: "text/html; charset=utf-8"}, "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT"],
                ["", {contentType: type === "audio" ? "audio/mp4" : "video/mp4", contentLength: 0}, "BROWSER_DOWNLOAD_EMPTY"],
                ["12345", {contentType: type === "audio" ? "audio/mp4" : "video/mp4", contentLength: 8}, "BROWSER_DOWNLOAD_SIZE_MISMATCH"],
            ]) {
                const descriptor = unknownServiceFile(provider, type);
                const before = JSON.stringify(descriptor);
                await assert.rejects(ordinary.download(descriptor, {
                    fetchImpl: async () => responseFor(content, {...options, url: descriptor.url}),
                    handoffBlob: () => assert.fail("rejected bytes must never reach save handoff"),
                }), error => error.errorCode === errorCode);
                assert.equal(JSON.stringify(descriptor), before);
            }
        }
    });
}

test("MIME suffix inference does not extend to yt-dlp or legacy ordinary video", async () => {
    for (const descriptor of [ordinaryItem({filename: "legacy.bin"}),
        unknownServiceFile("yt_dlp_generic", "video", {selection_schema: "ytdlp_selected_format_v1", filename: "ytdlp.bin"})]) {
        const result = await ordinary.download(descriptor, {
            fetchImpl: async () => responseFor("12345", {contentType: "video/mp4", url: descriptor.url}),
            handoffBlob: async (_blob, filename) => assert.equal(filename, descriptor.filename),
        });
        assert.equal(result.filename, descriptor.filename);
    }
});

test("declared or response size above the bounded Blob limit is rejected", async () => {
    await assert.rejects(
        ordinary.download(ordinaryItem({size_bytes: 11}), {
            maxBytes: 10,
            fetchImpl: async () => assert.fail("declared limit must fail before fetch"),
        }),
        (error) => error.errorCode === "BROWSER_BLOB_TOO_LARGE",
    );
    await assert.rejects(
        ordinary.download(ordinaryItem({size_bytes: 0}), {
            maxBytes: 10,
            fetchImpl: async () => responseFor("12345", {contentLength: 11}),
        }),
        (error) => error.errorCode === "BROWSER_BLOB_TOO_LARGE",
    );
});

test("a missing Content-Length is still bounded while the response body is read", async () => {
    let cancelled = false;
    let readCount = 0;
    const response = {
        ok: true,
        status: 200,
        url: "https://v11-default.365yg.com/media/video.mp4",
        headers: headers({"Content-Type": "video/mp4"}),
        body: {
            getReader() {
                return {
                    async read() {
                        readCount += 1;
                        if (readCount === 1) {
                            return {done: false, value: new Uint8Array(6)};
                        }
                        return {done: true, value: undefined};
                    },
                    async cancel() {
                        cancelled = true;
                    },
                    releaseLock() {},
                };
            },
        },
        async blob() {
            assert.fail("streaming response should use the bounded reader");
        },
    };

    await assert.rejects(
        ordinary.download(ordinaryItem({size_bytes: 0}), {
            maxBytes: 5,
            fetchImpl: async () => response,
        }),
        (error) => error.errorCode === "BROWSER_BLOB_TOO_LARGE",
    );
    assert.equal(cancelled, true);
});

test("a stalled browser fetch is aborted with a safe retryable error", async () => {
    let aborted = false;
    await assert.rejects(
        ordinary.download(ordinaryItem(), {
            idleTimeoutMs: 5,
            fetchImpl: async (_url, options) => new Promise((_resolve, reject) => {
                options.signal.addEventListener("abort", () => {
                    aborted = true;
                    const error = new Error("aborted");
                    error.name = "AbortError";
                    reject(error);
                }, {once: true});
            }),
        }),
        (error) => {
            assert.equal(error.errorCode, "BROWSER_DOWNLOAD_STALLED");
            assert.doesNotMatch(ordinary.safeErrorMessage(error), /服务器|中转|Relay/);
            return true;
        },
    );
    assert.equal(aborted, true);
});

test("a stalled response body is aborted by the same inactivity guard", async () => {
    let bodyAborted = false;
    await assert.rejects(
        ordinary.download(ordinaryItem(), {
            idleTimeoutMs: 5,
            fetchImpl: async (_url, options) => ({
                ok: true,
                status: 200,
                url: "https://v11-default.365yg.com/media/video.mp4",
                headers: headers({
                    "Content-Type": "video/mp4",
                    "Content-Length": 5,
                }),
                body: {
                    getReader() {
                        return {
                            async read() {
                                return new Promise((_resolve, reject) => {
                                    options.signal.addEventListener("abort", () => {
                                        bodyAborted = true;
                                        const error = new Error("aborted");
                                        error.name = "AbortError";
                                        reject(error);
                                    }, {once: true});
                                });
                            },
                            releaseLock() {},
                        };
                    },
                },
            }),
        }),
        (error) => error.errorCode === "BROWSER_DOWNLOAD_STALLED",
    );
    assert.equal(bodyAborted, true);
});

test("network and HTTP errors never include the signed URL", async () => {
    const signedUrl = "https://v11-default.365yg.com/video.mp4?token=secret";
    for (const fetchImpl of [
        async () => { throw new TypeError(`failed ${signedUrl}`); },
        async () => responseFor("error", {ok: false, status: 403}),
    ]) {
        await assert.rejects(
            ordinary.download(ordinaryItem({url: signedUrl}), {fetchImpl}),
            (error) => {
                assert.doesNotMatch(error.message, /token=secret/);
                return String(error.errorCode).startsWith("BROWSER_FETCH_");
            },
        );
    }
});

test("safe error copy asks for retry or reparse without server fallback", () => {
    const message = ordinary.safeErrorMessage({errorCode: "BROWSER_FETCH_FAILED"});
    assert.match(message, /重试下载/);
    assert.match(message, /重新解析/);
    assert.doesNotMatch(message, /服务器|中转|Relay|Blob|直连/);
});

test("filename sanitizer removes path and shell delimiters", () => {
    assert.equal(
        ordinary.sanitizeFilename("../bad:name?.mp4"),
        ".._bad_name_.mp4",
    );
});

test("default Blob handoff creates a download anchor without playback target", () => {
    const appended = [];
    let clicked = false;
    let removed = false;
    let revoked = "";
    const link = {
        href: "",
        download: "",
        rel: "",
        style: {},
        click() { clicked = true; },
        remove() { removed = true; },
    };
    ordinary.defaultBlobHandoff(new Blob(["12345"]), "demo.mp4", {
        documentObject: {
            body: {appendChild(node) { appended.push(node); }},
            createElement(tag) {
                assert.equal(tag, "a");
                return link;
            },
        },
        urlApi: {
            createObjectURL() { return "blob:proof"; },
            revokeObjectURL(value) { revoked = value; },
        },
        schedule(callback, delay) {
            assert.equal(delay, 60000);
            callback();
        },
    });

    assert.equal(appended.length, 1);
    assert.equal(clicked, true);
    assert.equal(removed, true);
    assert.equal(link.href, "blob:proof");
    assert.equal(link.download, "demo.mp4");
    assert.equal(Object.hasOwn(link, "target"), false);
    assert.equal(revoked, "blob:proof");
});

test("Single and Batch ordinary paths share the primitive without relay fallback", () => {
    const singleSource = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    const batchSource = fs.readFileSync(
        path.join(__dirname, "../static/js/batch_parse.js"),
        "utf8",
    );
    const singleStart = singleSource.indexOf("async function handleDirectRoute");
    const singleEnd = singleSource.indexOf("async function saveDownloadResponse", singleStart);
    const singleOrdinaryPath = singleSource.slice(singleStart, singleEnd);

    assert.ok(singleStart >= 0);
    assert.match(singleOrdinaryPath, /downloadOrdinaryMediaRouteItems/);
    assert.doesNotMatch(singleOrdinaryPath, /prepareAndHandoffServerSingle/);
    assert.doesNotMatch(singleOrdinaryPath, /download-prepare|download-stream|download-status/);
    assert.match(batchSource, /ordinaryMediaDownloadApi\.download/);
    assert.doesNotMatch(batchSource, /handoffBatchDirectUrl/);
    assert.doesNotMatch(batchSource, /server_single/);
});
