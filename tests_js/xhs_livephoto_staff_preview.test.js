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


function fakeCard(url, playResult) {
    const still = {hidden: false};
    const error = {hidden: true, textContent: ""};
    const label = {textContent: "播放实况"};
    const attributes = {};
    const video = {
        hidden: true,
        src: "",
        pauseCount: 0,
        loadCount: 0,
        pause() { this.pauseCount += 1; },
        load() { this.loadCount += 1; },
        removeAttribute(name) {
            if (name === "src") {
                this.src = "";
            }
        },
        play() {
            return playResult === false
                ? Promise.reject(new Error("private failure"))
                : Promise.resolve();
        },
    };
    const card = {
        classList: fakeClassList(),
        querySelector(selector) {
            return {
                "[data-livephoto-still]": still,
                "[data-livephoto-motion]": video,
                "[data-livephoto-toggle]": button,
                "[data-livephoto-toggle-label]": label,
                "[data-livephoto-error]": error,
            }[selector] || null;
        },
    };
    const button = {
        dataset: {motionUrl: url},
        setAttribute(name, value) { attributes[name] = value; },
        closest(selector) {
            return selector === "[data-livephoto-card]" ? card : null;
        },
    };
    return {attributes, button, card, error, label, still, video};
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


test("click assigns only the selected motion source and a second click restores still", async () => {
    const documentObject = fakeDocument();
    const controller = preview.createController(documentObject);
    const item = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");

    assert.equal(item.video.src, "");
    documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    assert.equal(item.video.src, item.button.dataset.motionUrl);
    assert.equal(item.video.hidden, false);
    assert.equal(item.still.hidden, true);
    assert.equal(item.label.textContent, "停止实况");
    assert.equal(item.card.classList.contains("is-active"), true);

    documentObject.listeners.get("click")(clickEvent(item.button));
    assert.equal(item.video.src, "");
    assert.equal(item.video.hidden, true);
    assert.equal(item.still.hidden, false);
    assert.equal(item.label.textContent, "播放实况");
    controller.destroy();
});


test("starting a second card pauses and unloads the first card", async () => {
    const documentObject = fakeDocument();
    const controller = preview.createController(documentObject);
    const first = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");
    const second = fakeCard("https://sns-bak-v3.xhscdn.com/path/two.mp4");

    documentObject.listeners.get("click")(clickEvent(first.button));
    await settle();
    documentObject.listeners.get("click")(clickEvent(second.button));
    await settle();

    assert.equal(first.video.src, "");
    assert.equal(first.video.hidden, true);
    assert.equal(first.still.hidden, false);
    assert.ok(first.video.pauseCount >= 1);
    assert.equal(second.video.src, second.button.dataset.motionUrl);
    assert.equal(second.still.hidden, true);
    controller.destroy();
});


test("a delayed previous play cannot reactivate its card after switching", async () => {
    const documentObject = fakeDocument();
    const controller = preview.createController(documentObject);
    let resolveFirstPlay;
    const first = fakeCard("https://sns-video-v7.xhscdn.com/path/one.mp4");
    first.video.play = () => new Promise((resolve) => {
        resolveFirstPlay = resolve;
    });
    const second = fakeCard("https://sns-bak-v3.xhscdn.com/path/two.mp4");

    documentObject.listeners.get("click")(clickEvent(first.button));
    documentObject.listeners.get("click")(clickEvent(second.button));
    await settle();
    resolveFirstPlay();
    await settle();

    assert.equal(first.video.src, "");
    assert.equal(first.still.hidden, false);
    assert.equal(first.card.classList.contains("is-active"), false);
    assert.equal(second.video.src, second.button.dataset.motionUrl);
    assert.equal(second.still.hidden, true);
    controller.destroy();
});


test("playback failure restores still and exposes only fixed safe text", async () => {
    const documentObject = fakeDocument();
    const controller = preview.createController(documentObject);
    const item = fakeCard(
        "https://sns-video-v7.xhscdn.com/path/failure.mp4?private=fixture",
        false,
    );

    documentObject.listeners.get("click")(clickEvent(item.button));
    await settle();

    assert.equal(item.video.src, "");
    assert.equal(item.still.hidden, false);
    assert.equal(item.error.hidden, false);
    assert.equal(item.error.textContent, preview.SAFE_ERROR_TEXT);
    assert.doesNotMatch(item.error.textContent, /https?:|private|xhscdn/i);
    controller.destroy();
});
