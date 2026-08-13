"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ordinary = require("../static/js/ordinary_media_download.js");


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
    assert.match(page, /\{% if batch_raw_links_allowed %\}[\s\S]*复制全部成功视频直链/);
    assert.match(page, /\{% if batch_raw_links_allowed %\}[\s\S]*高级说明：下载策略矩阵/);
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
