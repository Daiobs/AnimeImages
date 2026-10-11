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
            json: async () => new URL(url).searchParams.has("batch_id") ? {ok: true, batch, executions} : {ok: true, batches: [batch]}}; },
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

function routeHistoryFixture(read) {
    const bridge = {};
    const descriptors = [
        {provider_id: "parsehub_repo", execution_provider_id: "parsehub_generic", label: "ParseHub"},
        {provider_id: "yt_dlp_existing", execution_provider_id: "yt_dlp_generic", label: "yt-dlp"},
        {provider_id: "gallery_dl_existing", execution_provider_id: "gallery_dl_generic", label: "gallery-dl"},
        {provider_id: "videodl_repo", execution_provider_id: "videodl_repo", label: "videodl"},
    ].map((item) => ({...item, available: true, route_selectable: true}));
    const f = fixture({routeEditor: bridge, descriptors});
    const routePanel = new Node("section", {"data-lab-route-editor": "", "data-can-configure": "true"});
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
        {...seed, id: 30, sample_key: "earlier", started_at: "2026-09-29T00:30:00Z"});
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
        {...seed, id: 201, provider_id: "parsehub_repo", sample_key: "second", title: "Another sample"},
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
        ...seed, id: 100 + index, sample_key: `sample-${index}`, state: "returned",
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
