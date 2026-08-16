"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const batch = require("../static/js/batch_parse.js");


function readProjectFile(relativePath) {
    return fs.readFileSync(path.join(__dirname, "..", relativePath), "utf8");
}


function fakePhoto(url, index, extra) {
    return {
        dataset: {
            type: "image",
            url,
            sizeBytes: String(index * 10),
            assetId: `asset-${index}`,
            provider: "provider",
            providerResourceId: "resource",
            providerCacheKey: "cache",
            providerManifestToken: `token-${index}`,
            cardPreviewSrc: `https://site.test/proxy?card=${index}`,
            lightboxPreviewSrc: `https://site.test/proxy?lightbox=${index}`,
            ...(extra || {}),
        },
    };
}


function fakeSection(items, baseFilename) {
    return {
        dataset: {baseFilename: baseFilename || "gallery"},
        querySelectorAll(selector) {
            assert.equal(selector, "[data-batch-photo-item]");
            return items;
        },
    };
}


function connectPhotoRow(items) {
    const section = fakeSection(items);
    items.forEach((item) => {
        item.closest = (selector) => {
            assert.equal(selector, "[data-batch-photo-section]");
            return section;
        };
    });
    return section;
}


test("Download All payload contains exactly the current row image candidates in DOM order", () => {
    const photos = [
        fakePhoto("https://ci.xiaohongshu.com/one.jpg", 1),
        fakePhoto("https://ci.xiaohongshu.com/two.png", 2),
        fakePhoto("https://ci.xiaohongshu.com/three.webp", 3),
    ];
    const payload = batch.buildBatchPhotoArchivePayload(fakeSection(photos, "post title"));

    assert.equal(payload.media_items.length, 3);
    assert.deepEqual(payload.media_items.map((item) => item.url), photos.map((item) => item.dataset.url));
    assert.equal(payload.archive_profile, "batch_images");
    assert.equal(payload.download_mode, "zip");
    assert.equal(payload.base_filename, "post title");
    assert.equal(payload.media_items.some((item) => Object.hasOwn(item, "card_preview_url")), false);
    assert.equal(payload.media_items.some((item) => Object.hasOwn(item, "lightbox_preview_url")), false);
});


test("Download All payload preserves provider manifest fields without preview URLs", () => {
    const item = fakePhoto("https://media.example.com/image.jpg", 4);
    const payload = batch.buildBatchPhotoArchivePayload(fakeSection([item]));

    assert.deepEqual(payload.media_items[0], {
        type: "image",
        url: item.dataset.url,
        size_bytes: "40",
        asset_id: "asset-4",
        provider: "provider",
        provider_resource_id: "resource",
        provider_cache_key: "cache",
        provider_manifest_token: "token-4",
    });
    assert.doesNotMatch(JSON.stringify(payload), /card=|lightbox=/);
});


test("Download All reads replacement DOM instead of retaining an old image array", () => {
    const items = [fakePhoto("https://ci.xiaohongshu.com/old.jpg", 1)];
    const section = fakeSection(items);
    assert.deepEqual(
        batch.buildBatchPhotoArchivePayload(section).media_items.map((item) => item.url),
        ["https://ci.xiaohongshu.com/old.jpg"],
    );

    items.splice(0, 1,
        fakePhoto("https://ci.xiaohongshu.com/new-1.jpg", 1),
        fakePhoto("https://ci.xiaohongshu.com/new-2.jpg", 2),
    );
    assert.deepEqual(
        batch.buildBatchPhotoArchivePayload(section).media_items.map((item) => item.url),
        [
            "https://ci.xiaohongshu.com/new-1.jpg",
            "https://ci.xiaohongshu.com/new-2.jpg",
        ],
    );
});


test("Lightbox navigation stays within the current result row", () => {
    const rowOne = [1, 2, 3, 4, 5].map((index) => fakePhoto(`https://one.test/${index}`, index));
    const rowTwo = [1, 2, 3].map((index) => fakePhoto(`https://two.test/${index}`, index));
    connectPhotoRow(rowOne);
    connectPhotoRow(rowTwo);

    const middle = batch.getBatchLightboxNavigation(rowOne[2]);
    assert.equal(middle.index, 2);
    assert.equal(middle.total, 5);
    assert.equal(middle.previousItem, rowOne[1]);
    assert.equal(middle.nextItem, rowOne[3]);
    assert.equal(batch.getBatchLightboxNavigation(rowOne[4]).nextItem, null);
    assert.equal(batch.getBatchLightboxNavigation(rowTwo[0]).previousItem, null);
});


test("Batch gallery template renders all items and one global lightbox", () => {
    const card = readProjectFile("templates/partials/batch_result_card.html");
    const page = readProjectFile("templates/batch_parse.html");

    assert.match(card, /\{% for item in row\.image_items %\}/);
    assert.doesNotMatch(card, /slice:":4"/);
    assert.match(card, /loading="eager" fetchpriority="high"/);
    assert.match(card, /loading="lazy"/);
    assert.match(card, /data-lightbox-preview-src/);
    assert.equal((page.match(/id="batchPhotoLightbox"/g) || []).length, 1);
    assert.match(page, /data-batch-lightbox-prev/);
    assert.match(page, /data-batch-lightbox-next/);
    assert.match(page, /data-batch-lightbox-download/);
});


test("Gallery CSS preserves intrinsic ratios and removes the cropped four-cell grid", () => {
    const css = readProjectFile("static/css/app.css");
    const galleryBlock = css.slice(
        css.indexOf(".batch-photo-gallery"),
        css.indexOf(".batch-direct-tools"),
    );

    assert.match(galleryBlock, /grid-template-columns:\s*repeat\(auto-fill, minmax\(280px, 1fr\)\)/);
    assert.match(galleryBlock, /height:\s*auto/);
    assert.match(galleryBlock, /object-fit:\s*contain/);
    assert.doesNotMatch(galleryBlock, /height:\s*220px/);
    assert.doesNotMatch(galleryBlock, /object-fit:\s*cover/);
    assert.doesNotMatch(css, /\.batch-image-grid/);
    assert.match(css, /\.batch-photo-archive-progress\[hidden\]\s*{\s*display:\s*none/);
});


test("Batch archive handoff URLs must remain same-origin", () => {
    assert.equal(
        batch.safeSameOriginDownloadUrl("/media/download-tasks/id/file/", "https://site.test"),
        "https://site.test/media/download-tasks/id/file/",
    );
    assert.throws(
        () => batch.safeSameOriginDownloadUrl("https://evil.test/file.zip", "https://site.test"),
        /地址无效/,
    );
});


test("Gallery contracts keep single-image direct and multi-image ZIP paths separate", () => {
    const source = readProjectFile("static/js/batch_parse.js");

    assert.match(source, /function handoffBatchImage\(button\)/);
    assert.match(source, /function downloadBatchPhotoArchive\(/);
    assert.match(source, /archive_profile:\s*"batch_images"/);
    assert.match(source, /dataBatchDownloadTaskUrl|batchDownloadTaskUrl/);
    assert.doesNotMatch(source, /localStorage\.setItem\([^\n]*(?:cardPreview|lightboxPreview|media_items)/);
});
