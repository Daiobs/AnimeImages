"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const picker = require("../static/js/ytdlp_result_picker.js");
const ordinary = require("../static/js/ordinary_media_download.js");

const win = {location: {href: "http://localhost:8000/results/", origin: "http://localhost:8000"}};
const documentObject = {querySelector: () => ({value: "csrf-fixture"}), cookie: ""};
const root = {dataset: {selectionUrl: "/media/ytdlp-selected-format/"}};
function selected(id = "fmt720", kind = "video") { return {id, kind, token: `signed-${id}`}; }
function item(id = "fmt720", type = "video", extra = {}) {
    return {selection_schema: picker.SCHEMA, candidate_id: id, selected_format_id: id,
        type, provider: "yt_dlp_generic", source_platform: "upstream", asset_id: `upstream:selection:${id}`,
        url: `https://cdn.example.org/${id}.${type === "audio" ? "m4a" : "mp4"}`,
        filename: `${id}.${type === "audio" ? "m4a" : "mp4"}`,
        browser_upstream_bound: true, browser_direct_fetch_safe: true, requires_merge: false,
        requires_cookie: false, audio_tracks: "present", video_tracks: type === "audio" ? "absent" : "present",
        ...extra};
}
function response(content, source, type = "video/mp4", overrides = {}) {
    const bytes = new Blob([content], {type});
    return {ok: true, type: "cors", status: 200, url: source, redirected: false,
        headers: {get(name) { return ({"content-type": type, "content-length": String(bytes.size)})[name.toLowerCase()] || null; }},
        body: bytes.stream(), blob: async () => bytes, ...overrides};
}

test("selection captures one immutable candidate identity and its exact token", () => {
    const radio = {value: "b", dataset: {token: "signed-b", kind: "video", canDownload: "true", canPreview: "false", label: "720p"}};
    const result = picker.selected({querySelector: () => radio});
    radio.value = "c"; radio.dataset.token = "signed-c";
    assert.equal(result.id, "b"); assert.equal(result.token, "signed-b");
    assert.equal(result.canDownload, true); assert.equal(result.canPreview, false);
    assert.equal(Object.isFrozen(result), true);
});

test("selected descriptor posts candidate identity, never a client URL or format expression", async () => {
    const calls = [];
    const env = {windowObject: win, documentObject, fetchImpl: async (url, options) => {
        const body = JSON.parse(options.body); calls.push({url, options, body});
        return {ok: true, json: async () => ({status: "success", item: item(body.candidate_id)})};
    }};
    const a = await picker.descriptor(root, selected("1080"), "download", env);
    const b = await picker.descriptor(root, selected("720"), "download", env);
    assert.match(a.url, /1080\.mp4$/); assert.match(b.url, /720\.mp4$/);
    assert.deepEqual(Object.keys(calls[1].body).sort(), ["action", "candidate_id", "resource_token"]);
    assert.equal(calls[1].options.credentials, "same-origin");
    assert.equal(calls[1].options.headers["X-CSRFToken"], "csrf-fixture");
});

test("descriptor endpoints are same-origin even if DOM data is modified", async () => {
    for (const url of ["https://elsewhere.example/api", "//elsewhere.example/api", "javascript:alert(1)"]) {
        assert.throws(() => picker.sameOriginPath(url, win));
    }
    assert.equal(picker.sameOriginPath("/media/ytdlp-selected-format/", win), "http://localhost:8000/media/ytdlp-selected-format/");
});

test("returned identity, type, flags and URL cannot silently substitute another format", () => {
    for (const change of [{candidate_id: "other"}, {type: "audio"}, {provider: "videodl_repo"},
        {requires_merge: true}, {requires_cookie: true}, {browser_upstream_bound: false},
        {url: "http://cdn.example.org/video.mp4"}, {url: "https://user:pass@cdn.example.org/video.mp4"},
        {url: "https://cdn.example.org:444/video.mp4"}, {url: "https://cdn.example.org/v.mp4#fragment"},
        {url: "https://cdn.example.org/\\file"}, {url: "data:text/html,anything"}]) {
        assert.throws(() => picker.validateItem(item("fmt720", "video", change), selected()));
    }
    assert.equal(picker.validateItem(item(), selected()).candidate_id, "fmt720");
});

test("expired, denied and malformed descriptors stop without a media fetch", async () => {
    for (const result of [{ok: false, json: async () => ({status: "error", message: "已过期"})},
        {ok: true, json: async () => ({status: "success", item: item("other")})},
        {ok: false, json: async () => { throw new Error("bad json"); }}]) {
        let calls = 0;
        await assert.rejects(picker.descriptor(root, selected(), "download", {
            windowObject: win, documentObject, fetchImpl: async () => { calls += 1; return result; },
        }));
        assert.equal(calls, 1);
    }
});

test("descriptor propagates the operation abort signal and does not auto retry", async () => {
    const controller = new AbortController(); controller.abort();
    let count = 0;
    await assert.rejects(picker.descriptor(root, selected(), "download", {documentObject, windowObject: win,
        fetchImpl: async (_url, options) => {
            count += 1; assert.equal(options.signal, controller.signal);
            throw new DOMException("Aborted", "AbortError");
        }}, controller.signal), {name: "AbortError"});
    assert.equal(count, 1);
});

test("bound audio support does not open arbitrary image/audio or merge/cookie items", () => {
    assert.equal(ordinary.isOrdinaryMediaItem(item("voice", "audio")), true);
    for (const change of [{selection_schema: ""}, {source_platform: ""}, {provider: "gallery_dl_generic"},
        {audio_tracks: "unknown"}, {video_tracks: "present"}, {browser_upstream_bound: false},
        {requires_cookie: true}, {requires_merge: true}, {type: "image"}]) {
        assert.equal(ordinary.isOrdinaryMediaItem(item("voice", "audio", change)), false);
    }
});

test("selected audio retains original M4A bytes and filename via existing browser transport", async () => {
    const audio = item("voice", "audio"); let handoff = null; let requested = null;
    const result = await ordinary.download(audio, {
        filename: audio.filename,
        fetchImpl: async (url, options) => {
            requested = {url, options}; return response("exact-audio-bytes", audio.url, "audio/mp4");
        },
        handoffBlob: async (blob, filename) => { handoff = {text: await blob.text(), filename}; },
    });
    assert.equal(requested.url, audio.url); assert.equal(requested.options.credentials, "omit");
    assert.equal(requested.options.redirect, "error"); assert.equal(requested.options.mode, "cors");
    assert.equal(requested.options.referrerPolicy, "no-referrer");
    assert.deepEqual(handoff, {text: "exact-audio-bytes", filename: "voice.m4a"});
    assert.equal(result.transport, "browser_blob"); assert.equal(result.receivedBytes, 17);
});

test("switching two selected video descriptors uses the selected URL and no server fallback", async () => {
    const seen = [];
    for (const id of ["1080", "720"]) {
        const video = item(id);
        const saved = await ordinary.download(video, {filename: video.filename,
            fetchImpl: async (url) => { seen.push(url); return response(id, url); },
            handoffBlob: async (blob, filename) => { assert.equal(await blob.text(), id); assert.equal(filename, id + ".mp4"); },
            streamingClient: {capabilityAvailable() { assert.fail("bound selection must not switch to server/other transport"); }},
        });
        assert.equal(saved.receivedBytes, id.length);
    }
    assert.deepEqual(seen, ["https://cdn.example.org/1080.mp4", "https://cdn.example.org/720.mp4"]);
});

test("audio/video MIME guards do not accept HTML or swap a selected video for audio", async () => {
    assert.equal(ordinary.contentTypeAllowsSelectedAudio("audio/mp4; codecs=mp4a"), true);
    assert.equal(ordinary.contentTypeAllowsSelectedAudio("text/html"), false);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("audio/mp4"), false);
    for (const [kind, type] of [["audio", "text/html"], ["audio", "video/mp4"], ["video", "audio/mp4"]]) {
        const media = item("bad", kind);
        await assert.rejects(ordinary.download(media, {filename: media.filename,
            fetchImpl: async () => response("bytes", media.url, type),
            handoffBlob: () => assert.fail("no invalid handoff"),
        }), error => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT");
    }
});

test("size budget and redirects keep existing bounded-download protections", async () => {
    const media = item(); let requested = 0;
    await assert.rejects(ordinary.download({...media, size_bytes: 50}, {maxBytes: 10,
        fetchImpl: () => { requested += 1; },
    }), error => error.errorCode === "BROWSER_BLOB_TOO_LARGE");
    assert.equal(requested, 0);
    for (const override of [{redirected: true}, {type: "opaque"}, {url: "https://different.example/x.mp4"}]) {
        await assert.rejects(ordinary.download(media, {
            fetchImpl: async () => response("x", media.url, "video/mp4", override),
            handoffBlob: () => assert.fail("no redirected handoff"),
        }));
    }
});

test("cover descriptor stays a URL rather than a download grant", async () => {
    const source = await picker.descriptor(root, selected(), "cover", {documentObject, windowObject: win,
        fetchImpl: async () => ({ok: true, json: async () => ({status: "success", url: "https://cdn.example.org/cover.jpg"})}),
    });
    assert.equal(source, "https://cdn.example.org/cover.jpg");
});
