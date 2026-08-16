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


test("Batch bulk collection includes successful video and images in stable DOM order", () => {
    const root = fakeBatchRoot([
        fakeResultCard("success", [
            fakeDownloadItem("image", "https://media.example/row1-1.jpg", "row1_1.jpg"),
            fakeDownloadItem("image", "https://media.example/row1-2.jpg", "row1_2.jpg"),
        ]),
        fakeResultCard("success", [
            fakeDownloadItem("video", "https://media.example/row2.mp4", "row2.mp4"),
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


test("Batch preview is integrated into the media tools instead of a right-side sibling", () => {
    const template = readProjectFile("templates/partials/batch_result_card.html");

    assert.match(template, /class="batch-media-tools[^"]*"[\s\S]*class="batch-result-cover"/);
    assert.doesNotMatch(template, /<\/div>\s*<div class="batch-result-cover">\s*\{% if row\.video_items/);
    assert.match(template, /row\.image_items\|slice:":4"/);
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
