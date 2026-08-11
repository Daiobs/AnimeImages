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
        headers: headers({
            "Content-Type": settings.contentType || "video/mp4",
            "Content-Length": settings.contentLength === undefined
                ? blob.size
                : settings.contentLength,
        }),
        async blob() {
            return blob;
        },
    };
}

function ordinaryItem(overrides) {
    return {
        type: "video",
        url: "https://media.example.test/video.mp4",
        filename: "demo.mp4",
        provider: "tiantianjiexi",
        size_bytes: 5,
        browser_direct_fetch_safe: true,
        requires_merge: false,
        requires_cookie: false,
        ...(overrides || {}),
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
            (error) => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT",
        );
    }
});

test("video and binary media content types remain supported", () => {
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("video/mp4; charset=binary"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("video/webm"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("application/octet-stream"), true);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("text/html"), false);
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
    const signedUrl = "https://media.example.test/video.mp4?token=secret";
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
