"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const reliability = require("../static/js/download_reliability.js");

function response(status, retryAfter = null, payload = {ok: true, status: "streaming"}) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: {get: (name) => name.toLowerCase() === "retry-after" ? retryAfter : null},
        async json() {
            assert.notEqual(status, 429, "busy bodies must not be parsed as download states");
            assert.notEqual(status, 503, "busy HTML bodies are not download failures");
            return payload;
        },
    };
}

async function runPoll(responses, extra = {}) {
    let elapsed = 0;
    const waits = [], requests = [];
    const epoch = Date.UTC(2026, 0, 1);
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/test-ticket/",
        maxDurationMs: 15000,
        maxPolls: 30,
        now: () => elapsed,
        epochNow: () => epoch + elapsed,
        wait: async (delay) => { waits.push(delay); elapsed += delay; },
        fetchStatus: async (url, options) => {
            assert.equal(url, "/media/download-status/test-ticket/");
            assert.equal(options.method, "GET");
            assert.equal(options.credentials, "same-origin");
            requests.push(elapsed);
            return responses[Math.min(requests.length - 1, responses.length - 1)];
        },
        ...extra,
    });
    return {result, waits, requests, elapsed};
}

for (const code of [429, 503]) {
    test(`${code} respects Retry-After seconds without starting another download`, async () => {
        const observation = await runPoll([response(code, "2"), response(200)]);
        assert.deepEqual(observation.requests, [0, 2000]);
        assert.equal(observation.result.status, "streaming");
    });

    test(`${code} respects an HTTP-date`, async () => {
        const date = new Date(Date.UTC(2026, 0, 1, 0, 0, 3)).toUTCString();
        const observation = await runPoll([response(code, date), response(200)]);
        assert.deepEqual(observation.waits, [3000]);
        assert.equal(observation.result.status, "streaming");
    });

    test(`${code} longer than budget stops as unconfirmed rather than retrying early`, async () => {
        const observation = await runPoll([response(code, "60")]);
        assert.deepEqual(observation.requests, [0]);
        assert.deepEqual(observation.waits, []);
        assert.equal(observation.result.status, "poll_timeout");
        assert.equal(reliability.downloadStartDisposition(observation.result), "unconfirmed");
        assert.equal(observation.result.httpStatus, code);
        assert.equal(observation.result.retryAfterMs, 60000);
        assert.match(observation.result.message, /请勿反复点击下载/);
        assert.equal(observation.result.streamStarted, false);
    });
}

test("invalid/missing Retry-After uses bounded increasing backoff", async () => {
    const observation = await runPoll([
        response(429, "invalid"), response(503), response(429, "-5"), response(200),
    ]);
    assert.deepEqual(observation.waits, [1000, 2000, 4000]);
    assert.equal(observation.result.status, "streaming");
});

test("valid ordinary response resets the busy backoff", async () => {
    const observation = await runPoll([
        response(429), response(200, null, {ok: true, status: "connecting"}),
        response(503), response(200),
    ], {pollAfterMs: 500});
    assert.deepEqual(observation.waits, [1000, 500, 1000]);
    assert.equal(observation.result.status, "streaming");
});

test("repeated busy replies stay inside the original time and count limits", async () => {
    const observation = await runPoll([response(503)], {maxPolls: 1000, maxDurationMs: 60000});
    assert.ok(observation.requests.length <= reliability.MAX_STATUS_POLL_COUNT);
    assert.ok(observation.elapsed < reliability.MAX_STATUS_POLL_DURATION_MS);
    assert.ok(Math.max(...observation.waits) <= 4000);
    assert.equal(reliability.downloadStartDisposition(observation.result), "unconfirmed");
});

test("exhausted count after a busy reply preserves the busy explanation", async () => {
    const observation = await runPoll([response(429)], {maxPolls: 1});
    assert.equal(observation.requests.length, 1);
    assert.match(observation.result.message, /状态查询暂时繁忙/);
});

test("large Retry-After cannot overflow into an immediate retry", async () => {
    const observation = await runPoll([response(429, "9".repeat(100))]);
    assert.equal(observation.requests.length, 1);
    assert.deepEqual(observation.waits, []);
});

test("malformed seconds are not interpreted as dates; past dates remain bounded", () => {
    const now = Date.UTC(2026, 0, 1);
    for (const value of ["-1", "1.5", "1e3", "Infinity", "", "nonsense"]) {
        assert.equal(reliability.retryAfterDelayMs(response(429, value), now), null);
    }
    assert.equal(reliability.retryAfterDelayMs(response(429, "0"), now), 0);
    assert.equal(reliability.retryAfterDelayMs(response(503, new Date(now - 5000).toUTCString()), now), 0);
});

test("elapsed time is checked again after a delayed timer", async () => {
    let clock = 0, calls = 0;
    const observation = await runPoll([response(429)], {
        now: () => clock,
        wait: async () => { clock = 16000; },
        fetchStatus: async () => { calls += 1; return response(429, "1"); },
    });
    assert.equal(calls, 1);
    assert.equal(observation.result.status, "poll_timeout");
});

test("omitted poll budgets use documented defaults, not the minimum one-call budget", async () => {
    const observation = await runPoll([response(429), response(200)], {
        maxDurationMs: undefined, maxPolls: undefined,
    });
    assert.equal(observation.requests.length, 2);
    assert.equal(observation.result.status, "streaming");
});

test("explicit download failures and post-start observations keep existing semantics", async () => {
    const failed = await runPoll([response(410, null, {ok: false, status: "expired"})]);
    assert.equal(failed.result.status, "expired");
    const started = await runPoll([response(200, null, {
        ok: false, status: "failed", stream_started: true,
    })]);
    assert.equal(started.result.status, "streaming");
    assert.equal(started.result.finalStatus, "failed");
});

test("Blob size guidance explains a limit, not a refresh or server fallback", () => {
    const ordinary = require("../static/js/ordinary_media_download.js");
    const message = ordinary.safeErrorMessage({errorCode: "BROWSER_BLOB_TOO_LARGE"});
    assert.match(message, /大小上限/);
    assert.match(message, /本次未保存/);
    assert.match(message, /重复解析不会解除/);
    assert.match(message, /不会自动改用服务器下载/);
    assert.doesNotMatch(message, /请重新解析/);
    assert.equal(ordinary.MAX_BROWSER_BLOB_BYTES, 1024 * 1024 * 1024);
});

test("oversized bound upstream media still makes zero fetch/save/fallback calls", async () => {
    const ordinary = require("../static/js/ordinary_media_download.js");
    let fetches = 0, saves = 0, fallbacks = 0;
    await assert.rejects(ordinary.download({
        type: "video", url: "https://cdn.example.org/large.mp4",
        provider: "yt_dlp_generic", asset_id: "upstream:test",
        source_platform: "upstream", browser_upstream_bound: true,
        browser_direct_fetch_safe: true,
        size_bytes: ordinary.MAX_BROWSER_BLOB_BYTES + 1,
    }, {
        fetchImpl: async () => { fetches += 1; throw new Error("must not fetch"); },
        handoffBlob: async () => { saves += 1; },
        onTransportFallback: () => { fallbacks += 1; },
    }), (error) => error.errorCode === "BROWSER_BLOB_TOO_LARGE");
    assert.equal(fetches, 0);
    assert.equal(saves, 0);
    assert.equal(fallbacks, 0);
});
