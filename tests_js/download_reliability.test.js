"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const reliability = require("../static/js/download_reliability.js");

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

test("native handoff and fetch Blob are independent capabilities", () => {
    const nativeOnly = {
        type: "video",
        provider: "tiantianjiexi",
        browser_direct_mode: "native_handoff",
        browser_native_handoff_safe: true,
        browser_direct_fetch_safe: false,
        size_bytes: 500 * 1024 * 1024,
    };

    assert.equal(reliability.browserNativeHandoffSafe(nativeOnly), true);
    assert.equal(reliability.browserDirectFetchSafe(nativeOnly), false);
    assert.equal(reliability.browserDirectFetchBlobSafe(nativeOnly), false);
});

test("fetch Blob is limited to known small CORS-safe resources", () => {
    const small = {
        type: "video",
        browser_direct_fetch_safe: true,
        size_bytes: reliability.BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES,
    };

    assert.equal(reliability.browserDirectFetchBlobSafe(small), true);
    assert.equal(reliability.browserDirectFetchBlobSafe({
        ...small,
        size_bytes: reliability.BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES + 1,
    }), false);
    assert.equal(reliability.browserDirectFetchBlobSafe({
        ...small,
        size_bytes: 0,
    }), false);
    assert.equal(reliability.browserDirectFetchBlobSafe({
        ...small,
        requires_cookie: true,
    }), false);
    assert.equal(reliability.browserDirectFetchBlobSafe({
        ...small,
        requires_merge: true,
    }), false);
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
        "上游资源连接异常，请稍后重试。",
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

test("bounded status polling stops as soon as streaming is observable", async () => {
    const payloads = [
        {ok: true, status: "issued", upstream_attempt_count: 0},
        {ok: true, status: "connecting", upstream_attempt_count: 1},
        {
            ok: true,
            status: "streaming",
            recovered: true,
            upstream_attempt_count: 2,
        },
    ];
    let now = 0;
    let calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        pollAfterMs: 500,
        now: () => now,
        wait: async (delay) => {
            now += delay;
        },
        fetchStatus: async () => {
            const payload = payloads[calls];
            calls += 1;
            return fakeJsonResponse(200, payload);
        },
    });

    assert.equal(result.status, "streaming");
    assert.equal(result.recovered, true);
    assert.equal(result.upstreamAttemptCount, 2);
    assert.equal(calls, 3);
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
    assert.equal(failed.message, "上游资源连接异常，请稍后重试。");
    assert.equal(expired.status, "expired");
    assert.equal(expired.message, "下载链接已过期，请重新点击下载。");
});

test("a post-start disconnect still confirms browser download startup", async () => {
    const failedPayload = reliability.parseDownloadStatusPayload({
        ok: false,
        status: "failed",
        stream_started: true,
        error_code: "UPSTREAM_CONNECTION_ERROR",
    }, 200);
    assert.equal(
        reliability.downloadStartDisposition(failedPayload),
        "started",
    );

    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        fetchStatus: async () => fakeJsonResponse(200, {
            ok: false,
            status: "failed",
            stream_started: true,
            error_code: "UPSTREAM_CONNECTION_ERROR",
        }),
    });

    assert.equal(result.status, "streaming");
    assert.equal(result.finalStatus, "failed");
    assert.equal(result.streamStarted, true);
    assert.equal(
        reliability.downloadStartDisposition(result),
        "started",
    );
});

test("polling timeout is bounded and never remains pending", async () => {
    let calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        maxPolls: 3,
        wait: async () => {},
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
        maxDurationMs: 50,
        fetchStatus: async (_url, options) => {
            capturedSignal = options.signal;
            return new Promise(() => {});
        },
    });

    assert.equal(result.status, "poll_timeout");
    assert.ok(Date.now() - startedAt < 500);
    assert.equal(capturedSignal.aborted, true);
});

test("polling clamps caller input to the hard 30-request ceiling", async () => {
    let calls = 0;
    const result = await reliability.pollDownloadStatus({
        statusUrl: "/media/download-status/ticket/",
        maxPolls: 1000,
        wait: async () => {},
        fetchStatus: async () => {
            calls += 1;
            return fakeJsonResponse(200, {
                ok: true,
                status: "connecting",
            });
        },
    });

    assert.equal(calls, reliability.MAX_STATUS_POLL_COUNT);
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
                status: "streaming",
            });
        },
    });

    assert.equal(calls, 2);
    assert.equal(result.status, "streaming");
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
    assert.match(source, /setDownloadButtonsBusy\(false\)/);
    assert.doesNotMatch(source, /console\.(?:warn|error)\([^)]*item\.url/s);
});

test("server handoff completes UI progress only after confirmed stream start", () => {
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
    assert.ok(completeProgressIndex > pollIndex);
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
});

test("native direct handoff stays pending instead of claiming completion", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    const start = source.indexOf("function showNativeDirectHandoff");
    const end = source.indexOf("async function prepareServerSingleDownload", start);
    const implementation = source.slice(start, end);

    assert.match(implementation, /setProgressPending\("浏览器接管中"\)/);
    assert.match(implementation, /服务器无法确认文件是否下载完成/);
    assert.doesNotMatch(implementation, /setProgress\(100\)/);
});
