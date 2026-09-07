"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const ordinary = require("../static/js/ordinary_media_download.js");
const photo = require("../static/js/browser_photo_archive.js");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function item(type = "video") {
    return {type, asset_id: `upstream:job:${type}:1`, source_platform: "upstream",
        provider: "yt_dlp_generic", browser_upstream_bound: true,
        browser_direct_fetch_safe: true, url: `https://media.example.org/a.${type === "video" ? "mp4" : "jpg"}`};
}

test("upstream video uses Blob with strict redirects, never Service Worker", async () => {
    let saved = false;
    const result = await ordinary.download(item(), {
        streamingClient: {capabilityAvailable() { throw new Error("must not use SW"); }},
        fetchImpl: async (url, options) => {
            assert.equal(url, item().url);
            assert.equal(options.redirect, "error");
            assert.equal(options.credentials, "omit");
            assert.equal(options.mode, "cors");
            return new Response(new Blob(["video"], {type: "video/mp4"}));
        },
        handoffBlob: async () => { saved = true; },
    });
    assert.equal(result.transport, "browser_blob");
    assert.equal(saved, true);
});

test("prefix, server flag and source URL are all required", async () => {
    for (const changes of [{browser_upstream_bound: false}, {browser_upstream_bound: "true"},
        {source_platform: "other"}, {url: "http://example.org/a.mp4"},
        {url: "https://user:pass@example.org/a.mp4"}, {provider: "kukutool"}]) {
        assert.equal(ordinary.isOrdinaryMediaItem({...item(), ...changes}), false);
    }
    await assert.rejects(ordinary.download({...item(), asset_id: "legacy:video:1"}, {
        forceBlob: true, fetchImpl() { throw new Error("must not fetch arbitrary host"); },
    }), {errorCode: "UPSTREAM_MEDIA_INVALID"});
});

test("redirected response and fetch failure never fall back to server", async () => {
    await assert.rejects(ordinary.download(item(), {
        fetchImpl: async () => ({ok: true, redirected: true, url: "https://elsewhere.example.org/a.mp4"}),
    }), {errorCode: "UPSTREAM_MEDIA_INVALID"});
    await assert.rejects(ordinary.download(item(), {
        fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
    }), {errorCode: "UPSTREAM_BROWSER_FETCH_FAILED"});
    assert.match(ordinary.safeErrorMessage({errorCode: "UPSTREAM_BROWSER_FETCH_FAILED"}), /CORS/);
});

test("upstream images reuse archive fetch with strict redirects and authorization", async () => {
    let calls = 0;
    const options = {
        fetchImpl: async (_url, request) => {
            calls += 1;
            assert.equal(request.redirect, "error");
            return new Response(Uint8Array.from([255, 216, 255, 224, 1]), {headers: {"Content-Type": "image/jpeg"}});
        },
        handoff: async () => {},
    };
    await photo.downloadImage(item("image"), options);
    assert.equal(calls, 1);
    await assert.rejects(photo.downloadImage({...item("image"), browser_upstream_bound: false}, options),
        {errorCode: "UPSTREAM_PHOTO_INVALID_ITEM"});
    assert.equal(calls, 1);
    await assert.rejects(photo.downloadImage(item("image"), {
        fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
    }), {errorCode: "UPSTREAM_PHOTO_FETCH_FAILED"});
});

test("upstream Blob rejects HTTP, content type, empty, oversized and truncated bodies without saving", async () => {
    const cases = [
        [new Response("denied", {status: 403}), "BROWSER_FETCH_HTTP_ERROR", {}],
        [new Response("<html>error</html>", {headers: {"Content-Type": "text/html"}}), "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT", {}],
        [new Response(new Blob([], {type: "video/mp4"})), "BROWSER_DOWNLOAD_EMPTY", {}],
        [new Response(new Blob(["12345"], {type: "video/mp4"})), "BROWSER_BLOB_TOO_LARGE", {maxBytes: 3}],
        [new Response("123", {headers: {"Content-Type": "video/mp4", "Content-Length": "8"}}), "BROWSER_DOWNLOAD_SIZE_MISMATCH", {}],
    ];
    for (const [response, errorCode, extra] of cases) {
        let calls = 0;
        let saved = false;
        await assert.rejects(ordinary.download(item(), {...extra,
            fetchImpl: async (url, opts) => {
                assert.equal(url, item().url);
                assert.equal(opts.redirect, "error");
                calls += 1;
                return response;
            },
            handoffBlob: async () => { saved = true; },
            streamingClient: {capabilityAvailable() { throw new Error("No fallback permitted"); }},
        }), {errorCode});
        assert.equal(calls, 1);
        assert.equal(saved, false);
    }
});

test("image response redirect and invalid magic reject before handoff", async () => {
    for (const redirected of [true, false]) {
        const response = new Response("not an image", {headers: {"Content-Type": "image/jpeg"}});
        Object.defineProperty(response, "redirected", {value: redirected});
        let saved = false;
        await assert.rejects(photo.downloadImage(item("image"), {
            fetchImpl: async () => response,
            handoff: async () => { saved = true; },
        }));
        assert.equal(saved, false);
    }
});

test("source marker and browser-safe boolean alone cannot authorize upstream video", async () => {
    let fetched = false;
    await assert.rejects(ordinary.download({...item(), browser_upstream_bound: undefined}, {
        fetchImpl: async () => { fetched = true; },
    }), {errorCode: "BROWSER_DOWNLOAD_NOT_ORDINARY_MEDIA"});
    assert.equal(fetched, false);
});

test("batch browser selector preserves upstream identity and requires zero relay", () => {
    const filename = path.join(__dirname, "../static/js/batch_parse.js");
    const source = fs.readFileSync(filename, "utf8").replace(
        "const testApi = Object.freeze({", "const testApi = Object.freeze({batchBrowserPhotoItem, batchPhotoItemsRequireZeroRelay,");
    const context = {module: {exports: {}}};
    vm.runInNewContext(source, context, {filename});
    const api = context.module.exports;
    const selected = api.batchBrowserPhotoItem({dataset: {
        type: "image", url: item("image").url, assetId: item("image").asset_id,
        sourcePlatform: "upstream", provider: "gallery_dl_generic",
    }});
    assert.equal(selected.asset_id, item("image").asset_id);
    assert.equal(api.batchPhotoItemsRequireZeroRelay([selected]), true);
});
