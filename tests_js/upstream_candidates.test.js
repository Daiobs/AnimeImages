"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {Node} = require("./helpers/lab_dom");
const {act, permittedUrl, pageCandidates} = require("../static/js/upstream_candidates");

function fixture(action, response = {status: "success", url: "https://cdn.example.org/media?access=keep&shareKey=exact"}, ok = true) {
    const section = new Node("section", {"data-upstream-candidates": "", "data-candidate-url": "/media/upstream-candidate/"});
    const row = new Node("article", {"data-upstream-candidate": "", "data-candidate-id": "c1", "data-resource-token": "t1", "data-url": "https://untrusted.example/fallback"});
    const button = new Node("button", {"data-candidate-action": action});
    const status = new Node("span", {"data-candidate-status": ""});
    section.append(row); row.append(button, status);
    const calls = [], copied = [], opened = [];
    const popup = {opener: {}, document: {createElement: () => ({}), head: {appendChild() {}}},
        location: {replace: (url) => opened.push(url)}, close() { this.closed = true; }};
    const options = {csrf: "csrf", navigator: {clipboard: {writeText: async (url) => copied.push(url)}},
        window: {location: {origin: "http://localhost:8003"}, open: (...args) => { calls.push({open: args}); return popup; }},
        fetch: async (url, init) => { calls.push({url, init}); return {ok, json: async () => response}; }};
    return {section, row, button, status, calls, copied, opened, popup, options};
}

test("generic candidate copy obtains a fresh capability and never fetches media or counts saves", async () => {
    const f = fixture("copy");
    await act(f.button, f.options); await act(f.button, f.options);
    assert.equal(f.calls.length, 2);
    for (const call of f.calls) {
        assert.equal(call.url, "http://localhost:8003/media/upstream-candidate/");
        assert.equal(call.init.method, "POST");
        assert.deepEqual(JSON.parse(call.init.body), {resource_token: "t1", candidate_id: "c1"});
        assert.equal(call.init.headers["X-CSRFToken"], "csrf");
    }
    assert.deepEqual(f.copied, Array(2).fill("https://cdn.example.org/media?access=keep&shareKey=exact"));
    assert.match(f.status.textContent, /未计为下载/);
    assert.equal(f.button.disabled, false);
});

test("HTTPS open awaits permission, preserves source params, and isolates the new tab", async () => {
    const f = fixture("open");
    let release;
    f.options.fetch = () => new Promise((resolve) => { release = resolve; });
    const run = act(f.button, f.options);
    assert.deepEqual(f.opened, []);
    assert.equal(f.popup.opener, null);
    assert.equal(f.button.disabled, true);
    release({ok: true, json: async () => ({status: "success", url: "https://cdn.example.org/a?sn=exact"})});
    await run;
    assert.deepEqual(f.opened, ["https://cdn.example.org/a?sn=exact"]);
    assert.match(f.status.textContent, /不保证浏览器可读取/);
});

test("HTTP is copy-only and is never silently upgraded or opened", async () => {
    const payload = {status: "success", url: "http://cdn.example.org/a?sn=exact"};
    const copy = fixture("copy", payload), open = fixture("open", payload);
    await act(copy.button, copy.options); await act(open.button, open.options);
    assert.deepEqual(copy.copied, [payload.url]);
    assert.match(copy.status.textContent, /HTTP/);
    assert.deepEqual(open.opened, []);
    assert.equal(open.popup.closed, true);
    assert.match(open.status.textContent, /协议未被改写/);
});

test("denial and expired descriptors never fall back to DOM URL and always restore controls", async () => {
    for (const response of [{status: "error"}, {status: "success", url: "javascript:alert(1)"}]) {
        const f = fixture("open", response);
        await act(f.button, f.options);
        assert.deepEqual(f.opened, []); assert.deepEqual(f.copied, []);
        assert.equal(f.popup.closed, true); assert.equal(f.button.disabled, false);
    }
    const f = fixture("copy", {status: "success", url: "https://cdn.example/a"}, false);
    await act(f.button, f.options); assert.deepEqual(f.copied, []);
});

test("candidate protocol checks reject malformed URLs without excluding server-validated public IPs", () => {
    for (const url of ["javascript:alert(1)", "data:image/png,x", "https://user:pass@example.org/a", "https://cdn.example.org:8443/a", "https://cdn.example.org/a b", "https://cdn.example.org/\\evil"]) {
        assert.throws(() => permittedUrl(url, true));
    }
    assert.equal(permittedUrl("https://8.8.8.8/a", true), "https://8.8.8.8/a");
});

test("single-flight candidate operation cannot duplicate popup or request while awaiting API", async () => {
    const f = fixture("open"); let release; let count = 0;
    f.options.fetch = () => { count += 1; return new Promise((resolve) => { release = resolve; }); };
    const run = act(f.button, f.options); await act(f.button, f.options);
    assert.equal(count, 1); assert.equal(f.calls.length, 1);
    release({ok: false}); await run;
});

test("ordinary candidate paging reveals tail quality without rebinding identity or sorting images", () => {
    const section = new Node("section", {"data-upstream-candidates": ""});
    const list = new Node("div", {"class": "upstream-candidate-list"}); section.append(list);
    section.append(new Node("span", {"data-candidate-counts": ""}), new Node("button", {"data-candidates-expand": ""}));
    const rows = Array.from({length: 130}, (_, i) => new Node("article", {"data-upstream-candidate": "",
        "data-candidate-id": `c${i}`, "data-media-type": i === 0 || i === 2 ? "image" : "video",
        "data-width": i === 129 ? "" : "3840", "data-height": i === 129 ? "2160" : "720"}));
    list.append(...rows);
    pageCandidates(section, "source");
    assert.equal(rows.filter((row) => !row.hidden).length, 64);
    pageCandidates(section, "resolution");
    assert.equal(list.children[0], rows[0]); assert.equal(list.children[2], rows[2]);
    assert.equal(list.children[1], rows[129]); assert.equal(rows[129].dataset.candidateId, "c129");
    pageCandidates(section, null, true);
    assert.equal(rows.filter((row) => !row.hidden).length, 128);
    pageCandidates(section, "source");
    assert.deepEqual(list.children, rows);
});
