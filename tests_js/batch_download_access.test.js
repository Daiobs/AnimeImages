"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ordinary = require("../static/js/ordinary_media_download.js");
const batch = require("../static/js/batch_parse.js");


function readProjectFile(relativePath) {
    return fs.readFileSync(path.join(__dirname, "..", relativePath), "utf8");
}


test("Batch ordinary video uses the shared browser download primitive", () => {
    const source = readProjectFile("static/js/batch_parse.js");

    assert.match(source, /AnimeImagesOrdinaryMediaDownload/);
    assert.match(source, /ordinaryMediaDownloadApi\.isOrdinaryMediaItem\(item\)/);
    assert.match(source, /ordinaryMediaDownloadApi\.download\(item/);
});


test("Batch ordinary video does not restore route, prepare, status, or relay calls", () => {
    const source = readProjectFile("static/js/batch_parse.js");

    assert.doesNotMatch(source, /media\/download-prepare/);
    assert.doesNotMatch(source, /media\/download-stream/);
    assert.doesNotMatch(source, /media\/download-status/);
    assert.doesNotMatch(source, /executeBatchCompatibilityDownload/);
    assert.doesNotMatch(source, /data-batch-compat-download/);
});


test("Batch and Single load the same ordinary streaming transport", () => {
    const batchTemplate = readProjectFile("templates/batch_parse.html");
    const singleTemplate = readProjectFile("templates/xhs_download.html");

    for (const template of [batchTemplate, singleTemplate]) {
        assert.match(template, /ordinary_media_streaming_protocol\.js/);
        assert.match(template, /ordinary_media_streaming\.js/);
        assert.match(template, /ordinary_media_download\.js/);
    }
});


test("Batch exposes one ordinary-video action and no compatibility transport UI", () => {
    const template = readProjectFile("templates/partials/batch_result_card.html");

    assert.match(template, /data-batch-direct-download/);
    assert.match(template, />\s*下载\s*</);
    assert.doesNotMatch(template, /data-batch-compat-download/);
    assert.doesNotMatch(template, /兼容下载/);
    assert.doesNotMatch(template, /高速直连/);
    assert.match(template, /data-batch-download-status/);
});


function fakeClassList(initial) {
    const values = new Set(initial || []);
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        toggle(value, force) {
            if (force === undefined ? !values.has(value) : force) {
                values.add(value);
            } else {
                values.delete(value);
            }
        },
        contains(value) { return values.has(value); },
    };
}


function fakeBatchVideoDownloadUi() {
    const label = {textContent: "下载"};
    const track = {
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = String(value); },
        removeAttribute(name) { delete this.attributes[name]; },
    };
    const bar = {style: {width: "42%"}};
    const percent = {textContent: "42%"};
    const bytes = {textContent: "44 MiB / 106 MiB"};
    const speed = {textContent: "2.00 MiB/s"};
    const eta = {textContent: "剩余约 31 秒"};
    const warning = {hidden: false};
    const status = {hidden: true, textContent: ""};
    const progress = {
        hidden: false,
        classList: fakeClassList(["is-pending"]),
        querySelector(selector) {
            return {
                "[role='progressbar']": track,
                ".download-progress-bar": bar,
                "[data-download-progress-percent]": percent,
                "[data-download-progress-bytes]": bytes,
                "[data-download-progress-speed]": speed,
                "[data-download-progress-eta]": eta,
                "[data-blob-download-warning]": warning,
            }[selector] || null;
        },
    };
    const item = {
        querySelector(selector) {
            if (selector === "[data-batch-download-progress]") {
                return progress;
            }
            if (selector === "[data-batch-download-status]") {
                return status;
            }
            return null;
        },
    };
    const button = {
        dataset: {},
        disabled: true,
        title: "",
        classList: fakeClassList(["is-loading"]),
        querySelector(selector) {
            return selector === ".batch-download-label" ? label : null;
        },
        closest(selector) {
            return selector === ".batch-direct-item" ? item : null;
        },
    };
    return {bar, button, bytes, eta, label, percent, progress, speed, status, track, warning};
}


test("Batch ordinary failures enter a sanitized terminal retry state", () => {
    const failureCases = [
        ["UPSTREAM_MEDIA_INVALID", "FINAL_HOST_REJECTED", "HTTP_RESPONSE_REJECTED"],
        ["UPSTREAM_FETCH_FAILED", "", "FETCH_REJECTED_BEFORE_RESPONSE"],
        ["UPSTREAM_MEDIA_INVALID", "HTTP_STATUS_REJECTED", "HTTP_RESPONSE_REJECTED"],
        ["UPSTREAM_STREAM_FAILED", "", "STREAM_BODY_FAILURE"],
        ["UPSTREAM_SIZE_MISMATCH", "", "SIZE_VALIDATION_FAILURE"],
    ];
    failureCases.forEach(([errorCode, responseRejectReason, failureStage]) => {
        const ui = fakeBatchVideoDownloadUi();
        const error = Object.assign(new Error("unsafe signed URL"), {
            errorCode,
            streamingStarted: true,
            status: {
                state: "failed",
                errorCode,
                failureStage,
                responseRejectReason,
                mediaHost: "v5-default.365yg.com",
                initialMediaFamily: "DOUYIN_365YG",
                observedFinalHost: "random.edge.bdcgslb.com",
                observedFinalHostClass: "REVIEWED_FINAL_ONLY",
                httpStatus: 200,
                contentTypeClass: "VIDEO",
            },
        });
        const message = batch.applyBatchDownloadTerminalFailure(ui.button, error, {
            downloadApi: ordinary,
            showDiagnostics: false,
        });

        assert.equal(ui.progress.hidden, true);
        assert.equal(ui.progress.classList.contains("is-pending"), false);
        assert.equal(ui.bar.style.width, "0%");
        assert.equal(ui.percent.textContent, "");
        assert.equal(ui.bytes.textContent, "");
        assert.equal(ui.speed.textContent, "");
        assert.equal(ui.eta.textContent, "");
        assert.equal(ui.warning.hidden, true);
        assert.equal(ui.button.disabled, false);
        assert.equal(ui.button.classList.contains("is-loading"), false);
        assert.equal(ui.label.textContent, "重试下载");
        assert.equal(ui.status.hidden, false);
        assert.equal(ui.status.textContent, message);
        assert.doesNotMatch(message, /FINAL_HOST|bdcgslb|Service Worker|Provider|CORS/i);
    });
});


test("staff terminal diagnostics contain only safe fixed fields and hostnames", () => {
    const ui = fakeBatchVideoDownloadUi();
    const error = {
        errorCode: "UPSTREAM_MEDIA_INVALID",
        streamingStarted: true,
        status: {
            state: "failed",
            errorCode: "UPSTREAM_MEDIA_INVALID",
            failureStage: "HTTP_RESPONSE_REJECTED",
            responseRejectReason: "FINAL_HOST_REJECTED",
            mediaHost: "v5-default.365yg.com",
            initialMediaFamily: "DOUYIN_365YG",
            observedFinalHost: "unreviewed.example",
            observedFinalHostClass: "UNREVIEWED_PUBLIC",
            httpStatus: 200,
            contentTypeClass: "VIDEO",
            unsafeUrl: "https://unreviewed.example/media.mp4?token=secret",
        },
    };
    const message = batch.applyBatchDownloadTerminalFailure(ui.button, error, {
        downloadApi: ordinary,
        showDiagnostics: true,
    });

    assert.match(message, /stage=HTTP_RESPONSE_REJECTED/);
    assert.match(message, /reason=FINAL_HOST_REJECTED/);
    assert.match(message, /initial=v5-default\.365yg\.com/);
    assert.match(message, /final=unreviewed\.example/);
    assert.doesNotMatch(message, /media\.mp4|token|secret|https:\/\//i);
});


test("Single ordinary failures hide stale progress and preserve safe diagnostics", () => {
    const source = readProjectFile("static/js/xhs_download.js");

    assert.match(source, /wrapped\.isOrdinaryMediaFailure = true/);
    assert.match(source, /error\.isOrdinaryMediaFailure[\s\S]*hideDownloadProgress\(\)/);
    assert.equal(
        ordinary.safeErrorMessage({
            errorCode: "UPSTREAM_MEDIA_INVALID",
            streamingStarted: true,
        }),
        "当前媒体节点未能启动下载，请重试。",
    );
});


test("Batch raw media controls remain behind the staff-only template gate", () => {
    const template = readProjectFile("templates/partials/batch_result_card.html");
    const page = readProjectFile("templates/batch_parse.html");

    assert.match(template, /\{% if batch_raw_links_allowed %\}[\s\S]*复制直链/);
    assert.match(template, /\{% if batch_raw_links_allowed %\}[\s\S]*展开链接/);
    assert.match(page, /\{% if batch_download_allowed %\}[\s\S]*复制全部成功资源链接/);
    assert.match(page, /\{% if batch_raw_links_allowed %\}[\s\S]*data-batch-export="links"/);
    assert.match(page, /\{% if batch_raw_links_allowed %\}[\s\S]*data-batch-export="aria2"/);
    assert.match(page, /\{% if batch_raw_links_allowed %\}[\s\S]*高级说明：下载策略矩阵/);
});


test("Single and Batch product pages do not mount the global active-download panel", () => {
    const singleTemplate = readProjectFile("templates/xhs_download.html");
    const batchTemplate = readProjectFile("templates/batch_parse.html");
    const singleSource = readProjectFile("static/js/xhs_download.js");
    const batchSource = readProjectFile("static/js/batch_parse.js");
    const sharedSource = readProjectFile("static/js/ordinary_media_download.js");

    assert.doesNotMatch(singleTemplate, /data-ordinary-active-downloads/);
    assert.doesNotMatch(batchTemplate, /data-ordinary-active-downloads/);
    assert.doesNotMatch(singleSource, /mountActiveDownloadPanel/);
    assert.doesNotMatch(batchSource, /mountActiveDownloadPanel/);
    assert.match(sharedSource, /function mountActiveDownloadPanel/);
});


function fakeDownloadItem(type, url, filename) {
    return {
        dataset: {
            type,
            url,
            downloadFilename: filename,
        },
    };
}


function fakeResultCard(status, items) {
    return {
        dataset: {status},
        querySelectorAll(selector) {
            assert.equal(selector, "[data-batch-download-item]");
            return items;
        },
    };
}


function fakeBatchRoot(cards) {
    return {
        querySelectorAll(selector) {
            assert.equal(selector, ".batch-result-card");
            return cards;
        },
    };
}


test("Batch bulk collection preserves legacy video-first order and image DOM order", () => {
    const root = fakeBatchRoot([
        fakeResultCard("success", [
            fakeDownloadItem("image", "https://media.example/row1-1.jpg", "row1_1.jpg"),
            fakeDownloadItem("image", "https://media.example/row1-2.jpg", "row1_2.jpg"),
        ]),
        fakeResultCard("success", [
            fakeDownloadItem("image", "https://media.example/row2-1.jpg", "row2_1.jpg"),
            fakeDownloadItem("video", "https://media.example/row2.mp4", "row2.mp4"),
            fakeDownloadItem("image", "https://media.example/row2-2.jpg", "row2_2.jpg"),
        ]),
        fakeResultCard("success", [
            fakeDownloadItem("image", "https://media.example/row3-1.jpg", "row3_1.jpg"),
            fakeDownloadItem("image", "https://media.example/row3-2.png", "row3_2.png"),
            fakeDownloadItem("image", "https://media.example/row3-3.jpg", "row3_3.jpg"),
        ]),
        fakeResultCard("failed", [
            fakeDownloadItem("video", "https://media.example/failed.mp4", "failed.mp4"),
        ]),
    ]);

    assert.deepEqual(batch.collectBatchResourceLinks(root), [
        {type: "image", url: "https://media.example/row1-1.jpg", filename: "row1_1.jpg"},
        {type: "image", url: "https://media.example/row1-2.jpg", filename: "row1_2.jpg"},
        {type: "video", url: "https://media.example/row2.mp4", filename: "row2.mp4"},
        {type: "image", url: "https://media.example/row2-1.jpg", filename: "row2_1.jpg"},
        {type: "image", url: "https://media.example/row2-2.jpg", filename: "row2_2.jpg"},
        {type: "image", url: "https://media.example/row3-1.jpg", filename: "row3_1.jpg"},
        {type: "image", url: "https://media.example/row3-2.png", filename: "row3_2.png"},
        {type: "image", url: "https://media.example/row3-3.jpg", filename: "row3_3.jpg"},
    ]);
});


test("Batch bulk collection deduplicates URLs and reads replacement DOM on every action", () => {
    const cards = [
        fakeResultCard("success", [
            fakeDownloadItem("video", "https://media.example/shared.mp4", "first.mp4"),
            fakeDownloadItem("video", "https://media.example/shared.mp4", "duplicate.mp4"),
        ]),
    ];
    const root = fakeBatchRoot(cards);

    assert.deepEqual(batch.collectBatchResourceLinks(root), [
        {type: "video", url: "https://media.example/shared.mp4", filename: "first.mp4"},
    ]);

    cards[0] = fakeResultCard("success", [
        fakeDownloadItem("image", "https://media.example/retry.jpg", "retry.jpg"),
    ]);
    assert.deepEqual(batch.collectBatchResourceLinks(root), [
        {type: "image", url: "https://media.example/retry.jpg", filename: "retry.jpg"},
    ]);
});


test("Batch staff text exports share the mixed resource sequence and safe filenames", () => {
    const resources = [
        {type: "video", url: "https://media.example/video.mp4", filename: "../bad:name?.mp4"},
        {type: "image", url: "https://media.example/image.jpg", filename: "gallery 1.jpg"},
    ];

    assert.equal(
        batch.buildBatchLinksText(resources),
        "https://media.example/video.mp4\nhttps://media.example/image.jpg",
    );
    assert.equal(
        batch.buildBatchAria2Text(resources, ordinary.sanitizeFilename),
        [
            "https://media.example/video.mp4",
            "  out=.._bad_name_.mp4",
            "https://media.example/image.jpg",
            "  out=gallery 1.jpg",
        ].join("\n"),
    );
    assert.doesNotMatch(batch.buildBatchAria2Text(resources, ordinary.sanitizeFilename), /Cookie|Referer|Header|token=/i);
});


test("Batch image results use the full gallery while video keeps its existing cover", () => {
    const template = readProjectFile("templates/partials/batch_result_card.html");

    assert.match(template, /class="batch-media-tools[^"]*"[\s\S]*class="batch-result-cover"/);
    assert.match(template, /data-batch-photo-gallery/);
    assert.match(template, /\{% for item in row\.image_items %\}/);
    assert.doesNotMatch(template, /row\.image_items\|slice:":4"/);
    assert.doesNotMatch(template, /class="batch-image-grid"/);
});


test("shared primitive accepts a validated ordinary Provider video", () => {
    assert.equal(ordinary.isOrdinaryMediaItem({
        type: "video",
        url: "https://media.example.com/video.mp4",
        browser_direct_fetch_safe: true,
        requires_merge: false,
        requires_cookie: false,
    }), true);
});


test("shared primitive rejects merge, cookie, unsafe, and non-video resources", () => {
    const base = {
        type: "video",
        url: "https://media.example.com/video.mp4",
        browser_direct_fetch_safe: true,
        requires_merge: false,
        requires_cookie: false,
    };

    assert.equal(ordinary.isOrdinaryMediaItem({...base, requires_merge: true}), false);
    assert.equal(ordinary.isOrdinaryMediaItem({...base, requires_cookie: true}), false);
    assert.equal(ordinary.isOrdinaryMediaItem({...base, browser_direct_fetch_safe: false}), false);
    assert.equal(ordinary.isOrdinaryMediaItem({...base, type: "image"}), false);
});


test("Batch persistence remains limited to parse preferences and link drafts", () => {
    const source = readProjectFile("static/js/batch_parse.js");

    assert.match(source, /animeimages\.batchParseDraft\.v2/);
    assert.doesNotMatch(source, /localStorage\.setItem\([^\n]*(signed|manifest|media|result)/i);
});
