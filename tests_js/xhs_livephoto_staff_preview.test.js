"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const preview = require("../static/js/xhs_livephoto_staff_preview.js");


function fakeClassList() {
    const values = new Set();
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        contains(value) { return values.has(value); },
    };
}


function fakeClock() {
    let nextId = 1;
    const tasks = new Map();
    return {
        setTimeout(callback, delay) {
            const id = nextId;
            nextId += 1;
            tasks.set(id, {callback, delay});
            return id;
        },
        clearTimeout(id) {
            tasks.delete(id);
        },
        fireAll() {
            const pending = [...tasks.entries()];
            tasks.clear();
            for (const [_id, task] of pending) {
                task.callback();
            }
        },
        pendingCount() {
            return tasks.size;
        },
    };
}


function fakeVideo(options) {
    const settings = options || {};
    const listeners = new Map();
    const video = {
        hidden: true,
        src: "",
        error: null,
        readyState: 0,
        networkState: 0,
        pauseCount: 0,
        loadCount: 0,
        pause() { this.pauseCount += 1; },
        load() { this.loadCount += 1; },
        removeAttribute(name) {
            if (name === "src") {
                this.src = "";
            }
        },
        addEventListener(name, callback) {
            if (!listeners.has(name)) {
                listeners.set(name, new Set());
            }
            listeners.get(name).add(callback);
        },
        removeEventListener(name, callback) {
            listeners.get(name)?.delete(callback);
        },
        dispatch(name) {
            for (const callback of [...(listeners.get(name) || [])]) {
                callback({target: this, type: name});
            }
        },
        listenerCount() {
            return [...listeners.values()].reduce((total, values) => total + values.size, 0);
        },
        play() {
            if (typeof settings.play === "function") {
                return settings.play();
            }
            if (settings.playResult === false) {
                const error = new Error("private failure");
                error.name = settings.playErrorName || "NotSupportedError";
                return Promise.reject(error);
            }
            return Promise.resolve();
        },
    };
    return video;
}


function fakeCard(url, options) {
    const still = {hidden: false};
    const error = {hidden: true};
    const errorText = {textContent: ""};
    const label = {textContent: "播放实况"};
    const status = {textContent: "实况照片"};
    const spinner = {hidden: true};
    const playIcon = {hidden: false};
    const attributes = {};
    const video = fakeVideo(options);
    let card;
    const button = {
        dataset: {motionUrl: url},
        setAttribute(name, value) { attributes[name] = value; },
        closest(selector) {
            return selector === "[data-livephoto-card]" ? card : null;
        },
    };
    card = {
        dataset: {livephotoState: "idle"},
        classList: fakeClassList(),
        querySelector(selector) {
            return {
                "[data-livephoto-still]": still,
                "[data-livephoto-motion]": video,
                "[data-livephoto-toggle]": button,
                "[data-livephoto-toggle-label]": label,
                "[data-livephoto-play-icon]": playIcon,
                "[data-livephoto-spinner]": spinner,
                "[data-livephoto-status]": status,
                "[data-livephoto-error]": error,
                "[data-livephoto-error-text]": errorText,
            }[selector] || null;
        },
    };
    return {
        attributes,
        button,
        card,
        error,
        errorText,
        label,
        playIcon,
        spinner,
        status,
        still,
        video,
    };
}


function fakeDocument() {
    const listeners = new Map();
    return {
        listeners,
        addEventListener(name, callback) { listeners.set(name, callback); },
        removeEventListener(name, callback) {
            if (listeners.get(name) === callback) {
                listeners.delete(name);
            }
        },
    };
}


function clickEvent(button) {
    return {
        target: {
            closest(selector) {
                return selector === "[data-livephoto-toggle]" ? button : null;
            },
        },
        preventDefault() {},
    };
}


function createHarness(options) {
    const documentObject = fakeDocument();
    const clock = fakeClock();
    const controller = preview.createController(documentObject, {
        setTimeout: clock.setTimeout,
        clearTimeout: clock.clearTimeout,
        startupTimeoutMs: 8000,
        ...(options || {}),
    });
    return {clock, controller, documentObject};
}


async function settle() {
    await Promise.resolve();
    await Promise.resolve();
}


test("motion URL admission accepts only verified HTTPS XHS host classes", () => {
    assert.equal(
        preview.isSafeMotionUrl("https://sns-video-v7.xhscdn.com/path/video.mp4?x=1"),
        true,
    );
    assert.equal(
        preview.isSafeMotionUrl("https://sns-bak-v3.xhscdn.com/path/video.mp4"),
        true,
    );
    for (const rejected of [
        "https://sns-video-zl.xhscdn.com/path/video.mp4",
        "https://lookalike.example/path/video.mp4",
        "http://sns-video-v7.xhscdn.com/path/video.mp4",
        "https://user@sns-video-v7.xhscdn.com/path/video.mp4",
        "https://sns-video-v7.xhscdn.com:444/path/video.mp4",
    ]) {
        assert.equal(preview.isSafeMotionUrl(rejected), false);
    }
});


test("click starts loading, assigns the source, and calls load before playback", async () => {
    const harness = createHarness();
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");

    assert.equal(item.video.src, "");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    assert.equal(item.card.dataset.livephotoState, "loading");
    assert.equal(item.video.src, item.button.dataset.motionUrl);
    assert.equal(item.video.loadCount, 1);
    assert.equal(item.video.hidden, true);
    assert.equal(item.still.hidden, false);
    assert.equal(item.spinner.hidden, false);
    assert.equal(item.playIcon.hidden, true);
    assert.equal(harness.clock.pendingCount(), 1);
    harness.controller.destroy();
});


test("metadata and canplay keep the still visible until playing", async () => {
    const harness = createHarness();
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    item.video.dispatch("loadedmetadata");
    assert.equal(item.card.dataset.livephotoState, "metadata");
    assert.equal(item.still.hidden, false);
    item.video.dispatch("canplay");
    assert.equal(item.card.dataset.livephotoState, "canplay");
    assert.equal(item.still.hidden, false);
    item.video.dispatch("playing");

    assert.equal(item.card.dataset.livephotoState, "playing");
    assert.equal(item.video.hidden, false);
    assert.equal(item.still.hidden, true);
    assert.equal(item.label.textContent, "停止实况");
    assert.equal(item.attributes["aria-pressed"], "true");
    assert.equal(item.card.classList.contains("is-active"), true);
    assert.equal(harness.clock.pendingCount(), 0);
    harness.controller.destroy();
});


test("a second click stops and unloads the active card", async () => {
    const harness = createHarness();
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();
    item.video.dispatch("playing");

    harness.documentObject.listeners.get("click")(clickEvent(item.button));

    assert.equal(item.card.dataset.livephotoState, "stopped");
    assert.equal(item.video.src, "");
    assert.equal(item.video.hidden, true);
    assert.equal(item.still.hidden, false);
    assert.equal(item.label.textContent, "播放实况");
    assert.equal(item.video.listenerCount(), 0);
    assert.equal(harness.clock.pendingCount(), 0);
    harness.controller.destroy();
});


test("startup timeout restores the still and exposes a safe retry state", async () => {
    const harness = createHarness();
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/timeout.mp4?private=fixture");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    harness.clock.fireAll();

    assert.equal(item.card.dataset.livephotoState, "startup_timeout");
    assert.equal(item.video.src, "");
    assert.equal(item.still.hidden, false);
    assert.equal(item.error.hidden, false);
    assert.equal(item.label.textContent, "重试播放");
    assert.equal(
        item.errorText.textContent,
        preview.SAFE_ERROR_TEXT_BY_STATE.startup_timeout,
    );
    assert.doesNotMatch(item.errorText.textContent, /https?:|private|xhscdn|sig=/i);
    assert.equal(item.video.listenerCount(), 0);
    assert.equal(harness.clock.pendingCount(), 0);
    harness.controller.destroy();
});


test("MediaError codes map to fixed safe states", async (t) => {
    for (const [code, state] of [
        [2, "network_error"],
        [3, "decode_error"],
        [4, "source_not_supported"],
    ]) {
        await t.test(String(code), async () => {
            const harness = createHarness();
            const item = fakeCard(`https://sns-video-v7.xhscdn.com/path/error-${code}.mp4`);
            harness.documentObject.listeners.get("click")(clickEvent(item.button));
            await settle();
            item.video.error = {code};
            item.video.dispatch("error");

            assert.equal(item.card.dataset.livephotoState, state);
            assert.equal(item.error.hidden, false);
            assert.equal(item.errorText.textContent, preview.SAFE_ERROR_TEXT_BY_STATE[state]);
            assert.doesNotMatch(item.errorText.textContent, /https?:|xhscdn|query|token/i);
            harness.controller.destroy();
        });
    }
});


test("stalled playback remains bounded by the startup timer", async () => {
    const harness = createHarness();
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/stalled.mp4");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();
    item.video.dispatch("stalled");
    assert.equal(item.card.dataset.livephotoState, "loading");

    harness.clock.fireAll();
    assert.equal(item.card.dataset.livephotoState, "startup_timeout");
    assert.equal(item.video.listenerCount(), 0);
    harness.controller.destroy();
});


test("starting a second card pauses, unloads, and detaches the first card", async () => {
    const harness = createHarness();
    const first = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");
    const second = fakeCard("https://sns-bak-v3.xhscdn.com/path/two.mp4");

    harness.documentObject.listeners.get("click")(clickEvent(first.button));
    await settle();
    harness.documentObject.listeners.get("click")(clickEvent(second.button));
    await settle();

    assert.equal(first.video.src, "");
    assert.equal(first.still.hidden, false);
    assert.equal(first.video.listenerCount(), 0);
    assert.ok(first.video.pauseCount >= 1);
    assert.equal(second.video.src, second.button.dataset.motionUrl);
    assert.equal(second.card.dataset.livephotoState, "loading");
    harness.controller.destroy();
});


test("rapid A B C switching allows only C to become active", async () => {
    const harness = createHarness();
    const first = fakeCard("https://sns-video-v7.xhscdn.com/path/a.mp4");
    const second = fakeCard("https://sns-video-v7.xhscdn.com/path/b.mp4");
    const third = fakeCard("https://sns-video-v7.xhscdn.com/path/c.mp4");

    harness.documentObject.listeners.get("click")(clickEvent(first.button));
    harness.documentObject.listeners.get("click")(clickEvent(second.button));
    harness.documentObject.listeners.get("click")(clickEvent(third.button));
    await settle();
    first.video.dispatch("loadedmetadata");
    second.video.dispatch("playing");
    third.video.dispatch("playing");

    assert.notEqual(first.card.dataset.livephotoState, "playing");
    assert.notEqual(second.card.dataset.livephotoState, "playing");
    assert.equal(third.card.dataset.livephotoState, "playing");
    assert.equal(third.still.hidden, true);
    assert.equal(first.video.listenerCount(), 0);
    assert.equal(second.video.listenerCount(), 0);
    harness.controller.destroy();
});


test("a delayed old play promise cannot reactivate a previous card", async () => {
    let resolveFirstPlay;
    const harness = createHarness();
    const first = fakeCard(
        "https://sns-video-v7.xhscdn.com/path/one.mp4",
        {play: () => new Promise((resolve) => { resolveFirstPlay = resolve; })},
    );
    const second = fakeCard("https://sns-bak-v3.xhscdn.com/path/two.mp4");

    harness.documentObject.listeners.get("click")(clickEvent(first.button));
    harness.documentObject.listeners.get("click")(clickEvent(second.button));
    await settle();
    resolveFirstPlay();
    await settle();
    first.video.dispatch("playing");
    second.video.dispatch("playing");

    assert.equal(first.video.src, "");
    assert.equal(first.still.hidden, false);
    assert.equal(first.card.classList.contains("is-active"), false);
    assert.equal(second.card.dataset.livephotoState, "playing");
    harness.controller.destroy();
});


test("playback rejection restores the still and never exposes private failure text", async () => {
    const harness = createHarness();
    const item = fakeCard(
        "https://sns-video-v7.xhscdn.com/path/failure.mp4?private=fixture",
        {playResult: false, playErrorName: "NotSupportedError"},
    );

    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    assert.equal(item.card.dataset.livephotoState, "source_not_supported");
    assert.equal(item.video.src, "");
    assert.equal(item.still.hidden, false);
    assert.equal(item.error.hidden, false);
    assert.equal(
        item.errorText.textContent,
        preview.SAFE_ERROR_TEXT_BY_STATE.source_not_supported,
    );
    assert.doesNotMatch(item.errorText.textContent, /https?:|private|xhscdn/i);
    harness.controller.destroy();
});


test("client policy rejection never assigns a motion source", async () => {
    const harness = createHarness();
    const item = fakeCard("https://unverified.example/path/video.mp4?token=private");
    harness.documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    assert.equal(item.card.dataset.livephotoState, "policy_rejected");
    assert.equal(item.video.src, "");
    assert.equal(item.video.loadCount, 1);
    assert.equal(item.error.hidden, false);
    assert.doesNotMatch(item.errorText.textContent, /unverified|token|https?:/i);
    harness.controller.destroy();
});


test("safe state vocabulary is fixed and contains no transport detail", () => {
    assert.deepEqual(
        [...preview.SAFE_STATES].sort(),
        [
            "aborted",
            "canplay",
            "decode_error",
            "idle",
            "loading",
            "metadata",
            "network_error",
            "playing",
            "policy_rejected",
            "source_not_supported",
            "startup_timeout",
            "stopped",
        ],
    );
    assert.equal(preview.STARTUP_TIMEOUT_MS, 8000);
});
