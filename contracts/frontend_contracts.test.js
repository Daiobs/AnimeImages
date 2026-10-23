"use strict";

// DOM/MessageChannel/navigation doubles are browser boundaries, not a real browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const {Node} = require("../tests_js/helpers/lab_dom.js");

const source = name => fs.readFileSync(path.join(__dirname, "../static/js", name), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
}
const json = payload => ({ok: true, status: 200, json: async () => payload});

class DomNode extends Node {
    get isConnected() { return this.parentNode ? this.parentNode.isConnected : this.tagName === "DOCUMENT"; }
    get innerText() { return this.textContent; }
    set innerText(value) { this.textContent = value; }
    get id() { return this.getAttribute("id") || ""; }
    set id(value) { this.setAttribute("id", value); }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    appendChild(node) { this.append(node); return node; }
    removeAttribute(name) { delete this.attributes[name]; }
    matches(selector) {
        if (selector.startsWith("#")) return this.id === selector.slice(1);
        if (selector.endsWith(":checked")) return this.checked && super.matches(selector.slice(0, -8));
        return super.matches(selector);
    }
}
class BrowserMessageChannel {
    constructor() {
        this.port1 = {onmessage: null};
        this.port2 = {postMessage: data => queueMicrotask(() => this.port1.onmessage?.({data}))};
    }
}
function browser(fetchImpl, userAgent = "contract-boundary", clock = null) {
    const doc = new DomNode("document");
    doc.body = new DomNode("body"); doc.append(doc.body);
    doc.cookie = "csrftoken=contract";
    doc.createElement = tag => new DomNode(tag);
    doc.getElementById = id => doc.querySelector(`#${id}`);
    const win = {
        document: doc, navigator: {userAgent}, fetch: fetchImpl, URL, AbortController,
        location: {origin: "https://product.example", href: "https://product.example/"},
        localStorage: {getItem: () => "{}"}, addEventListener() {}, removeEventListener() {},
        setTimeout: clock ? (callback, delay) => {
            clock.now += delay;
            return setImmediate(callback);
        } : setTimeout,
        clearTimeout,
    };
    const context = vm.createContext({
        Date: clock ? class extends Date { static now() { return clock.now; } } : Date,
        window: win, self: win, document: doc, navigator: win.navigator, location: win.location,
        fetch: fetchImpl, URL, Blob, Response, Headers, ReadableStream, AbortController,
        DOMException, MessageChannel: BrowserMessageChannel, crypto: webcrypto, performance,
        setTimeout(callback, delay) {
            const timer = setTimeout(callback, delay);
            if (delay >= 60000) timer.unref();
            return timer;
        }, clearTimeout, setInterval, clearInterval, console,
    });
    return {doc, win, context, load(name) { vm.runInContext(source(name), context, {filename: name}); }};
}

test("INV04 original attachment button observes a three-minute transfer without another handoff", async () => {
    const clock = {now: 0}, calls = [];
    const app = browser(async (url, options) => {
        calls.push(url);
        if (url === "/download") {
            assert.equal(JSON.parse(options.body).route_only, true);
            return json({status: "routed", route_type: "proxy", download_strategy: "server_single_attachment"});
        }
        if (url === "/prepare") return json({ok: true, status: "issued", expires_in_seconds: 60,
            download_url: "/media/download-stream/ticket/", status_url: "/status", poll_after_ms: 500});
        assert.equal(url, "/status");
        return json({ok: true, stream_started: clock.now >= 20000,
            status: clock.now < 20000 ? "connecting" : clock.now < 180000 ? "streaming" : "completed"});
    }, "Chrome/131.0.0.0", clock);
    const page = new DomNode("main", {class: "xhs-download-page", "data-download-url": "/download",
        "data-download-prepare-url": "/prepare", "data-base-filename": "Contract short title"});
    const region = new DomNode("section", {id: "parse-results-region"});
    const card = new DomNode("article", {class: "asset-card", "data-type": "video",
        "data-url": "https://media.bilivideo.com/resource"});
    const button = new DomNode("button", {id: "download-btn"});
    const status = new DomNode("p", {id: "status"});
    region.append(card, button, status); page.append(region); app.doc.body.append(page);
    app.load("download_reliability.js"); app.load("xhs_download.js");
    app.doc.listeners.DOMContentLoaded();
    await region.listeners.click({target: card, preventDefault() {}});
    await region.listeners.click({target: button, preventDefault() {}});
    for (let step = 0; step < 300 && !/传输已结束/.test(status.textContent); step += 1) await tick();
    assert.ok(clock.now >= 180000, JSON.stringify({clock, calls, status: status.textContent}));
    assert.equal(app.doc.getElementById("xhs-server-download-frame").src, "/media/download-stream/ticket/");
    assert.equal(calls.filter(url => url === "/download").length, 1);
    assert.equal(calls.filter(url => url === "/prepare").length, 1);
    assert.ok(calls.filter(url => url === "/status").length > 30);
    assert.match(status.textContent, /服务器传输已结束.*确认文件保存结果/);
    assert.doesNotMatch(status.textContent, /已保存|保存成功/);
});
function selectionItem(quality) {
    const provider = "tiantianjiexi", resource = "7669858827638845160";
    return {
        selection_schema: "service_selected_format_v1", candidate_id: quality, selected_format_id: quality,
        selection_expires_at: Date.now() + 60000, type: "video", provider, source_platform: "douyin",
        asset_id: `douyin:${resource}:video:${provider}:format:${(quality === "1080" ? "a" : "b").repeat(24)}`,
        provider_resource_id: resource, provider_cache_key: `douyin:${provider}:validated:${resource}`,
        provider_manifest_token: `manifest-${quality}`, browser_upstream_bound: true,
        browser_direct_fetch_safe: true, requires_merge: false, requires_cookie: false,
        filename: "Contract short title.bin", url: `https://v11-default.365yg.com/${quality}.mp4`,
        size_bytes: 2,
    };
}
function mountPicker(app) {
    const root = new DomNode("section", {"data-ytdlp-picker": "", "data-provider": "tiantianjiexi",
        "data-selection-schema": "service_selected_format_v1", "data-selection-url": "/media/service-selected-format/"});
    for (const quality of ["1080", "720"]) {
        const label = new DomNode("label", {"data-picker-option": ""});
        const radio = new DomNode("input", {"data-picker-radio": "", value: quality,
            "data-kind": "video", "data-token": `token-${quality}`, "data-format-id": quality,
            "data-can-download": "true", "data-legacy-asset-id": selectionItem(quality).asset_id});
        radio.checked = quality === "1080"; label.append(radio); root.append(label);
    }
    root.append(new DomNode("p", {"data-picker-status": ""}));
    app.doc.body.append(root);
    const controller = app.win.DokiToolYtdlpPicker.mount(root, app.doc, app.win);
    return {root, controller, choose(quality) {
        root.querySelectorAll("[data-picker-radio]").forEach(radio => { radio.checked = radio.value === quality; });
        root.listeners.change?.({target: root.querySelector("[data-picker-radio]:checked")});
    }};
}

for (const batch of [false, true]) {
    test(`INV03 ${batch ? 'Batch' : 'homepage'} ordinary click uses newly prepared native descriptor`, async () => {
        const requests = [], links = [];
        const mediaUrl = 'https://media.bilivideo.com/known-size.mp4';
        const app = browser(async (url, options) => {
            const payload = JSON.parse(options.body);
            requests.push(payload);
            assert.equal(payload.intent, 'download');
            assert.equal(payload.route_only, true);
            assert.equal(payload.media_items.length, 1);
            assert.equal(payload.media_items[0].url, mediaUrl);
            return json({status: 'routed', route_type: 'direct', items: [{...payload.media_items[0],
                browser_direct_fetch_safe: true, requires_merge: false,
                download_strategy: 'browser_native', native_download_expires_at: Date.now() + 60000}]});
        });
        app.win.navigator.serviceWorker = {register() { assert.fail('fresh native evidence must bypass SW'); }};
        const create = app.doc.createElement;
        app.doc.createElement = tag => {
            const node = create(tag);
            if (tag === 'a') node.click = () => links.push(node.href);
            return node;
        };
        for (const name of ['ordinary_media_streaming_protocol.js', 'ordinary_media_streaming.js',
            'ordinary_media_download.js', 'download_reliability.js']) app.load(name);
        let scope, button;
        if (batch) {
            scope = new DomNode('main', {class: 'batch-parse-page', 'data-batch-download-url': '/download'});
            const csrf = new DomNode('input', {name: 'csrfmiddlewaretoken', value: 'csrf'});
            const card = new DomNode('article', {class: 'batch-direct-item', 'data-batch-download-item': '', 'data-type': 'video'});
            button = new DomNode('button', {'data-batch-direct-download': '', 'data-batch-direct-url': mediaUrl,
                'data-batch-direct-size-bytes': '12345', 'data-batch-browser-fetch-safe': 'true'});
            card.append(button); scope.append(csrf, card); app.doc.body.append(scope);
            app.load('batch_parse.js');
        } else {
            const page = new DomNode('main', {class: 'xhs-download-page', 'data-download-url': '/download'});
            scope = new DomNode('section', {id: 'parse-results-region'});
            const card = new DomNode('article', {class: 'asset-card', 'data-type': 'video',
                'data-url': mediaUrl, 'data-size-bytes': '12345'});
            button = new DomNode('button', {id: 'download-btn'});
            scope.append(card, button, new DomNode('p', {id: 'status'}));
            page.append(scope); app.doc.body.append(page);
            app.load('xhs_download.js'); app.doc.listeners.DOMContentLoaded();
            await scope.listeners.click({target: card, preventDefault() {}});
        }
        await scope.listeners.click({target: button, preventDefault() {}});
        for (let i = 0; i < 10 && links.length === 0; i++) await tick();
        assert.equal(requests.length, 1);
        assert.deepEqual(links, [mediaUrl]);
    });
}

test('INV03 native attachment handoff preserves selection without Worker, Blob or completion claims', async () => {
    const requests = [], links = [];
    const app = browser(async (url, options) => {
        requests.push(url);
        assert.equal(url, 'https://product.example/media/service-selected-format/');
        const body = JSON.parse(options.body);
        return json({status: 'success', item: {...selectionItem(body.candidate_id),
            download_strategy: 'browser_native', native_download_expires_at: Date.now() + 60000}});
    });
    app.win.navigator.serviceWorker = {register() { assert.fail('Native must not register a Worker'); },
        controller: {postMessage() { assert.fail('Native must not wait for an old controller'); }}};
    const create = app.doc.createElement;
    app.doc.createElement = tag => {
        const node = create(tag);
        if (tag === 'a') node.click = () => links.push({url: node.href, target: node.target});
        return node;
    };
    for (const name of ['ordinary_media_streaming_protocol.js', 'ordinary_media_streaming.js',
        'ordinary_media_download.js', 'ytdlp_result_picker.js']) app.load(name);
    const mounted = mountPicker(app);
    mounted.choose('720');
    await mounted.controller.save();
    assert.equal(requests.length, 1);
    assert.deepEqual(links, [{url: selectionItem('720').url, target: '_blank'}]);
    const status = mounted.root.querySelector('[data-picker-status]').textContent;
    assert.match(status, /已发起下载请求/);
    assert.doesNotMatch(status, /已保存|保存成功|100%/);
    mounted.controller.destroy();
});

function attachWorker(app, {legacy = false, unavailable = false, rejectedUrl = false} = {}) {
    const messages = [], navigations = [], mediaRequests = [], workerListeners = new Map();
    const statusListeners = new Set(), statuses = new Map(), responses = [], endings = [];
    let protocol;
    const emit = status => {
        statuses.set(status.downloadId, status);
        statusListeners.forEach(listener => listener({source: worker, data: {
            type: `DOWNLOAD_${status.state.toUpperCase()}`, status, telemetryOwner: false,
        }}));
    };
    const worker = {
        state: "activated",
        postMessage(message, ports) {
            messages.push(message);
            if (!legacy) {
                workerListeners.get("message")({data: message, source: {id: "contract-tab"},
                    ports, waitUntil() {}});
                return;
            }
            const reply = data => ports[0].postMessage(data);
            if (message.type === "REGISTER_DOWNLOAD") {
                if (rejectedUrl) {
                    reply({ok: false, code: "STREAMING_URL_UNSUPPORTED"});
                    return;
                }
                const descriptor = message.descriptor;
                const status = {...descriptor, protocolVersion: 2, state: "prepared",
                    receivedBytes: 0, totalBytes: 2, streamingStarted: false};
                statuses.set(descriptor.downloadId, status);
                reply({ok: true, status});
            } else if (message.type === "GET_DOWNLOAD_STATUS") {
                reply({ok: true, status: statuses.get(message.downloadId)});
            } else if (message.type === "LIST_ACTIVE_DOWNLOADS") {
                reply({ok: true, statuses: [...statuses.values()]});
            } else {
                reply({ok: false, code: "UNKNOWN_MESSAGE"});
            }
        },
    };
    const registration = {active: worker, waiting: null, installing: null,
        async update() { return this; }};
    const serviceWorker = {
        controller: worker,
        async register() { return registration; },
        addEventListener(name, listener) { if (name === "message") statusListeners.add(listener); },
        removeEventListener(name, listener) { if (name === "message") statusListeners.delete(listener); },
    };
    if (!unavailable) app.win.navigator.serviceWorker = serviceWorker;
    for (const name of ["ordinary_media_streaming_protocol.js", "ordinary_media_streaming.js",
        "ordinary_media_download.js", "ytdlp_result_picker.js"]) app.load(name);
    protocol = app.win.AnimeImagesOrdinaryMediaStreamingProtocol;
    if (!legacy) {
        const workerContext = {
            Date, AbortController, DOMException, Headers, Promise, ReadableStream, Response, URL,
            console, encodeURIComponent, performance, setTimeout, clearTimeout,
            importScripts() { workerContext.self.AnimeImagesOrdinaryMediaStreamingProtocol = protocol; },
            self: {location: {origin: app.win.location.origin, hostname: "product.example"},
                clients: {async matchAll() { return [{id: "contract-tab", postMessage(message) { emit(message.status); }}]; }},
                addEventListener(name, listener) { workerListeners.set(name, listener); }},
            fetch: async url => {
                mediaRequests.push(url);
                const ending = deferred(); endings.push(ending);
                let sent = false;
                return {ok: true, status: 200, url,
                    headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "2"}),
                    body: new ReadableStream({async pull(controller) {
                        if (!sent) { sent = true; controller.enqueue(Uint8Array.of(1, 2)); }
                        else { await ending.promise; controller.close(); }
                    }})};
            },
        };
        vm.runInNewContext(source("ordinary_media_download_sw.js"), workerContext);
    }
    app.doc.createElement = tag => {
        const node = new DomNode(tag);
        if (tag === "a") node.click = () => {
            navigations.push(node.href);
            const id = protocol.descriptorIdFromPath(node.href);
            if (legacy) {
                emit({...statuses.get(id), state: "fetching", streamingStarted: true});
            } else {
                workerListeners.get("fetch")({
                    request: {method: "GET", url: new URL(node.href, app.win.location.origin).href},
                    respondWith(promise) { responses.push(Promise.resolve(promise)); }, waitUntil() {},
                });
            }
        };
        return node;
    };
    return {messages, navigations, mediaRequests, responses, endings, statuses, emit,
        async close() { await app.win.AnimeImagesOrdinaryMediaStreaming.getSharedClient().close(); }};
}

test("INV03 selecting quality changes only resource/auth; real controller/client/Worker keeps streaming", async () => {
    const actions = [];
    const app = browser(async (url, options) => {
        assert.equal(url, "https://product.example/media/service-selected-format/");
        const action = JSON.parse(options.body); actions.push(action);
        return json({status: "success", item: selectionItem(action.candidate_id)});
    });
    const worker = attachWorker(app);
    const picker = mountPicker(app);
    try {
        for (const [index, quality] of ["1080", "720"].entries()) {
            picker.choose(quality);
            const saving = picker.controller.save();
            await tick();
            assert.equal(worker.responses.length, index + 1, picker.root.querySelector("[data-picker-status]").textContent);
            const response = await worker.responses[index];
            const reader = response.body.getReader();
            assert.deepEqual(Array.from((await reader.read()).value), [1, 2]);
            assert.match(decodeURIComponent(response.headers.get("Content-Disposition")), /Contract short title.mp4/);
            assert.doesNotMatch(picker.root.querySelector("[data-picker-status]").textContent, /已保存|保存成功/);
            worker.endings[index].resolve();
            assert.equal((await reader.read()).done, true);
            await saving;
        }
        assert.deepEqual(actions, ["1080", "720"].map(id => ({
            candidate_id: id, resource_token: `token-${id}`, action: "download",
        })));
        assert.deepEqual(worker.mediaRequests, ["1080", "720"].map(id => selectionItem(id).url));
        assert.equal(worker.messages.filter(message => message.type === "REGISTER_DOWNLOAD").length, 2);
        assert.equal(worker.navigations.length, 2);
    } finally {
        worker.endings.forEach(ending => ending.resolve());
        picker.controller.destroy();
        await worker.close();
    }
});

test("INV03 old active Worker accepts selected transfers without handshake or forced version switch", async () => {
    const app = browser(async (_url, options) => {
        const body = JSON.parse(options.body);
        return json({status: "success", item: selectionItem(body.candidate_id)});
    });
    const worker = attachWorker(app, {legacy: true});
    const picker = mountPicker(app);
    try {
        picker.choose("720");
        const saving = picker.controller.save();
        await tick();
        assert.equal(worker.navigations.length, 1, picker.root.querySelector("[data-picker-status]").textContent);
        const registration = worker.messages.find(message => message.type === "REGISTER_DOWNLOAD");
        assert.ok(registration);
        assert.equal(registration.protocolVersion, undefined);
        assert.equal(registration.descriptor.url, selectionItem("720").url);
        assert.equal(registration.descriptor.selectionBinding, undefined);
        assert.equal(worker.messages.some(message => /CAPABILIT|SKIP_WAITING|ACTIVATE/.test(message.type)), false);
        const status = worker.statuses.get(registration.descriptor.downloadId);
        worker.emit({...status, state: "completed", receivedBytes: 2, totalBytes: 2, streamingStarted: true});
        await saving;
        assert.doesNotMatch(picker.root.querySelector("[data-picker-status]").textContent, /关闭所有|升级|更新后/);
    } finally { picker.controller.destroy(); await worker.close(); }
});

test("INV03 unavailable Worker uses one bounded Blob fallback and INV06 real MIME fixes placeholder extension", async () => {
    const requests = [], saves = [];
    let blob;
    const app = browser(async (url, options) => {
        requests.push(url);
        if (url.includes("service-selected-format")) {
            const action = JSON.parse(options.body);
            return json({status: "success", item: selectionItem(action.candidate_id)});
        }
        assert.equal(url, selectionItem("720").url);
        return {ok: true, status: 200, url, headers: new Headers({"Content-Type": "video/mp4", "Content-Length": "2"}),
            body: new Blob([Uint8Array.of(1, 2)]).stream()};
    });
    const worker = attachWorker(app, {unavailable: true});
    class BoundaryURL extends URL {
        static createObjectURL(value) { blob = value; return "blob:contract"; }
        static revokeObjectURL() {}
    }
    app.context.URL = BoundaryURL;
    app.win.URL = BoundaryURL;
    app.doc.createElement = tag => Object.assign(new DomNode(tag), {
        click() { saves.push({filename: this.download, href: this.href}); },
    });
    const picker = mountPicker(app);
    try {
        picker.choose("720");
        await picker.controller.save();
        assert.deepEqual(requests, ["https://product.example/media/service-selected-format/", selectionItem("720").url]);
        assert.deepEqual(saves, [{filename: "Contract short title.mp4", href: "blob:contract"}]);
        assert.deepEqual(Array.from(new Uint8Array(await blob.arrayBuffer())), [1, 2]);
        assert.equal(worker.navigations.length, 0);
    } finally { picker.controller.destroy(); await worker.close(); }
});

test("INV03 old Worker resource rules cannot silently change a selected download to Blob", async () => {
    const requests = [];
    const app = browser(async (url, options) => {
        requests.push(url);
        assert.equal(url, "https://product.example/media/service-selected-format/");
        return json({status: "success", item: selectionItem(JSON.parse(options.body).candidate_id)});
    });
    const worker = attachWorker(app, {legacy: true, rejectedUrl: true});
    const picker = mountPicker(app);
    try {
        picker.choose("720");
        await picker.controller.save();
        assert.equal(requests.length, 1, "only the original authorization request, no media retry");
        assert.equal(worker.navigations.length, 0);
        assert.equal(worker.messages.filter(message => message.type === "REGISTER_DOWNLOAD").length, 1);
        assert.equal(worker.messages.some(message => /CAPABILIT|SKIP_WAITING|ACTIVATE/.test(message.type)), false);
        assert.doesNotMatch(picker.root.querySelector("[data-picker-status]").textContent, /已交给|已保存/);
    } finally { picker.controller.destroy(); await worker.close(); }
});

for (const userAgent of ["Version/18.0 Safari/605.1.15", "Chrome/131.0.0.0 Safari/537.36"]) {
    test(`INV04 attachment controller waits for terminal status (${userAgent.split("/")[0]} boundary, not real browser)`, async () => {
        const release = deferred(), polled = deferred(), calls = [];
        let polls = 0;
        const app = browser(async (url, options) => {
            calls.push(url);
            if (url === "/download") {
                assert.equal(JSON.parse(options.body).route_only, true);
                return json({status: "routed", route_type: "proxy", download_strategy: "server_single_attachment"});
            }
            if (url === "/prepare") return json({ok: true, status: "issued",
                download_url: "/media/download-stream/ticket/", status_url: "/status", poll_after_ms: 1});
            assert.equal(url, "/status");
            if (++polls === 1) return json({ok: true, status: "streaming", stream_started: true});
            polled.resolve();
            await release.promise;
            return json({ok: true, status: "completed", stream_started: true});
        }, userAgent);
        const page = new DomNode("main", {class: "xhs-download-page", "data-download-url": "/download",
            "data-download-prepare-url": "/prepare", "data-base-filename": "Contract short title"});
        const region = new DomNode("section", {id: "parse-results-region"});
        const card = new DomNode("article", {class: "asset-card", "data-type": "video",
            "data-url": "https://media.bilivideo.com/resource"});
        const button = new DomNode("button", {id: "download-btn"});
        const status = new DomNode("p", {id: "status"});
        region.append(card, button, status); page.append(region); app.doc.body.append(page);
        app.load("download_reliability.js"); app.load("xhs_download.js");
        app.doc.listeners.DOMContentLoaded();
        await region.listeners.click({target: card, preventDefault() {}});
        const saving = region.listeners.click({target: button, preventDefault() {}});
        try {
            await polled.promise;
            assert.equal(app.doc.getElementById("xhs-server-download-frame").src, "/media/download-stream/ticket/");
            assert.match(status.textContent, /尚未确认文件保存完成/);
            assert.doesNotMatch(status.textContent, /已保存|保存成功|传输已结束/);
            assert.equal(calls.filter(url => url === "/prepare").length, 1);
        } finally {
            release.resolve(); await saving; await tick();
        }
        assert.match(status.textContent, /服务器传输已结束.*确认文件保存结果/);
        assert.doesNotMatch(status.textContent, /已保存|保存成功/);
    });
}
