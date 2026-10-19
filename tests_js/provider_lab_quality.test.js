"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("comparison runs every selected provider with concurrency three, never a first-success winner", async () => {
    const releases = new Map();
    let active = 0;
    let peak = 0;
    const results = [];
    const queue = lab.createExecutionQueue({concurrency: 3,
        execute(id) {
            active += 1; peak = Math.max(peak, active);
            return new Promise((resolve) => releases.set(id, () => {
                active -= 1;
                resolve({status: id === "D" ? "not_configured" : "success", provider_id: id,
                    assets: id === "B" ? [{asset_id: "video", height: 2160, acodec: "none"}, {asset_id: "audio"}] : [{asset_id: id}]});
            }));
        }, onState(job) { if (job.result) results.push(job); },
    });
    queue.start(["A", "B", "C", "D", "E", "F"], {source: "one"});
    await flush();
    assert.equal(active, 3);
    releases.get("A")(); await flush();
    assert.equal(queue.jobs.get("A").status, "success");
    assert.equal(queue.jobs.get("B").status, "running");
    assert.equal(queue.jobs.get("D").status, "running");
    for (const id of ["B", "C", "D", "E", "F"]) { releases.get(id)(); await flush(); }
    assert.equal(peak, 3);
    assert.equal(results.length, 6);
    assert.equal(queue.jobs.get("B").result.assets.length, 2);
    assert.equal(queue.jobs.get("C").status, "success");
    queue.invalidate();
});

test("cancel retains finished results, prevents queued calls, and retry is single-flight", async () => {
    const pending = new Map();
    const called = [];
    const queue = lab.createExecutionQueue({concurrency: 1,
        execute(id) { called.push(id); return new Promise((resolve) => pending.set(id, resolve)); },
    });
    queue.start(["A", "B", "C"], {});
    await flush(); pending.get("A")({status: "success"}); await flush();
    assert.equal(queue.retry("B"), false);
    queue.cancel(); await flush();
    assert.equal(queue.jobs.get("A").status, "success");
    assert.equal(queue.jobs.get("B").status, "cancelled");
    assert.equal(queue.jobs.get("C").status, "cancelled");
    assert.deepEqual(called, ["A", "B"]);
    assert.equal(queue.retry("B"), true);
    assert.equal(queue.retry("B"), false);
    await flush(); pending.get("B")({status: "success"}); await flush();
    assert.equal(queue.jobs.get("A").status, "success");
    queue.invalidate();
});

test("new source rejects late old results even when an executor ignores abort", async () => {
    const pending = [];
    const published = [];
    const queue = lab.createExecutionQueue({execute(id, context) {
        return new Promise((resolve) => pending.push({source: context.source, resolve}));
    }, onState(job) { if (job.result) published.push(job.result.title); }});
    queue.start(["A"], {source: "old"}); await flush();
    queue.start(["A"], {source: "new"}); await flush();
    pending[0].resolve({status: "success", title: "old"}); await flush();
    pending[1].resolve({status: "success", title: "new"}); await flush();
    assert.deepEqual(published, ["new"]);
    queue.invalidate();
});

test("provider timeout begins on execution, not while waiting in queue", async () => {
    const starts = [];
    const queue = lab.createExecutionQueue({concurrency: 1, timeoutMs: 25, budgetMs: 200,
        execute(id) { starts.push(id); return new Promise(() => {}); }});
    queue.start(["A", "B"], {});
    await new Promise((resolve) => setTimeout(resolve, 35));
    assert.equal(queue.jobs.get("A").status, "timeout");
    assert.equal(queue.jobs.get("B").status, "running");
    assert.deepEqual(starts, ["A", "B"]);
    queue.invalidate();
});

test("overall budget explicitly marks unexecuted jobs without dropping successful results", async () => {
    const queue = lab.createExecutionQueue({concurrency: 1, timeoutMs: 100, budgetMs: 20,
        execute() { return new Promise(() => {}); }});
    queue.start(["A", "B"], {});
    await new Promise((resolve) => setTimeout(resolve, 35));
    assert.equal(queue.jobs.get("B").status, "skipped");
    assert.match(queue.jobs.get("B").reason, /预算/);
    queue.invalidate();
});

test("quality ordering does not invent dimensions or mutate original format order", () => {
    const assets = [{asset_id: "label-only", media_type: "video", quality_label: "4K"},
        {asset_id: "1080", media_type: "video", width: 1920, height: 1080}, {asset_id: "2160", media_type: "video", width: 3840, height: 2160}];
    assert.deepEqual(lab.qualityOrder(assets).map((asset) => asset.asset_id), ["2160", "1080", "label-only"]);
    assert.equal(assets[0].asset_id, "label-only");
    assert.equal(lab.knownBoolean(undefined, "需要"), "未知");
    assert.equal(lab.knownBoolean(false, "需要"), "否");
});

test("route save transmits reusable preferences and exact revision, never current candidate identity", () => {
    const selection = {resource_token: "secret", url: "https://cdn.example/signed", format_id: "transient"};
    const body = lab.routeRequestBody("https://source.example/post/1", {applicable: true, revision: 7}, {
        provider_order: ["gallery_dl_generic", "parsehub_generic"],
        provider_options: {}, format_preferences: {max_height: 2160}, selection,
    });
    assert.equal(body.expected_revision, 7);
    assert.deepEqual(body.provider_order, ["gallery_dl_generic", "parsehub_generic"]);
    assert.doesNotMatch(JSON.stringify(body), /secret|signed|transient/);
    assert.deepEqual(lab.routeRequestBody("source", {applicable: true, revision: 8}, {}, true), {
        source_url: "source", expected_revision: 8,
    });
    assert.throws(() => lab.routeRequestBody("source", {applicable: false, revision: 0}, {}));
});

test("generic diagnostic export strips media capabilities and private URL fields", () => {
    const value = lab.diagnosticResult({provider_id: "A", assets: [{asset_id: "1", resource_token: "secret",
        url: "https://cdn.example/signed", width: 1920}], metadata: {title: "title"}, headers: {Authorization: "secret"}});
    assert.equal(value.assets[0].width, 1920);
    assert.doesNotMatch(JSON.stringify(value), /secret|signed|cdn\.example/);
});

test("different content evidence warns without discarding either result", () => {
    const results = [{status: "success", metadata: {content_id: "1"}, assets: [{duration_seconds: 10}]},
        {status: "success", metadata: {content_id: "2"}, assets: [{duration_seconds: 60}, {}]}];
    assert.equal(lab.comparisonWarnings(results).length, 3);
    assert.equal(results.length, 2);
});

test("bounded scope does not claim complete collection even with known truncation count", () => {
    for (const scope of [{bounded: true, completion: "unknown"}, "legacy bounded scope"]) {
        const message = lab.scopeMessage({unexpanded_scope: scope, truncated_count: 3});
        assert.match(message, /完整性未知/);
        assert.match(message, /3 项未展开/);
        assert.doesNotMatch(message, /object Object/);
    }
    assert.equal(lab.scopeMessage({}), "");
});

test("saved route evidence requires a unique explicit backend and reports per-backend adoption", () => {
    const exact = {mode: "specific_backend", backend_category: "common", backend_id: "GVVideoClient"};
    const inventory = {items: [{backend_category: "common", backend_id: "GVVideoClient", saved_route_selectable: true}]};
    assert.deepEqual(lab.exactHistoryBackend(exact), {backend_category: "common", backend_id: "GVVideoClient"});
    assert.deepEqual(lab.exactHistoryBackend({mode: "specific_backend", backend_ids: ["LegacyClient"]}),
        {backend_category: "dedicated", backend_id: "LegacyClient"});
    assert.deepEqual(lab.exactHistoryBackend({...exact, backend_ids: ["GVVideoClient"]}),
        {backend_category: "common", backend_id: "GVVideoClient"});
    assert.deepEqual(lab.exactHistoryBackend({mode: "specific_backend", backend_id: "LegacyClient"}),
        {backend_category: "dedicated", backend_id: "LegacyClient"});
    for (const options of [{...exact, backend_ids: ["OtherClient"]}, {...exact, backend_category: "unknown"},
        {mode: "specific_backend"}, {...exact, mode: "common_only"}, {...exact, mode: "reviewed_auto"}]) {
        assert.equal(lab.exactHistoryBackend(options), null);
        assert.match(lab.savedRouteHistoryNote(options, inventory), /重新选择精确 Backend.*不从首个媒体/);
    }
    assert.match(lab.savedRouteHistoryNote(exact, inventory), /Common · GVVideoClient：可恢复草稿.*重新确认/);
    assert.match(lab.savedRouteHistoryNote({...exact, backend_id: "OtherClient"}, inventory), /OtherClient：当前 Backend 不可固定/);
    inventory.items[0].saved_route_selectable = false;
    assert.match(lab.savedRouteHistoryNote(exact, inventory), /当前 Backend 不可固定/);
});

test("exact history backend rejects malformed fields and never deduplicates explicit backend lists", () => {
    const exact = {mode: "specific_backend", backend_category: "common", backend_id: "GVVideoClient"};
    for (const value of [undefined, null, false, 0, [], {}, ""]) {
        assert.equal(lab.exactHistoryBackend({...exact, backend_category: value}), null);
        assert.equal(lab.exactHistoryBackend({...exact, backend_id: value}), null);
        assert.equal(lab.exactHistoryBackend({...exact, backend_id: value, backend_ids: ["GVVideoClient"]}), null);
    }
    for (const ids of [undefined, null, false, 0, "GVVideoClient", {}, [], [""], [null], [42], [{}], [[]],
        ["GVVideoClient", "GVVideoClient"], ["GVVideoClient", "AnyFetcherVideoClient"]]) {
        assert.equal(lab.exactHistoryBackend({...exact, backend_ids: ids}), null);
        assert.equal(lab.exactHistoryBackend({mode: "specific_backend", backend_ids: ids}), null);
    }
});

test("video quality order leaves image and audio positions in source order", () => {
    const rows = [{asset_id: "img1", media_type: "image", width: 100, height: 100},
        {asset_id: "low", media_type: "video", width: 1280, height: 720},
        {asset_id: "img2", media_type: "image", width: 8000, height: 8000},
        {asset_id: "audio", media_type: "audio"}, {asset_id: "high", media_type: "video", width: 3840, height: 2160}];
    assert.deepEqual(lab.qualityOrder(rows).map((row) => row.asset_id), ["img1", "high", "img2", "audio", "low"]);
});

test("nullable merge and independent audio are not inferred from embedded tracks", () => {
    const {Node, documentFor} = require("./helpers/lab_dom");
    const doc = documentFor(new Node());
    const fragment = lab.metaFragment(doc, {media_type: "video", requires_merge: null,
        audio_asset_present: null, audio_tracks: "present", video_tracks: "present", height: 2160, width: null});
    const textOf = (node) => node.textContent + node.children.map(textOf).join("");
    const text = textOf(fragment);
    assert.match(text, /合并 未知/);
    assert.match(text, /未知×2160/);
    assert.match(text, /独立音频资源 未知/);
    assert.match(text, /内嵌视频轨 \/ 音频轨 存在 \/ 存在/);
    assert.match(text, /语言 未知/);
    assert.match(textOf(lab.metaFragment(doc, {language: 'deu'})), /语言 deu/);
    const hostile = lab.metaFragment(doc, {language: '<script>not executable</script>'});
    assert.match(textOf(hostile), /语言 <script>not executable<\/script>/);
    assert.equal(hostile.querySelector('script'), null);
});
