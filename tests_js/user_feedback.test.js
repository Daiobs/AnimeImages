"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const feedback = require("../static/js/user_feedback.js");


test("page metadata strips query strings and fragments", () => {
    assert.equal(
        feedback.pagePath("https://download.example/batch/?token=secret#result"),
        "/batch/"
    );
    assert.equal(feedback.pagePath("/xhs/results/?q=secret"), "/xhs/results/");
});

test("source platform derives only from stable public route prefixes", () => {
    assert.equal(feedback.sourcePlatform("/batch/"), "batch");
    assert.equal(feedback.sourcePlatform("/xhs/results/"), "xhs");
    assert.equal(feedback.sourcePlatform("/tiktok/"), "tiktok");
    assert.equal(feedback.sourcePlatform("/bilibili/results/"), "bilibili");
    assert.equal(feedback.sourcePlatform("/admin/login/"), "home");
});

test("viewport dimensions are bounded and contain no fingerprint data", () => {
    assert.equal(feedback.viewportDimension(390), "390");
    assert.equal(feedback.viewportDimension("844"), "844");
    assert.equal(feedback.viewportDimension(0), "");
    assert.equal(feedback.viewportDimension(10001), "");
    assert.equal(feedback.viewportDimension("not-a-number"), "");
});

test("metadata contains only path, platform, and bounded viewport", () => {
    assert.deepEqual(
        feedback.buildMetadata(
            {href: "https://download.example/xhs/results/?signed=value"},
            {innerWidth: 1440, innerHeight: 900, userAgent: "do-not-copy"}
        ),
        {
            page_path: "/xhs/results/",
            source_platform: "xhs",
            viewport_width: "1440",
            viewport_height: "900",
        }
    );
});

test("feedback client does not persist payloads in browser storage", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/user_feedback.js"),
        "utf8"
    );

    assert.equal(source.includes("localStorage"), false);
    assert.equal(source.includes("sessionStorage"), false);
});

test("optional feedback image uses a bounded client-side hint", () => {
    assert.equal(feedback.imageValidationMessage(null), "");
    assert.equal(
        feedback.imageValidationMessage({size: 1024, type: "image/png"}),
        ""
    );
    assert.equal(
        feedback.imageValidationMessage({size: 6 * 1024 * 1024, type: "image/png"}),
        "问题截图不能超过 5 MB。"
    );
    assert.equal(
        feedback.imageValidationMessage({size: 1024, type: "image/svg+xml"}),
        "仅支持 JPG、PNG 或 WebP 图片。"
    );
});

test("feedback form has no category or editable request id", () => {
    const template = fs.readFileSync(
        path.join(__dirname, "../templates/base.html"),
        "utf8"
    );

    assert.equal(template.includes('name="message"'), true);
    assert.equal(template.includes('name="image"'), true);
    assert.equal(template.includes('name="contact"'), true);
    assert.equal(template.includes('name="category"'), false);
    assert.equal(template.includes('name="request_id"'), false);
    assert.equal(template.includes('enctype="multipart/form-data"'), true);
});
