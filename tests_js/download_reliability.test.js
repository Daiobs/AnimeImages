"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const reliability = require("../static/js/download_reliability.js");

test("large-file observation outlives startup and the old 30-query ceiling", async () => {
    let elapsed = 0, calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        startTimeoutMs: 60000,
        now: () => elapsed,
        wait: async ms => { elapsed += ms; },
        fetchStatus: async () => {
            calls += 1;
            return fakeJsonResponse(200, {ok: true, stream_started: elapsed >= 20000,
                status: elapsed < 20000 ? "connecting" : elapsed < 180000 ? "streaming" : "completed"});
        },
    });
    assert.equal(result.status, "completed");
    assert.ok(elapsed >= 180000);
    assert.ok(calls > 30);
});

test("provider resources require an explicit browser direct capability", () => {
    assert.equal(reliability.browserDirectFetchSafe({
        provider: "tiantianjiexi",
    }), false);
    assert.equal(reliability.browserDirectFetchSafe({
        provider: "kukutool",
        browser_direct_fetch_safe: false,
    }), false);
    assert.equal(reliability.browserDirectFetchSafe({
        provider: "future-provider",
        browser_direct_fetch_safe: true,
    }), true);
    assert.equal(reliability.browserDirectFetchSafe({
        provider: "",
    }), true);
});

test("prepare success exposes only the same-origin handoff contract", () => {
    const result = reliability.parsePreparePayload({
        ok: true,
        status: "issued",
        download_url: "/media/download-stream/signed-ticket/",
        status_url: "/media/download-status/status-ticket/",
        poll_after_ms: 500,
        expires_in_seconds: 60,
    });

    assert.equal(result.ok, true);
    assert.equal(
        result.downloadUrl,
        "/media/download-stream/signed-ticket/",
    );
    assert.equal(
        result.statusUrl,
        "/media/download-status/status-ticket/",
    );
    assert.equal(result.pollAfterMs, 500);
    assert.equal(result.expiresInSeconds, 60);
});

test("prepare success requires both opaque stream and status URLs", () => {
    assert.equal(reliability.parsePreparePayload({
        ok: true,
        status: "issued",
        download_url: "/media/download-stream/signed-ticket/",
    }).ok, false);
    assert.equal(reliability.parsePreparePayload({
        ok: true,
        status: "ready",
        download_url: "/media/download-stream/signed-ticket/",
        status_url: "/media/download-status/status-ticket/",
    }).ok, false);
});

test("prepare failures map stable error codes to safe user messages", () => {
    const failed = reliability.parsePreparePayload({
        ok: false,
        status: "failed",
        error_code: "UPSTREAM_SSL_ERROR",
        message: "must not override the stable message",
    });

    assert.equal(failed.ok, false);
    assert.equal(failed.errorCode, "UPSTREAM_SSL_ERROR");
    assert.equal(
        failed.message,
        "资源连接异常，请稍后重试。",
    );
});

test("prepare 4xx and 5xx payloads always become terminal failures", () => {
    for (const [errorCode, message] of [
        ["PROXY_FAILED_RECOVERY_BLOCKED", "ignored"],
        ["RATE_LIMITED", "ignored"],
        ["UPSTREAM_CONNECTION_ERROR", "ignored"],
    ]) {
        const failed = reliability.parsePreparePayload({
            ok: false,
            status: "failed",
            error_code: errorCode,
            message,
        }, "failed");

        assert.equal(failed.ok, false);
        assert.equal(failed.status, "failed");
        assert.equal(failed.errorCode, errorCode);
        assert.notEqual(failed.message, "ignored");
    }
});

test("server attachment routes use prepare before browser handoff", () => {
    assert.equal(reliability.shouldUsePreparedServerDownload({
        status: "routed",
        route_type: "proxy",
        download_strategy: "server_single_attachment",
    }), true);
    assert.equal(reliability.shouldUsePreparedServerDownload({
        status: "routed",
        route_type: "direct",
        download_strategy: "browser_direct",
    }), false);
});

function fakeJsonResponse(status, payload) {
    return {
        ok: status >= 200 && status < 300,
        status,
        async json() {
            return payload;
        },
    };
}

test("bounded status polling observes streaming but waits for the terminal result", async () => {
    const payloads = [
        {ok: true, status: "issued", upstream_attempt_count: 0},
        {ok: true, status: "connecting", upstream_attempt_count: 1},
        {
            ok: true,
            status: "streaming",
            recovered: true,
            upstream_attempt_count: 2,
        },
        {ok: true, status: "completed", stream_started: true, recovered: true, upstream_attempt_count: 2},
    ];
    let now = 0;
    let calls = 0;
    const observed = [];
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        pollAfterMs: 500,
        now: () => now,
        onStreaming: (state) => observed.push(state.status),
        wait: async (delay) => {
            now += delay;
        },
        fetchStatus: async () => {
            const payload = payloads[calls];
            calls += 1;
            return fakeJsonResponse(200, payload);
        },
    });

    assert.equal(result.status, "completed");
    assert.equal(result.recovered, true);
    assert.equal(result.upstreamAttemptCount, 2);
    assert.equal(calls, 4);
    assert.deepEqual(observed, ["streaming"]);
});

test("streaming followed by downstream failure cannot remain a successful handoff", async () => {
    let calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        wait: async () => {},
        fetchStatus: async () => fakeJsonResponse(200, ++calls === 1
            ? {ok: true, status: "streaming", stream_started: true}
            : {ok: false, status: "failed", stream_started: true, error_code: "DOWNLOAD_CONNECTION_CLOSED"}),
    });
    assert.equal(calls, 2);
    assert.equal(result.status, "failed");
    assert.equal(result.errorCode, "DOWNLOAD_CONNECTION_CLOSED");
    assert.doesNotMatch(result.message, /上游|取消/);
    assert.equal(reliability.downloadStartDisposition(result), "failed");
});

test("failed and expired statuses terminate polling with safe messages", async () => {
    const failed = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        fetchStatus: async () => fakeJsonResponse(502, {
            ok: false,
            status: "failed",
            error_code: "UPSTREAM_SSL_ERROR",
            message: "unsafe upstream details",
        }),
    });
    const expired = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        fetchStatus: async () => fakeJsonResponse(410, {
            ok: false,
            status: "expired",
            error_code: "DOWNLOAD_HANDOFF_EXPIRED",
        }),
    });

    assert.equal(failed.status, "failed");
    assert.equal(failed.message, "资源连接异常，请稍后重试。");
    assert.equal(expired.status, "expired");
    assert.equal(expired.message, "下载链接已过期，请重新点击下载。");
});

for (const [status, responseStatus, errorCode] of [
    ["failed", 200, "UPSTREAM_CONNECTION_ERROR"],
    ["expired", 410, "DOWNLOAD_HANDOFF_EXPIRED"],
]) {
    test(`${status} overrides stream_started and ends polling without another request`, async () => {
        const payload = {
            ok: false,
            status,
            stream_started: true,
            upstream_attempt_count: 1,
            error_code: errorCode,
        };
        const parsed = reliability.parseDownloadStatusPayload(payload, responseStatus);
        assert.equal(reliability.downloadStartDisposition(parsed), "failed");

        let calls = 0;
        const result = await reliability.pollDownloadStatus({
            statusUrl: "/media/download-status/ticket/",
            wait: async () => assert.fail("terminal failure must not schedule another poll"),
            fetchStatus: async (url, options) => {
                calls += 1;
                assert.equal(url, "/media/download-status/ticket/");
                assert.equal(options.method, "GET");
                return fakeJsonResponse(responseStatus, payload);
            },
        });

        assert.equal(calls, 1);
        assert.equal(result.ok, false);
        assert.equal(result.status, status);
        assert.equal(result.streamStarted, true);
        assert.equal(result.upstreamAttemptCount, 1);
        assert.equal(result.errorCode, errorCode);
        assert.equal(reliability.downloadStartDisposition(result), "failed");
    });
}

test("completed ends observation; leaving a streaming page only ends observation", async () => {
    for (const status of ["streaming", "completed"]) {
        for (const streamStarted of [false, true]) {
            const page = new EventTarget();
            const result = await reliability.pollDownloadStatus({
                statusUrl: "/media/download-status/ticket/",
                window: page,
                wait: async () => page.dispatchEvent(new Event("pagehide")),
                fetchStatus: async () => fakeJsonResponse(200, {
                    ok: true,
                    status,
                    stream_started: streamStarted,
                }),
            });
            assert.equal(result.status, status === "completed" ? "completed" : "poll_timeout");
            assert.equal(reliability.downloadStartDisposition(result), status === "completed" ? "started" : "unconfirmed");
            if (status === "streaming") assert.equal(result.streamStarted, true);
        }
    }
});

test("polling timeout is bounded and never remains pending", async () => {
    let calls = 0, clock = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        startTimeoutMs: 1500,
        now: () => clock,
        wait: async ms => { clock += ms; },
        fetchStatus: async () => {
            calls += 1;
            return fakeJsonResponse(200, {
                ok: true,
                status: "connecting",
            });
        },
    });

    assert.equal(calls, 3);
    assert.equal(result.status, "poll_timeout");
    assert.equal(
        result.message,
        reliability.DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
    );
    assert.equal(
        reliability.downloadStartDisposition(result),
        "unconfirmed",
    );
});

test("a hanging status request is aborted by the overall polling boundary", async () => {
    let capturedSignal = null;
    const startedAt = Date.now();
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        startTimeoutMs: 50,
        fetchStatus: async (_url, options) => {
            capturedSignal = options.signal;
            return new Promise(() => {});
        },
    });

    assert.equal(result.status, "poll_timeout");
    assert.ok(Date.now() - startedAt < 500);
    assert.equal(capturedSignal.aborted, true);
});

test("startup observation remains bounded by launch authorization, not file duration", async () => {
    let calls = 0, clock = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        startTimeoutMs: 60000,
        now: () => clock,
        wait: async ms => { clock += ms; },
        fetchStatus: async () => {
            calls += 1;
            return fakeJsonResponse(200, {
                ok: true,
                status: "connecting",
            });
        },
    });

    assert.equal(calls, 120);
    assert.ok(clock < 60000);
    assert.equal(result.status, "poll_timeout");
});

test("temporary status rate limiting is retried within the bounded poll", async () => {
    let calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        wait: async () => {},
        fetchStatus: async () => {
            calls += 1;
            if (calls === 1) {
                return fakeJsonResponse(429, {
                    status: "error",
                    error_code: "RATE_LIMITED",
                });
            }
            return fakeJsonResponse(200, {
                ok: true,
                status: "completed",
            });
        },
    });

    assert.equal(calls, 2);
    assert.equal(result.status, "completed");
});

test("single-page code no longer submits an unobservable iframe POST", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );

    assert.doesNotMatch(source, /submitBrowserManagedServerDownload/);
    assert.doesNotMatch(source, /submitAutomaticServerSingleFallback/);
    assert.doesNotMatch(source, /PROVIDER_BROWSER_BLOB_PROVIDERS/);
    assert.match(source, /await prepareAndHandoffServerSingle/);
    assert.match(source, /frame\.src = downloadUrl/);
    assert.match(source, /sameOriginHandoffPath\(prepared\.statusUrl\)/);
    assert.match(source, /pollDownloadStatus/);
    assert.match(source, /setProgress\(100\)/);
    assert.match(source, /setDownloadButtonsBusy\(false, null, null, controls\)/);
    assert.doesNotMatch(source, /console\.(?:warn|error)\([^)]*item\.url/s);
});

test("server handoff never presents server startup as 100 percent saved", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    const start = source.indexOf(
        "async function prepareAndHandoffServerSingle",
    );
    const end = source.indexOf(
        "async function writeResponseToFileWithProgress",
        start,
    );
    const implementation = source.slice(start, end);
    const handoffIndex = implementation.indexOf(
        "handoffPreparedDownload(prepared)",
    );
    const completeProgressIndex = implementation.indexOf(
        "setProgress(100)",
    );
    const pollIndex = implementation.indexOf(
        "await downloadReliabilityApi.pollDownloadStatus",
    );

    assert.ok(handoffIndex >= 0);
    assert.ok(pollIndex > handoffIndex);
    assert.equal(completeProgressIndex, -1);
    assert.match(
        implementation.slice(0, pollIndex),
        /setProgressPending\("连接下载"\)/,
    );
    assert.doesNotMatch(
        implementation.slice(handoffIndex, pollIndex),
        /setProgress\(100\)/,
    );
    assert.doesNotMatch(implementation, /unconfirmed"\) \{\s*setProgress\(0\)/);
    assert.doesNotMatch(implementation, /fetch\(prepared\.downloadUrl/);
    assert.doesNotMatch(implementation, /response\.blob\(\)/);
    assert.doesNotMatch(implementation, /URL\.createObjectURL/);
    assert.doesNotMatch(implementation, /console\.(?:log|warn|error)/);
    assert.match(implementation, /onStreaming:/);
    assert.match(implementation, /hideDownloadProgress\(\)/);
    assert.match(implementation, /服务器传输已结束.*确认文件保存结果/);
});

test("cancelling while observing a started stream stops queries without restarting media", async () => {
    let calls = 0;
    const cancelled = Object.assign(new Error("cancelled"), {name: "AbortError"});
    await assert.rejects(reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        fetchStatus: async () => {
            calls += 1;
            return fakeJsonResponse(200, {ok: true, status: "streaming"});
        },
        wait: async () => { throw cancelled; },
    }), {name: "AbortError"});
    assert.equal(calls, 1);
});

for (const unavailable of ["expired", "invalid-json", "unknown-state", "busy"]) {
    test(`status ${unavailable} after streaming does not claim the media failed or completed`, async () => {
        let calls = 0, clock = 0;
        const result = await reliability.pollDownloadStatus({
            statusUrl: "/media/download-status/ticket/",
            now: () => clock,
            wait: async ms => { clock += ms; },
            fetchStatus: async () => {
                if (++calls === 1) return fakeJsonResponse(200, {ok: true, status: "streaming"});
                if (unavailable === "expired") return fakeJsonResponse(410, {ok: false, status: "expired"});
                if (unavailable === "invalid-json") return {ok: false, status: 502, async json() { throw new Error("HTML"); }};
                if (unavailable === "busy") return {ok: false, status: 429, headers: {get: () => "60"}};
                return fakeJsonResponse(200, {status: "other"});
            },
        });
        assert.ok(calls >= 2 && calls <= 10);
        assert.equal(result.status, "poll_timeout");
        assert.equal(result.streamStarted, true);
        assert.equal(reliability.downloadStartDisposition(result), "unconfirmed");
    });
}

test("status body parsing is covered by the existing overall deadline", async () => {
    let signal;
    const start = Date.now();
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        startTimeoutMs: 50,
        fetchStatus: async (_url, options) => {
            signal = options.signal;
            return {ok: true, status: 200, json: () => new Promise(() => {})};
        },
    });
    assert.equal(result.status, "poll_timeout");
    assert.equal(signal.aborted, true);
    assert.ok(Date.now() - start < 500);
});

test("external cancellation during a status request does not schedule another poll", async () => {
    let calls = 0;
    await assert.rejects(reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        fetchStatus: async () => {
            calls += 1;
            throw Object.assign(new Error("cancelled"), {name: "AbortError"});
        },
    }), {name: "AbortError"});
    assert.equal(calls, 1);
});

test("hidden pages pause status reads; return resumes the same transfer", async () => {
    const doc = new EventTarget();
    doc.hidden = false;
    let calls = 0, elapsed = 0, paused;
    const hidden = new Promise(resolve => { paused = resolve; });
    const observing = reliability.pollDownloadStatus({
        statusUrl: "/status", document: doc, now: () => elapsed,
        wait: async ms => { elapsed += ms; },
        fetchStatus: async () => {
            if (++calls === 1) {
                doc.hidden = true;
                paused();
                return fakeJsonResponse(200, {ok: true, status: "streaming"});
            }
            return fakeJsonResponse(200, {ok: true, status: "completed"});
        },
    });
    await hidden;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls, 1);
    elapsed += 180000;
    doc.hidden = false;
    doc.dispatchEvent(new Event("visibilitychange"));
    assert.equal((await observing).status, "completed");
    assert.equal(calls, 2);
});

test("cancelling while hidden removes observers and never resumes the media request", async () => {
    const doc = new EventTarget(), controller = new AbortController();
    doc.hidden = true;
    let calls = 0;
    const observing = reliability.pollDownloadStatus({
        statusUrl: "/status", document: doc, signal: controller.signal,
        fetchStatus: async () => { calls += 1; },
    });
    controller.abort();
    await assert.rejects(observing, {name: "AbortError"});
    doc.hidden = false;
    doc.dispatchEvent(new Event("visibilitychange"));
    assert.equal(calls, 0);
});

test("pagehide aborts only the in-flight status GET", async () => {
    const page = new EventTarget();
    let querySignal;
    const observing = reliability.pollDownloadStatus({
        statusUrl: "/status", window: page,
        fetchStatus: async (url, init) => {
            assert.equal(url, "/status");
            querySignal = init.signal;
            return new Promise(() => {});
        },
    });
    page.dispatchEvent(new Event("pagehide"));
    assert.equal((await observing).status, "poll_timeout");
    assert.equal(querySignal.aborted, true);
});
