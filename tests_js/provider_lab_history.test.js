"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");
const {Node, documentFor} = require("./helpers/lab_dom.js");

function fixture(overrides = {}) {
    const root = new Node("main");
    const panel = new Node("section", {"data-lab-history": ""}); root.append(panel);
    for (const name of ["batches", "executions", "status", "heading", "host", "order", "state"]) {
        panel.append(new Node(["host", "order", "state"].includes(name) ? "select" : "div", {[`data-history-${name}`]: ""}));
    }
    panel.append(new Node("button", {"data-history-host-all": ""}), new Node("button", {"data-history-back-batch": ""}));
    const batch = {id: 1, label: "<img src=x onerror=alert(1)>", created_at: "2026-09-01", sync_status: "stored",
        metadata: {version: "old", planned_at: "2026-08-20"}};
    const executions = [{id: 10, sample_key: "sample", source_url: "https://source.example/work", source_host: "source.example",
        provider_id: "A", can_retest: true, state: "success", version: {animeimages: "old-head"}, options: {},
        result: {status: "success", assets: Array.from({length: 130}, (_, i) => ({asset_id: `asset${i}`,
            media_type: "video", height: i === 129 ? 2160 : 720, resource_token: "untrusted-token", url: "https://media.invalid/file"})),
        metadata: {retained_count: 130, returned_count: 200, truncated: true, truncated_count: 70}}},
    {id: 11, sample_key: "sample", source_url: "https://source.example/work", source_host: "source.example",
        provider_id: "removed", state: "sent", can_retest: true, route_selectable: true, result: {assets: []}},
    {id: 12, sample_key: "missing", provider_id: "A", state: "failed", source_status: "sensitive", source_state: "not_retained", can_retest: true, result: {}}];
    const calls = [], selections = [], drafts = [], retests = [];
    const doc = documentFor(root);
    const workspace = lab.createHistoryWorkspace(doc, {root, url: "/admin/provider-lab/history/",
        descriptors: [{provider_id: "A", available: true, route_selectable: true}],
        window: {location: {origin: "http://local.example"}, confirm: () => assert.fail("History must use inline confirmation")},
        fetch: async (url, options) => { calls.push({url, options}); return {ok: true,
            json: async () => new URL(url).searchParams.has("source_host")
                ? {ok: true, source_host: new URL(url).searchParams.get("source_host"), total: executions.length,
                    returned: executions.length, truncated: false, executions: executions.map((e) => ({...e, batch: e.batch || batch})), ...overrides.hostPayload}
                : new URL(url).searchParams.has("batch_id") ? {ok: true, batch, executions} : {ok: true, batches: [batch]}}; },
        onSelection: (selection) => selections.push(selection),
        routeEditor: {invalidate() {}, async load() {}, selectProvider: (...args) => drafts.push(args)},
        retest: async (execution) => { retests.push(execution); return {status: "success", executed: true, history: {saved: false, error_code: "SAVE_FAILED"}, assets: []}; },
        ...overrides});
    const click = async (target) => {
        assert.ok(target, "Expected a rendered click target");
        return panel.listeners.click({target, preventDefault() {}});
    };
    const inspect = async (id) => {
        await click(panel.querySelector(`[data-history-inspect="${id}"]`));
        const cards = panel.querySelectorAll("[data-history-execution]");
        assert.equal(cards.length, 1, "Only the selected execution detail is rendered");
        assert.equal(cards[0].dataset.historyExecution, String(id));
        return cards[0];
    };
    const change = async (target, value) => {
        assert.ok(target, "Expected a rendered change target");
        target.value = String(value);
        return panel.listeners.change({target});
    };
    return {root, panel, doc, workspace, calls, selections, drafts, retests, click, inspect, change, executions};
}

function routeHistoryFixture(read, canConfigure = true) {
    const bridge = {};
    const descriptors = [
        {provider_id: "parsehub_repo", execution_provider_id: "parsehub_generic", label: "ParseHub"},
        {provider_id: "yt_dlp_existing", execution_provider_id: "yt_dlp_generic", label: "yt-dlp"},
        {provider_id: "gallery_dl_existing", execution_provider_id: "gallery_dl_generic", label: "gallery-dl"},
        {provider_id: "videodl_repo", execution_provider_id: "videodl_repo", label: "videodl"},
    ].map((item) => ({...item, available: true, route_selectable: true}));
    const f = fixture({routeEditor: bridge, descriptors});
    const routePanel = new Node("section", {"data-lab-route-editor": "", "data-can-configure": String(canConfigure)});
    for (const name of ["status", "scope", "current", "order", "add", "confirm", "save", "reset", "fields", "reference", "confirmation-summary", "videodl"]) {
        routePanel.append(new Node("div", {[`data-route-${name}`]: ""}));
    }
    for (const name of ["max_height", "preferred_video_codec", "preferred_container", "route_videodl_mode", "route_videodl_backend"]) {
        routePanel.append(new Node("input", {name}));
    }
    f.root.append(routePanel);
    const routeCalls = [];
    const snapshot = (host, revision = 3) => ({source_host: host, applicable: true, active: true, revision,
        provider_order: ["yt_dlp_generic", "gallery_dl_generic"], effective_provider_order: ["yt_dlp_generic", "gallery_dl_generic"],
        provider_options: {}, format_preferences: {}, allowed_provider_ids: descriptors.map((item) => item.execution_provider_id)});
    const editor = lab.createRouteEditor(f.doc, {descriptors, url: "/provider-lab/routes/", csrf: () => "csrf",
        window: {location: {origin: "http://local.example"}}, getSource: () => f.workspace.source,
        onChange: () => f.workspace.refreshCurrentRoute(),
        fetch: async (url, options) => {
            assert.equal(options.method, undefined, "Draft actions must never save or execute");
            assert.equal(new URL(url).pathname, "/provider-lab/routes/");
            routeCalls.push({url, options});
            if (read) return read(url, options);
            const host = new URL(new URL(url).searchParams.get("source_url")).hostname;
            return {ok: true, json: async () => ({ok: true, route: snapshot(host)})};
        }});
    for (const name of ["load", "invalidate", "selectProvider", "mount", "restore"]) bridge[name] = editor[name];
    for (const name of ["route", "currentSummary"]) Object.defineProperty(bridge, name, {get: () => editor[name]});
    const seed = {...f.executions[0], state: "returned", result: {status: "success", assets: []}};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, provider_id: "parsehub_repo"},
        {...seed, id: 11, provider_id: "videodl_repo", source_url: "https://source.example/another", options: {mode: "dedicated_only"}},
        {...seed, id: 12, provider_id: "parsehub_repo", sample_key: "other", source_host: "other.example", source_url: "https://other.example/work"});
    return {...f, routePanel, editor, routeCalls, snapshot,
        draftOrder: () => routePanel.querySelector("[data-route-order]").querySelectorAll('[data-route-move="remove"]').map((node) => node.dataset.providerId)};
}

test("history route draft survives same-host observations, deduplicates fallbacks and restores the editor", async () => {
    const f = routeHistoryFixture();
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    const home = f.routePanel.parentNode;
    const homeIndex = home.children.indexOf(f.routePanel);
    assert.equal(f.panel.querySelector("[data-history-current-route]").textContent, "当前路线：尚未选择站点");
    await f.inspect(10);
    assert.equal(f.panel.querySelector('[data-history-execution="10"]').children.find((node) => node.tagName === "H4").textContent, "ParseHub");
    assert.equal(f.panel.querySelector("[data-history-current-route]").textContent, "source.example · 当前人工路线：yt-dlp → gallery-dl · revision 3");
    assert.ok(f.routePanel.closest("[data-history-sample]"));
    assert.match(f.routePanel.querySelector("[data-route-current]").textContent, /yt-dlp → gallery-dl.*revision 3/);
    await f.click(f.panel.querySelector('[data-history-route="primary"]'));
    assert.deepEqual(f.draftOrder(), ["parsehub_generic", "yt_dlp_generic", "gallery_dl_generic"]);
    assert.doesNotMatch(f.panel.querySelector("[data-history-current-route]").textContent, /ParseHub/);
    assert.equal(f.routePanel.querySelector("[data-route-videodl]").hidden, true);
    await f.inspect(11);
    assert.deepEqual(f.draftOrder(), ["parsehub_generic", "yt_dlp_generic", "gallery_dl_generic"]);
    await f.click(f.panel.querySelector('[data-history-route="fallback"]'));
    await f.click(f.panel.querySelector('[data-history-route="fallback"]'));
    assert.deepEqual(f.draftOrder(), ["parsehub_generic", "yt_dlp_generic", "gallery_dl_generic", "videodl_repo"]);
    assert.equal(f.routePanel.querySelector("[data-route-videodl]").hidden, false);
    assert.match(f.routePanel.querySelector("[data-route-confirmation-summary]").textContent, /source.example.*revision 3.*拟固定：ParseHub/);
    assert.match(f.routePanel.querySelector("[data-route-reference]").textContent, /old-head/);
    assert.equal(f.routeCalls.length, 1, "Same-host browsing preserves the unsaved draft without reloading it");
    await f.inspect(10);
    await f.click(f.panel.querySelector('[data-history-route="primary"]'));
    assert.deepEqual(f.draftOrder(), ["parsehub_generic", "yt_dlp_generic", "gallery_dl_generic", "videodl_repo"]);
    await f.inspect(12);
    assert.deepEqual(f.draftOrder(), ["yt_dlp_generic", "gallery_dl_generic"]);
    assert.equal(f.editor.route.source_host, "other.example");
    assert.match(f.panel.querySelector("[data-history-current-route]").textContent, /other.example/);
    assert.doesNotMatch(f.panel.querySelector("[data-history-current-route]").textContent, /source.example/);
    assert.equal(f.routePanel.querySelector("[data-route-reference]").textContent, "");
    assert.equal(f.routePanel.querySelector("[data-route-videodl]").hidden, true);
    assert.equal(f.routeCalls.length, 2);
    f.workspace.activate(false);
    assert.equal(f.routePanel.parentNode === home, true);
    assert.equal(home.children.indexOf(f.routePanel), homeIndex);
    assert.equal(f.root.querySelectorAll("[data-lab-route-editor]").length, 1);
    assert.equal(f.retests.length, 0);
    assert.ok(f.calls.every(({options}) => !options.method));
    f.workspace.destroy();
});

test("late history route GET cannot overwrite a new host and default order is shown from the server", async () => {
    const pending = [];
    const f = routeHistoryFixture((url) => new Promise((resolve) => pending.push({url, resolve})));
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    const first = f.click(f.panel.querySelector('[data-history-inspect="10"]'));
    const second = f.click(f.panel.querySelector('[data-history-inspect="12"]'));
    assert.equal(pending.length, 2);
    const current = {...f.snapshot("other.example", 8), active: false, provider_order: [],
        effective_provider_order: ["gallery_dl_generic", "parsehub_generic"]};
    pending[1].resolve({ok: true, json: async () => ({ok: true, route: current})}); await second;
    pending[0].resolve({ok: true, json: async () => ({ok: true, route: f.snapshot("source.example")})}); await first;
    assert.equal(f.editor.route.source_host, "other.example");
    assert.equal(f.editor.route.revision, 8);
    assert.match(f.panel.querySelector("[data-history-current-route]").textContent, /other.example.*当前默认路线：gallery-dl → ParseHub.*revision 8/);
    assert.deepEqual(f.draftOrder(), []);
    assert.match(f.routePanel.querySelector("[data-route-current]").textContent, /当前默认路线：gallery-dl → ParseHub.*revision 8/);
    assert.equal(f.routePanel.closest("[data-history-sample]").querySelector("[data-history-execution]").dataset.historyExecution, "12");
    assert.equal(f.retests.length, 0);
    assert.equal(f.routeCalls.length, 2);
    f.workspace.destroy();
});

test("explicit host browsing preserves provenance, exact source groups and the batch return entry", async () => {
    const f = fixture();
    const seed = {...f.executions[0], result: {status: "success", assets: []}};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, sample_key: "one", batch: {id: 1, label: "first", origin: "lab"}},
        {...seed, id: 20, sample_key: "different-key", batch: {id: 2, label: "second", origin: "scout"}},
        {...seed, id: 30, sample_key: "one", source_url: "https://source.example/other", batch: {id: 2}},
        {...seed, id: 40, source_url: "", sample_key: "missing", batch: {id: 1}},
        {...seed, id: 50, source_url: "", sample_key: "missing", batch: {id: 2}});
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    const all = f.panel.querySelector("[data-history-host-all]");
    assert.equal(all.hidden, true);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    assert.equal(all.hidden, false);
    const count = f.calls.length;
    await f.click(all);
    assert.equal(f.calls.length, count + 1);
    assert.deepEqual([...new URL(f.calls.at(-1).url).searchParams], [["source_host", "source.example"]]);
    assert.match(f.panel.querySelector("[data-history-query-scope]").textContent, /全部批次.*5 \/ 5.*未截断/);
    assert.equal(f.panel.querySelector("tbody").querySelectorAll("tr").length, 4);
    const card = await f.inspect(20);
    assert.ok(card.querySelectorAll("p").some((node) => /参考批次：second/.test(node.textContent)));
    assert.ok(card.querySelectorAll("[data-history-origin]").some((node) => node.dataset.historyOrigin === "scout"));
    const back = f.panel.querySelector("[data-history-back-batch]");
    assert.equal(back.hidden, false);
    await f.click(back);
    assert.equal(new URL(f.calls.at(-1).url).searchParams.get("batch_id"), "1");
    assert.equal(f.panel.querySelector("[data-history-query-scope]"), null);
    assert.equal(f.panel.querySelector("[data-history-host]").disabled, false);
    assert.ok(f.calls.every(({options}) => !options.method));
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("cross-batch detail and observation labels identify each batch despite identical times and differing sources", async () => {
    const f = fixture();
    const seed = {...f.executions[0], started_at: "2026-10-16T12:00:00Z", result: {status: "success", assets: []}};
    const first = {id: 1, label: "First capture", batch_key: "lab:first", origin: "lab"};
    const second = {id: 2, label: "Second capture", batch_key: "scout:second", origin: "scout"};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, batch: first},
        {...seed, id: 20, batch: second},
        {...seed, id: 30, source_url: "https://source.example/other", batch: second},
        {...seed, id: 40, batch: {}},
        {...seed, id: 50, batch: {batch_key: "scout:key-only"}});
    await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    await f.click(f.panel.querySelector("[data-history-host-all]"));
    let card = await f.inspect(50);
    await f.change(card.querySelector("[data-history-observation]"), 20);
    card = f.panel.querySelector('[data-history-execution="20"]');
    const badge = () => card.querySelector("[data-history-execution-batch]");
    assert.equal(badge().textContent, "所属批次：Second capture（scout:second）");
    assert.equal(badge().closest("details"), null, "Batch must be visible without expanding details");
    const chooser = card.querySelector("[data-history-observation]");
    const labels = Object.fromEntries(chooser.querySelectorAll("option").map((option) => [option.value, option.textContent]));
    assert.match(labels[10], /First capture（lab:first）/);
    assert.match(labels[20], /Second capture（scout:second）/);
    assert.match(labels[40], /批次未记录/);
    assert.doesNotMatch(labels[40], /First capture|Second capture/);
    assert.match(labels[50], /scout:key-only/);
    assert.equal(labels[30], undefined, "Different source URLs remain separate works");
    await f.change(chooser, 10);
    card = f.panel.querySelector('[data-history-execution="10"]');
    assert.equal(badge().textContent, "所属批次：First capture（lab:first）");
    card = await f.inspect(30);
    assert.equal(badge().textContent, "所属批次：Second capture（scout:second）");
    card = await f.inspect(50);
    await f.change(card.querySelector("[data-history-observation]"), 40);
    card = f.panel.querySelector('[data-history-execution="40"]');
    assert.equal(badge().textContent, "所属批次：批次未记录");
    assert.ok(f.calls.every(({options}) => !options.method));
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("host references deduplicate application copies but retain retests and truncated provenance", async () => {
    const f = fixture({hostPayload: {total: 201, returned: 3, truncated: true}});
    const seed = {...f.executions[0], state: "returned", result: {status: "success", assets: [{media_type: "video"}]}};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, origin: "lab"},
        {...seed, id: 20, origin: "scout", application_record_id: 10},
        {...seed, id: 30, origin: "lab", replay_of: "10"});
    await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    await f.click(f.panel.querySelector("[data-history-host-all]"));
    assert.match(f.panel.querySelector("[data-history-query-scope]").textContent, /3 \/ 201.*已截断，证据不全/);
    const line = f.panel.querySelector("[data-history-route-reference]").querySelector("li").textContent;
    assert.match(line, /2 条，记录 #10、#20、#30/);
    assert.doesNotMatch(line, /唯一已验证/);
    const card = await f.inspect(30);
    assert.deepEqual(card.querySelector("[data-history-observation]").querySelectorAll("option").map((node) => node.value).sort(), ["10", "20", "30"]);
    f.workspace.destroy();
});

test("switching to host clears pending retest and prepares only the selected historical draft", async () => {
    const f = routeHistoryFixture();
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    await f.inspect(10);
    await f.click(f.panel.querySelector("[data-history-retest]"));
    const stale = f.panel.querySelector("[data-history-confirm]");
    assert.ok(stale);
    await f.click(f.panel.querySelector("[data-history-host-all]"));
    assert.equal(f.panel.querySelector("[data-history-confirm]"), null);
    await f.click(stale);
    await f.inspect(11);
    await f.click(f.panel.querySelector('[data-history-route="primary"]'));
    assert.equal(f.workspace.source, "https://source.example/another");
    assert.deepEqual(f.draftOrder(), ["videodl_repo", "yt_dlp_generic", "gallery_dl_generic"]);
    const cancel = new Node("button", {"data-route-cancel": ""}); f.routePanel.append(cancel);
    f.routePanel.listeners.click({target: cancel});
    assert.deepEqual(f.draftOrder(), ["yt_dlp_generic", "gallery_dl_generic"]);
    assert.equal(f.retests.length, 0);
    assert.ok([...f.calls, ...f.routeCalls].every(({options}) => !options.method));
    f.workspace.destroy();
});

test("readonly staff can inspect host evidence without configuring routes", async () => {
    const f = routeHistoryFixture(undefined, false);
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    await f.click(f.panel.querySelector("[data-history-host-all]"));
    await f.inspect(10);
    await f.click(f.panel.querySelector('[data-history-route="primary"]'));
    assert.deepEqual(f.draftOrder(), ["yt_dlp_generic", "gallery_dl_generic"]);
    assert.equal(f.routePanel.querySelector("[data-route-save]").disabled, true);
    assert.ok([...f.calls, ...f.routeCalls].every(({options}) => !options.method));
    f.workspace.destroy();
});

test("late host response cannot replace a returned batch and failed host reads remain retryable", async () => {
    let resolveHost;
    const f = fixture({fetch: async (url) => ({ok: true, json: async () => {
        if (new URL(url).searchParams.has("source_host")) return new Promise((resolve) => { resolveHost = resolve; });
        return {ok: true, batch: {id: 1, label: "original"}, executions: f.executions};
    }})});
    await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const reading = f.click(f.panel.querySelector("[data-history-host-all]"));
    await new Promise((resolve) => setImmediate(resolve));
    await f.click(f.panel.querySelector("[data-history-back-batch]"));
    resolveHost({ok: true, source_host: "source.example", total: 0, returned: 0, truncated: false, executions: []});
    await reading;
    assert.equal(f.panel.querySelector("[data-history-heading]").textContent, "original");
    assert.equal(f.panel.querySelector("[data-history-query-scope]"), null);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const failed = f.click(f.panel.querySelector("[data-history-host-all]"));
    await new Promise((resolve) => setImmediate(resolve));
    resolveHost({ok: false}); await failed;
    assert.match(f.panel.querySelector("[data-history-status]").textContent, /读取失败/);
    assert.equal(f.panel.querySelector("[data-history-host-all]").disabled, false);
    assert.equal(f.panel.querySelector("[data-history-back-batch]").hidden, false);
    const leaving = f.click(f.panel.querySelector("[data-history-host-all]"));
    await new Promise((resolve) => setImmediate(resolve));
    f.workspace.activate(false);
    resolveHost({ok: true, source_host: "wrong.example", total: 0, returned: 0, truncated: false, executions: []});
    await leaving;
    assert.doesNotMatch(f.panel.querySelector("[data-history-heading]").textContent, /wrong.example/);
    assert.equal(f.panel.querySelector("[data-history-host-all]").disabled, false);
    f.workspace.destroy();
});

test("history times use Beijing independently of browser timezone and preserve unknown precision", () => {
    assert.equal(lab.historyTime("2026-09-29T08:43:15.110688+00:00"), "2026-09-29 16:43:15（北京时间）");
    assert.equal(lab.historyTime("2026-09-29T20:00:00Z"), "2026-09-30 04:00:00（北京时间）");
    assert.equal(lab.historyTime("2026-09-29T16:43:15+08:00"), "2026-09-29 16:43:15（北京时间）");
    assert.equal(lab.historyTime(null), "未知");
    assert.equal(lab.historyTime("2026-09-29"), "2026-09-29（未记录时刻）");
    assert.match(lab.historyTime("2026-09-29T16:43:15"), /缺少时区/);
    assert.equal(lab.historyOrigin("scout"), "Scout · 自动评估");
    assert.equal(lab.historyOrigin("lab"), "Provider Lab · 人工测试/复测");
    assert.equal(lab.historyOrigin("other"), "来源未记录");
});

test("history browsing, sorting and expansion keep all candidates without media or execution requests", async () => {
    const f = fixture();
    await f.workspace.loadBatches(); await f.workspace.loadBatch(1);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    assert.equal(f.calls.length, 2);
    assert.ok(f.calls.every(({url, options}) => new URL(url).pathname === "/admin/provider-lab/history/" && !options.method));
    assert.equal(f.panel.querySelectorAll("img, video").length, 0);
    assert.equal(f.panel.querySelector("strong").textContent, "<img src=x onerror=alert(1)>");
    const card = await f.inspect(10);
    const buttons = card.querySelectorAll("[data-lab-inspect]");
    assert.equal(buttons.length, 130);
    assert.ok(buttons.every((button) => button.disabled && !button.dataset.resourceToken));
    await f.click(buttons[0]); assert.equal(f.calls.length, 2);
    const sort = card.querySelector("[data-history-format]"); sort.value = "resolution";
    f.panel.listeners.change({target: sort});
    assert.equal(card.querySelector(".provider-lab-asset").dataset.assetId, "asset129");
    await f.click(card.querySelector("[data-lab-expand]"));
    assert.equal(card.querySelectorAll(".provider-lab-asset").filter((row) => !row.hidden).length, 128);
    assert.match(card.querySelector("[data-provider-scope-note]").textContent, /70/);
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("batch creation, original metadata and confirmed storage status are displayed separately", async () => {
    const f = fixture(); await f.workspace.loadBatches();
    const labels = f.panel.querySelector("[data-history-batches]").querySelectorAll("span").map((node) => node.textContent);
    assert.ok(labels.some((text) => text.includes("建档时间：2026-09-01")));
    assert.ok(labels.includes("同步状态：已入库"));
    assert.match(f.panel.querySelector("[data-history-batches]").querySelector("pre").textContent, /planned_at.*2026-08-20/);
    assert.ok(labels.every((text) => !text.includes("版本 / 同步信息")));
    const evidence = lab.historyEvidence({batch: {created_at: "2026-09-01"}, started_at: "2026-08-21", finished_at: "2026-08-22"});
    assert.match(evidence, /建档时间：2026-09-01（未记录时刻）；执行时间：2026-08-21（未记录时刻） → 2026-08-22（未记录时刻）/);
    assert.doesNotMatch(evidence, /批次时间/);
    assert.match(lab.historyEvidence({batch: {created_at: "2026-09-01"}}), /执行时间：未知 → 未知/);
    f.workspace.destroy();
});

test("large metadata stays outside batch buttons and execution versions use factual short summaries", async () => {
    const version = {head: "abcdef0123456789abcdef0123456789abcdef0123", dirty: true,
        scout_revision: {head: "123456789abcdef0123456789abcdef0123456789a", dirty: false},
        scout_code: "9876543210abcdef9876543210abcdef9876543210abcdef9876543210abcdef",
        tool_versions: {large: "tool-details".repeat(1000)}};
    const metadata = {version, targets: Array.from({length: 100}, (_, i) => ({name: `target${i}`}))};
    const f = fixture({fetch: async (url) => ({ok: true, json: async () => new URL(url).searchParams.has("batch_id")
        ? {ok: true, batch: {id: 1}, executions: [{id: 10, state: "returned", provider_id: "A", version, result: {}}]}
        : {ok: true, batches: [{id: 1, metadata, target_count: 100}]}})});
    await f.workspace.loadBatches();
    const button = f.panel.querySelector("[data-history-batch]");
    const labels = button.querySelectorAll("span").map((node) => node.textContent).join(" ");
    assert.doesNotMatch(labels, /代码指纹|App abcdef01/);
    const secondary = button.parentNode.querySelectorAll("p").map((node) => node.textContent).join(" ");
    assert.match(secondary, /App abcdef01（有未提交改动）.*Scout 12345678.*Scout 代码指纹 98765432/);
    assert.match(secondary, /目标 100/);
    assert.doesNotMatch(labels, /tool-details|target99/);
    assert.equal(button.querySelector("details"), null);
    const details = button.parentNode.querySelector("details");
    assert.equal(details.getAttribute("open"), null);
    assert.deepEqual(JSON.parse(details.querySelector("pre").textContent), metadata);
    await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    assert.ok(card.querySelectorAll("p").some((node) => /App abcdef01.*Scout 12345678/.test(node.textContent)));
    assert.ok(card.querySelectorAll("p").every((node) => !node.textContent.includes("tool-details")));
    assert.deepEqual(JSON.parse(card.querySelector("pre").textContent), version);
    assert.match(lab.historyEvidence({version: {head: version.head, scout_code: version.scout_code}}), /Scout 未知.*代码指纹 98765432/);
    assert.match(lab.historyEvidence({version: {}}), /App 未知.*Scout 未知/);
    f.workspace.destroy();
});

test("native replay displays identity and limitations while 267 candidates remain view-only", async () => {
    const f = fixture();
    const record = f.executions[0];
    record.result = {status: "native_observed", metadata: {evidence_kind: "native_capture_replay",
        target_identity: {platform: "xiaohongshu", id: "fixture-note"}, identity_confirmed: false,
        capture_status: "captured", conversion_status: "converted", integrity: {complete: false},
        scope: "captured subset", limitations: ["not live acceptance"]},
        assets: Array.from({length: 267}, (_, i) => ({asset_id: `native-${i}`, media_type: "image", resource_token: "not-authority"}))};
    const original = JSON.stringify(record);
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    const cell = f.panel.querySelector('[data-history-inspect="10"]');
    assert.equal(cell.getAttribute("data-state"), "native_observed");
    let card = await f.inspect(10);
    assert.equal(card.querySelector("[data-provider-status-label]").textContent, "原生媒体证据（离线转换，非实时产品验收）");
    assert.equal(card.querySelector("[data-provider-error]").hidden, true);
    assert.equal(card.querySelector("[data-provider-error]").textContent, "");
    assert.match(card.querySelector("[data-native-identity]").textContent, /fixture-note.*身份状态：未确认/);
    assert.match(card.querySelector("[data-native-conversion]").textContent, /captured.*converted/);
    assert.match(card.querySelector("[data-native-limitation]").textContent, /不代表实时产品解析/);
    const metadata = card.querySelectorAll("pre").map((node) => node.textContent).join(" ");
    assert.match(metadata, /captured subset/); assert.match(metadata, /not live acceptance/);
    const resources = card.querySelectorAll("[data-lab-inspect]");
    assert.equal(resources.length, 267);
    assert.ok(resources.every((button) => button.disabled && button.dataset.resourceToken === ""));
    await f.click(resources[0]);
    assert.equal(f.retests.length, 0); assert.equal(f.drafts.length, 0);
    assert.ok(f.calls.every(({options}) => !options.method));
    await f.change(f.panel.querySelector("[data-history-state]"), "success");
    assert.equal(f.panel.querySelector('[data-history-inspect="10"]'), null);
    await f.change(f.panel.querySelector("[data-history-state]"), "native_observed");
    card = await f.inspect(10);
    assert.match(lab.historyEvidence(record), /原生媒体证据/);
    assert.equal(JSON.stringify(record), original);
    f.workspace.destroy();
});

test("native batch marker is visible and missing identity is unknown, never product success", async () => {
    for (const confirmed of [undefined, true, "true"]) {
        const batch = {id: 1, metadata: {evidence_kind: "native_capture_replay"}, counts: {success: 0, native_observed: 1}};
        const record = {id: 10, provider_id: "native_capture", state: "success", result: {status: "success",
            metadata: {identity_confirmed: confirmed}}};
        const f = fixture({fetch: async (url) => ({ok: true, json: async () => new URL(url).searchParams.has("batch_id")
            ? {ok: true, batch, executions: [record]} : {ok: true, batches: [batch]}})});
        await f.workspace.loadBatches();
        const labels = f.panel.querySelector("[data-history-batch]").querySelectorAll("span").map((node) => node.textContent).join(" ");
        assert.match(labels, /产品成功 0.*原生离线证据 1/);
        assert.match(labels, /原生媒体证据（离线转换，非实时产品验收）/);
        await f.workspace.loadBatch(1);
        const card = await f.inspect(10);
        assert.match(card.querySelector("[data-native-identity]").textContent, /目标身份："未知"/);
        assert.ok(card.querySelector("[data-native-identity]").textContent.endsWith(`身份状态：${confirmed === true ? "已确认" : "未知"}`));
        assert.equal(card.dataset.providerStatus, "native_observed");
        assert.equal(card.querySelector("[data-history-retest]").disabled, true);
        f.workspace.destroy();
    }
});

test("host route reference groups returned successes by content without ranking different works or requests", async () => {
    const f = fixture({descriptors: [{provider_id: "A", label: "Alpha", available: true, route_selectable: true},
        {provider_id: "B", label: "Beta", available: true, route_selectable: true}]});
    const seed = {...f.executions[0], state: "returned"};
    const row = (id, provider, kind, changes = {}) => ({...seed, id, provider_id: provider, sample_key: `work-${id}`,
        result: {status: "success", result_kind: kind, assets: [{media_type: kind === "gallery" ? "image" : "video", height: id * 1000}]}, ...changes});
    f.executions.splice(0, f.executions.length,
        row(10, "A", "video"), row(20, "B", "video"), row(30, "A", "gallery"),
        row(40, "A", "article", {state: "sent"}), row(50, "A", "article", {result: {status: "failed", result_kind: "article"}}),
        row(60, "A", "article", {result: {status: "success", executed: false, result_kind: "article"}}),
        row(70, "B", "gallery", {source_host: "other.example"}),
        row(80, "A", "article", {result: {status: "success", client_status: "OUTCOME_UNKNOWN", result_kind: "article"}}));
    const original = JSON.stringify(f.executions);
    await f.workspace.loadBatch(1);
    assert.equal(f.panel.querySelector("[data-history-route-reference]"), null);
    const requests = f.calls.length;
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const reference = f.panel.querySelector("[data-history-route-reference]");
    const lines = reference.querySelectorAll("li").map((node) => node.textContent);
    assert.equal(lines.length, 2);
    assert.match(lines.find((line) => line.startsWith("视频")), /Alpha.*#10.*Beta.*#20.*多家记录不排名.*需同作品比较/);
    assert.match(lines.find((line) => line.startsWith("图集")), /Alpha.*#30.*唯一已验证参考/);
    assert.doesNotMatch(lines.join(" "), /#40|#50|#60|#70|#80|10000|20000|最高|最佳上游/);
    const notes = reference.querySelectorAll("p").map((node) => node.textContent).join(" ");
    assert.match(notes, /尺寸非目视画质/);
    assert.match(notes, /实际 hostname：source.example.*不按内容类型持久化/);
    assert.match(notes, /成熟平台继续沿用既有路线/);
    assert.equal(reference.querySelectorAll("button, select, input").length, 0);
    await f.change(f.panel.querySelector("[data-history-order]"), "oldest");
    await f.change(f.panel.querySelector("[data-history-state]"), "failed");
    assert.deepEqual(f.panel.querySelector("[data-history-route-reference]").querySelectorAll("li").map((node) => node.textContent), lines);
    assert.equal(f.calls.length, requests);
    assert.equal(f.retests.length, 0); assert.equal(f.drafts.length, 0);
    assert.equal(f.selections.filter(Boolean).length, 0);
    assert.equal(JSON.stringify(f.executions), original);
    f.workspace.destroy();
});

test("native route reference requires boolean confirmed identity and RETURNED capture", async () => {
    const f = fixture();
    const seed = {...f.executions[0], state: "returned"};
    f.executions.splice(0, f.executions.length, ...[
        [10, true, "RETURNED"], [20, false, "RETURNED"], [30, "true", "RETURNED"],
        [40, undefined, "RETURNED"], [50, true, "RESERVED"], [60, true, "UNKNOWN"],
    ].map(([id, confirmed, capture]) => ({...seed, id, result: {status: "native_observed", result_kind: "gallery", assets: [{media_type: "image"}],
        metadata: {evidence_kind: "native_capture_replay", identity_confirmed: confirmed, capture_status: capture}}})));
    await f.workspace.loadBatch(1);
    const requests = f.calls.length;
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const line = f.panel.querySelector("[data-history-route-reference]").querySelector("li").textContent;
    assert.match(line, /1 条，记录 #10.*身份已确认的原生离线证据.*不证明产品通道可执行.*唯一已验证参考/);
    assert.doesNotMatch(line, /#20|#30|#40|#50|#60/);
    f.executions[0].result.metadata.identity_confirmed = false;
    await f.change(f.panel.querySelector("[data-history-order]"), "oldest");
    const reference = f.panel.querySelector("[data-history-route-reference]");
    assert.equal(reference.querySelectorAll("li").length, 0);
    assert.ok(reference.querySelectorAll("p").some((node) => node.textContent.includes("暂无满足条件")));
    assert.equal(f.calls.length, requests);
    assert.equal(f.retests.length, 0); assert.equal(f.drafts.length, 0);
    f.workspace.destroy();
});

test("route reference separates Common and unknown evidence from executable routes and respects mature scope", async () => {
    const routeEditor = {invalidate() {}, restore() {}, route: {source_host: "source.example", applicable: false}};
    const f = fixture({routeEditor, descriptors: [{provider_id: "videodl_repo", execution_provider_id: "videodl_repo",
        available: true, route_selectable: true}]});
    const seed = {...f.executions[0], state: "returned", provider_id: "videodl_repo", result: {status: "success", result_kind: "video", assets: [{media_type: "video"}]}};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, options: {mode: "common_only", backend_category: "common"}},
        {...seed, id: 20, options: null}, {...seed, id: 30, options: {mode: "dedicated_only"}},
        {...seed, id: 40, provider_id: "unknown_provider"});
    await f.workspace.loadBatch(1);
    const requests = f.calls.length;
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const reference = f.panel.querySelector("[data-history-route-reference]");
    const line = reference.querySelector("li").textContent;
    assert.match(line, /Common 或未知执行选项：仅采集证据，不可直接作为可执行路线/);
    assert.match(line, /Dedicated 历史证据，仍需核对当前 backend 与来源适用性/);
    assert.match(line, /unknown_provider.*不能视为可执行路线/);
    assert.match(reference.querySelector("[data-route-reference-scope]").textContent, /当前来源受既有路线保护，不适用通用路线保存/);
    routeEditor.route = null;
    f.workspace.refreshCurrentRoute();
    assert.match(reference.querySelector("[data-route-reference-scope]").textContent, /可否保存以路线编辑器读取的范围为准/);
    assert.equal(f.calls.length, requests);
    assert.equal(f.drafts.length, 0); assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("native reference infers media kinds without covers and independently labels Common limitations", async () => {
    const f = fixture({descriptors: [{provider_id: "videodl_repo", execution_provider_id: "videodl_repo", available: true, route_selectable: true}]});
    const seed = {...f.executions[0], state: "returned", provider_id: "videodl_repo", options: {mode: "common_only"}};
    const metadata = {evidence_kind: "native_capture_replay", identity_confirmed: true, capture_status: "RETURNED"};
    const covers = ["cover", "thumbnail", "poster"].map((role) => ({role, media_type: "image"}));
    f.executions.splice(0, f.executions.length, ...[
        [10, [{media_type: "audio"}]], [20, [{media_type: "video"}]],
        [30, [{media_type: "image"}, {media_type: "image"}]],
        [40, [{media_type: "audio"}, {media_type: "video"}]],
    ].map(([id, assets]) => ({...seed, id, result: {status: "native_observed", result_kind: "unknown", metadata, assets: [...covers, ...assets]}})),
        {...seed, id: 50, result: {status: "native_observed", metadata, asset_count: 2}},
        {...seed, id: 60, result: {status: "native_observed", metadata, assets: covers, asset_count: 3}});
    await f.workspace.loadBatch(1);
    const requests = f.calls.length;
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const lines = f.panel.querySelector("[data-history-route-reference]").querySelectorAll("li").map((node) => node.textContent);
    for (const [kind, id] of [["音频", 10], ["视频", 20], ["图集", 30], ["混合媒体", 40], ["内容类型未知", 50]]) {
        const line = lines.find((value) => value.startsWith(`${kind}：`));
        assert.ok(line, kind); assert.ok(line.includes(`#${id}`));
        assert.match(line, /身份已确认的原生离线证据.*Common 或未知执行选项.*不可直接作为可执行路线/);
    }
    assert.equal(lines.length, 5); assert.doesNotMatch(lines.join(" "), /#60/);
    assert.equal(f.calls.length, requests); assert.equal(f.drafts.length, 0); assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("success reference requires media evidence and explicit assets override counts including covers", async () => {
    const f = fixture();
    const seed = {...f.executions[0], state: "returned"};
    const payloads = [
        {}, {asset_count: 0}, {asset_count: -1}, {asset_count: 1.5}, {asset_count: "2"}, {asset_count: true},
        {assets: [], asset_count: 9}, {assets: [{media_type: "image", role: "cover"}], asset_count: 9},
        {assets: [{media_type: "video", role: "poster"}], metadata: {asset_count: 9}},
        {assets: [{media_type: "image", role: "thumbnail"}]}, {assets: null, asset_count: 9},
    ];
    f.executions.splice(0, f.executions.length, ...payloads.map((payload, index) => ({...seed, id: index + 100,
        result: {status: "success", result_kind: "video", ...payload}})));
    await f.workspace.loadBatch(1);
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    assert.equal(f.panel.querySelector("[data-history-route-reference]").querySelectorAll("li").length, 0);
    // A later batch with missing asset details may use a positive integer count, not a coerced value.
    f.executions.splice(0, f.executions.length,
        {...seed, id: 201, result: {status: "success", metadata: {asset_count: 2}}},
        {...seed, id: 202, result: {status: "success", assets: [{media_type: "video"}], asset_count: 0}});
    await f.workspace.loadBatch(1);
    const requests = f.calls.length;
    await f.change(f.panel.querySelector("[data-history-host]"), "source.example");
    const lines = f.panel.querySelector("[data-history-route-reference]").querySelectorAll("li").map((node) => node.textContent);
    assert.equal(lines.length, 2);
    assert.match(lines.find((line) => line.startsWith("内容类型未知")), /#201.*唯一已验证参考/);
    assert.match(lines.find((line) => line.startsWith("视频")), /#202.*唯一已验证参考/);
    assert.equal(f.calls.length, requests); assert.equal(f.drafts.length, 0); assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("history selection uses original source and prepares a draft without a provider call", async () => {
    const f = fixture(); f.workspace.activate(true);
    await f.workspace.loadBatch(1);
    let card = await f.inspect(10);
    await f.click(card.querySelector("[data-history-route]"));
    card = f.panel.querySelector('[data-history-execution="10"]');
    assert.equal(f.workspace.source, "https://source.example/work");
    assert.equal(f.drafts.length, 1); assert.equal(f.drafts[0][1].id, 10);
    assert.match(lab.historyEvidence(f.drafts[0][1]), /2026-09-01.*old-head/);
    assert.equal(f.retests.length, 0);
    await f.click(card.querySelector("[data-lab-select]"));
    assert.equal(f.selections.at(-1).asset.asset_id, "asset0");
    assert.equal(card.querySelector("[data-lab-select]").getAttribute("aria-pressed"), "true");
    assert.equal(f.drafts.length, 1);
    f.workspace.destroy();
});

test("legacy candidates without upstream IDs have stable view-only identities through sorting and expansion", async () => {
    const f = fixture();
    const assets = f.executions[0].result.assets;
    assets.forEach((asset) => { delete asset.asset_id; });
    const original = JSON.stringify(f.executions);
    f.workspace.activate(true); await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    const choices = () => card.querySelectorAll("[data-lab-select]");
    assert.equal(new Set(choices().map((button) => button.dataset.historySelectionId)).size, 130);
    assert.ok(choices().every((button) => button.dataset.assetId === ""));
    await f.click(choices()[1]);
    assert.equal(f.selections.at(-1).asset, assets[1]);
    assert.equal(choices().filter((button) => button.getAttribute("aria-pressed") === "true").length, 1);
    const sort = card.querySelector("[data-history-format]"); sort.value = "resolution";
    f.panel.listeners.change({target: sort});
    assert.equal(choices()[0].dataset.historySelectionId, "history:10:129");
    await f.click(choices()[0]);
    assert.equal(f.selections.at(-1).asset, assets[129]);
    sort.value = "source"; f.panel.listeners.change({target: sort});
    assert.equal(choices()[129].getAttribute("aria-pressed"), "true");
    assert.equal(choices()[0].getAttribute("aria-pressed"), "false");
    await f.click(card.querySelector("[data-lab-expand]"));
    await f.click(card.querySelector("[data-lab-expand]"));
    assert.equal(card.querySelectorAll(".provider-lab-asset").filter((row) => !row.hidden).length, 130);
    assert.equal(choices()[129].dataset.historySelectionId, "history:10:129");
    assert.equal(choices()[129].getAttribute("aria-pressed"), "true");
    await f.click(card.querySelector("[data-history-route]"));
    assert.equal(f.drafts.at(-1)[0], "A");
    assert.equal(f.drafts.at(-1)[1].id, 10);
    assert.equal(f.selections.at(-1).asset, null);
    assert.equal(JSON.stringify(f.executions), original);
    assert.ok(!JSON.stringify(f.selections).includes("history:10:"));
    assert.equal(f.retests.length, 0);
    assert.ok(f.calls.every(({options}) => !options.method));
    f.workspace.destroy();
});

test("missing source and removed providers stay readable; uncertain executions never resend", async () => {
    const f = fixture(); await f.workspace.loadBatch(1);
    for (const id of [11, 12]) {
        const card = await f.inspect(id);
        assert.equal(card.querySelector("[data-history-retest]").disabled, true);
        assert.equal(card.querySelector("[data-history-route]").disabled, true);
        await f.click(card.querySelector("[data-history-retest]"));
    }
    assert.equal(f.retests.length, 0);
    const missing = f.panel.querySelector('[data-history-execution="12"]');
    assert.match(missing.querySelector('[data-field="asset_count"]').textContent, /未知/);
    assert.match(missing.querySelector("[data-provider-assets]").querySelector("p").textContent, /不等于零/);
    assert.ok(missing.querySelectorAll("p").some((item) => /sensitive.*not_retained/.test(item.textContent)));
    for (const state of ["unknown", "sent", "reserved", "in_flight", "queued", "running", "waiting"]) {
        assert.match(lab.historyRetestReason({...f.executions[0], state}, {available: true}), /不能重发/);
    }
    f.workspace.destroy();
});

test("retest rejects malformed and pending states even when can_retest is true", async () => {
    for (const state of ["QUEUED", " waiting ", "UNKNOWN", " Running ", null, undefined, "", "  ", 1, true, [], {}, "unrecognized"]) {
        const f = fixture();
        Object.assign(f.executions[0], {state, can_retest: true});
        await f.workspace.loadBatch(1);
        const card = await f.inspect(10);
        assert.equal(card.querySelector("[data-history-retest]").disabled, true);
        await f.click(card.querySelector("[data-history-retest]"));
        assert.equal(f.retests.length, 0);
        f.workspace.destroy();
    }
});

test("retest normalizes known terminal states without changing original evidence", () => {
    const f = fixture();
    for (const state of ["SUCCESS", " returned ", "failed", "timeout", "unsupported", "not_executed", "planned", "skipped", "cancelled"]) {
        const record = {...f.executions[0], state, can_retest: true};
        assert.equal(lab.historyRetestReason(record, {available: true}), "");
        assert.equal(record.state, state);
    }
    f.workspace.destroy();
});

test("explicit retest calls exactly one item and persistence failure cannot trigger another call", async () => {
    const f = fixture(); await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    await f.click(card.querySelector("[data-history-retest]"));
    assert.equal(f.retests.length, 0);
    const confirm = f.panel.querySelector('[data-history-confirm="10"]');
    assert.ok(confirm);
    await f.click(confirm);
    assert.equal(f.retests.length, 1); assert.equal(f.retests[0].id, 10);
    assert.match(f.panel.querySelector("[data-history-status]").textContent, /SAVE_FAILED.*不会自动重跑/);
    await f.click(f.panel.querySelector('[data-history-execution="10"]').querySelector("[data-history-retest]"));
    await f.click(confirm);
    assert.equal(f.retests.length, 1);
    assert.equal(f.executions[0].version.animeimages, "old-head");
    f.workspace.destroy();
});

test("returned executions with unknown service outcome explain the block and cannot resend", async () => {
    for (const result of [{error_detail: "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"}, {client_status: "OUTCOME_UNKNOWN"}]) {
        const f = fixture();
        Object.assign(f.executions[0], {state: "returned", can_retest: false, result: {...result, status: "failed"}});
        await f.workspace.loadBatch(1);
        const card = await f.inspect(10);
        assert.equal(card.querySelector("[data-history-retest]").disabled, true);
        assert.ok(card.querySelectorAll("p").some((item) => /前次服务调用结果未知.*不能直接复测或重发/.test(item.textContent)));
        await f.click(card.querySelector("[data-history-retest]"));
        assert.equal(f.retests.length, 0);
        assert.match(lab.historyRetestReason({...f.executions[0], can_retest: true}, {available: true}), /前次服务调用结果未知/);
        assert.match(lab.historyRetestReason({...f.executions[0], source_url: ""}, {available: true}), /前次服务调用结果未知/);
        f.workspace.destroy();
    }
});

test("out-of-order batch responses cannot replace the selected batch", async () => {
    const resolvers = [];
    const f = fixture({fetch: () => new Promise((resolve) => resolvers.push(resolve))});
    const first = f.workspace.loadBatch(1), second = f.workspace.loadBatch(2);
    resolvers[1]({ok: true, json: async () => ({ok: true, batch: {id: 2, label: "new"}, executions: []})}); await second;
    resolvers[0]({ok: true, json: async () => ({ok: true, batch: {id: 1, label: "old"}, executions: []})}); await first;
    assert.equal(f.panel.querySelector("[data-history-heading]").textContent, "new");
    f.workspace.destroy();
});

test("history relations distinguish Scout planned links from numeric Lab retests", () => {
    const planned = lab.historyRelation({id: 10, origin: "scout", replay_of: "sample:parsehub:planned"});
    assert.match(planned, /计划关联/);
    assert.doesNotMatch(planned, /单项复测/);
    const retest = lab.historyRelation({id: 20, origin: "lab", replay_of: "10"});
    assert.match(retest, /单项复测/);
    assert.match(retest, /10.*20/);
    assert.match(lab.historyRelation({id: 20, batch: {origin: "lab"}, replay_of: "10"}), /单项复测/);
    assert.doesNotMatch(lab.historyRelation({id: 20, origin: "scout", replay_of: "10"}), /单项复测/);
    assert.doesNotMatch(lab.historyRelation({id: 20, origin: "lab", replay_of: "scout-execution"}), /单项复测/);
    assert.doesNotMatch(lab.historyRelation({id: 10, origin: "scout", replay_of: ""}), /单项复测|计划关联/);
});

test("history timestamps compare explicit offsets and never infer missing timezones", () => {
    assert.equal(lab.historyTimestamp("2026-09-29T09:00:00+08:00"), Date.UTC(2026, 8, 29, 1));
    assert.equal(lab.historyTimestamp("2026-09-29T01:00:00Z"), Date.UTC(2026, 8, 29, 1));
    assert.equal(lab.historyTimestamp("2026-09-28T20:00:00-05:00"), Date.UTC(2026, 8, 29, 1));
    for (const value of [undefined, null, "", 0, false, "2026-09-29", "2026-09-29T09:00:00", "invalidZ"]) {
        assert.equal(lab.historyTimestamp(value), null, String(value));
    }
});

test("matrix sorts mixed timezones by actual time and picks the genuinely latest observation", async () => {
    const f = fixture();
    const seed = {...f.executions[0], result: {status: "success", assets: []}};
    f.executions.splice(0, f.executions.length,
        {...seed, id: 10, started_at: "2026-09-29T09:00:00+08:00"},
        {...seed, id: 20, started_at: "2026-09-29T02:00:00Z"},
        {...seed, id: 30, sample_key: "earlier", source_url: "https://source.example/earlier", started_at: "2026-09-29T00:30:00Z"});
    await f.workspace.loadBatch(1);
    const buttons = () => f.panel.querySelector(".provider-lab-history-matrix").querySelectorAll("[data-history-inspect]");
    assert.deepEqual(buttons().map((button) => button.dataset.historyInspect), ["20", "30"]);
    assert.match(buttons()[0].querySelector("small").textContent, /最新/);
    const card = await f.inspect(20);
    assert.ok(card.querySelectorAll("p").some((node) => node.textContent.includes("2026-09-29 10:00:00")));
    await f.change(f.panel.querySelector("[data-history-order]"), "oldest");
    assert.deepEqual(buttons().map((button) => button.dataset.historyInspect), ["30", "20"]);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    assert.equal(f.calls.length, 1);
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("observations with missing or ambiguous times remain accessible without claiming latest", async () => {
    for (const known of ["2026-09-29T02:00:00Z", null]) {
        for (const unknown of [undefined, null, "2099-01-01", "2099-01-01T23:59:59", "invalidZ"]) {
            const f = fixture();
            try {
                const seed = {...f.executions[0], result: {status: "success", assets: []}};
                f.executions.splice(0, f.executions.length,
                    {...seed, id: 10, started_at: known}, {...seed, id: 20, started_at: unknown});
                const original = JSON.stringify(f.executions);
                await f.workspace.loadBatch(1);
                const button = f.panel.querySelector(".provider-lab-history-matrix").querySelector("[data-history-inspect]");
                assert.match(button.querySelector("small").textContent, /2 次记录.*时间不全/);
                assert.doesNotMatch(button.querySelector("small").textContent, /最新/);
                if (known) assert.equal(button.dataset.historyInspect, "10");
                const card = await f.inspect(button.dataset.historyInspect);
                const choices = card.querySelector("[data-history-observation]");
                assert.deepEqual(new Set(choices.querySelectorAll("option").map((option) => option.value)), new Set(["10", "20"]));
                await f.change(choices, 20);
                assert.ok(f.panel.querySelector('[data-history-execution="20"]'));
                assert.equal(JSON.stringify(f.executions), original);
                assert.equal(f.calls.length, 1);
                assert.equal(f.retests.length, 0);
            } finally { f.workspace.destroy(); }
        }
    }
});

test("matrix has one row per sample, six fixed providers, unknown columns and a collapsed baseline", async () => {
    const f = fixture();
    const seed = {...f.executions[0], result: {status: "success", assets: []}};
    const providers = ["parsehub_repo", "videodl_repo", "yt_dlp_existing", "gallery_dl_existing", "tiantian_existing", "kukutool_existing"];
    f.executions.splice(0, f.executions.length,
        ...providers.map((provider_id, index) => ({...seed, id: 100 + index, provider_id})),
        {...seed, id: 200, provider_id: "removed-provider"},
        {...seed, id: 201, provider_id: "parsehub_repo", sample_key: "second", source_url: "https://source.example/second", title: "Another sample"},
        {...seed, id: 300, provider_id: "animeimages_current"});
    await f.workspace.loadBatch(1);
    const matrix = f.panel.querySelector(".provider-lab-history-matrix");
    assert.ok(matrix);
    const headers = matrix.querySelector("thead").querySelectorAll("th").map((node) => node.textContent);
    assert.deepEqual(headers, ["站点 / 作品", "ParseHub", "videodl", "yt-dlp", "gallery-dl", "天天解析", "Kukutool", "removed-provider"]);
    const rows = matrix.querySelector("tbody").querySelectorAll("tr");
    assert.equal(rows.length, 2);
    assert.ok(rows.every((row) => row.querySelectorAll("td").length === 7));
    assert.equal(matrix.querySelectorAll("[data-history-inspect]").length, 8);
    assert.ok(!matrix.querySelector('[data-history-inspect="300"]'));
    assert.ok(matrix.querySelectorAll("span").some((node) => node.textContent === "未纳入"));
    const baseline = f.panel.querySelector('[data-history-inspect="300"]').closest("details");
    assert.ok(baseline);
    assert.equal(baseline.getAttribute("open"), null);
    assert.match(baseline.querySelector("summary").textContent, /非独立上游/);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    await f.inspect(200);
    await f.inspect(300);
    assert.equal(f.calls.length, 1);
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("matrix paginates ten samples and state filtering resets page and detail without requests", async () => {
    const f = fixture();
    const seed = f.executions[0];
    f.executions.splice(0, f.executions.length, ...Array.from({length: 23}, (_, index) => ({
        ...seed, id: 100 + index, sample_key: `sample-${index}`, source_url: `https://source.example/work/${index}`, state: "returned",
        started_at: `2026-09-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
        result: {status: index === 1 ? "timeout" : index % 2 ? "failed" : "success", assets: []},
    })));
    await f.workspace.loadBatch(1);
    const matrix = () => f.panel.querySelector(".provider-lab-history-matrix");
    const rows = () => matrix().querySelector("tbody").querySelectorAll("tr");
    const ids = () => matrix().querySelectorAll("[data-history-inspect]").map((node) => Number(node.dataset.historyInspect));
    const next = () => f.panel.querySelector('[data-history-page="next"]');
    const prev = () => f.panel.querySelector('[data-history-page="prev"]');
    assert.equal(rows().length, 10);
    assert.equal(prev().disabled, true);
    assert.equal(next().disabled, false);
    const seen = ids();
    await f.inspect(seen[0]);
    await f.click(next());
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    assert.equal(rows().length, 10);
    seen.push(...ids());
    await f.click(next());
    assert.equal(rows().length, 3);
    seen.push(...ids());
    assert.equal(next().disabled, true);
    assert.equal(new Set(seen).size, 23);
    assert.deepEqual(new Set(seen), new Set(f.executions.map((e) => e.id)));
    await f.click(next());
    assert.equal(rows().length, 3);
    await f.click(prev());
    assert.equal(rows().length, 10);
    await f.inspect(ids()[0]);
    const state = f.panel.querySelector("[data-history-state]");
    await f.change(state, "failed");
    assert.equal(prev().disabled, true);
    assert.equal(rows().length, 10);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    const failed = ids();
    await f.click(next());
    assert.equal(rows().length, 1);
    failed.push(...ids());
    assert.deepEqual(new Set(failed), new Set(f.executions.filter((e) => ["failed", "timeout"].includes(e.result.status)).map((e) => e.id)));
    await f.change(state, "success");
    assert.equal(rows().length, 10);
    assert.equal(prev().disabled, true);
    assert.ok(ids().every((id) => f.executions.find((e) => e.id === id).result.status === "success"));
    await f.change(state, "not_executed");
    assert.equal(rows().length, 0);
    assert.equal(prev().disabled, true);
    assert.equal(next().disabled, true);
    await f.change(state, "");
    assert.equal(rows().length, 10);
    await f.inspect(ids()[0]);
    await f.change(f.panel.querySelector("[data-history-order]"), "oldest");
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    assert.equal(ids()[0], 100);
    await f.inspect(100);
    assert.equal(f.calls.length, 1);
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("matrix shows the latest observation and detail can select older evidence without executing", async () => {
    const f = fixture();
    Object.assign(f.executions[0], {started_at: "2026-09-01T01:00:00Z", state: "returned"});
    f.executions.push({...f.executions[0], id: 20, started_at: "2026-09-02T01:00:00Z",
        version: {animeimages: "new-head"}, result: {status: "failed", error_detail: "AUTH HTML", assets: []}});
    const original = JSON.stringify(f.executions);
    await f.workspace.loadBatch(1);
    assert.equal(f.panel.querySelectorAll('[data-history-inspect="10"]').length, 0);
    assert.equal(f.panel.querySelectorAll('[data-history-inspect="20"]').length, 1);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 0);
    let card = await f.inspect(20);
    await f.click(card.querySelector("[data-history-retest]"));
    const staleConfirm = f.panel.querySelector('[data-history-confirm="20"]');
    assert.ok(staleConfirm);
    card = f.panel.querySelector('[data-history-execution="20"]');
    const observation = card.querySelector("[data-history-observation]");
    assert.ok(observation);
    assert.deepEqual(new Set(observation.querySelectorAll("option").map((option) => option.value)), new Set(["10", "20"]));
    await f.change(observation, 10);
    assert.ok(!f.panel.querySelector("[data-history-confirm]"));
    await f.click(staleConfirm);
    assert.equal(f.retests.length, 0);
    assert.equal(f.panel.querySelectorAll("[data-history-execution]").length, 1);
    card = f.panel.querySelector('[data-history-execution="10"]');
    assert.ok(card);
    assert.equal(card.querySelectorAll("[data-lab-inspect]").length, 130);
    await f.change(card.querySelector("[data-history-observation]"), 20);
    card = f.panel.querySelector('[data-history-execution="20"]');
    assert.ok(card);
    assert.equal(card.querySelectorAll("[data-lab-inspect]").length, 0);
    assert.equal(JSON.stringify(f.executions), original);
    assert.equal(f.calls.length, 1);
    assert.equal(f.retests.length, 0);
    assert.equal(f.drafts.length, 0);
    f.workspace.destroy();
});

test("inline retest cancellation never executes and requires a new confirmation", async () => {
    const f = fixture(); await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    await f.click(card.querySelector("[data-history-retest]"));
    const firstConfirm = f.panel.querySelector('[data-history-confirm="10"]');
    assert.ok(firstConfirm);
    assert.equal(f.retests.length, 0);
    await f.click(f.panel.querySelector("[data-history-cancel]"));
    assert.ok(!f.panel.querySelector("[data-history-confirm]"));
    await f.click(firstConfirm);
    assert.equal(f.retests.length, 0);
    await f.click(f.panel.querySelector('[data-history-execution="10"]').querySelector("[data-history-retest]"));
    assert.ok(f.panel.querySelector('[data-history-confirm="10"]'));
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("changing batch, detail, filter or leaving clears a pending retest confirmation", async () => {
    const transitions = {
        batch: (f) => f.workspace.loadBatch(2),
        detail: (f) => f.inspect(11),
        state: (f) => f.change(f.panel.querySelector("[data-history-state]"), "failed"),
        host: (f) => f.change(f.panel.querySelector("[data-history-host]"), "source.example"),
        order: (f) => f.change(f.panel.querySelector("[data-history-order]"), "oldest"),
        leave: (f) => f.workspace.activate(false),
    };
    for (const [name, transition] of Object.entries(transitions)) {
        const f = fixture();
        try {
            await f.workspace.loadBatch(1);
            const card = await f.inspect(10);
            await f.click(card.querySelector("[data-history-retest]"));
            const confirm = f.panel.querySelector('[data-history-confirm="10"]');
            assert.ok(confirm, name);
            await transition(f);
            assert.ok(f.panel.hidden || !f.panel.querySelector("[data-history-confirm]"), name);
            await f.click(confirm);
            assert.equal(f.retests.length, 0, name);
        } finally { f.workspace.destroy(); }
    }
});

test("double confirmation while a retest is in flight invokes only the chosen item once", async () => {
    const retests = [];
    let finish;
    const f = fixture({retest: (execution) => {
        retests.push(execution);
        return new Promise((resolve) => { finish = resolve; });
    }});
    await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    await f.click(card.querySelector("[data-history-retest]"));
    assert.equal(retests.length, 0);
    const confirm = f.panel.querySelector('[data-history-confirm="10"]');
    const pending = f.click(confirm);
    try {
        assert.equal(retests.length, 1);
        assert.equal(retests[0].id, 10);
        assert.equal(f.workspace.busy, true);
        await f.click(confirm);
        assert.equal(retests.length, 1);
    } finally {
        finish?.({status: "success", executed: true, history: {saved: true, id: 21}, assets: []});
        await pending;
        f.workspace.destroy();
    }
    assert.equal(retests.length, 1);
});

test("configureRetest is scoped to the chosen inline confirmation and cleared on cancellation", async () => {
    const configured = [];
    const controls = new Node("fieldset", {"data-test-authorization-controls": ""});
    const f = fixture({configureRetest: (execution, container) => {
        configured.push(execution?.id ?? null);
        if (execution) {
            assert.equal(container.getAttribute("data-history-confirmation"), "");
            assert.equal(container.closest("[data-history-execution]").dataset.historyExecution, String(execution.id));
            container.append(controls);
        } else { controls.remove(); }
    }});
    await f.workspace.loadBatch(1);
    const card = await f.inspect(10);
    assert.ok(configured.every((id) => id === null));
    await f.click(card.querySelector("[data-history-retest]"));
    assert.equal(configured.at(-1), 10);
    assert.ok(f.panel.querySelector("[data-test-authorization-controls]"));
    assert.equal(f.retests.length, 0);
    await f.click(f.panel.querySelector("[data-history-cancel]"));
    assert.equal(configured.at(-1), null);
    assert.ok(!f.panel.querySelector("[data-test-authorization-controls]"));
    assert.equal(f.retests.length, 0);
    f.workspace.destroy();
});

test("an unexecuted retest result permits a fresh explicit confirmation but never retries automatically", async () => {
    const attempts = [];
    const f = fixture({retest: async (execution) => {
        attempts.push(execution.id);
        return {executed: false, not_run_reason: "Current authorization required"};
    }});
    await f.workspace.loadBatch(1);
    let card = await f.inspect(10);
    await f.click(card.querySelector("[data-history-retest]"));
    assert.deepEqual(attempts, []);
    const staleConfirm = f.panel.querySelector('[data-history-confirm="10"]');
    await f.click(staleConfirm);
    assert.deepEqual(attempts, [10]);
    assert.match(f.panel.querySelector("[data-history-status]").textContent, /Current authorization required/);
    await f.click(staleConfirm);
    assert.deepEqual(attempts, [10]);
    card = f.panel.querySelector('[data-history-execution="10"]');
    assert.equal(card.querySelector("[data-history-retest]").disabled, false);
    await f.click(card.querySelector("[data-history-retest]"));
    assert.deepEqual(attempts, [10]);
    await f.click(f.panel.querySelector('[data-history-confirm="10"]'));
    assert.deepEqual(attempts, [10, 10]);
    assert.equal(f.calls.length, 1);
    f.workspace.destroy();
});
