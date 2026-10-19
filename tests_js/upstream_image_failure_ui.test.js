"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const photo = require("../static/js/browser_photo_archive.js");

function homepage() {
    const timers = [];
    const window = {location: {origin: "https://site.test"},
        setTimeout(callback, delay) { timers.push({callback, delay}); }};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/js/xhs_download.js"), "utf8"), {
        window, URL, document: {addEventListener() {}},
    });
    return {window, timers};
}

function preview(src, noReferrer, upstream) {
    const children = [];
    const card = {dataset: {sourcePlatform: upstream ? "upstream" : "xiaohongshu"}};
    return {src, dataset: {}, style: {}, children, onerror: () => {},
        closest() { return card; },
        getAttribute() { return noReferrer ? "no-referrer" : null; },
        insertAdjacentElement(position, node) { assert.equal(position, "afterend"); children.push(node); },
        ownerDocument: {createElement() { return {style: {}, setAttribute(name, value) { this[name] = value; }}; }},
    };
}

test("homepage external and no-referrer preview failures never alter source parameters or retry", () => {
    for (const [url, noReferrer, upstream] of [
        ["https://media.example/image.jpg?signature=a%2Bb&s=abc", true, true],
        ["https://media.example/image.jpg?signature=a%2Bb", false, false],
        ["https://site.test/image.jpg?shareKey=a%2Bb", true, false],
        ["https://site.test/image.jpg?token=exact", false, true],
    ]) {
        const {window, timers} = homepage();
        const img = preview(url, noReferrer, upstream);
        window.retryImage(img);
        window.retryImage(img);
        assert.equal(img.src, url);
        assert.equal(timers.length, 0);
        assert.equal(img.onerror, null);
        assert.equal(img.style.display, "none");
        assert.equal(img.children.length, 1);
        assert.equal(img.children[0].role, "status");
        assert.equal(img.children[0].textContent, "预览加载失败");
        assert.equal(img.children[0].style.height, "220px");
        assert.equal(img.children[0].style.boxSizing, "border-box");
        assert.equal(img.children[0].style.padding, "48px 16px 24px");
        assert.equal(img.children[0].style.placeItems, "center");
        assert.doesNotMatch(img.children[0].textContent, /CORS|过期|已保存/);
    }
});

test("homepage mature same-origin proxy previews keep the existing retry path", () => {
    const {window, timers} = homepage();
    const img = preview("https://site.test/proxy?u=old&token=kept", false, false);
    window.retryImage(img);
    assert.equal(timers.length, 1);
    timers[0].callback();
    const url = new URL(img.src);
    assert.equal(url.pathname, "/proxy");
    assert.equal(url.searchParams.get("token"), "kept");
    assert.ok(url.searchParams.has("_retry"));
    assert.equal(img.children.length, 0);
});

test("actual upstream fetch TypeError produces generic failure without saving or relay", async () => {
    let fetches = 0;
    let saves = 0;
    const item = {type: "image", asset_id: "upstream:job:image:1", source_platform: "upstream",
        provider: "parsehub_generic", browser_upstream_bound: true, browser_direct_fetch_safe: true,
        url: "https://media.example/photo.jpg?signature=private"};
    await assert.rejects(photo.downloadImage(item, {
        fetchImpl: async (url, options) => {
            fetches += 1;
            assert.equal(url, item.url);
            assert.equal(options.redirect, "error");
            assert.equal(options.credentials, "omit");
            throw new TypeError("Failed to fetch https://private.example");
        },
        handoff: async () => { saves += 1; },
    }), (error) => {
        assert.equal(error.errorCode, "UPSTREAM_PHOTO_FETCH_FAILED");
        const message = photo.safeErrorMessage(error);
        assert.match(message, /解析已成功.*浏览器无法读取.*未保存/);
        assert.match(message, /网络或来源访问限制/);
        assert.doesNotMatch(message, /CORS|过期|private\.example|signature/);
        return true;
    });
    assert.equal(fetches, 1);
    assert.equal(saves, 0);
});

test("existing Batch copy and open actions do not download or alter parse/save counters", async () => {
    let click;
    const copied = [];
    const counters = {parsed: 1, saved: 0, status: "success"};
    const forbid = () => { throw new Error("link actions must not fetch, save or relay"); };
    const batchPage = {
        dataset: {}, querySelector() { return null; },
        addEventListener(name, callback) { if (name === "click") click = callback; },
    };
    const window = {
        location: {origin: "https://site.test"}, addEventListener() {}, setTimeout() {},
        AnimeImagesBrowserPhoto: {downloadImage: forbid, createArchive: forbid},
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/js/batch_parse.js"), "utf8"), {
        window, URL, fetch: forbid,
        navigator: {clipboard: {async writeText(value) { copied.push(value); }}},
        document: {readyState: "complete", querySelector() { return batchPage; }, addEventListener() {}},
    });
    const url = "https://media.example/image.jpg?signature=unchanged";
    const copy = {textContent: "复制直链", getAttribute() { return url; }};
    await click({target: {closest(selector) { return selector === "[data-copy-text]" ? copy : null; }}});
    assert.deepEqual(copied, [url]);
    assert.equal(copy.textContent, "已复制");
    // An ordinary anchor click is left to browser navigation, not a download handoff.
    await click({target: {closest() { return null; }}, preventDefault: forbid});
    assert.deepEqual(counters, {parsed: 1, saved: 0, status: "success"});
});

test("upstream all-failed ZIP retains first safe HTTP versus generic fetch category", async () => {
    for (const httpFailure of [false, true]) {
        const items = [1, 2].map((id) => ({type: "image", asset_id: `upstream:job:image:${id}`,
            source_platform: "upstream", provider: "parsehub_generic", browser_upstream_bound: true,
            url: `https://media.example/${id}.jpg`}));
        let saved = 0;
        await assert.rejects(photo.createArchive(items, {
            fetchImpl: async () => {
                if (httpFailure) return new Response("denied", {status: 418});
                throw new TypeError("Failed to fetch");
            }, handoff: async () => { saved += 1; },
        }), (error) => {
            assert.equal(error.errorCode, "BROWSER_PHOTO_NO_SUCCESS");
            assert.equal(error.firstFailureCode, httpFailure ? "BROWSER_PHOTO_HTTP_FAILED" : "UPSTREAM_PHOTO_FETCH_FAILED");
            const message = photo.safeErrorMessage(error, {upstream: true});
            assert.match(message, /解析已成功.*未保存 ZIP/);
            assert.doesNotMatch(message, /CORS|过期/);
            assert.equal(message.includes("HTTP"), httpFailure);
            return true;
        });
        assert.equal(saved, 0);
    }
});

test("homepage open uses fresh exact binding and never exposes a rejected route", async () => {
    for (const rejected of [false, true]) {
        const navigated = [];
        let closed = false;
        const popup = {document: {createElement() { return {}; }, head: {appendChild() {}}},
            location: {replace(url) { navigated.push(url); }}, close() { closed = true; }};
        const status = {};
        const card = {dataset: {type: "image", assetId: "upstream:job:image:1", sourcePlatform: "upstream",
            provider: "parsehub_generic", url: "https://media.example/image.jpg?signature=exact"},
            querySelector() { return status; }};
        const button = {closest() { return card; }};
        const window = {open() { return popup; }, AnimeImagesBrowserPhoto: photo};
        const source = fs.readFileSync(path.join(__dirname, "../static/js/xhs_download.js"), "utf8")
            .replace("window.retryImage = retryImage;", "window.retryImage = retryImage; window.testOpenImage = openUpstreamImage;");
        vm.runInNewContext(source, {window, URL, document: {addEventListener() {}}});
        await window.testOpenImage(button, async (payload) => {
            assert.equal(payload.intent, "resource_link");
            assert.equal(navigated.length, 0);
            assert.equal(payload.media_items.length, 1);
            const item = payload.media_items[0];
            return {route_type: "direct", items: [{...item, browser_upstream_bound: true,
                url: rejected ? "https://media.example/other.jpg" : item.url}]};
        });
        assert.equal(popup.opener, null);
        assert.equal(closed, rejected);
        assert.deepEqual(navigated, rejected ? [] : [card.dataset.url]);
        assert.equal(button.disabled, false);
        assert.match(status.textContent, rejected ? /授权不可用或已失效/ : /不代表已保存/);
    }
});

test("Batch upstream open and copy require fresh link-only exact binding with no DOM fallback", async () => {
    for (const action of ["open", "copy"]) {
        for (const failure of ["", "expired", "substituted"]) {
            const copied = [];
            const navigated = [];
            let calls = 0;
            let closed = false;
            const itemNode = {dataset: {type: "image", assetId: "upstream:job:image:1",
                sourcePlatform: "upstream", provider: "parsehub_generic", url: "https://media.example/original.jpg"}};
            const popup = {document: {createElement() { return {}; }, head: {appendChild() {}}},
                location: {replace(url) { navigated.push(url); }}, close() { closed = true; }};
            const window = {AnimeImagesBrowserPhoto: photo, location: {origin: "https://site.test"},
                open() { return popup; }, setTimeout() {}};
            const context = {window, URL, module: {exports: {}},
                navigator: {clipboard: {async writeText(url) { copied.push(url); }}},
                fetch: async (_url, options) => {
                    calls += 1;
                    const payload = JSON.parse(options.body);
                    assert.equal(payload.intent, "resource_link");
                    assert.equal(payload.route_only, true);
                    assert.deepEqual(copied, []);
                    assert.deepEqual(navigated, []);
                    return {ok: failure !== "expired", async json() {
                        return {route_type: "direct", items: [{...payload.media_items[0], browser_upstream_bound: true,
                            ...(failure === "substituted" ? {url: "https://media.example/other.jpg"} : {})}]};
                    }};
                }};
            const source = fs.readFileSync(path.join(__dirname, "../static/js/batch_parse.js"), "utf8")
                .replace("const testApi = Object.freeze({", "const testApi = Object.freeze({handleBatchUpstreamLink,");
            vm.runInNewContext(source, context);
            const button = {textContent: action, getAttribute() { return action; }};
            const page = {dataset: {batchDownloadUrl: "/authorize"}, querySelector() { return {value: "csrf"}; }};
            await context.module.exports.handleBatchUpstreamLink(page, button, itemNode);
            assert.equal(calls, 1);
            assert.deepEqual(copied, !failure && action === "copy" ? [itemNode.dataset.url] : []);
            assert.deepEqual(navigated, !failure && action === "open" ? [itemNode.dataset.url] : []);
            assert.equal(closed, Boolean(failure && action === "open"));
            assert.equal(button.disabled, false);
        }
    }
});

test("link validator rejects stale, substituted, unsafe and unbound responses", () => {
    const item = {type: "image", asset_id: "upstream:job:image:1", url: "https://media.example/1.jpg"};
    const bound = {...item, source_platform: "upstream", provider: "parsehub_generic", browser_upstream_bound: true};
    for (const route of [null, {route_type: "proxy", items: [bound]}, {route_type: "direct", items: []},
        ...[{browser_upstream_bound: false}, {asset_id: "upstream:other:image:1"}, {type: "video"},
            {source_platform: "other"}, {url: "javascript:alert(1)"}].map((change) => ({
            route_type: "direct", items: [{...bound, ...change}],
        }))]) {
        assert.throws(() => photo.validatedUpstreamImageUrl(item, route), {errorCode: "UPSTREAM_PHOTO_INVALID_ITEM"});
    }
});

test("homepage failed image download clears busy progress and reenables button without parse failure", async () => {
    const nodes = new Map();
    function node(selector) {
        if (!nodes.has(selector)) nodes.set(selector, {dataset: {}, style: {}, isConnected: true,
            innerHTML: "下载", innerText: "", hidden: false,
            classList: {add() {}, remove() {}, toggle() {}},
            setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
            querySelector: node});
        return nodes.get(selector);
    }
    const item = {type: "image", logical_type: "image", asset_id: "upstream:job:image:1",
        source_platform: "upstream", provider: "parsehub_generic", url: "https://media.example/1.jpg"};
    const region = {querySelector: node, querySelectorAll() { return []; }};
    const page = {dataset: {downloadUrl: "/authorize", baseFilename: "parsed"}};
    let ready;
    const window = {localStorage: {getItem() { return "{}"; }},
        AnimeImagesBrowserPhoto: {...photo, async downloadImage(_item, options) {
            options.onProgress({stage: "connecting", totalItems: 1, completedItems: 0,
                totalKnown: false, receivedBytes: 0, bytesPerSecond: 0});
            assert.equal(node("#download-progress-wrap").hidden, false);
            assert.equal(node("#download-btn").disabled, true);
            throw Object.assign(new TypeError("Failed to fetch"), {errorCode: "UPSTREAM_PHOTO_FETCH_FAILED"});
        }},
    };
    const source = fs.readFileSync(path.join(__dirname, "../static/js/xhs_download.js"), "utf8")
        .replace("let livePhotoScriptPromise = null;", `let livePhotoScriptPromise = null;
            window.testDownload = async (items) => { selected = items; await handleDownloadClick(getDownloadButton(), true); };
            return;`);
    vm.runInNewContext(source, {window, URL, AbortController,
        document: {cookie: "", querySelector(selector) { return selector === ".xhs-download-page" ? page : null; },
            getElementById(id) { return id === "parse-results-region" ? region : null; },
            addEventListener(_name, callback) { ready = callback; }},
        fetch: async () => ({ok: true, async json() { return {route_type: "direct", items: [{...item, browser_upstream_bound: true}]}; }}),
    });
    ready();
    await window.testDownload([item]);
    assert.equal(node("#download-progress-wrap").hidden, true);
    assert.equal(node("[data-blob-download-warning]").hidden, true);
    assert.equal(node("[data-blob-download-warning]").innerText, "");
    assert.equal(node("#download-progress-speed").innerText, "");
    assert.equal(node("#download-progress-percent").innerText, "");
    assert.equal(node("[data-photo-download-cancel]").hidden, true);
    assert.equal(node("[data-photo-server-fallback]").hidden, true);
    assert.equal(node("#download-btn").disabled, false);
    assert.equal(node("#download-btn")["aria-busy"], "false");
    assert.match(node("#status").innerText, /解析已成功.*未保存/);
    assert.equal(page.dataset.baseFilename, "parsed");
});
