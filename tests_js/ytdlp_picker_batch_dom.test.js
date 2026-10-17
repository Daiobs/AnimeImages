"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
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
function linkItem(id, type = "video", extra = {}) {
    return {candidate_id: id, selected_format_id: `format-${id}`, url: `https://cdn.example/${id}.${type === "audio" ? "m4a" : "mp4"}`,
        type, filename: `${id}.${type === "audio" ? "m4a" : "mp4"}`, selection_schema: picker.SCHEMA,
        provider: "yt_dlp_generic", ...extra};
}

test("cached template radios have no shared name before async mounting", () => {
    const source = fs.readFileSync(path.join(__dirname, "../templates/partials/ytdlp_format_option.html"), "utf8");
    assert.doesNotMatch(source, /\bname\s*=/);
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
    current.append(legacy("video", "https://cdn.example/wrong.mp4", "wrong.mp4"));
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
        expired.append(legacy("video", "https://cdn.example/raw-bypass.mp4", "raw-bypass.mp4"));
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
    current.append(legacy("video", "https://cdn.example/unsafe-fallback.mp4", "unsafe.mp4"));
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
