"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const batch = require("../static/js/batch_parse.js");
const reliability = require("../static/js/download_reliability.js");


function directRoute(items) {
    return {
        status: "routed",
        route_type: "direct",
        download_strategy: "browser_direct",
        items,
    };
}


function preparedRoute() {
    return {
        status: "routed",
        route_type: "proxy",
        download_strategy: "server_single_attachment",
        items: [],
    };
}


test("Batch media payload matches the existing allowlisted download contract", () => {
    const payload = batch.buildBatchDownloadPayload({
        dataset: {
            type: "video",
            url: "https://media.example.com/video.mp4",
            coverUrl: "https://media.example.com/cover.jpg",
            sizeBytes: "2048",
            assetId: "asset-1",
            provider: "tiantianjiexi",
            providerResourceId: "resource-1",
            providerCacheKey: "cache-1",
            providerManifestToken: "opaque-manifest",
            downloadStrategy: "server_attachment",
            fallbackStrategy: "server_attachment",
            corsExpected: "false",
            browserDirectFetchSafe: "false",
            browserNativeHandoffCapable: "true",
            requiresMerge: "false",
            requiresCookie: "false",
            platformKey: "douyin",
            baseFilename: "demo",
            ignoredProviderBody: "must-not-leak",
        },
    });

    assert.equal(payload.media_items.length, 1);
    assert.equal(payload.media_items[0].url, "https://media.example.com/video.mp4");
    assert.equal(payload.media_items[0].provider_manifest_token, "opaque-manifest");
    assert.equal(payload.media_items[0].browser_native_handoff_capable, true);
    assert.equal(payload.media_items[0].ignoredProviderBody, undefined);
    assert.equal(payload.download_mode, "auto");
    assert.equal(payload.platform_key, "douyin");
});


test("route disposition keeps direct, prepared, and server paths distinct", () => {
    assert.equal(batch.batchDownloadRouteDisposition(
        directRoute([{url: "https://media.example.com/video.mp4"}]),
        reliability,
    ), "direct");
    assert.equal(batch.batchDownloadRouteDisposition(
        preparedRoute(),
        reliability,
    ), "prepared");
    assert.equal(batch.batchDownloadRouteDisposition({
        status: "routed",
        route_type: "zip",
        download_strategy: "server_zip",
    }, reliability), "server");
    assert.equal(batch.batchDownloadRouteDisposition({
        status: "error",
        route_type: "direct",
    }, reliability), "blocked");
});


test("direct images and explicitly safe Provider videos use browser Blob download", () => {
    assert.equal(batch.batchDirectRouteUsesBlob([
        {type: "image", url: "https://media.example.com/image.jpg"},
    ], reliability), true);
    assert.equal(batch.batchDirectRouteUsesBlob([
        {
            type: "video",
            url: "https://media.example.com/video.mp4",
            provider: "safe-provider",
            browser_direct_fetch_safe: true,
            size_bytes: 1024 * 1024,
        },
    ], reliability), true);
    assert.equal(batch.batchDirectRouteUsesBlob([
        {
            type: "video",
            url: "https://media.example.com/video.mp4",
            provider: "",
        },
    ], reliability), false);
    assert.equal(batch.batchDirectRouteUsesBlob([
        {
            type: "video",
            url: "https://media.example.com/video.mp4",
            provider: "safe-provider",
            browser_direct_fetch_safe: true,
            size_bytes: reliability.BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES + 1,
        },
    ], reliability), false);
});


test("Batch native handoff requires the distinct server-approved capability", () => {
    const item = {
        type: "video",
        url: "https://media.example.com/video.mp4",
        provider: "tiantianjiexi",
        browser_direct_mode: "native_handoff",
        browser_native_handoff_safe: true,
        browser_direct_fetch_safe: false,
        size_bytes: 500 * 1024 * 1024,
    };

    assert.equal(batch.batchDirectRouteUsesNativeHandoff(
        [item],
        reliability,
    ), true);
    assert.equal(batch.batchDirectRouteUsesBlob([item], reliability), false);
    assert.equal(batch.batchDirectRouteUsesNativeHandoff(
        [{...item, browser_native_handoff_safe: false}],
        reliability,
    ), false);
});


test("direct-safe route is handed to the browser without prepare", async () => {
    const calls = [];
    const outcome = await batch.executeBatchDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => directRoute([
            {url: "https://media.example.com/video.mp4"},
        ]),
        handoffDirect: async (items) => calls.push(["direct", items.length]),
        prepare: async () => calls.push(["prepare"]),
        downloadServer: async () => calls.push(["server"]),
    });

    assert.equal(outcome.kind, "direct");
    assert.equal(outcome.handoffConfirmed, false);
    assert.equal(outcome.handoffIssued, true);
    assert.equal(outcome.completed, false);
    assert.deepEqual(calls, [["direct", 1]]);
});

test("native direct route never invokes prepare or server download", async () => {
    const calls = [];
    const outcome = await batch.executeBatchDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => directRoute([{
            type: "video",
            url: "https://media.example.com/video.mp4",
            browser_direct_mode: "native_handoff",
            browser_native_handoff_safe: true,
        }]),
        handoffDirect: async () => {
            calls.push("native-open");
            return {
                completed: false,
                mode: "native_open",
                state: reliability.DIRECT_LAUNCH_ISSUED,
            };
        },
        prepare: async () => calls.push("prepare"),
        handoffPrepared: async () => calls.push("prepared-handoff"),
        pollStatus: async () => calls.push("status"),
        downloadServer: async () => calls.push("server"),
    });

    assert.deepEqual(calls, ["native-open"]);
    assert.equal(outcome.kind, "direct");
    assert.equal(outcome.routeState, reliability.DIRECT_ROUTE_SELECTED);
    assert.equal(outcome.directState, reliability.DIRECT_LAUNCH_ISSUED);
    assert.equal(outcome.handoffIssued, true);
    assert.equal(outcome.handoffConfirmed, false);
    assert.equal(outcome.completed, false);
});


test("prepared download waits for stream_started before confirming handoff", async () => {
    const phases = [];
    const outcome = await batch.executeBatchDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => preparedRoute(),
        prepare: async () => ({
            downloadUrl: "/media/download-stream/opaque/",
            statusUrl: "/media/download-status/opaque/",
        }),
        handoffPrepared: async () => phases.push("handoff"),
        pollStatus: async () => ({
            status: "streaming",
            streamStarted: true,
        }),
        onPhase: (phase) => phases.push(phase),
    });

    assert.equal(outcome.kind, "prepared");
    assert.equal(outcome.handoffConfirmed, true);
    assert.equal(outcome.unconfirmed, false);
    assert.deepEqual(phases, ["routing", "preparing", "connecting", "handoff"]);
});


test("failed direct browser launch never silently falls back to server relay", async () => {
    const calls = [];
    await assert.rejects(
        batch.executeBatchDownload({
            reliabilityApi: reliability,
            fetchRoute: async () => directRoute([
                {type: "image", url: "https://media.example.com/image.jpg"},
            ]),
            handoffDirect: async () => {
                calls.push("direct");
                throw new Error("browser launch failed");
            },
            fallbackDirectToPrepared: true,
            prepare: async () => calls.push("prepare"),
            handoffPrepared: async () => calls.push("prepared-handoff"),
        }),
        /browser launch failed/,
    );

    assert.deepEqual(calls, ["direct"]);
});


test("explicit compatibility action is the only prepared relay path", async () => {
    const calls = [];
    const outcome = await batch.executeBatchCompatibilityDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => calls.push("route"),
        prepare: async () => {
            calls.push("prepare");
            return {
                downloadUrl: "/media/download-stream/opaque/",
                statusUrl: "/media/download-status/opaque/",
            };
        },
        handoffPrepared: async () => calls.push("prepared-handoff"),
        pollStatus: async () => ({
            status: "streaming",
            streamStarted: true,
        }),
        onPhase: () => {},
    });

    assert.equal(outcome.kind, "prepared");
    assert.equal(outcome.handoffConfirmed, true);
    assert.deepEqual(calls, ["prepare", "prepared-handoff"]);
});


test("Batch native open uses a preopened context and never completes the download", () => {
    const events = [];
    const context = batch.preopenBatchDirectContext(
        reliability,
        () => ({
            opener: {},
            closed: false,
            location: {
                replace: (url) => events.push(new URL(url).hostname),
            },
            focus: () => events.push("focus"),
            close: () => events.push("close"),
        }),
    );
    const result = batch.openBatchDirectItem([{
        type: "video",
        url: "https://media.example.com/video.mp4",
        browser_direct_mode: "native_handoff",
        browser_native_handoff_safe: true,
    }], context, reliability);

    assert.equal(result.mode, "native_open");
    assert.equal(result.completed, false);
    assert.equal(result.downloadStarted, false);
    assert.deepEqual(events, ["media.example.com", "focus"]);
});


test("Single and Batch expose the same explicit direct and compatibility actions", () => {
    const batchSource = fs.readFileSync(
        path.join(__dirname, "../static/js/batch_parse.js"),
        "utf8",
    );
    const singleTemplate = fs.readFileSync(
        path.join(__dirname, "../templates/partials/xhs_results_region.html"),
        "utf8",
    );
    const batchTemplate = fs.readFileSync(
        path.join(__dirname, "../templates/partials/batch_result_card.html"),
        "utf8",
    );

    assert.doesNotMatch(batchSource, /batch-direct-download-frame/);
    assert.doesNotMatch(batchSource, /direct_fallback/);
    assert.match(singleTemplate, /高速直连/);
    assert.match(singleTemplate, /compat-download-btn/);
    assert.match(singleTemplate, /兼容下载/);
    assert.match(batchTemplate, /data-batch-compat-download/);
    assert.match(batchTemplate, /高速直连/);
    assert.match(batchTemplate, /兼容下载/);
});


test("stream_started false never reports browser handoff success", async () => {
    const outcome = await batch.executeBatchDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => preparedRoute(),
        prepare: async () => ({
            downloadUrl: "/media/download-stream/opaque/",
            statusUrl: "/media/download-status/opaque/",
        }),
        handoffPrepared: async () => {},
        pollStatus: async () => ({
            status: "poll_timeout",
            streamStarted: false,
            message: reliability.DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
        }),
    });

    assert.equal(outcome.handoffConfirmed, false);
    assert.equal(outcome.unconfirmed, true);
});


test("prepare or status failure is terminal and keeps a safe message", async () => {
    await assert.rejects(
        batch.executeBatchDownload({
            reliabilityApi: reliability,
            fetchRoute: async () => preparedRoute(),
            prepare: async () => {
                throw new Error("下载准备失败，请稍后再试。");
            },
        }),
        /下载准备失败/,
    );

    await assert.rejects(
        batch.executeBatchDownload({
            reliabilityApi: reliability,
            fetchRoute: async () => preparedRoute(),
            prepare: async () => ({}),
            handoffPrepared: async () => {},
            pollStatus: async () => ({
                status: "failed",
                streamStarted: false,
                message: "下载启动失败，请稍后重试。",
            }),
        }),
        /下载启动失败/,
    );
});


test("malformed direct route cannot bypass validated server routing", async () => {
    let handoffCount = 0;
    await assert.rejects(
        batch.executeBatchDownload({
            reliabilityApi: reliability,
            fetchRoute: async () => directRoute([
                {url: "javascript:alert(1)"},
            ]),
            handoffDirect: async () => {
                handoffCount += 1;
            },
        }),
        /无效资源/,
    );
    assert.equal(handoffCount, 0);
});


test("non-single server routes stay on the existing server download contract", async () => {
    let serverCalls = 0;
    const outcome = await batch.executeBatchDownload({
        reliabilityApi: reliability,
        fetchRoute: async () => ({
            status: "routed",
            route_type: "zip",
            download_strategy: "server_zip",
        }),
        downloadServer: async () => {
            serverCalls += 1;
        },
    });

    assert.equal(outcome.kind, "server");
    assert.equal(outcome.handoffConfirmed, true);
    assert.equal(serverCalls, 1);
});
