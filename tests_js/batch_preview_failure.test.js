"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page(previousRetryImage, browserPhotoApi, fetch) {
    const timers = [];
    const window = {
        location: {origin: "https://site.test"},
        retryImage: previousRetryImage,
        AnimeImagesBrowserPhoto: browserPhotoApi,
        setTimeout(callback, delay) { timers.push({callback, delay}); },
    };
    const context = vm.createContext({window, URL, WeakMap, AbortController, fetch, module: {exports: {}}});
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/js/batch_parse.js"), "utf8"), context);
    return {window, timers, context};
}

function image(src, upstream = false, kind = "photo") {
    const children = [];
    const container = {kind, appendChild(node) { children.push(node); }};
    return {
        src, dataset: {}, style: {}, hidden: false, isConnected: true, onerror: "retryImage(this)",
        children,
        closest(selector) {
            if (selector === "[data-batch-photo-item]") return null;
            assert.equal(selector, ".batch-photo-preview, .batch-result-cover");
            return container;
        },
        getAttribute(name) { return name === "referrerpolicy" && upstream ? "no-referrer" : null; },
        ownerDocument: {
            createElement(tag) {
                assert.equal(tag, "span");
                return {setAttribute(name, value) { this[name] = value; }};
            },
        },
    };
}

test("inline image and cover errors show one stable failure without direct URL rewrites", () => {
    for (const kind of ["photo", "cover"]) {
        const {window, timers, context} = page();
        const img = image("https://media.example/image.jpg?signature=abc", true, kind);
        context.img = img;
        // Inline handlers resolve properties of window as globals in the browser.
        context.retryImage = window.retryImage;
        vm.runInContext("retryImage(img)", context);
        window.retryImage(img);
        assert.equal(img.src, "https://media.example/image.jpg?signature=abc");
        assert.equal(timers.length, 0);
        assert.equal(img.hidden, true);
        assert.equal(img.style.display, "none");
        assert.equal(img.onerror, null);
        assert.equal(img.children.length, 1);
        assert.equal(img.children[0].textContent, "预览加载失败");
        assert.equal(img.children[0].role, "status");
    }
});

test("upstream archive unreadable failure keeps safe messaging and never offers server fallback", async () => {
    const photoApi = require("../static/js/browser_photo_archive.js");
    for (const [upstream, rawAllowed] of [[true, false], [true, true], [false, false]]) {
        const error = Object.assign(new Error("private signed URL"), {
            errorCode: upstream ? "UPSTREAM_PHOTO_FETCH_FAILED" : "BROWSER_PHOTO_CORS_UNAVAILABLE",
        });
        let archiveCalls = 0;
        let authorizationCalls = 0;
        const api = {
            safeErrorCode: photoApi.safeErrorCode,
            safeErrorMessage: photoApi.safeErrorMessage,
            async createArchive() { archiveCalls += 1; throw error; },
        };
        const {context} = page(null, api, async (_url, options) => {
            authorizationCalls += 1;
            const payload = JSON.parse(options.body);
            assert.equal(payload.route_only, true);
            return {ok: true, async json() {
                return {route_type: "direct", items: payload.media_items.map((item) => ({
                    ...item, browser_upstream_bound: true,
                }))};
            }};
        });
        const status = {classList: {toggle() {}}};
        const fallback = {hidden: false};
        const section = {
            dataset: {baseFilename: "synthetic"},
            querySelectorAll() {
                return [1, 2].map((id) => ({dataset: {
                    type: "image", url: `https://media.example/${id}.jpg`,
                    assetId: upstream ? `upstream:test:${id}` : `legacy:${id}`,
                    sourcePlatform: upstream ? "upstream" : "xiaohongshu",
                }}));
            },
            querySelector(selector) {
                if (selector === "[data-batch-photo-archive-status]") return status;
                if (selector === "[data-batch-photo-server-fallback]") return fallback;
                if (selector === "[data-batch-image-link-action]" && rawAllowed) return {};
                if (selector === '[data-asset-id^="upstream:"]' && upstream) return {};
                return null;
            },
        };
        const batchPage = {dataset: {batchDownloadUrl: "/authorize"}, querySelector() { return {value: "csrf"}; }};
        await context.module.exports.downloadBatchPhotoArchive(batchPage, {closest() { return section; }});
        assert.equal(archiveCalls, 1);
        assert.equal(authorizationCalls, upstream ? 1 : 0);
        assert.equal(fallback.hidden, true);
        const hint = rawAllowed ? " 可使用现有的“打开直链”或“复制直链”；打开或复制不代表已保存。" : "";
        assert.equal(status.textContent, upstream
            ? photoApi.safeErrorMessage(error) + hint : "原始图片资源已失效，请重新解析后再试。");
        if (upstream) {
            assert.match(status.textContent, /解析已成功.*浏览器无法读取/);
            assert.doesNotMatch(status.textContent, /CORS|已失效|private signed URL/);
        }
    }
});

test("legacy same-origin flat proxy previews retain three bounded retries", () => {
    const {window, timers} = page();
    const img = image("https://site.test/image-proxy?u=legacy&token=kept");
    for (let retry = 1; retry <= 3; retry += 1) {
        window.retryImage(img);
        window.retryImage(img);
        assert.equal(timers.length, 1);
        const timer = timers.shift();
        assert.equal(timer.delay, retry * 1000);
        timer.callback();
        const url = new URL(img.src);
        assert.equal(url.pathname, "/image-proxy");
        assert.equal(url.searchParams.get("u"), "legacy");
        assert.equal(url.searchParams.get("token"), "kept");
        assert.ok(url.searchParams.has("_retry"));
    }
    window.retryImage(img);
    assert.equal(timers.length, 0);
    assert.equal(img.children.length, 1);
    assert.equal(img.hidden, true);
});

test("same-origin upstream previews still never enter legacy retry", () => {
    const {window, timers} = page();
    const img = image("https://site.test/signed-image?signature=abc", true);
    window.retryImage(img);
    assert.equal(timers.length, 0);
    assert.equal(img.src, "https://site.test/signed-image?signature=abc");
});

test("non-batch handlers are preserved and detached retry timers do nothing", () => {
    let delegated = 0;
    const {window, timers} = page(() => { delegated += 1; });
    window.retryImage({closest() { return null; }});
    assert.equal(delegated, 1);
    const img = image("https://site.test/image-proxy?u=legacy");
    window.retryImage(img);
    img.isConnected = false;
    timers.shift().callback();
    assert.equal(img.src, "https://site.test/image-proxy?u=legacy");
    assert.equal(img.children.length, 0);
});
