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

test("prepare success exposes only the same-origin handoff contract", () => {
    const result = reliability.parsePreparePayload({
        ok: true,
        status: "ready",
        download_url: "/media/download-stream/signed-ticket/",
        recovered: true,
    });

    assert.equal(result.ok, true);
    assert.equal(result.recovered, true);
    assert.equal(
        result.downloadUrl,
        "/media/download-stream/signed-ticket/",
    );
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

test("single-page code no longer submits an unobservable iframe POST", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );

    assert.doesNotMatch(source, /submitBrowserManagedServerDownload/);
    assert.doesNotMatch(source, /submitAutomaticServerSingleFallback/);
    assert.doesNotMatch(source, /PROVIDER_BROWSER_BLOB_PROVIDERS/);
    assert.match(source, /await prepareAndHandoffServerSingle/);
    assert.match(source, /frame\.src = prepared\.downloadUrl/);
    assert.match(source, /setProgress\(0\)/);
    assert.match(source, /setDownloadButtonsBusy\(false\)/);
    assert.doesNotMatch(source, /console\.(?:warn|error)\([^)]*item\.url/s);
});
