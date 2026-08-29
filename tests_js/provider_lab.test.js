"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const lab = require("../static/js/provider_lab.js");


test("safeStatus accepts only fixed states", () => {
    assert.equal(lab.safeStatus("success"), "success");
    assert.equal(lab.safeStatus("private-state"), "failed");
});


test("selection accepts at most five explicit providers", () => {
    const inputs = Array.from({length: 5}, (_, index) => ({
        checked: true,
        disabled: false,
        value: `provider-${index}`,
    }));
    assert.equal(lab.selectedProviderIds(inputs, 5).length, 5);
    assert.deepEqual(lab.selectedProviderIds([...inputs, {checked: true, value: "six"}], 5), []);
});


test("selection ignores disabled providers and duplicate ids", () => {
    const inputs = [
        {checked: true, disabled: true, value: "videodl_repo"},
        {checked: true, disabled: false, value: "parsehub_repo"},
        {checked: true, disabled: false, value: "parsehub_repo"},
    ];
    assert.deepEqual(lab.selectedProviderIds(inputs, 5), ["parsehub_repo"]);
});


test("provider executions are bounded to three concurrent requests", async () => {
    let active = 0;
    let peak = 0;
    const resolvers = [];
    const promise = lab.runWithConcurrency([1, 2, 3, 4, 5], 3, async (value) => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => resolvers.push(resolve));
        active -= 1;
        return value;
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(active, 3);
    while (resolvers.length) {
        resolvers.shift()();
        await new Promise((resolve) => setImmediate(resolve));
    }
    assert.deepEqual(await promise, [1, 2, 3, 4, 5]);
    assert.equal(peak, 3);
});


test("one provider failure does not block other provider results", async () => {
    const results = await lab.runWithConcurrency(["ok", "bad", "later"], 3, async (value) => {
        if (value === "bad") {
            throw new Error("private failure");
        }
        return `${value}-done`;
    });
    assert.equal(results[0], "ok-done");
    assert.ok(results[1].error instanceof Error);
    assert.equal(results[2], "later-done");
});


test("timeout and failure text use fixed safe messages", () => {
    assert.equal(lab.errorMessage("PROVIDER_TIMEOUT"), "该 Provider 在限定时间内未返回。");
    assert.equal(lab.errorMessage("traceback with secret"), "Provider 执行失败。");
});


test("sanitized JSON filename contains only provider id", () => {
    assert.equal(
        lab.jsonFilename({provider_id: "parsehub_repo"}),
        "parsehub_repo-sanitized-result.json",
    );
    assert.equal(
        lab.jsonFilename({provider_id: "../../private"}),
        "-private-sanitized-result.json",
    );
});


test("bounded Range probe reads no more than 4096 bytes", async () => {
    let cancelled = false;
    let calls = 0;
    const fetchImpl = async (_url, options) => {
        assert.equal(options.credentials, "omit");
        assert.equal(options.headers.Range, "bytes=0-4095");
        return {
            status: 206,
            headers: {get(name) { return name === "Content-Range" ? "bytes 0-4095/9000" : "video/mp4"; }},
            body: {
                getReader() {
                    return {
                        async read() {
                            calls += 1;
                            return calls === 1
                                ? {done: false, value: new Uint8Array(8192)}
                                : {done: true};
                        },
                        async cancel() { cancelled = true; },
                    };
                },
            },
        };
    };
    const result = await lab.probeRange(fetchImpl, "https://cdn.example/video.mp4");
    assert.equal(result.bytes, 4096);
    assert.equal(cancelled, true);
});


test("requestJson sends one provider request with CSRF and no-store", async () => {
    let captured;
    const result = await lab.requestJson(async (url, options) => {
        captured = {url, options};
        return {ok: true, async json() { return {status: "success"}; }};
    }, "/admin/provider-lab/run/", "csrf", {
        source: "https://example.com/post",
        provider_id: "parsehub_repo",
    });
    assert.equal(result.status, "success");
    assert.equal(captured.options.cache, "no-store");
    assert.equal(captured.options.headers["X-CSRFToken"], "csrf");
    assert.deepEqual(Object.keys(JSON.parse(captured.options.body)).sort(), ["provider_id", "source"]);
});


test("Provider Lab frontend never persists result data", () => {
    const source = fs.readFileSync(path.join(__dirname, "../static/js/provider_lab.js"), "utf8");
    assert.equal(source.includes("localStorage"), false);
    assert.equal(source.includes("sessionStorage"), false);
    assert.equal(source.includes("indexedDB"), false);
});


test("ordinary display helpers do not invent unknown sizes", () => {
    assert.equal(lab.formatBytes(0), "大小未知");
    assert.equal(lab.formatDuration(0), "时长未知");
    assert.equal(lab.formatBytes(1024 * 1024), "1.0 MB");
});


test("controller initializes immediately after DOMContentLoaded", () => {
    let initialized = 0;
    const documentObject = {
        readyState: "complete",
        addEventListener() {
            assert.fail("completed documents must not wait for DOMContentLoaded");
        },
    };
    assert.equal(lab.initializeWhenReady(documentObject, () => { initialized += 1; }), true);
    assert.equal(initialized, 1);
});


test("controller waits once while the document is loading", () => {
    let listener;
    let options;
    let initialized = 0;
    const documentObject = {
        readyState: "loading",
        addEventListener(name, callback, suppliedOptions) {
            assert.equal(name, "DOMContentLoaded");
            listener = callback;
            options = suppliedOptions;
        },
    };
    assert.equal(lab.initializeWhenReady(documentObject, () => { initialized += 1; }), false);
    assert.equal(initialized, 0);
    assert.deepEqual(options, {once: true});
    listener();
    assert.equal(initialized, 1);
});


test("controller retries at DOMContentLoaded when an interactive document has no root yet", () => {
    let listener;
    let calls = 0;
    const documentObject = {
        readyState: "interactive",
        addEventListener(name, callback, options) {
            assert.equal(name, "DOMContentLoaded");
            assert.deepEqual(options, {once: true});
            listener = callback;
        },
    };
    assert.equal(lab.initializeWhenReady(documentObject, () => {
        calls += 1;
        return calls === 1 ? null : {ready: true};
    }), false);
    assert.equal(calls, 1);
    listener();
    assert.equal(calls, 2);
});
