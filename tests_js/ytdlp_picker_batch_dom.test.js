"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {Node} = require("./helpers/lab_dom.js");
const picker = require("../static/js/ytdlp_result_picker.js");
const batch = require("../static/js/batch_parse.js");

// Extend the existing DOM double with browser radio grouping and checked selectors.
class DomNode extends Node {
    get tree() { return this.parentNode ? this.parentNode.tree : this; }
    get name() { return this.getAttribute("name") || ""; }
    set name(value) { this.setAttribute("name", value); if (this.checked) this.checked = true; }
    get checked() { return Boolean(this._checked); }
    set checked(value) {
        this._checked = Boolean(value);
        if (value && this.name) this.tree.querySelectorAll("[data-picker-radio]").forEach(node => {
            if (node !== this && node.name === this.name) node._checked = false;
        });
    }
    get defaultChecked() { return this.getAttribute("checked") !== null; }
    get isConnected() { return this.tree.tagName === "DOCUMENT"; }
    matches(selector) {
        if (selector.endsWith(":checked")) return this.checked && super.matches(selector.slice(0, -8));
        return super.matches(selector);
    }
    append(...nodes) {
        nodes.forEach(node => {
            const checked = [node, ...node.querySelectorAll("[data-picker-radio]")]
                .filter(radio => radio.matches("[data-picker-radio]") && radio.checked);
            super.append(node);
            checked.forEach(radio => { radio.checked = true; });
        });
    }
    appendChild(node) { this.append(node); return node; }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    removeAttribute(name) { delete this.attributes[name]; }
}
function el(tag, attrs = {}) { return new DomNode(tag, attrs); }
function fixture() {
    const doc = el("document"); doc.body = el("body"); doc.append(doc.body);
    doc.createElement = tag => el(tag);
    const observers = [];
    const win = {location: {href: "https://product.example/batch/", origin: "https://product.example"},
        AbortController, setTimeout, clearTimeout, fetch: async () => { throw new Error("unexpected fetch"); },
        MutationObserver: class { constructor(fn) { observers.push(fn); } observe() {} }};
    return {doc, win, scan: () => observers.forEach(fn => fn())};
}
function newPicker(ids = ["video", "audio"], name = "") {
    const root = el("section", {"data-ytdlp-picker": "", "data-selection-url": "/media/ytdlp-selected-format/"});
    ids.forEach((id, index) => {
        const row = el("label", {"data-picker-option": ""});
        const radio = el("input", {type: "radio", "data-picker-radio": "", value: id,
            "data-token": `signed-${id}`, "data-kind": id.includes("audio") ? "audio" : "video",
            "data-format-id": `format-${id}`, "data-can-download": "false"});
        if (name) radio.name = name;
        if (!index) { radio.setAttribute("checked", ""); radio.checked = true; }
        row.append(radio); root.append(row);
    });
    root.append(el("p", {"data-picker-status": ""}));
    root.append(el("button", {"data-ytdlp-action": "copy"}));
    return root;
}
function radios(root) { return root.querySelectorAll("[data-picker-radio]"); }
function layeredPicker() {
    const root = newPicker(["video", "restricted", "audio", "audio-alt"]);
    const rows = radios(root).map(radio => radio.parentNode);
    radios(root).forEach(radio => {
        radio.dataset.canDownload = String(radio.value !== "restricted");
        radio.dataset.limitation = radio.value === "restricted" ? "requires merge" : "";
    });
    const primary = el("div", {"data-picker-primary": ""});
    const other = el("details", {"data-picker-other": ""}); other.open = false;
    const audio = el("details", {class: "ytdlp-audio-formats"}); audio.open = false;
    const audioOther = el("details", {"data-picker-other": ""}); audioOther.open = false;
    primary.append(rows[0]); other.append(rows[1]);
    audio.append(rows[2], audioOther); audioOther.append(rows[3]);
    root.append(primary, other, audio);
    root.append(el("button", {"data-ytdlp-action": "download"}));
    root.append(el("p", {"data-picker-limitation": ""}));
    return root;
}
function choose(root, index) {
    const radio = radios(root)[index]; radio.checked = true;
    root.listeners.change?.({target: radio});
}
function card(root, status = "success") {
    const result = el("article", {class: "batch-result-card", "data-status": status});
    result.append(root); return result;
}
function legacy(type, url, filename) {
    return el("div", {"data-batch-download-item": "", "data-type": type, "data-url": url, "data-download-filename": filename});
}
function technicalCandidate(url) {
    // Represented resources remain in technical details, not legacy download rows.
    return el("article", {"data-upstream-candidate": "", "data-url": url});
}
function linkItem(id, type = "video", extra = {}) {
    return {candidate_id: id, selected_format_id: `format-${id}`, url: `https://cdn.example/${id}.${type === "audio" ? "m4a" : "mp4"}`,
        type, filename: `${id}.${type === "audio" ? "m4a" : "mp4"}`, selection_schema: picker.SCHEMA,
        provider: "yt_dlp_generic", ...extra};
}

const SERVICE_SCHEMA = "service_selected_format_v1";
const SERVICE_PROVIDERS = ["tiantianjiexi_generic", "kukutool_generic"];
function servicePicker(provider, ids = ["high", "low"]) {
    const root = newPicker(ids);
    root.dataset.provider = provider;
    root.dataset.selectionSchema = SERVICE_SCHEMA;
    root.dataset.selectionUrl = `/media/service-selected-format/?fixture=${provider}`;
    radios(root).forEach((radio, index) => {
        radio.dataset.canDownload = "true";
        radio.dataset.sizeLabel = `${index + 1} MB`;
    });
    root.append(el("button", {"data-ytdlp-action": "download"}), el("button", {"data-ytdlp-action": "cancel"}),
        el("span", {"data-picker-size": ""}));
    return root;
}
function serviceGrant(provider, id, type = "video", extra = {}) {
    return linkItem(id, type, {provider, selection_schema: SERVICE_SCHEMA,
        browser_upstream_bound: true, browser_direct_fetch_safe: true, requires_cookie: false, requires_merge: false,
        ...extra});
}

function legacyPicker(provider) {
    const root = servicePicker(provider);
    radios(root)[0].dataset.canDownload = "false";
    radios(root)[0].dataset.canLegacy = "true";
    radios(root)[0].dataset.legacyAssetId = "third_party:source:video:high";
    radios(root)[0].dataset.limitation = "此链接为 HTTP，不能直接保存；可复制链接。";
    root.append(el("p", {"data-picker-limitation": ""}), el("button", {"data-ytdlp-action": "legacy"}));
    return root;
}
function legacyGrant(provider, id = "high") {
    return serviceGrant(provider, id, "video", {selection_delivery: "legacy_manifest",
        browser_upstream_bound: false, browser_direct_fetch_safe: false,
        asset_id: `third_party:source:video:${id}`, provider_resource_id: "resource", provider_cache_key: "cache",
        provider_manifest_token: `manifest-${id}`, source_platform: "third_party",
        url: `http://cdn.example/${id}.mp4`, size_bytes: 2048});
}
function jsonResponse(payload, status = 200) {
    return {ok: status < 400, status, json: async () => payload};
}

function homeLegacyController(fetchImpl, existingPickerRoot) {
    const nodes = new Map();
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {dataset: {}, style: {}, isConnected: true, innerHTML: "保存",
            innerText: "", hidden: false, classList: {add() {}, remove() {}, toggle() {}},
            setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
            querySelector() { return null; }});
        return nodes.get(id);
    }
    const oldCard = node("old-card");
    oldCard.dataset = {type: "video", assetId: "third_party:old:video:1", provider: "kukutool_generic",
        sourcePlatform: "third_party", url: "http://cdn.example/old.mp4", providerResourceId: "old-resource",
        providerCacheKey: "old-cache", providerManifestToken: "old-manifest", downloadFilename: "old.mp4"};
    const pickerRoot = existingPickerRoot || node("picker"), otherPicker = node("other-picker");
    node("[data-legacy-download-status]").hidden = true;
    let regionClick, ready;
    const region = {
        contains(value) { return [oldCard, pickerRoot, node("#download-btn"), node("#select-all-btn")].includes(value); },
        querySelector(selector) { return ["#status", "#download-btn", "#select-all-btn", "[data-legacy-download-status]"].includes(selector) ? node(selector) : null; },
        querySelectorAll(selector) { return selector === ".asset-card" ? [oldCard] : []; },
        addEventListener(name, callback) { if (name === "click") regionClick = callback; },
    };
    const page = {dataset: {downloadUrl: "/download", downloadTaskUrl: "/tasks", downloadAsyncEnabled: "true",
        baseFilename: "original-page-title"}, querySelector() { return null; }};
    const saved = [];
    class BrowserURL extends URL {
        static createObjectURL() { return "blob:https://product.example/zip"; }
        static revokeObjectURL() {}
    }
    const document = {
        cookie: "csrftoken=fixture-csrf",
        querySelector(selector) { return selector === ".xhs-download-page" ? page : null; },
        getElementById(id) { return id === "parse-results-region" ? region : null; },
        addEventListener(name, callback) { if (name === "DOMContentLoaded") ready = callback; },
        createElement(tag) { return {tagName: tag, style: {}, setAttribute() {}, remove() {}, click() { saved.push(this.download); }}; },
        body: {appendChild() {}, removeChild() {}},
    };
    const window = {location: {origin: "https://product.example"}, localStorage: {getItem() { return "{}"; }},
        addEventListener() {}, setTimeout, clearTimeout, alert: message => assert.fail(message), URL: BrowserURL};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/js/xhs_download.js"), "utf8"), {
        window, document, URL: BrowserURL, AbortController, Blob, fetch: fetchImpl,
        console: {warn() {}, error() {}},
    });
    ready();
    function clickRegion(target, selector) {
        regionClick({target: {closest(value) { return value === selector ? target : null; }}, preventDefault() {}});
    }
    clickRegion(oldCard, ".asset-card");
    assert.match(node("#status").innerText, /已选择 1 个资源/);
    return {bridge: window.AnimeImagesLegacySelectionDownload, pickerRoot, otherPicker, saved, node, page,
        clickOldDownload: () => clickRegion(node("#download-btn"), "#download-btn")};
}

test("real home bridge snapshots the candidate, rejects concurrency and preserves the previous selection through the ZIP workflow", async () => {
    const requests = [];
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const home = homeLegacyController(async (url, options) => {
        const payload = options.body ? JSON.parse(options.body) : null;
        requests.push({url, payload});
        if (url === "/download") {
            assert.equal(home.node("[data-legacy-download-status]").hidden, false, "status must be visible before the first route request");
            if (payload.media_items[0].asset_id === "third_party:old:video:1") return new Response(JSON.stringify({message: "old selected item"}), {status: 403});
            await gate;
            return new Response(JSON.stringify({status: "routed", route_type: "zip", download_strategy: "server_zip"}));
        }
        if (url === "/tasks") return new Response(JSON.stringify({status_url: "/task-status", file_url: "/task-file"}));
        if (url === "/task-status") return new Response(JSON.stringify({status: "completed", saved_count: 1}));
        assert.equal(url, "/task-file");
        return new Response(new Blob(["mock zip bytes"]), {headers: {"Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="high.zip"', "X-Saved-Count": "1"}});
    });
    const item = legacyGrant("kukutool_generic");
    const expected = {...item};
    assert.equal(home.node("[data-legacy-download-status]").hidden, true);
    const first = home.bridge(item, home.pickerRoot);
    assert.equal(home.node("[data-legacy-download-status]").hidden, false);
    assert.equal(requests.length, 1);
    await assert.rejects(home.bridge(legacyGrant("kukutool_generic", "other"), home.pickerRoot), /等待已有任务/);
    assert.equal(requests.length, 1);
    item.url = "https://cdn.example/substituted.mp4"; item.filename = "substituted.mp4";
    release(); await first;
    assert.deepEqual(requests.map(request => request.url), ["/download", "/tasks", "/task-status", "/task-file"]);
    for (const request of requests.slice(0, 2)) {
        assert.deepEqual(request.payload.media_items, [expected]);
        assert.equal(request.payload.base_filename, "high");
        assert.equal(request.payload.download_mode, "auto");
    }
    assert.equal(requests[0].payload.route_only, true);
    assert.equal(requests[1].payload.route_only, undefined);
    assert.deepEqual(home.saved, ["high.zip"]);
    assert.equal(home.page.dataset.baseFilename, "original-page-title");
    home.clickOldDownload(); await settle();
    assert.equal(requests.length, 5);
    assert.equal(requests[4].payload.media_items.length, 1);
    assert.equal(requests[4].payload.media_items[0].asset_id, "third_party:old:video:1");
});

test("real home bridge refuses foreign roots and propagates route, task and network failures without retries", async () => {
    for (const failure of ["route", "task", "network"]) {
        const requests = [];
        const home = homeLegacyController(async (url, options) => {
            requests.push({url, payload: options.body && JSON.parse(options.body)});
            if (failure === "network") throw new Error("network failed");
            if (failure === "route") return new Response(JSON.stringify({message: "route denied"}), {status: 403});
            if (url === "/download") return new Response(JSON.stringify({status: "routed", route_type: "zip", download_strategy: "server_zip"}));
            assert.equal(url, "/tasks");
            return new Response(JSON.stringify({message: "task denied"}), {status: 503});
        });
        await assert.rejects(home.bridge(legacyGrant("kukutool_generic"), home.otherPicker), /当前无法/);
        assert.equal(requests.length, 0);
        assert.equal(home.node("[data-legacy-download-status]").hidden, true, "a rejected foreign picker must not reveal status");
        await assert.rejects(home.bridge(legacyGrant("kukutool_generic"), home.pickerRoot), new RegExp(failure === "network" ? "network failed" : `${failure} denied`));
        assert.equal(requests.length, failure === "task" ? 2 : 1);
        assert.equal(home.node("#download-btn").disabled, false);
        assert.deepEqual(home.saved, []);
    }
});

test("real home bridge propagates an HTTP 200 logical failure instead of reporting a successful handoff", async () => {
    let calls = 0;
    const home = homeLegacyController(async () => {
        calls += 1;
        return new Response(JSON.stringify(calls === 1
            ? {status: "routed", route_type: "zip", download_strategy: "server_zip"}
            : {status: "error", message: "legacy logical failure"}), {headers: {"Content-Type": "application/json"}});
    });
    await assert.rejects(home.bridge(legacyGrant("tiantianjiexi_generic"), home.pickerRoot), /legacy logical failure/);
    assert.equal(calls, 2);
    assert.deepEqual(home.saved, []);
});

test("real home bridge ZIP filename fallback uses the candidate rather than the original page title", async () => {
    const home = homeLegacyController(async url => {
        if (url === "/download") return new Response(JSON.stringify({status: "routed", route_type: "zip", download_strategy: "server_zip"}));
        if (url === "/tasks") return new Response(JSON.stringify({status_url: "/task-status", file_url: "/task-file"}));
        if (url === "/task-status") return new Response(JSON.stringify({status: "completed", saved_count: 1}));
        assert.equal(url, "/task-file");
        return new Response(new Blob(["mock zip bytes"]), {headers: {"Content-Type": "application/zip"}});
    });
    await home.bridge(legacyGrant("kukutool_generic"), home.pickerRoot);
    assert.deepEqual(home.saved, ["high.zip"]);
});

test("empty native format ID reaches the real home ZIP bridge while keeping HTTP restriction and legacy action explicit", async () => {
    const {doc, win} = fixture(); const root = legacyPicker("kukutool_generic"); doc.body.append(root);
    radios(root)[0].setAttribute("data-format-id", "");
    const grant = {...legacyGrant("kukutool_generic"), selected_format_id: ""};
    const calls = [];
    const home = homeLegacyController(async (url, options) => {
        const payload = options.body && JSON.parse(options.body); calls.push({url, payload});
        if (url === "/download") {
            assert.deepEqual(payload.media_items, [grant]);
            return new Response(JSON.stringify({status: "routed", route_type: "zip", download_strategy: "server_zip"}));
        }
        if (url === "/tasks") {
            assert.deepEqual(payload.media_items, [grant]);
            return new Response(JSON.stringify({status_url: "/task-status", file_url: "/task-file"}));
        }
        if (url === "/task-status") return new Response(JSON.stringify({status: "completed", saved_count: 1}));
        assert.equal(url, "/task-file");
        return new Response(new Blob(["mock zip bytes"]), {headers: {"Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="high.zip"'}});
    }, root);
    win.AnimeImagesLegacySelectionDownload = home.bridge;
    picker.mount(root, doc, win, {fetchImpl: async (url, options) => {
        const payload = JSON.parse(options.body); calls.push({url, payload});
        assert.equal(payload.action, "legacy");
        assert.equal(payload.candidate_id, "high");
        return jsonResponse({status: "success", item: grant});
    }});
    assert.equal(picker.selected(root).formatId, "");
    const restriction = radios(root)[0].dataset.limitation;
    assert.equal(root.querySelector("[data-ytdlp-action='download']").textContent, "原有方式保存所选资源");
    assert.equal(root.querySelector("[data-picker-limitation]").textContent,
        `${restriction} 可用原有方式保存所选资源；传输方式由原下载流程决定。`);
    click(root); await settle();
    assert.deepEqual(calls.slice(1).map(call => call.url), ["/download", "/tasks", "/task-status", "/task-file"]);
    assert.deepEqual(home.saved, ["high.zip"]);
    choose(root, 1);
    assert.equal(root.querySelector("[data-ytdlp-action='download']").textContent, "↓ 下载所选视频");
    assert.doesNotMatch(root.querySelector("[data-picker-limitation]").textContent, /原有方式/);
});
for (const provider of SERVICE_PROVIDERS) {
    test(`${provider} legacy bridge receives only the selected grant and originating picker once`, async () => {
        const {doc, win} = fixture(); const first = legacyPicker(provider), second = legacyPicker(provider);
        doc.body.append(card(first), card(second));
        const calls = [], saves = [], handoffs = [];
        win.AnimeImagesLegacySelectionDownload = async (item, root) => handoffs.push({item, root});
        picker.attach(doc, win, {ordinary: {download: async item => saves.push(item)},
            fetchImpl: async (url, options) => {
                assert.match(url, /service-selected-format/);
                const request = JSON.parse(options.body); calls.push(request);
                return jsonResponse({status: "success", item: request.action === "legacy"
                    ? legacyGrant(provider) : serviceGrant(provider, request.candidate_id)});
            }});
        choose(second, 1);
        click(first); click(first); await settle();
        assert.deepEqual(calls, [{candidate_id: "high", resource_token: "signed-high", action: "legacy"}]);
        assert.equal(handoffs.length, 1);
        assert.deepEqual(handoffs[0].item, legacyGrant(provider));
        assert.equal(handoffs[0].root, first);
        assert.equal(doc.body.querySelectorAll("iframe").length, 0);
        assert.equal(saves.length, 0);
        assert.match(first.querySelector("[data-picker-status]").textContent, /原有保存流程.*状态提示/);
        assert.doesNotMatch(first.querySelector("[data-picker-status]").textContent, /开始接收|已保存|保存成功/);
        assert.equal(first.querySelector("[data-ytdlp-action='download']").textContent, "原有方式保存所选资源");
        assert.equal(first.querySelector("[data-picker-limitation]").textContent,
            `${radios(first)[0].dataset.limitation} 可用原有方式保存所选资源；传输方式由原下载流程决定。`);
        assert.equal(first.querySelector("[data-ytdlp-action='legacy']").hidden, true);
        click(second); await settle();
        assert.deepEqual(saves.map(item => item.candidate_id), ["low"]);
        assert.equal(handoffs.length, 1);
        assert.equal(picker.selected(first).id, "high");
    });
}

test("direct capability stays primary and legacy is a separate explicit secondary action", async () => {
    const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(root);
    radios(root)[0].dataset.canDownload = "true";
    const calls = [], saves = [], handoffs = [];
    win.AnimeImagesLegacySelectionDownload = async item => handoffs.push(item);
    picker.mount(root, doc, win, {ordinary: {download: async item => saves.push(item)},
        fetchImpl: async (_url, options) => {
            const request = JSON.parse(options.body); calls.push(request.action);
            return jsonResponse({status: "success", item: request.action === "legacy"
                ? legacyGrant(SERVICE_PROVIDERS[0]) : serviceGrant(SERVICE_PROVIDERS[0], "high")});
        }});
    const secondary = root.querySelector("[data-ytdlp-action='legacy']");
    assert.equal(secondary.hidden, false);
    assert.equal(secondary.textContent, "原有方式保存所选资源");
    click(root); await settle();
    assert.deepEqual(calls, ["download"]); assert.equal(saves.length, 1); assert.equal(handoffs.length, 0);
    click(root, "legacy"); await settle();
    assert.deepEqual(calls, ["download", "legacy"]); assert.equal(handoffs.length, 1);
    choose(root, 1);
    assert.equal(secondary.hidden, true); assert.equal(secondary.disabled, true);
});

test("Batch without a home bridge keeps HTTP legacy-only selection copy-only and direct downloads unchanged", async () => {
    const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(card(root));
    const calls = [], copied = [], saved = [];
    win.navigator = {clipboard: {writeText: async value => copied.push(value)}};
    picker.mount(root, doc, win, {ordinary: {download: async item => saved.push(item)}, fetchImpl: async (_url, options) => {
        const request = JSON.parse(options.body); calls.push(request.action);
        return jsonResponse({status: "success", item: request.action === "link"
            ? legacyGrant(SERVICE_PROVIDERS[0]) : serviceGrant(SERVICE_PROVIDERS[0], "low")});
    }});
    assert.equal(root.querySelector("[data-ytdlp-action='download']").disabled, true);
    assert.equal(root.querySelector("[data-picker-limitation]").textContent, radios(root)[0].dataset.limitation);
    assert.equal(root.querySelector("[data-ytdlp-action='legacy']").hidden, true);
    click(root); click(root, "legacy"); await settle(); assert.equal(calls.length, 0);
    click(root, "copy"); await settle(); assert.deepEqual(copied, ["http://cdn.example/high.mp4"]);
    choose(root, 1); click(root); await settle();
    assert.deepEqual(calls, ["link", "download"]); assert.equal(saved.length, 1);
});

test("a failed direct download never invokes the available legacy bridge automatically", async () => {
    const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(root);
    radios(root)[0].dataset.canDownload = "true";
    let calls = 0;
    win.AnimeImagesLegacySelectionDownload = () => assert.fail("legacy must require its own explicit action");
    picker.mount(root, doc, win, {ordinary: {download: async () => { throw new Error("direct failed"); }},
        fetchImpl: async (_url, options) => {
            calls += 1; assert.equal(JSON.parse(options.body).action, "download");
            return jsonResponse({status: "success", item: serviceGrant(SERVICE_PROVIDERS[0], "high")});
        }});
    click(root); await settle();
    assert.equal(calls, 1);
    assert.equal(root.querySelector("[data-picker-status]").textContent, "direct failed");
});

test("legacy descriptor and controller failures propagate without retries or direct fallback", async () => {
    for (const variant of ["denied", "malformed", "delivery-swap", "asset-swap", "controller-failure"]) {
        const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(root);
        let calls = 0, handoffs = 0;
        win.AnimeImagesLegacySelectionDownload = async () => { handoffs += 1; throw new Error("old route failed"); };
        picker.mount(root, doc, win, {ordinary: {download: () => assert.fail("no direct fallback")},
            fetchImpl: async (_url, options) => {
                calls += 1; assert.equal(JSON.parse(options.body).action, "legacy");
                if (variant === "denied") return jsonResponse({message: "legacy denied"}, 403);
                if (variant === "malformed") return {ok: true, json: async () => { throw new Error("invalid JSON"); }};
                const item = variant === "delivery-swap" ? serviceGrant(SERVICE_PROVIDERS[0], "high") : legacyGrant(SERVICE_PROVIDERS[0]);
                if (variant === "asset-swap") item.asset_id = "another-asset";
                return jsonResponse({status: "success", item});
            }});
        click(root); await settle();
        assert.equal(calls, 1); assert.equal(handoffs, variant === "controller-failure" ? 1 : 0);
        if (variant === "controller-failure") assert.equal(root.querySelector("[data-picker-status]").textContent, "old route failed");
        assert.equal(doc.body.querySelectorAll("iframe").length, 0);
    }
});

for (const phase of ["descriptor", "controller"]) {
    test(`legacy stop-waiting during ${phase} ignores late completion without cancelling the old workflow`, async () => {
        const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(root);
        let release, signal; let calls = 0, handoffs = 0;
        win.AnimeImagesLegacySelectionDownload = async () => {
            handoffs += 1;
            if (phase === "controller") await new Promise(resolve => { release = resolve; });
        };
        picker.mount(root, doc, win, {fetchImpl: async (_url, options) => {
            calls += 1; signal = options.signal;
            if (phase === "descriptor") await new Promise(resolve => { release = resolve; });
            return jsonResponse({status: "success", item: legacyGrant(SERVICE_PROVIDERS[0])});
        }});
        click(root); await settle();
        assert.ok(release);
        assert.equal(root.querySelector("[data-ytdlp-action='cancel']").textContent, "停止等待");
        click(root, "cancel"); await settle();
        assert.equal(signal.aborted, true);
        assert.match(root.querySelector("[data-picker-status]").textContent, /已停止等待.*可能仍在进行/);
        choose(root, 1); const changedMessage = root.querySelector("[data-picker-status]").textContent;
        release(); await settle();
        assert.equal(root.querySelector("[data-picker-status]").textContent, changedMessage);
        assert.equal(picker.selected(root).id, "low"); assert.equal(calls, 1);
        assert.equal(handoffs, phase === "controller" ? 1 : 0);
    });
}

for (const mode of ["copy-links", "links", "aria2"]) {
    test(`legacy-selected Batch ${mode} resolves links only and retains independent legacy resources on expiry`, async () => {
        const {doc, win} = fixture(); const page = el("main"); doc.body.append(page);
        const button = el("button", {"data-batch-export": mode}); page.append(button);
        const first = legacyPicker(SERVICE_PROVIDERS[0]), second = legacyPicker(SERVICE_PROVIDERS[0]);
        const expired = legacyPicker(SERVICE_PROVIDERS[1]);
        const expiredCard = card(expired);
        expiredCard.append(legacy("video", "http://cdn.example/unmatched.mp4", "unmatched.mp4"));
        page.append(card(first), card(second), expiredCard);
        picker.attach(doc, win); choose(second, 1);
        const calls = [], outputs = [];
        const result = await batch.exportBatchResourceLinks(page, button, {windowObject: win, documentObject: doc,
            pickerApi: picker, fetchImpl: async (url, options) => {
                assert.match(url, /service-selected-format/);
                const request = JSON.parse(options.body); calls.push(request);
                assert.equal(request.action, "link");
                if (url.includes(SERVICE_PROVIDERS[1])) return jsonResponse({message: "legacy candidate expired"}, 403);
                return jsonResponse({status: "success", item: request.candidate_id === "high"
                    ? legacyGrant(SERVICE_PROVIDERS[0]) : serviceGrant(SERVICE_PROVIDERS[0], "low")});
            }, copyText: async text => outputs.push(text), downloadTextFile: (_name, text) => outputs.push(text)});
        assert.deepEqual(calls.map(request => request.candidate_id), ["high", "low", "high"]);
        assert.deepEqual(result.resources.map(item => item.url),
            ["http://cdn.example/high.mp4", "https://cdn.example/low.mp4", "http://cdn.example/unmatched.mp4"]);
        assert.deepEqual(result.skipped, ["legacy candidate expired"]);
        assert.equal(outputs.length, 1);
        assert.equal(doc.body.querySelectorAll("iframe").length, 0);
        if (mode === "aria2") assert.match(outputs[0], /out=high.mp4/);
    });
}

test("legacy redraw or removal before descriptor returns cannot hand off a stale selection", async () => {
    for (const change of ["redraw", "remove"]) {
        const {doc, win} = fixture(); const root = legacyPicker(SERVICE_PROVIDERS[0]); doc.body.append(root);
        let release; let requests = 0, handoffs = 0;
        win.AnimeImagesLegacySelectionDownload = async () => { handoffs += 1; };
        picker.mount(root, doc, win, {fetchImpl: async () => {
            requests += 1;
            return new Promise(resolve => { release = () => resolve(jsonResponse({status: "success", item: legacyGrant(SERVICE_PROVIDERS[0])})); });
        }});
        click(root); await settle();
        if (change === "redraw") root.replaceChildren(...legacyPicker(SERVICE_PROVIDERS[0]).children.slice());
        else root.remove();
        release(); await settle();
        assert.equal(requests, 1);
        assert.equal(handoffs, 0);
        assert.equal(doc.body.querySelectorAll("iframe").length, 0);
    }
});

for (const provider of SERVICE_PROVIDERS) {
    test(`${provider} cached service cards keep actions isolated through insertion, sorting and redraw`, async () => {
        const {doc, win, scan} = fixture();
        const first = servicePicker(provider), second = servicePicker(provider);
        doc.body.append(card(first), card(second));
        const requests = [], copied = [], saved = [];
        win.navigator = {clipboard: {writeText: async value => copied.push(value)}};
        picker.attach(doc, win, {ordinary: {download: async media => saved.push(media)},
            fetchImpl: async (_url, options) => {
                const body = JSON.parse(options.body); requests.push(body);
                return {ok: true, json: async () => ({status: "success", item: serviceGrant(provider, body.candidate_id)})};
            }});
        choose(first, 1); choose(second, 0);
        const third = servicePicker(provider); doc.body.append(card(third)); scan();
        choose(third, 1);
        assert.equal(new Set([first, second, third].map(r => radios(r)[0].name)).size, 3);
        assert.deepEqual([first, second, third].map(r => picker.selected(r).id), ["low", "high", "low"]);
        assert.deepEqual([first, second, third].map(r => r.querySelector("[data-picker-size]").textContent), ["2 MB", "1 MB", "2 MB"]);
        first.insertBefore(radios(first)[1].parentNode, radios(first)[0].parentNode); scan();
        first.replaceChildren(...servicePicker(provider).children.slice()); scan();
        picker.mount(first, doc, win);
        assert.equal(picker.selected(first).id, "low");
        assert.equal(picker.selected(first).provider, provider);
        assert.equal(picker.selected(first).schema, SERVICE_SCHEMA);
        assert.equal(requests.length, 0, "mount, selection, sorting and redraw never parse or authorize");
        click(first, "copy"); await settle();
        click(second); await settle();
        click(third); await settle();
        assert.deepEqual(requests.map(r => [r.candidate_id, r.resource_token, r.action]),
            [["low", "signed-low", "link"], ["high", "signed-high", "download"], ["low", "signed-low", "download"]]);
        assert.deepEqual(copied, ["https://cdn.example/low.mp4"]);
        assert.deepEqual(saved.map(media => [media.provider, media.candidate_id, media.selected_format_id, media.filename]),
            [[provider, "high", "format-high", "high.mp4"], [provider, "low", "format-low", "low.mp4"]]);
    });

    test(`${provider} alternate preview retains service identity without changing the save selection`, async () => {
        const {doc, win} = fixture(); const root = servicePicker(provider);
        doc.body.append(root);
        root.dataset.previewId = "high";
        radios(root)[0].dataset.canPreview = "true";
        radios(root)[1].dataset.canPreview = "false";
        const region = el("div", {"data-picker-player-region": ""}); region.hidden = true;
        const slot = el("div", {"data-picker-player": ""}); region.append(slot);
        root.append(region, el("p", {"data-picker-preview-note": ""}), el("button", {"data-ytdlp-action": "preview"}));
        const calls = [];
        picker.mount(root, doc, win, {fetchImpl: async (_url, options) => {
            calls.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({status: "success", item: serviceGrant(provider, "high")})};
        }});
        choose(root, 1); click(root, "preview"); await settle();
        assert.deepEqual(calls, [{candidate_id: "high", resource_token: "signed-high", action: "preview"}]);
        assert.equal(picker.selected(root).id, "low");
        assert.equal(picker.selected(root).provider, provider);
        assert.equal(root.querySelector("[data-picker-size]").textContent, "2 MB");
        assert.equal(slot.children.length, 1, root.querySelector("[data-picker-status]").textContent);
        assert.equal(slot.children[0].src, "https://cdn.example/high.mp4");
        assert.equal(slot.children[0].preload, "none");
        assert.equal(region.hidden, false);
    });

    for (const action of ["copy", "download"]) {
        test(`${provider} cancelled late ${action} response cannot affect another card or retry`, async () => {
            const {doc, win} = fixture(); const first = servicePicker(provider), second = servicePicker(provider);
            doc.body.append(card(first), card(second));
            const pending = [], saves = [], copies = [];
            win.navigator = {clipboard: {writeText: async value => copies.push(value)}};
            picker.attach(doc, win, {ordinary: {download: async media => saves.push(media)},
                fetchImpl: async (_url, options) => new Promise(resolve => pending.push({resolve, options}))});
            click(first, action); click(first, action);
            assert.equal(pending.length, 1, "double click is single flight");
            click(first, "cancel");
            assert.equal(pending[0].options.signal.aborted, true);
            choose(first, 1); click(second, action);
            pending[1].resolve({ok: true, json: async () => ({status: "success", item: serviceGrant(provider, "high")})});
            await settle();
            const message = first.querySelector("[data-picker-status]").textContent;
            pending[0].resolve({ok: true, json: async () => ({status: "success", item: serviceGrant(provider, "high")})});
            await settle();
            assert.equal(picker.selected(first).id, "low");
            assert.equal(first.querySelector("[data-picker-size]").textContent, "2 MB");
            assert.equal(first.querySelector("[data-picker-status]").textContent, message);
            assert.equal(pending.length, 2);
            assert.equal(saves.length, action === "download" ? 1 : 0);
            assert.deepEqual(copies, action === "copy" ? ["https://cdn.example/high.mp4"] : []);
        });
    }
}

for (const mode of ["copy-links", "links", "aria2"]) {
    for (const provider of SERVICE_PROVIDERS) {
        test(`${provider} one multi-video card ${mode} exports selections and unrepresented legacy resources`, async () => {
            const {doc, win} = fixture(); const page = el("main"); doc.body.append(page);
            const button = el("button", {"data-batch-export": mode}); page.append(button);
            const first = servicePicker(provider, ["work-one-high", "work-one-low"]);
            const second = servicePicker(provider, ["work-two-high", "work-two-low"]);
            const image = legacy("image", "https://cdn.example/photo-one.jpg", "photo-one.jpg");
            const mixed = card(first);
            mixed.append(second, image,
                legacy("image", "https://cdn.example/photo-two.png", "photo-two.png"),
                legacy("image", "https://cdn.example/photo-one.jpg", "duplicate.jpg"),
                technicalCandidate("https://cdn.example/obsolete-main.mp4"),
                legacy("video", "https://cdn.example/unrepresented.mp4", "unrepresented.mp4"));
            page.append(mixed);
            picker.attach(doc, win); choose(first, 1);
            const snapshot = batch.collectBatchResourceLinks(page, picker);
            assert.equal(snapshot.length, 5, "selected videos, unmatched legacy video and both unique images must survive collection");
            assert.deepEqual(snapshot.slice(0, 2).map(entry => entry.selection),
                ["work-one-low", "work-two-high"].map(id => ({id, token: `signed-${id}`, kind: "video",
                    formatId: `format-${id}`, provider, schema: SERVICE_SCHEMA})));
            let release; const gate = new Promise(resolve => { release = resolve; });
            const calls = [], outputs = [];
            const task = batch.exportBatchResourceLinks(page, button, {windowObject: win, documentObject: doc,
                pickerApi: picker, fetchImpl: async (_url, options) => {
                    const body = JSON.parse(options.body); calls.push(body);
                    if (calls.length === 1) await gate;
                    return {ok: true, json: async () => ({status: "success", item: serviceGrant(provider, body.candidate_id)})};
                }, copyText: async text => outputs.push(text), downloadTextFile: (_name, text) => outputs.push(text)});
            assert.equal(calls.length, 1);
            choose(first, 0); choose(second, 1);
            image.dataset.url = "https://cdn.example/changed.jpg";
            image.dataset.downloadFilename = "changed.jpg";
            release(); const result = await task;
            assert.deepEqual(calls, ["work-one-low", "work-two-high"].map(id =>
                ({candidate_id: id, resource_token: `signed-${id}`, action: "link"})));
            const expected = [
                {type: "video", url: "https://cdn.example/work-one-low.mp4", filename: "work-one-low.mp4"},
                {type: "video", url: "https://cdn.example/work-two-high.mp4", filename: "work-two-high.mp4"},
                {type: "video", url: "https://cdn.example/unrepresented.mp4", filename: "unrepresented.mp4"},
                {type: "image", url: "https://cdn.example/photo-one.jpg", filename: "photo-one.jpg"},
                {type: "image", url: "https://cdn.example/photo-two.png", filename: "photo-two.png"},
            ];
            assert.deepEqual(result.resources, expected);
            assert.deepEqual(result.skipped, []);
            assert.deepEqual(outputs, [expected.map(resource => mode === "aria2"
                ? `${resource.url}\n  out=${resource.filename}` : resource.url).join("\n")]);
            assert.doesNotMatch(outputs[0], /obsolete-main|changed|duplicate/);
            assert.equal(button.disabled, false);
        });
    }

    test(`mixed YT/service/legacy Batch ${mode} freezes provider, schema and selections before authorization`, async () => {
        const {doc, win} = fixture(); const page = el("main"); doc.body.append(page);
        const button = el("button", {"data-batch-export": mode}); page.append(button);
        const yt = newPicker(["yt-video", "yt-audio"]);
        const tt = servicePicker(SERVICE_PROVIDERS[0], ["tt-high", "tt-low"]);
        const kuku = servicePicker(SERVICE_PROVIDERS[1], ["kuku-video", "kuku-audio"]);
        const duplicate = servicePicker(SERVICE_PROVIDERS[0], ["tt-low"]);
        const expired = servicePicker(SERVICE_PROVIDERS[1], ["expired"]);
        page.append(card(legacy("image", "https://cdn.example/old.jpg", "old.jpg")), card(yt), card(tt), card(kuku),
            card(duplicate), card(expired), card(servicePicker(SERVICE_PROVIDERS[0], ["failed"]), "failed"));
        picker.attach(doc, win); choose(tt, 1); choose(kuku, 1);
        const snapshot = batch.collectBatchResourceLinks(page, picker);
        assert.equal(Object.isFrozen(snapshot[2].selection), true);
        const grants = {"yt-video": linkItem("yt-video"),
            "tt-low": serviceGrant(SERVICE_PROVIDERS[0], "tt-low"),
            "kuku-audio": serviceGrant(SERVICE_PROVIDERS[1], "kuku-audio", "audio", {
                url: "https://cdn.example/kuku-audio.webm", filename: "kuku-audio.webm"})};
        let release; const gate = new Promise(resolve => { release = resolve; });
        const calls = [], outputs = [];
        const task = batch.exportBatchResourceLinks(page, button, {windowObject: win, documentObject: doc, pickerApi: picker,
            fetchImpl: async (url, options) => {
                const body = JSON.parse(options.body); calls.push({url, body});
                if (calls.length === 1) await gate;
                return grants[body.candidate_id]
                    ? {ok: true, json: async () => ({status: "success", item: grants[body.candidate_id]})}
                    : {ok: false, status: 403, json: async () => ({status: "error", message: "expired service candidate"})};
            }, copyText: async text => outputs.push(text), downloadTextFile: (_name, text) => outputs.push(text)});
        assert.equal(calls.length, 1);
        choose(tt, 0); choose(kuku, 0); choose(yt, 1);
        tt.dataset.provider = SERVICE_PROVIDERS[1]; tt.dataset.selectionSchema = picker.SCHEMA;
        kuku.dataset.selectionUrl = "https://wrong.example/api";
        radios(kuku)[1].dataset.token = "changed";
        release(); const result = await task;
        assert.deepEqual(calls.map(c => c.body), ["yt-video", "tt-low", "kuku-audio", "tt-low", "expired"].map(id =>
            ({candidate_id: id, resource_token: `signed-${id}`, action: "link"})));
        assert.equal(calls[2].url, `https://product.example/media/service-selected-format/?fixture=${SERVICE_PROVIDERS[1]}`);
        const expected = [{type: "image", url: "https://cdn.example/old.jpg", filename: "old.jpg"},
            ...Object.values(grants).map(({type, url, filename}) => ({type, url, filename}))];
        assert.deepEqual(result.resources, expected);
        assert.deepEqual(result.skipped, ["expired service candidate"]);
        assert.equal(snapshot[2].selection.provider, SERVICE_PROVIDERS[0]);
        assert.equal(snapshot[2].selection.schema, SERVICE_SCHEMA);
        assert.deepEqual(outputs, [expected.map(r => mode === "aria2" ? `${r.url}\n  out=${r.filename}` : r.url).join("\n")]);
        assert.match(page.querySelector("[data-batch-export-status]").textContent, /跳过 1 项/);
        assert.equal(button.disabled, false);
    });
}

test("cached template radios have no shared name before async mounting", () => {
    const source = fs.readFileSync(path.join(__dirname, "../templates/partials/ytdlp_format_option.html"), "utf8");
    assert.doesNotMatch(source, /\bname\s*=/);
    assert.match(source, /data-can-check=/);
    assert.match(source, /data-picker-detail/);
});

function checkPicker() {
    const root = newPicker(["video", "other"]);
    radios(root).forEach(radio => {
        radio.dataset.canCheck = "true";
        radio.dataset.canPreview = "false";
        radio.dataset.limitation = "unknown tracks";
        radio.parentNode.append(el("small", {"data-picker-detail": ""}),
            el("small", {"data-picker-option-limitation": ""}), el("span", {"data-picker-option-size": ""}));
    });
    root.append(el("button", {"data-ytdlp-action": "download"}), el("button", {"data-ytdlp-action": "cancel"}),
        el("p", {"data-picker-limitation": ""}), el("span", {"data-picker-size": ""}));
    return root;
}
function click(root, action = "download") {
    root.listeners.click({target: root.querySelector(`[data-ytdlp-action="${action}"]`), preventDefault() {}});
}
function checkResult(id = "video", changes = {}, reason = "complete") {
    return {status: "success", candidate: {candidate_id: id, format_id: `format-${id}`,
        can_download: true, can_preview: true, can_check: false, limitation: "",
        detail_label: "MP4 / video + audio", size_label: "12 MB", ...changes},
        check: {reason, requests: 2, bytes_read: 4096, complete_moov: true, video: "present", audio: "present"}};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("check is opt-in, fires once while busy, then needs a second click for the same resource download", async () => {
    const {doc, win} = fixture(); const root = checkPicker(); doc.body.append(card(root));
    const calls = [], saves = []; let release;
    const gate = new Promise(resolve => { release = resolve; });
    picker.mount(root, doc, win, {ordinary: {download: async item => saves.push(item)},
        fetchImpl: async (_url, options) => {
            const body = JSON.parse(options.body); calls.push(body);
            if (body.action === "check") {
                await gate;
                return {ok: true, json: async () => checkResult()};
            }
            return {ok: true, json: async () => ({status: "success", item: linkItem("video", "video", {
                browser_upstream_bound: true, browser_direct_fetch_safe: true, requires_merge: false, requires_cookie: false})})};
        }});
    const button = root.querySelector('[data-ytdlp-action="download"]');
    assert.equal(button.textContent, "检查所选视频");
    assert.equal(button.disabled, false);
    const snapshot = batch.collectBatchResourceLinks(doc, picker);
    const selectionBeforeCheck = picker.selected(root);
    await settle();
    assert.equal(calls.length, 0, "mount and batch collection never probe formats");
    click(root); click(root);
    assert.equal(calls.length, 1);
    assert.equal(button.disabled, true);
    release(); await settle();
    assert.equal(saves.length, 0, "check never automatically downloads");
    assert.equal(button.disabled, false);
    assert.match(button.textContent, /下载所选视频/);
    assert.equal(picker.selected(root).canDownload, true);
    assert.equal(picker.selected(root).canPreview, true);
    assert.equal(root.querySelector("[data-picker-detail]").textContent, "MP4 / video + audio");
    assert.equal(root.querySelector("[data-picker-option-size]").textContent, "12 MB");
    assert.equal(root.querySelector("[data-picker-size]").textContent, "12 MB");
    assert.equal(root.querySelector("[data-picker-option-limitation]").hidden, true);
    assert.deepEqual(snapshot[0].selection, {id: "video", token: "signed-video", kind: "video", formatId: "format-video",
        provider: "yt_dlp_generic", schema: picker.SCHEMA});
    assert.equal(Object.isFrozen(snapshot[0].selection), true);
    assert.equal(selectionBeforeCheck.canDownload, false);
    assert.equal(selectionBeforeCheck.canCheck, true);
    click(root); await settle();
    assert.deepEqual(calls, [
        {candidate_id: "video", resource_token: "signed-video", action: "check"},
        {candidate_id: "video", resource_token: "signed-video", action: "download"},
    ]);
    assert.equal(saves.length, 1);
    assert.equal(saves[0].candidate_id, "video");
    assert.equal(saves[0].selected_format_id, "format-video");
});

test("downloadable selections skip checking, and restricted or audio selections never check", async () => {
    for (const state of ["downloadable", "restricted", "audio"]) {
        const {doc, win} = fixture(); const root = checkPicker(); doc.body.append(root);
        const radio = radios(root)[0];
        if (state === "downloadable") radio.dataset.canDownload = "true";
        if (state === "restricted") radio.dataset.canCheck = "false";
        if (state === "audio") radio.dataset.kind = "audio";
        const actions = [];
        picker.mount(root, doc, win, {ordinary: {download: async () => {}},
            fetchImpl: async (_url, options) => {
                actions.push(JSON.parse(options.body).action);
                return {ok: false, json: async () => ({status: "error", message: "download denied"})};
            }});
        await settle(); assert.deepEqual(actions, []);
        click(root); await settle();
        assert.deepEqual(actions, state === "downloadable" ? ["download"] : []);
    }
});

test("inconclusive or restricted checks show reasons without enabling retries or changing selection", async () => {
    for (const limitation of ["", "此格式需要合并音视频；当前选择器不将它当作完整文件下载。"]) {
        const {doc, win} = fixture(); const root = checkPicker(); doc.body.append(root);
        let calls = 0;
        picker.mount(root, doc, win, {ordinary: {download: () => assert.fail("no download")},
            fetchImpl: async () => {
                calls += 1;
                return {ok: true, json: async () => checkResult("video", {can_download: false, can_preview: false,
                    can_check: true, limitation, detail_label: "tracks unknown"}, "incomplete_moov")};
            }});
        click(root); await settle(); click(root); await settle();
        assert.equal(calls, 1);
        assert.equal(picker.selected(root).id, "video");
        assert.equal(picker.selected(root).token, "signed-video");
        assert.equal(picker.selected(root).canCheck, false);
        assert.equal(picker.selected(root).canDownload, false);
        assert.equal(root.querySelector('[data-ytdlp-action="download"]').disabled, true);
        const expected = limitation || "检查未确认可下载，未开始下载。";
        assert.equal(root.querySelector("[data-picker-status]").textContent, expected);
        assert.doesNotMatch(root.querySelector("[data-picker-status]").textContent, /incomplete_moov/);
        assert.equal(root.querySelector("[data-picker-limitation]").textContent, expected);
        assert.equal(root.querySelector("[data-picker-detail]").textContent, "tracks unknown");
    }
});

test("cancel stops local waiting and rejects late success even when fetch ignores abort", async () => {
    const {doc, win} = fixture(); const root = checkPicker(); doc.body.append(root);
    radios(root)[0].dataset.limitation = "尚未检查此格式的完整音视频，可检查所选视频后再保存。";
    let release, signal, calls = 0;
    const gate = new Promise(resolve => { release = resolve; });
    picker.mount(root, doc, win, {ordinary: {download: () => assert.fail("no late download")},
        fetchImpl: async (_url, options) => {
            calls += 1; signal = options.signal; await gate;
            return {ok: true, json: async () => checkResult()};
        }});
    click(root); click(root, "cancel");
    assert.equal(signal.aborted, true);
    assert.equal(radios(root)[0].disabled, false);
    const message = root.querySelector("[data-picker-status]").textContent;
    assert.match(message, /远端检查可能仍在进行/);
    assert.match(message, /本页不再重复检查此格式，请重新解析后再试/);
    assert.equal(picker.selected(root).canCheck, false);
    assert.equal(picker.selected(root).limitation, message);
    assert.equal(root.querySelector("[data-picker-limitation]").textContent, message);
    assert.equal(root.querySelector("[data-picker-option-limitation]").textContent, message);
    assert.equal(root.querySelector("[data-picker-option-limitation]").hidden, false);
    assert.equal(root.querySelector('[data-ytdlp-action="download"]').disabled, true);
    release(); await settle(); click(root); await settle();
    assert.equal(calls, 1);
    assert.equal(picker.selected(root).canDownload, false);
    assert.equal(root.querySelector("[data-picker-status]").textContent, message);
});

for (const change of ["selection", "away-and-back", "token", "format", "redraw", "redraw-before-scan", "detach", "destroy"]) {
    test(`late check cannot update another selection or card after ${change}`, async () => {
        const {doc, win, scan} = fixture(); const first = checkPicker(), second = checkPicker();
        doc.body.append(card(first), card(second));
        const pending = [];
        picker.attach(doc, win, {ordinary: {download: () => assert.fail("no automatic download")},
            fetchImpl: async () => new Promise(resolve => pending.push(resolve))});
        click(first);
        const original = radios(first)[0];
        if (change === "selection" || change === "away-and-back") choose(first, 1);
        if (change === "away-and-back") choose(first, 0);
        if (change === "token") original.dataset.token = "replacement-token";
        if (change === "format") original.dataset.formatId = "replacement-format";
        if (change.startsWith("redraw")) {
            first.replaceChildren(...checkPicker().children.slice());
            if (change === "redraw") scan();
        }
        if (change === "detach") first.remove();
        if (change === "destroy") picker.mount(first, doc, win).destroy();
        click(second);
        pending[1]({ok: true, json: async () => checkResult("video", {detail_label: "second card"})});
        await settle();
        pending[0]({ok: true, json: async () => checkResult("video", {detail_label: "stale first card"})});
        await settle();
        assert.equal(picker.selected(first).canDownload, false);
        assert.equal(first.querySelector("[data-picker-detail]").textContent, "");
        assert.equal(picker.selected(second).canDownload, true);
        assert.equal(second.querySelector("[data-picker-detail]").textContent, "second card");
    });
}

test("check HTTP errors keep denial reasons and never auto retry or enable download", async () => {
    for (const code of [403, 409, 429, 503]) {
        const {doc, win} = fixture(); const root = checkPicker(); doc.body.append(root);
        let calls = 0;
        picker.mount(root, doc, win, {fetchImpl: async () => {
            calls += 1;
            return {ok: false, status: code, json: async () => ({status: "error", message: `denied-${code}`})};
        }});
        click(root); await settle();
        assert.equal(calls, 1);
        assert.equal(picker.selected(root).canDownload, false);
        assert.equal(root.querySelector("[data-picker-status]").textContent, `denied-${code}`);
        assert.equal(radios(root)[0].disabled, false);
    }
});

test("independent cached copies survive delayed insertion, reinit, sorting and redraw", () => {
    const {doc, win, scan} = fixture();
    const first = newPicker(); doc.body.append(first); picker.attach(doc, win);
    choose(first, 1);
    const second = newPicker(); doc.body.append(second);
    assert.equal(picker.selected(first).id, "audio", "insertion must not uncheck mounted selection");
    scan();
    assert.notEqual(radios(first)[0].name, radios(second)[0].name);
    assert.equal(radios(first)[0].name, radios(first)[1].name);
    choose(second, 0);
    assert.equal(picker.selected(first).id, "audio");
    const controller = picker.mount(first, doc, win);
    assert.equal(picker.mount(first, doc, win), controller);
    first.insertBefore(radios(first)[1].parentNode, radios(first)[0].parentNode); scan();
    assert.equal(picker.selected(first).id, "audio");
    const fresh = newPicker(); first.replaceChildren(...fresh.children.slice()); scan();
    assert.equal(picker.selected(first).id, "audio", "redraw retains existing candidate identity");
    assert.equal(picker.selected(first).token, "signed-audio");
    const replacement = newPicker(["other"]); first.replaceChildren(...replacement.children.slice()); scan();
    assert.equal(picker.selected(first).id, "other", "removed selection falls back to available default");
});

test("layered duplicate cards reveal retained selections on reinit and redraw without rebinding", () => {
    const {doc, win, scan} = fixture();
    win.AnimeImagesOrdinaryMediaDownload = {download() { assert.fail("no download requested"); }};
    const first = layeredPicker(), second = layeredPicker();
    doc.body.append(card(first), card(second)); picker.attach(doc, win);
    const download = () => first.querySelector('[data-ytdlp-action="download"]');
    assert.equal(download().disabled, false);
    assert.equal(first.querySelector(".ytdlp-audio-formats").open, false);
    choose(first, 1);
    assert.equal(first.querySelector("[data-picker-other]").open, true);
    assert.equal(download().disabled, true);
    assert.equal(first.querySelector("[data-picker-limitation]").textContent, "requires merge");
    assert.equal(picker.selected(second).id, "video");
    choose(first, 3);
    const audio = first.querySelector(".ytdlp-audio-formats");
    assert.equal(audio.open, true);
    assert.equal(audio.querySelector("[data-picker-other]").open, true);
    assert.equal(download().disabled, false);
    assert.match(download().textContent, /音频/);
    audio.open = false; audio.querySelector("[data-picker-other]").open = false;
    picker.mount(first, doc, win);
    assert.equal(audio.open, true, "unchanged DOM reinit reveals the current selection");
    assert.equal(audio.querySelector("[data-picker-other]").open, true);
    const fresh = layeredPicker(); first.replaceChildren(...fresh.children.slice()); scan();
    assert.equal(picker.selected(first).id, "audio-alt");
    assert.equal(picker.selected(first).token, "signed-audio-alt");
    assert.equal(first.querySelector(".ytdlp-audio-formats").open, true);
    assert.equal(first.querySelector(".ytdlp-audio-formats").querySelector("[data-picker-other]").open, true);
    assert.match(download().textContent, /音频/);
    assert.equal(picker.selected(second).id, "video");
    assert.equal(second.querySelector(".ytdlp-audio-formats").open, false);
    assert.notEqual(radios(first)[0].name, radios(second)[0].name);
    assert.deepEqual(batch.collectBatchResourceLinks(doc, picker).map(entry =>
        [entry.selection.id, entry.selection.token]), [["audio-alt", "signed-audio-alt"], ["video", "signed-video"]]);
});

test("layered selections keep all three Batch export modes bound to each duplicate card", async () => {
    for (const mode of ["copy-links", "links", "aria2"]) {
        const {doc, win} = fixture(); const page = el("main"); doc.body.append(page);
        const button = el("button", {"data-batch-export": mode}); page.append(button);
        const first = layeredPicker(), second = layeredPicker();
        page.append(card(first), card(second)); picker.attach(doc, win);
        choose(first, 3); choose(second, 1);
        const calls = [], outputs = [];
        const result = await batch.exportBatchResourceLinks(page, button, {
            windowObject: win, documentObject: doc, pickerApi: picker,
            fetchImpl: async (_url, options) => {
                const body = JSON.parse(options.body); calls.push(body);
                return {ok: true, json: async () => ({status: "success", item: linkItem(body.candidate_id,
                    body.candidate_id === "audio-alt" ? "audio" : "video")})};
            },
            copyText: async text => outputs.push(text),
            downloadTextFile: (_name, text) => outputs.push(text),
        });
        assert.deepEqual(calls, [
            {candidate_id: "audio-alt", resource_token: "signed-audio-alt", action: "link"},
            {candidate_id: "restricted", resource_token: "signed-restricted", action: "link"},
        ]);
        assert.equal(result.resources.length, 2);
        assert.equal(result.skipped.length, 0);
        assert.equal(outputs.length, 1);
        assert.match(outputs[0], /audio-alt\.m4a/);
        assert.match(outputs[0], /restricted\.mp4/);
        if (mode === "aria2") assert.match(outputs[0], /out=audio-alt\.m4a/);
    }
});

test("Batch collects new selected video/audio without hidden raw URL fallback", async () => {
    const {doc, win} = fixture(); const current = newPicker(); doc.body.append(card(current));
    picker.mount(current, doc, win); choose(current, 1);
    current.append(technicalCandidate("https://cdn.example/wrong.mp4"));
    const snapshot = batch.collectBatchResourceLinks(doc, picker);
    assert.equal(snapshot.length, 1);
    assert.equal(snapshot[0].selection.id, "audio");
    assert.equal(snapshot[0].selection.token, "signed-audio");
    assert.equal(snapshot[0].url, undefined);
});

test("old named cached copies recover their default without modifying candidate/token", () => {
    const {doc, win} = fixture();
    const first = newPicker(["video", "audio"], "cached-work");
    const second = newPicker(["video", "audio"], "cached-work");
    doc.body.append(first, second);
    assert.equal(picker.selected(first), null, "browser radio behavior reproduces shared-name loss");
    picker.attach(doc, win);
    assert.equal(picker.selected(first).id, "video");
    assert.equal(picker.selected(second).id, "video");
    choose(first, 1);
    assert.equal(picker.selected(first).id, "audio");
    assert.equal(picker.selected(second).id, "video");
    assert.deepEqual(radios(first).map(radio => [radio.value, radio.dataset.token]),
        [["video", "signed-video"], ["audio", "signed-audio"]]);
});

for (const mode of ["copy-links", "links", "aria2"]) {
    test(`Batch ${mode} snapshots mixed DOM, authorizes every picker, keeps order and filenames`, async () => {
        const {doc, win} = fixture();
        const page = el("main"); doc.body.append(page);
        const button = el("button", {"data-batch-export": mode}); page.append(button);
        const oldItems = el("div");
        const oldVideo = legacy("video", "https://cdn.example/old.mp4", "old.mp4");
        oldItems.append(legacy("image", "https://cdn.example/a.jpg", "image 1.jpg"), oldVideo,
            legacy("video", "https://cdn.example/old.mp4", "duplicate.mp4"));
        const video = newPicker(), audio = newPicker(), dash = newPicker(["dash"]);
        const duplicate = newPicker(), expired = newPicker(["expired"]), denied = newPicker(["denied"]);
        page.append(card(oldItems), card(video), card(audio), card(dash), card(duplicate), card(expired), card(denied),
            card(newPicker(["failed"]), "failed"));
        picker.attach(doc, win); choose(audio, 1);
        expired.append(technicalCandidate("https://cdn.example/raw-bypass.mp4"));
        const grants = {
            video: linkItem("video", "video", {url: "http://cdn.example/movie.m3u8?sig=exact%2Fvalue", filename: "movie.m3u8"}),
            audio: linkItem("audio", "audio", {url: "https://cdn.example/voice.webm", filename: "voice.webm"}),
            dash: linkItem("dash", "video", {url: "https://cdn.example/source.mpd", filename: "source.mpd"}),
        };
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        const calls = [], outputs = [];
        const task = batch.exportBatchResourceLinks(page, button, {windowObject: win, documentObject: doc,
            pickerApi: picker,
            fetchImpl: async (url, options) => {
                const body = JSON.parse(options.body); calls.push(body);
                assert.equal(url, "https://product.example/media/ytdlp-selected-format/");
                assert.equal(body.action, "link");
                assert.equal(body.resource_token, `signed-${body.candidate_id}`);
                assert.equal(options.credentials, "same-origin");
                assert.equal(options.cache, "no-store");
                if (calls.length === 1) await gate;
                const message = body.candidate_id === "expired" ? "授权已过期，请重新解析。" : "当前访问受限。";
                return grants[body.candidate_id]
                    ? {ok: true, json: async () => ({status: "success", item: grants[body.candidate_id]})}
                    : {ok: false, status: 403, json: async () => ({status: "error", message})};
            },
            copyText: async text => outputs.push({name: "clipboard", text}),
            downloadTextFile: (name, text) => outputs.push({name, text}),
        });
        assert.equal(button.disabled, true);
        assert.equal(calls.length, 1);
        choose(video, 1); choose(audio, 0);
        radios(audio)[1].dataset.token = "changed-after-click";
        audio.dataset.selectionUrl = "https://evil.example/endpoint";
        oldVideo.dataset.downloadFilename = "changed-after-click.mp4";
        oldVideo.dataset.url = "https://cdn.example/changed.mp4";
        release();
        const result = await task;
        assert.equal(button.disabled, false);
        assert.deepEqual(calls.map(call => call.candidate_id), ["video", "audio", "dash", "video", "expired", "denied"]);
        const expected = [
            {type: "video", url: "https://cdn.example/old.mp4", filename: "old.mp4"},
            {type: "image", url: "https://cdn.example/a.jpg", filename: "image 1.jpg"},
            ...[grants.video, grants.audio, grants.dash].map(({type, url, filename}) => ({type, url, filename})),
        ];
        assert.deepEqual(result.resources, expected);
        assert.equal(result.skipped.length, 2);
        const expectedText = mode === "aria2"
            ? expected.map(item => `${item.url}\n  out=${item.filename}`).join("\n")
            : expected.map(item => item.url).join("\n");
        assert.deepEqual(outputs, [{name: mode === "copy-links" ? "clipboard" : `${mode}.txt`, text: expectedText}]);
        const message = page.querySelector("[data-batch-export-status]").textContent;
        assert.match(message, /跳过 2 项/);
        assert.match(message, /授权已过期/);
        assert.match(message, /当前访问受限/);
        assert.doesNotMatch(expectedText, /raw-bypass|failed|changed-after-click/);
    });
}

test("all expired or missing selections give persistent skipped counts and produce no empty export", async () => {
    const {doc, win} = fixture(); const page = el("main"); doc.body.append(page);
    const button = el("button", {"data-batch-export": "copy-links"}); page.append(button);
    const empty = newPicker([]), expired = newPicker(["expired"]);
    page.append(card(empty), card(expired)); picker.attach(doc, win);
    let requests = 0;
    const result = await batch.exportBatchResourceLinks(page, button, {windowObject: win, documentObject: doc,
        pickerApi: picker, fetchImpl: async () => { requests += 1; return {ok: false, status: 403,
            json: async () => ({status: "error", message: "授权已过期"})}; },
        copyText: () => assert.fail("no empty clipboard overwrite"),
        downloadTextFile: () => assert.fail("no empty export"),
    });
    assert.equal(requests, 1);
    assert.equal(result.skipped.length, 2);
    assert.equal(result.resources.length, 0);
    assert.match(page.querySelector("[data-batch-export-status]").textContent, /可用 0 项，跳过 2 项/);
});

test("missing picker API never exports a new card's raw URL", async () => {
    const {doc} = fixture(); const current = newPicker(); doc.body.append(card(current));
    current.append(technicalCandidate("https://cdn.example/unsafe-fallback.mp4"));
    const result = await batch.resolveBatchResourceLinks(batch.collectBatchResourceLinks(doc), null, {});
    assert.deepEqual(result.resources, []);
    assert.equal(result.skipped.length, 1);
    assert.match(result.skipped[0], /核验不可用/);
});

test("single-card copy uses the same fresh link action even when download is unavailable", async () => {
    const {doc, win} = fixture(); const current = newPicker(); doc.body.append(current);
    const copied = [], requests = []; let release;
    const gate = new Promise(resolve => { release = resolve; });
    win.navigator = {clipboard: {writeText: async value => copied.push(value)}};
    picker.mount(current, doc, win, {fetchImpl: async (_url, options) => {
        requests.push(JSON.parse(options.body)); await gate;
        return {ok: true, json: async () => ({status: "success", item: linkItem("audio", "audio", {
            url: "http://cdn.example/voice.webm", filename: "voice.webm"})})};
    }});
    choose(current, 1);
    const button = current.querySelector('[data-ytdlp-action="copy"]');
    assert.equal(button.disabled, false);
    current.listeners.click({target: button, preventDefault() {}});
    assert.equal(button.disabled, true);
    radios(current)[1].dataset.token = "changed";
    radios(current)[0].checked = true;
    release(); await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(requests, [{candidate_id: "audio", resource_token: "signed-audio", action: "link"}]);
    assert.deepEqual(copied, ["http://cdn.example/voice.webm"]);
    assert.match(current.querySelector("[data-picker-status]").textContent, /已复制/);
    assert.equal(button.disabled, false);
});

test("single-card expired copy preserves the denial reason without copying raw data", async () => {
    const {doc, win} = fixture(); const current = newPicker(); doc.body.append(current);
    win.navigator = {clipboard: {writeText: () => assert.fail("denied copy")}};
    picker.mount(current, doc, win, {fetchImpl: async () => ({ok: false, status: 403,
        json: async () => ({status: "error", message: "授权已过期，请重新解析。"})})});
    const button = current.querySelector('[data-ytdlp-action="copy"]');
    current.listeners.click({target: button, preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(current.querySelector("[data-picker-status]").textContent, /授权已过期/);
    assert.equal(button.disabled, false);
});
