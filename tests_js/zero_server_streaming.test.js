"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const protocol = require("../research/zero_server_streaming/protocol.js");

const researchDir = path.join(__dirname, "../research/zero_server_streaming");
const serviceWorkerSource = fs.readFileSync(path.join(researchDir, "sw.js"), "utf8");
const applicationSource = fs.readFileSync(path.join(researchDir, "app.js"), "utf8");
const productTemplates = [
    "../templates/xhs_download.html",
    "../templates/batch_parse.html",
].map((filename) => fs.readFileSync(path.join(__dirname, filename), "utf8")).join("\n");

test("descriptor validation is bounded and keeps the signed URL memory-only", () => {
    const descriptor = protocol.createDescriptor({
        id: "0123456789abcdef0123456789abcdef",
        url: "https://v5-default.365yg.com/video.mp4?fixture=1",
        filename: "../demo:name?.mp4",
        expectedBytes: 104857600,
    });
    assert.equal(descriptor.id, "0123456789abcdef0123456789abcdef");
    assert.equal(descriptor.filename, ".._demo_name_.mp4");
    assert.equal(descriptor.expectedBytes, 104857600);
    assert.match(descriptor.url, /^https:/);
    assert.throws(() => protocol.createDescriptor({...descriptor, id: "bad"}), /INVALID_DESCRIPTOR_ID/);
    assert.throws(() => protocol.createDescriptor({...descriptor, url: "javascript:alert(1)"}), /INVALID_MEDIA_URL/);
});

test("controlled loopback URLs are allowed without opening general insecure HTTP", () => {
    assert.equal(protocol.isAllowedResearchUrl("http://127.0.0.1:8766/bytes/5"), true);
    assert.equal(protocol.isAllowedResearchUrl("http://localhost:8766/bytes/5"), true);
    assert.equal(protocol.isAllowedResearchUrl("https://v5-default.365yg.com/video.mp4"), true);
    assert.equal(protocol.isAllowedResearchUrl("https://365yg.com/video.mp4"), true);
    assert.equal(protocol.isAllowedResearchUrl("https://not365yg.com/video.mp4"), false);
    assert.equal(protocol.isAllowedResearchUrl("http://example.test/video.mp4"), false);
});

test("synthetic path accepts only one opaque descriptor id", () => {
    const id = "0123456789abcdef0123456789abcdef";
    assert.equal(protocol.downloadPath(id), `./__animeimages-stream-download/${id}`);
    assert.equal(
        protocol.descriptorIdFromPath(`/research/zero_server_streaming/__animeimages-stream-download/${id}`),
        id,
    );
    assert.equal(protocol.descriptorIdFromPath(`/__animeimages-stream-download/${id}/extra`), "");
});

test("Service Worker streams the response body without complete buffering", () => {
    assert.match(serviceWorkerSource, /upstream\.body\.pipeThrough\(counter\)/);
    assert.match(serviceWorkerSource, /new TransformStream/);
    assert.match(serviceWorkerSource, /Content-Disposition/);
    assert.doesNotMatch(serviceWorkerSource, /\.blob\s*\(/);
    assert.doesNotMatch(serviceWorkerSource, /\.arrayBuffer\s*\(/);
    assert.doesNotMatch(serviceWorkerSource, /new Blob/);
    assert.doesNotMatch(serviceWorkerSource, /CacheStorage|caches\.|indexedDB/i);
});

test("Service Worker bounds actual streamed bytes and verifies exact completion", () => {
    assert.match(serviceWorkerSource, /streamByteLimit/);
    assert.match(serviceWorkerSource, /receivedBytes > streamByteLimit/);
    assert.match(serviceWorkerSource, /receivedBytes !== descriptor\.expectedBytes/);
    assert.match(serviceWorkerSource, /UPSTREAM_SIZE_LIMIT_EXCEEDED/);
    assert.match(serviceWorkerSource, /UPSTREAM_SIZE_MISMATCH/);
});

test("POC cannot call AnimeImages ordinary-media relay endpoints", () => {
    const source = `${serviceWorkerSource}\n${applicationSource}`;
    assert.doesNotMatch(source, /\/media\/download-prepare\//);
    assert.doesNotMatch(source, /\/media\/download-stream\//);
    assert.doesNotMatch(source, /\/media\/download-status\//);
    assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|caches\./i);
});

test("POC files are not imported by production Single or Batch templates", () => {
    assert.doesNotMatch(productTemplates, /zero_server_streaming|__animeimages-stream-download|sw\.js/);
});

test("page drops the media URL after handing the descriptor to the worker", () => {
    assert.match(applicationSource, /byId\("media-url"\)\.value = ""/);
    assert.match(applicationSource, /state\.prepared = Object\.freeze/);
    assert.doesNotMatch(applicationSource, /state\.prepared = descriptor/);
});

test("real descriptor bridge is loopback-only and research-scoped", () => {
    assert.match(applicationSource, /http:\/\/127\.0\.0\.1:8767\/descriptor/);
    assert.doesNotMatch(applicationSource, /\/media\/download-(prepare|stream|status)\//);
});
