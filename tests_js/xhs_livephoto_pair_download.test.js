"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const pair = require("../static/js/xhs_livephoto_pair_download.js");


function validPayload() {
    return {
        ok: true,
        archive_filename: "fixture_livephoto.zip",
        still: {media_type: "image", url: "https://still.invalid/item"},
        motion: {
            media_type: "video",
            format_class: "mp4",
            url: "https://motion.invalid/item.mp4",
        },
    };
}


test("pair descriptor request is POST-only control-plane JSON with CSRF", async () => {
    let observed = null;
    const payload = await pair.requestPairDescriptor(
        "opaque-ticket",
        "/media/xhs-livephoto-pair-descriptor/",
        {
            csrfToken: "csrf-fixture",
            async fetchImpl(url, options) {
                observed = {url, options};
                return {ok: true, async json() { return validPayload(); }};
            },
        },
    );

    assert.equal(payload.ok, true);
    assert.equal(observed.url, "/media/xhs-livephoto-pair-descriptor/");
    assert.equal(observed.options.method, "POST");
    assert.equal(observed.options.credentials, "same-origin");
    assert.equal(observed.options.cache, "no-store");
    assert.equal(observed.options.headers["X-CSRFToken"], "csrf-fixture");
    assert.deepEqual(JSON.parse(observed.options.body), {pair_token: "opaque-ticket"});
});


test("motion descriptor uses an independent opaque POST token", async () => {
    let observed = null;
    const payload = await pair.requestMotionDescriptor(
        "opaque-motion-ticket",
        "/media/xhs-livephoto-motion-descriptor/",
        {
            csrfToken: "csrf-fixture",
            async fetchImpl(url, options) {
                observed = {url, options};
                return {
                    ok: true,
                    async json() {
                        return {
                            ok: true,
                            motion: {
                                media_type: "video",
                                format_class: "mp4",
                                url: "https://motion.invalid/item.mp4",
                            },
                        };
                    },
                };
            },
        },
    );

    assert.equal(payload.ok, true);
    assert.equal(observed.url, "/media/xhs-livephoto-motion-descriptor/");
    assert.equal(observed.options.method, "POST");
    assert.equal(observed.options.credentials, "same-origin");
    assert.deepEqual(
        JSON.parse(observed.options.body),
        {motion_token: "opaque-motion-ticket"},
    );
});


test("pair descriptor fails closed on malformed control-plane payload", async () => {
    await assert.rejects(
        pair.requestPairDescriptor("opaque-ticket", "/descriptor/", {
            async fetchImpl() {
                return {
                    ok: true,
                    async json() {
                        return {
                            ok: true,
                            archive_filename: "fixture.zip",
                            still: {media_type: "image", url: "https://still.invalid/item"},
                            motion: {media_type: "video", format_class: "webm", url: "https://motion.invalid/item"},
                        };
                    },
                };
            },
        }),
        (error) => error.errorCode === "XHS_LIVEPHOTO_PAIR_INVALID",
    );
});


test("aborted descriptor fetch maps to a fixed cancellation code", async () => {
    await assert.rejects(
        pair.requestPairDescriptor("opaque-ticket", "/descriptor/", {
            async fetchImpl() {
                throw Object.assign(new Error(), {name: "AbortError"});
            },
        }),
        (error) => error.errorCode === "BROWSER_PHOTO_CANCELLED",
    );
});


test("pair errors remain safe and product copy exposes no transport internals", () => {
    assert.equal(
        pair.safeErrorMessage({errorCode: "XHS_LIVEPHOTO_PAIR_EXPIRED"}),
        "资源已失效，请重新解析。",
    );
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_livephoto_pair_download.js"),
        "utf8",
    );
    assert.doesNotMatch(source, /media\/download-(?:prepare|stream|status)/);
    assert.doesNotMatch(source, /server relay|服务器中转/i);
});


test("pair helper has no card-level download listener or browser handoff", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_livephoto_pair_download.js"),
        "utf8",
    );

    assert.doesNotMatch(source, /data-livephoto-download-(?:original|pair)/);
    assert.doesNotMatch(source, /createLivePhotoPairArchive|downloadImage/);
    assert.doesNotMatch(source, /addEventListener\("click"/);
});


test("pair helper controller is inert and reusable for async bootstrap", () => {
    let clickListeners = 0;
    const documentObject = {
        addEventListener(name) {
            if (name === "click") clickListeners += 1;
        },
    };
    const first = pair.ensureController(documentObject);
    const second = pair.ensureController(documentObject);

    assert.equal(first, second);
    assert.equal(clickListeners, 0);
    assert.doesNotThrow(() => first.cancelAll());
});


test("result replacement paths cancel playback and active unified photo work", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    assert.match(source, /function cancelLivePhotoActivity\(\)/);
    assert.match(source, /previewApi\.cancelActive\(\)/);
    assert.match(source, /activePhotoController\.abort\(\)/);
    assert.equal((source.match(/cancelLivePhotoActivity\(\);/g) || []).length >= 5, true);
});


test("pair stage failures use distinct safe product messages", () => {
    assert.equal(
        pair.safeErrorMessage({errorCode: "BROWSER_LIVEPHOTO_STILL_FETCH_FAILED"}),
        "原图获取失败。",
    );
    assert.equal(
        pair.safeErrorMessage({errorCode: "BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED"}),
        "实况视频获取失败。",
    );
});
