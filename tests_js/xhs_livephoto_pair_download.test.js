"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const pair = require("../static/js/xhs_livephoto_pair_download.js");


function fakeClassList() {
    const values = new Set();
    return {
        toggle(value, enabled) {
            if (enabled) values.add(value);
            else values.delete(value);
        },
        contains(value) { return values.has(value); },
    };
}


function fakeCard() {
    const status = {textContent: "", classList: fakeClassList()};
    const buttons = [{disabled: false}, {disabled: false}];
    return {
        status,
        buttons,
        querySelector(selector) {
            return selector === "[data-livephoto-download-status]" ? status : null;
        },
        querySelectorAll() { return buttons; },
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
                return {
                    ok: true,
                    async json() {
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
                    },
                };
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


test("pair download resolves one descriptor then invokes one browser ZIP handoff", async () => {
    const card = fakeCard();
    const descriptor = {
        archive_filename: "fixture_livephoto.zip",
        still: {media_type: "image", url: "https://still.invalid/item"},
        motion: {media_type: "video", format_class: "mp4", url: "https://motion.invalid/item.mp4"},
    };
    let descriptorCalls = 0;
    let archiveCalls = 0;
    const controller = pair.createController({
        addEventListener() {},
        removeEventListener() {},
        querySelector() { return null; },
    }, {
        async requestPairDescriptor(token, endpoint) {
            descriptorCalls += 1;
            assert.equal(token, "opaque-ticket");
            assert.equal(endpoint, "/media/xhs-livephoto-pair-descriptor/");
            return descriptor;
        },
        photoApi: {
            async createLivePhotoPairArchive(received, options) {
                archiveCalls += 1;
                assert.equal(received, descriptor);
                for (const stage of ["fetching", "fetching_motion", "assembling", "completed"]) {
                    options.onProgress({stage});
                }
            },
        },
    });
    const button = {
        dataset: {
            livephotoPairToken: "opaque-ticket",
            livephotoDescriptorUrl: "/media/xhs-livephoto-pair-descriptor/",
        },
    };

    await controller.downloadPair(button, card);
    assert.equal(descriptorCalls, 1);
    assert.equal(archiveCalls, 1);
    assert.equal(card.status.textContent, "已交给浏览器保存");
    controller.destroy();
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


test("busy state preserves a permanently disabled pair action", () => {
    const card = fakeCard();
    card.buttons[1].disabled = true;

    pair.setCardBusy(card, true);
    assert.deepEqual(card.buttons.map((button) => button.disabled), [true, true]);

    pair.setCardBusy(card, false);
    assert.deepEqual(card.buttons.map((button) => button.disabled), [false, true]);
});


test("result replacement cancellation aborts an active pair operation", async () => {
    let clickHandler = null;
    let archiveSignal = null;
    let handoffCalls = 0;
    const status = {textContent: "", classList: fakeClassList()};
    let card;
    const button = {
        disabled: false,
        dataset: {
            livephotoPairToken: "opaque-ticket",
            livephotoDescriptorUrl: "/media/xhs-livephoto-pair-descriptor/",
        },
        hasAttribute(name) { return name === "data-livephoto-download-pair"; },
        closest(selector) { return selector === "[data-livephoto-item]" ? card : null; },
    };
    card = {
        querySelector(selector) {
            return selector === "[data-livephoto-download-status]" ? status : null;
        },
        querySelectorAll() { return [button]; },
    };
    const controller = pair.createController({
        addEventListener(name, handler) {
            if (name === "click") clickHandler = handler;
        },
        removeEventListener() {},
        querySelector() { return null; },
    }, {
        async requestPairDescriptor() {
            return {
                archive_filename: "fixture_livephoto.zip",
                still: {media_type: "image", url: "https://still.invalid/item"},
                motion: {
                    media_type: "video",
                    format_class: "mp4",
                    url: "https://motion.invalid/item.mp4",
                },
            };
        },
        photoApi: {
            async createLivePhotoPairArchive(_descriptor, options) {
                archiveSignal = options.signal;
                await new Promise((resolve, reject) => {
                    options.signal.addEventListener("abort", () => {
                        reject(Object.assign(new Error(), {errorCode: "BROWSER_PHOTO_CANCELLED"}));
                    }, {once: true});
                });
                handoffCalls += 1;
            },
            safeErrorMessage() { return "已取消"; },
        },
    });

    clickHandler({
        target: {closest() { return button; }},
        preventDefault() {},
    });
    await Promise.resolve();
    await Promise.resolve();
    controller.cancelAll();
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(archiveSignal.aborted, true);
    assert.equal(handoffCalls, 0);
    controller.destroy();
});


test("result replacement paths cancel playback and pair downloads before DOM replacement", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    assert.match(source, /function cancelLivePhotoActivity\(\)/);
    assert.match(source, /previewApi\.cancelActive\(\)/);
    assert.match(source, /pairApi\.cancelAll\(\)/);
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


test("original download maps generic transport failures to the still stage", async () => {
    const card = fakeCard();
    const controller = pair.createController({
        addEventListener() {},
        removeEventListener() {},
        querySelector() { return null; },
    }, {
        photoApi: {
            async downloadImage() {
                throw Object.assign(new Error(), {
                    errorCode: "BROWSER_PHOTO_CORS_UNAVAILABLE",
                });
            },
        },
    });
    const button = {
        dataset: {
            originalUrl: "https://still.invalid/item",
            originalFilename: "still.jpg",
        },
    };

    await assert.rejects(
        controller.downloadOriginal(button, card),
        (error) => error.errorCode === "BROWSER_LIVEPHOTO_STILL_FETCH_FAILED",
    );
    controller.destroy();
});
