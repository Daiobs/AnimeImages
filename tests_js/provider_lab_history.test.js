"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");
const {Node, documentFor} = require("./helpers/lab_dom.js");

function fixture(overrides = {}) {
    const root = new Node("main");
    const panel = new Node("section", {"data-lab-history": ""}); root.append(panel);
    for (const name of ["batches", "executions", "status", "heading", "host", "order"]) {
        panel.append(new Node(["host", "order"].includes(name) ? "select" : "div", {[`data-history-${name}`]: ""}));
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
        window: {location: {origin: "http://local.example"}, confirm: () => true},
        fetch: async (url, options) => { calls.push({url, options}); return {ok: true,
            json: async () => new URL(url).searchParams.has("batch_id") ? {ok: true, batch, executions} : {ok: true, batches: [batch]}}; },
        onSelection: (selection) => selections.push(selection),
        routeEditor: {invalidate() {}, async load() {}, selectProvider: (...args) => drafts.push(args)},
        retest: async (execution) => { retests.push(execution); return {status: "success", executed: true, history: {saved: false, error_code: "SAVE_FAILED"}, assets: []}; },
        ...overrides});
    const click = async (target) => panel.listeners.click({target, preventDefault() {}});
    return {root, panel, doc, workspace, calls, selections, drafts, retests, click, executions};
}

test("history browsing, sorting and expansion keep all candidates without media or execution requests", async () => {
    const f = fixture();
    await f.workspace.loadBatches(); await f.workspace.loadBatch(1);
    assert.equal(f.calls.length, 2);
    assert.ok(f.calls.every(({url, options}) => new URL(url).pathname === "/admin/provider-lab/history/" && !options.method));
    assert.equal(f.panel.querySelectorAll("img, video").length, 0);
    assert.equal(f.panel.querySelector("strong").textContent, "<img src=x onerror=alert(1)>");
    const card = f.panel.querySelector('[data-history-execution="10"]');
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
    assert.match(evidence, /建档时间：2026-09-01；执行时间：2026-08-21 → 2026-08-22/);
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
    assert.match(labels, /App abcdef01（有未提交改动）.*Scout 12345678.*Scout 代码指纹 98765432/);
    assert.match(labels, /目标 100/);
    assert.doesNotMatch(labels, /tool-details|target99/);
    assert.equal(button.querySelector("details"), null);
    const details = button.parentNode.querySelector("details");
    assert.equal(details.getAttribute("open"), null);
    assert.deepEqual(JSON.parse(details.querySelector("pre").textContent), metadata);
    await f.workspace.loadBatch(1);
    const card = f.panel.querySelector('[data-history-execution="10"]');
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
    const card = f.panel.querySelector('[data-history-execution="10"]');
    await f.click(card.querySelector("[data-history-route]"));
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
    const card = f.panel.querySelector('[data-history-execution="10"]');
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
        const card = f.panel.querySelector(`[data-history-execution="${id}"]`);
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

test("explicit retest calls exactly one item and persistence failure cannot trigger another call", async () => {
    const f = fixture(); await f.workspace.loadBatch(1);
    await f.click(f.panel.querySelector('[data-history-execution="10"]').querySelector("[data-history-retest]"));
    assert.equal(f.retests.length, 1); assert.equal(f.retests[0].id, 10);
    assert.match(f.panel.querySelector("[data-history-status]").textContent, /SAVE_FAILED.*不会自动重跑/);
    await f.click(f.panel.querySelector('[data-history-execution="10"]').querySelector("[data-history-retest]"));
    assert.equal(f.retests.length, 1);
    assert.equal(f.executions[0].version.animeimages, "old-head");
    f.workspace.destroy();
});

test("returned executions with unknown service outcome explain the block and cannot resend", async () => {
    for (const result of [{error_detail: "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"}, {client_status: "OUTCOME_UNKNOWN"}]) {
        const f = fixture();
        Object.assign(f.executions[0], {state: "returned", can_retest: false, result: {...result, status: "failed"}});
        await f.workspace.loadBatch(1);
        const card = f.panel.querySelector('[data-history-execution="10"]');
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
