"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");
const ledger = require("../static/js/platform_ledger.js");
const {Node, documentFor} = require("./helpers/lab_dom.js");

const ORIGIN = "http://local.example";
const A = "www.douban.com", B = "www.coolapk.com";
const flush = () => new Promise((resolve) => setImmediate(resolve));
const reply = (payload, status = 200) => ({ok: status < 400, status, json: async () => payload});

test("unknown and blocked sources do not masquerade as mature routes", () => {
    assert.equal(ledger.routeText({applicable: false, reason: "unknown_source"}), "来源未知，无法确定路线");
    assert.equal(ledger.routeText({applicable: false, reason: "source_blocked"}), "当前来源不适用通用路线");
    assert.equal(ledger.routeText({applicable: false, reason: "existing_route_not_overridden"}), "沿用成熟路线");
});

test("oversized record and asset projections disclose omitted display fields", async () => {
    const f = fixture({read: (q) => q.has("execution_id") ? {ok: true, execution: record(1, A,
        {summary_truncated: true, asset_count: 1, result: {status: "success", assets: [
            {asset_id: "a", media_type: "video", summary_truncated: true}]}})} : undefined});
    await f.workspace.activate(true); await f.workspace.open(A); await f.workspace.inspect(1);
    assert.match(text(f.detail), /大记录轻量摘要/);
    assert.match(text(f.detail), /候选摘要已缩减/);
    assert.match(text(f.detail), /原始历史未修改/);
});

test("large provider summaries have independent pages without skipping executions", async () => {
    const f = fixture({read: (q) => {
        if (q.get("view") === "platforms") return tablePayload(1, {platforms: [platform(A, {providers_truncated: true, provider_total: 5000})]});
        if (q.get("view") !== "platform") return undefined;
        const page = Number(q.get("provider_page") || 1);
        return {...detailPayload(A), provider_paging: {page, page_size: 20, pages: 250, total: 5000}};
    }});
    await f.workspace.activate(true);
    assert.match(text(f.overview), /上游摘要 1 \/ 5000/);
    await f.workspace.open(A); await f.click('[data-ledger-provider-page="2"]');
    assert.equal(f.calls.at(-1).url.searchParams.get("provider_page"), "2");
    assert.equal(f.calls.at(-1).url.searchParams.get("page"), "1");
    assert.ok(f.detail.querySelector('[data-ledger-inspect="1"]'));
    await f.workspace.open(B);
    assert.equal(f.calls.at(-1).url.searchParams.get("provider_page"), "1");
});

test("refresh reflects external route revision changes instead of stale editor state", async () => {
    let revision = 3;
    const f = fixture({read: (q) => q.get("view") === "platform" ? {...detailPayload(A),
        platform: platform(A, {current_route: snapshot(A, revision, {provider_order: ["gallery_dl_generic"]})})} : undefined});
    await f.workspace.activate(true); await f.workspace.open(A, true);
    assert.equal(f.editor.route.revision, 3);
    revision = 4;
    await f.workspace.refresh();
    assert.equal(f.editor.route, null);
    assert.match(text(f.detail.querySelector('[data-ledger-current-route]')), /gallery-dl/);
    assert.match(text(f.detail), /服务端路线版本已变化/);
    assert.equal(f.routeCalls.filter((c) => c.init.method).length, 0);
});
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const text = (node) => [node.textContent, ...node.children.map(text)].join(" ");
const descriptors = [
    ["yt_dlp_existing", "yt_dlp_generic"], ["gallery_dl_existing", "gallery_dl_generic"],
    ["parsehub_repo", "parsehub_generic"], ["videodl_repo", "videodl_repo"],
].map(([provider_id, execution_provider_id]) => ({provider_id, execution_provider_id, available: true, route_selectable: true}));
const snapshot = (host, revision = 3, extra = {}) => ({source_host: host, applicable: true, active: true,
    revision, provider_order: ["yt_dlp_generic"], effective_provider_order: ["yt_dlp_generic"],
    provider_options: {}, format_preferences: {}, allowed_provider_ids: descriptors.map((d) => d.execution_provider_id), ...extra});
const platform = (host, extra = {}) => ({source_host: host, display_name: host, content_types: ["video"],
    record_count: 205, observation_count: 204, latest_at: "2026-10-01T12:00:00Z", current_route: snapshot(host),
    providers: [{provider_id: "parsehub_repo", evidence: "confirmed", latest_status: "failed", latest_error: "TLS_FAILED"}], ...extra});
const record = (id, host = A, extra = {}) => ({id, source_host: host, source_url: `https://${host}/work/${id}`,
    title: `Work ${id}`, sample_key: `sample-${id}`, provider_id: "parsehub_repo", state: "returned",
    origin: "scout", started_at: "2026-09-01T12:00:00Z", version: {animeimages: "historic-head"},
    content_types: ["video"], options: {}, asset_count: 0, batch: {id, label: `Batch ${id}`},
    result: {status: "success"}, ...extra});
const tablePayload = (page = 1, extra = {}) => ({ok: true, page, page_size: 20, pages: 3, total: 45,
    record_count: 10000, unique_observation_count: 9999, unknown_source_count: 2,
    platforms: [platform(A), platform(B, {record_count: 0, providers: []})], ...extra});
function detailPayload(host = A, page = 1, total = 205) {
    const start = (page - 1) * 20;
    return {ok: true, platform: platform(host), executions: Array.from({length: Math.max(0, Math.min(20, total - start))},
        (_, index) => record(start + index + 1, host)),
    // Actual ledger.platform_payload contract: record pagination is nested, not top-level.
    paging: {page, page_size: 20, pages: Math.ceil(total / 20), total, returned: Math.min(20, total - start),
        has_next: start + 20 < total, next_page: start + 20 < total ? page + 1 : null}};
}

function domFixture() {
    const root = new Node("main", {"data-provider-lab": "", "data-history-url": "/admin/provider-lab/history/"});
    const panel = new Node("section", {"data-platform-ledger": ""});
    const overview = new Node("div", {"data-ledger-overview": ""});
    const detail = new Node("section", {"data-ledger-detail": ""}); detail.hidden = true;
    const filters = new Node("form", {"data-ledger-filters": ""});
    for (const key of ["q", "content_type", "provider_id", "evidence", "route_state", "sort"]) {
        filters.append(new Node("input", {name: key, value: key === "sort" ? "latest" : ""}));
    }
    overview.append(filters);
    for (const key of ["status", "scope", "table", "pagination", "refresh"]) overview.append(new Node("div", {[`data-ledger-${key}`]: ""}));
    panel.append(overview, detail); root.append(panel);
    const routePanel = new Node("section", {"data-lab-route-editor": "", "data-can-configure": "true"});
    for (const key of ["status", "scope", "current", "order", "add", "confirm", "save", "reset", "cancel", "fields", "reference", "confirmation-summary", "videodl"]) {
        routePanel.append(new Node("div", {[`data-route-${key}`]: ""}));
    }
    for (const name of ["max_height", "preferred_video_codec", "preferred_container", "route_videodl_mode", "route_videodl_backend"]) {
        routePanel.append(new Node("input", {name}));
    }
    root.append(routePanel);
    return {root, panel, overview, detail, filters, routePanel, doc: documentFor(root)};
}

function fixture(options = {}) {
    const f = domFixture();
    f.calls = []; f.routeCalls = []; f.instant = []; f.saved = 0;
    f.window = {location: {origin: ORIGIN}, confirm: () => true};
    f.fetch = async (url, init = {}) => {
        const parsed = new URL(url, ORIGIN);
        assert.equal(parsed.origin, ORIGIN, "Never request external media");
        assert.equal(parsed.pathname, "/admin/provider-lab/history/", "No run, resource, token or probe endpoint");
        assert.equal(init.method, undefined, "Ledger browsing is GET-only");
        f.calls.push({url: parsed, init});
        if (options.read) {
            const overridden = options.read(parsed.searchParams, f);
            if (overridden !== undefined) return reply(await overridden);
        }
        const q = parsed.searchParams;
        if (q.has("execution_id")) return reply({ok: true, execution: record(Number(q.get("execution_id")), f.workspace.host,
            {result: {status: "success", assets: []}})});
        if (q.get("view") === "platform") return reply(detailPayload(q.get("source_host"), Number(q.get("page") || 1)));
        assert.equal(q.get("view"), "platforms", "No implicit batch lookup");
        return reply(tablePayload(Number(q.get("page") || 1)));
    };
    f.editor = lab.createRouteEditor(f.doc, {descriptors, url: "/provider-lab/routes/", csrf: () => "test-csrf",
        window: f.window, getHost: () => f.sourceOverride?.host ?? f.workspace.host,
        getSource: () => f.sourceOverride?.source ?? f.workspace.host,
        onChange: () => f.workspace?.currentRoute(), onSaved: () => { f.saved += 1; },
        fetch: async (url, init = {}) => {
            const parsed = new URL(url, ORIGIN);
            assert.equal(parsed.origin, ORIGIN); assert.equal(parsed.pathname, "/provider-lab/routes/");
            f.routeCalls.push({url: parsed, init});
            if (options.routeRead) {
                const overridden = options.routeRead(parsed, init, f);
                if (overridden !== undefined) return overridden;
            }
            if (init.method) {
                const body = JSON.parse(init.body);
                return reply({ok: true, route: snapshot(body.source_host || new URL(body.source_url).hostname,
                    body.expected_revision + 1, init.method === "DELETE" ? {active: false, provider_order: []}
                        : {provider_order: body.provider_order, effective_provider_order: body.provider_order,
                            provider_options: body.provider_options, format_preferences: body.format_preferences})});
            }
            const host = parsed.searchParams.get("source_host") || new URL(parsed.searchParams.get("source_url")).hostname;
            return reply({ok: true, route: snapshot(host)});
        }});
    f.workspace = ledger.createWorkspace(f.doc, {root: f.root, descriptors, routeEditor: f.editor,
        window: f.window, url: "/admin/provider-lab/history/", fetch: f.fetch, lab,
        openInstant: (...args) => f.instant.push(args)});
    f.click = async (selector) => {
        const target = typeof selector === "string" ? f.panel.querySelector(selector) : selector;
        assert.ok(target, `Expected rendered target: ${selector}`);
        return f.panel.listeners.click({target, preventDefault() {}});
    };
    f.routeClick = (selector) => f.routePanel.listeners.click({target: f.routePanel.querySelector(selector)});
    f.order = () => f.routePanel.querySelectorAll('[data-route-move="remove"]').map((n) => n.dataset.providerId);
    f.field = (name) => f.routePanel.querySelector(`[name="${name}"]`);
    f.confirm = () => { const target = f.routePanel.querySelector("[data-route-confirm]"); target.checked = true; f.routePanel.listeners.change({target}); };
    return f;
}

test("controller defaults to the platform table without a batch selection or execution", async () => {
    const f = domFixture(), calls = [];
    const form = new Node("form", {"data-provider-lab-form": ""});
    const source = new Node("textarea"); form.elements = {source}; form.append(source); f.root.append(form);
    const controller = lab.createController(f.doc, {window: {location: {origin: ORIGIN}}, navigator: {}, descriptors,
        ledgerApi: ledger, fetch: async (url, init) => {
            calls.push({url: new URL(url, ORIGIN), init}); return reply(tablePayload());
        }});
    await flush();
    assert.equal(f.root.dataset.labView, "platforms");
    assert.equal(controller.platformWorkspace.active, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url.searchParams.get("view"), "platforms");
    assert.equal(calls[0].url.searchParams.has("batch_id"), false);
    assert.equal(calls[0].init.method, undefined);
    assert.match(text(f.panel), /10000.*9999/);
    assert.ok(f.panel.querySelectorAll("small").some((node) => node.title === "TLS_FAILED"));
    assert.equal(f.panel.querySelectorAll("[data-ledger-open]").length, 3);
});

test("search, filters and table pagination remain server-side and survive detail return", async () => {
    const f = fixture(); await f.workspace.activate(true);
    await f.click('[data-ledger-page="2"]');
    const values = {q: "douban", content_type: "video", provider_id: "parsehub_repo", evidence: "confirmed", route_state: "manual", sort: "host"};
    for (const [key, value] of Object.entries(values)) f.filters.querySelector(`[name="${key}"]`).value = value;
    await f.filters.listeners.submit({preventDefault() {}});
    assert.equal(f.calls.at(-1).url.searchParams.get("page"), "1");
    for (const [key, value] of Object.entries(values)) assert.equal(f.calls.at(-1).url.searchParams.get(key), value);
    await f.click('[data-ledger-page="2"]');
    await f.workspace.open(A); await f.click("[data-ledger-back]");
    assert.equal(f.calls.at(-1).url.searchParams.get("page"), "2");
    for (const [key, value] of Object.entries(values)) assert.equal(f.calls.at(-1).url.searchParams.get(key), value);
    assert.ok(f.calls.every(({url}) => !url.searchParams.has("batch_id")));
    assert.equal(f.routeCalls.length, 0);
});

test("actual backend nested paging reaches records beyond 200 without truncating the host", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A);
    assert.match(text(f.detail), /205/);
    for (let page = 2; page <= 11; page += 1) {
        await f.click(`[data-ledger-detail-page="${page}"]`);
        assert.equal(f.calls.at(-1).url.searchParams.get("page"), String(page));
    }
    assert.ok(f.detail.querySelector('[data-ledger-inspect="205"]'));
    assert.equal(f.detail.querySelectorAll("[data-ledger-inspect]").length, 5);
    assert.equal(f.calls.at(-1).url.searchParams.get("page_size"), "20");
});

test("same-work comparison requests an execution ID across batches, never a URL query", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A);
    await f.click('[data-ledger-work="1"]');
    const q = f.calls.at(-1).url.searchParams;
    assert.equal(q.get("work_id"), "1"); assert.equal(q.get("source_host"), A);
    assert.equal(q.has("source_url"), false); assert.equal(q.has("batch_id"), false);
    await f.click("[data-ledger-all-works]");
    assert.equal(f.calls.at(-1).url.searchParams.has("work_id"), false);
});

test("record inspection is lazy and candidate expansion creates no media or token requests", async () => {
    const assets = Array.from({length: 130}, (_, i) => ({asset_id: `candidate-${i}`, media_type: "video", height: 720 + i,
        url: "https://media.invalid/secret-file", resource_token: "secret-token", thumbnail_url: "https://media.invalid/thumb"}));
    const f = fixture({read: (q) => q.has("execution_id") ? {ok: true, execution: record(1, A,
        {asset_count: 130, result: {status: "success", assets, candidate_count: 130}})} : undefined});
    await f.workspace.activate(true); await f.workspace.open(A);
    assert.equal(f.calls.filter(({url}) => url.searchParams.has("execution_id")).length, 0);
    await f.click('[data-ledger-inspect="1"]');
    assert.equal(f.calls.filter(({url}) => url.searchParams.has("execution_id")).length, 1);
    assert.equal(f.detail.querySelectorAll(".provider-lab-asset").length, 64);
    await f.click("[data-ledger-expand]"); await f.click("[data-ledger-expand]");
    assert.equal(f.detail.querySelectorAll(".provider-lab-asset").length, 130);
    assert.equal(f.detail.querySelectorAll("video, audio, img, iframe, [src], [href]").length, 0);
    assert.doesNotMatch(text(f.detail), /secret-token|secret-file|media\.invalid/);
    assert.equal(f.calls.length, 3); assert.equal(f.routeCalls.length, 0);
});

test("candidate details consume backend asset_paging beyond the first 100 and past 200", async () => {
    const f = fixture({read: (q) => {
        if (!q.has("execution_id")) return undefined;
        const page = Number(q.get("asset_page") || 1), start = (page - 1) * 100;
        return {ok: true, execution: record(1, A, {asset_count: 205, assets_omitted: page < 3,
            asset_paging: {page, page_size: 100, pages: 3, total: 205, returned: Math.min(100, 205 - start)},
            result: {status: "success", assets: Array.from({length: Math.min(100, 205 - start)},
                (_, i) => ({asset_id: `asset-${start + i + 1}`, media_type: "video", height: start + i + 1}))}})};
    }});
    await f.workspace.activate(true); await f.workspace.open(A); await f.workspace.inspect(1);
    for (let i = 0; i < 8; i += 1) {
        const more = f.detail.querySelector("[data-ledger-expand], [data-ledger-asset-page]");
        if (!more || more.disabled) break;
        await f.click(more);
    }
    assert.ok(f.calls.some(({url}) => url.searchParams.get("asset_page") === "3"), "All retained candidate pages must be reachable");
    assert.match(text(f.detail), /205/);
});

test("failed table reads remain errors rather than zero-platform success", async () => {
    const f = fixture({read: () => Promise.reject(new Error("READ_FAILED"))});
    await f.workspace.activate(true);
    assert.match(text(f.panel), /READ_FAILED/);
    assert.doesNotMatch(text(f.panel), /已收录 0/);
});

test("late table filter responses cannot replace the latest query", async () => {
    const old = deferred();
    const f = fixture({read: (q) => q.get("q") === "old" ? old.promise : undefined});
    await f.workspace.activate(true);
    f.filters.querySelector('[name="q"]').value = "old";
    const pending = f.filters.listeners.submit({preventDefault() {}});
    f.filters.querySelector('[name="q"]').value = "new";
    await f.filters.listeners.submit({preventDefault() {}});
    old.resolve(tablePayload(1, {platforms: [platform("stale.douban.com")]})); await pending;
    assert.doesNotMatch(text(f.panel), /stale\.douban/);
});

test("late detail and record responses never cross the selected hostname", async () => {
    const oldDetail = deferred(), oldRecord = deferred(); let delayDetail = true;
    const f = fixture({read: (q) => {
        if (q.get("source_host") === A && delayDetail) return oldDetail.promise;
        if (q.has("execution_id")) return oldRecord.promise;
    }});
    await f.workspace.activate(true);
    const first = f.workspace.open(A); await f.workspace.open(B);
    oldDetail.resolve(detailPayload(A)); await first;
    assert.equal(f.workspace.host, B); assert.doesNotMatch(text(f.detail), /www\.douban\.com/);
    delayDetail = false; await f.workspace.open(A);
    const inspecting = f.workspace.inspect(1); await f.workspace.open(B);
    oldRecord.resolve({ok: true, execution: record(1, A, {title: "STALE_RECORD", result: {status: "success", assets: []}})});
    await inspecting;
    assert.equal(f.detail.querySelector("[data-ledger-record]").children.length, 0);
});

test("supplemental action only opens instant mode with a safe source or empty input", async () => {
    const safe = `https://${A}/work/1`;
    const f = fixture({read: (q) => q.get("view") === "platforms" ? tablePayload(1,
        {platforms: [platform(A, {safe_source_url: safe}), platform(B)]}) : undefined});
    await f.workspace.activate(true); await f.click(`[data-ledger-test="${A}"]`);
    assert.deepEqual(f.instant, [[safe, A]]); assert.equal(f.calls.length, 1);
    await f.click(`[data-ledger-test="${B}"]`);
    assert.deepEqual(f.instant.at(-1), ["", B]);
    assert.equal(f.calls.at(-1).url.searchParams.get("page_size"), "1");
    assert.equal(f.routeCalls.length, 0);
});

test("a failed stale supplemental lookup must not open instant mode for the previous host", async () => {
    const pending = deferred();
    const f = fixture({read: (q) => q.get("source_host") === A ? pending.promise : undefined});
    await f.workspace.activate(true);
    const opening = f.click(`[data-ledger-test="${A}"]`);
    await f.workspace.open(B); pending.reject(new Error("OLD_LOOKUP_FAILED")); await opening;
    assert.deepEqual(f.instant, [], "Old host lookup failure must not redirect a new selection");
});

test("host drafts survive paging, close and A-B-A switches; cancel never writes", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A, true);
    assert.deepEqual([...f.routeCalls[0].url.searchParams], [["source_host", A]]);
    f.editor.selectProvider("parsehub_repo"); f.field("max_height").value = "2160";
    await f.workspace.refresh(); await f.click(`[data-ledger-route="${A}"]`);
    assert.deepEqual(f.order(), ["parsehub_generic"]); assert.equal(f.routeCalls.length, 1);
    await f.workspace.open(B, true); assert.deepEqual(f.order(), ["yt_dlp_generic"]);
    await f.workspace.open(A, true); assert.deepEqual(f.order(), ["parsehub_generic"]);
    assert.equal(f.field("max_height").value, "2160");
    f.workspace.activate(false); assert.equal(f.routePanel.parentNode, f.root);
    await f.workspace.activate(true); await f.click(`[data-ledger-route="${A}"]`);
    assert.deepEqual(f.order(), ["parsehub_generic"]);
    f.routeClick("[data-route-cancel]");
    assert.deepEqual(f.order(), ["yt_dlp_generic"]); assert.equal(f.field("max_height").value, "");
    await f.workspace.open(B, true); await f.workspace.open(A, true);
    assert.deepEqual(f.order(), ["yt_dlp_generic"]);
    assert.ok(f.routeCalls.every(({init}) => !init.method)); assert.equal(f.saved, 0);
});

test("record primary and fallback actions modify only the current host draft", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A);
    await f.workspace.inspect(1);
    await f.click('[data-ledger-draft="primary"]');
    assert.deepEqual(f.order(), ["parsehub_generic", "yt_dlp_generic"]);
    const current = text(f.detail.querySelector("[data-ledger-current-route]"));
    assert.match(current, /yt-dlp/); assert.doesNotMatch(current, /ParseHub/);
    await f.click('[data-ledger-draft="fallback"]');
    assert.deepEqual(f.order(), ["parsehub_generic", "yt_dlp_generic"]);
    assert.equal(f.routeCalls.length, 1); assert.equal(f.routeCalls[0].init.method, undefined);
    f.routeClick("[data-route-cancel]"); assert.deepEqual(f.order(), ["yt_dlp_generic"]);
    assert.equal(f.calls.length, 3); assert.equal(f.saved, 0);
});

test("late asset-page responses cannot append candidates into another host's record", async () => {
    const pending = deferred();
    const f = fixture({read: (q) => {
        if (q.has("asset_page")) return pending.promise;
        if (q.has("execution_id")) return {ok: true, execution: record(1, A, {asset_count: 2,
            asset_paging: {page: 1, page_size: 1, pages: 2, total: 2},
            result: {status: "success", assets: [{asset_id: "first", media_type: "video"}]}})};
    }});
    await f.workspace.activate(true); await f.workspace.open(A); await f.workspace.inspect(1);
    const loading = f.click("[data-ledger-expand]"); await f.workspace.open(B);
    pending.resolve({ok: true, execution: record(1, A, {asset_paging: {page: 2, pages: 2},
        result: {assets: [{asset_id: "late", quality_label: "STALE_ASSET"}]}})});
    await loading;
    assert.doesNotMatch(text(f.detail), /STALE_ASSET/);
    assert.equal(f.detail.querySelector("[data-ledger-record]").children.length, 0);
});

test("a saved revision change invalidates an older retained host draft", async () => {
    let revision = 3;
    const f = fixture({routeRead: (url) => reply({ok: true,
        route: snapshot(url.searchParams.get("source_host"), revision)})});
    await f.workspace.activate(true); await f.workspace.open(A, true);
    f.editor.selectProvider("parsehub_repo"); f.field("max_height").value = "2160";
    await f.workspace.open(B, true); revision = 4; await f.workspace.open(A, true);
    assert.equal(f.editor.route.revision, 4); assert.deepEqual(f.order(), ["yt_dlp_generic"]);
    assert.equal(f.field("max_height").value, "");
    assert.ok(f.routeCalls.every(({init}) => !init.method));
});

test("host-only route persists only after confirmation with exact scope, CSRF and revision", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A, true);
    f.editor.selectProvider("parsehub_repo");
    await f.editor.persist(false); assert.equal(f.routeCalls.length, 1);
    f.confirm(); await f.editor.persist(false);
    const saved = f.routeCalls.at(-1);
    assert.equal(saved.init.method, "POST"); assert.equal(saved.init.headers["X-CSRFToken"], "test-csrf");
    assert.equal(saved.init.credentials, "same-origin");
    assert.deepEqual(JSON.parse(saved.init.body), {source_host: A, expected_revision: 3,
        provider_order: ["parsehub_generic"], provider_options: {}, format_preferences: {}});
    assert.equal(f.editor.route.revision, 4); assert.equal(f.saved, 1);
    f.window.confirm = () => false; await f.editor.persist(true); assert.equal(f.routeCalls.length, 2);
    f.window.confirm = () => true; await f.editor.persist(true);
    assert.equal(f.routeCalls.at(-1).init.method, "DELETE");
    assert.deepEqual(JSON.parse(f.routeCalls.at(-1).init.body), {source_host: A, expected_revision: 4});
    assert.equal(f.editor.route.revision, 5);
});

test("late route GET and POST responses cannot replace another host's editor", async () => {
    const get = deferred(), post = deferred(); let delayGet = true;
    const f = fixture({routeRead: (url, init) => {
        if (init.method === "POST") return post.promise;
        if (delayGet && url.searchParams.get("source_host") === A) return get.promise;
    }});
    await f.workspace.activate(true);
    const first = f.workspace.open(A, true); await flush(); await f.workspace.open(B, true);
    get.resolve(reply({ok: true, route: snapshot(A)})); await first;
    assert.equal(f.editor.route.source_host, B);
    delayGet = false; await f.workspace.open(A, true); f.editor.selectProvider("parsehub_repo"); f.confirm();
    const saving = f.editor.persist(false); await f.workspace.open(B, true);
    post.resolve(reply({ok: true, route: snapshot(A, 4)})); await saving;
    assert.equal(f.editor.route.source_host, B); assert.equal(f.editor.route.revision, 3);
    assert.equal(f.saved, 0);
});

test("revision conflicts do not retry writes or silently overwrite newer server state", async () => {
    let revision = 3;
    const f = fixture({routeRead: (url, init) => init.method ? reply({ok: false, error_code: "REVISION_CONFLICT"}, 409)
        : reply({ok: true, route: snapshot(url.searchParams.get("source_host"), revision)})});
    await f.workspace.activate(true); await f.workspace.open(A, true);
    f.editor.selectProvider("parsehub_repo"); f.confirm(); await f.editor.persist(false);
    assert.equal(f.editor.route, null); assert.match(text(f.routePanel), /不会覆盖较新的版本/);
    await f.editor.persist(false); assert.equal(f.routeCalls.length, 2);
    revision = 4; await f.editor.load(); assert.equal(f.editor.route.revision, 4);
    assert.deepEqual(f.order(), ["yt_dlp_generic"]);
});

test("preserved same-host drafts update selector kind when moving from URL to host-only", async () => {
    const f = fixture();
    f.sourceOverride = {source: `https://${A}/work/1`, host: ""};
    await f.editor.load(); f.editor.selectProvider("parsehub_repo");
    f.sourceOverride = {source: A, host: A}; await f.editor.load({preserveDraft: true});
    f.confirm(); await f.editor.persist(false);
    const body = JSON.parse(f.routeCalls.at(-1).init.body);
    assert.equal(body.source_host, A); assert.equal(Object.hasOwn(body, "source_url"), false);
    assert.deepEqual(body.provider_order, ["parsehub_generic"]);
});

test("restored evidence drafts keep the visible reference that will be submitted", async () => {
    const f = fixture(); await f.workspace.activate(true); await f.workspace.open(A, true);
    f.editor.selectProvider("parsehub_repo", record(77));
    const reference = text(f.routePanel.querySelector("[data-route-reference]")); assert.match(reference, /historic-head/);
    await f.workspace.open(B, true); await f.workspace.open(A, true);
    assert.equal(text(f.routePanel.querySelector("[data-route-reference]")), reference);
    f.confirm(); await f.editor.persist(false);
    assert.equal(JSON.parse(f.routeCalls.at(-1).init.body).reference_execution_id, 77);
});

test("work grouping uses retained URLs or opaque work keys without merging unrelated missing sources", () => {
    const rows = [record(1), record(2, A, {source_url: record(1).source_url}),
        record(3, A, {source_url: "", work_key: "opaque-query-work"}),
        record(4, A, {source_url: "", work_key: "opaque-query-work"}),
        record(5, A, {source_url: "", work_key: "different-query-work"}),
        record(6, A, {source_url: "", work_key: ""}), record(7, A, {source_url: "", work_key: ""})];
    assert.deepEqual(ledger.groupWorks(rows).map((group) => group.map((r) => r.id)), [[1, 2], [3, 4], [5], [6], [7]]);
});

test("linked observation grouping preserves true retests and independent providers", () => {
    const rows = [record(100, A, {application_record_id: 10}), record(10, A, {origin: "lab"}),
        record(101, A, {application_record_id: 10}), record(11, A, {source_url: record(10).source_url,
            started_at: "2026-10-01T12:00:00Z"}),
        record(102, A, {provider_id: "gallery_dl_existing", application_record_id: 10})];
    assert.deepEqual(ledger.groupObservations(rows).map((group) => group.map((r) => r.id)), [[100, 10, 101], [11], [102]]);
});

test("same-page linked copies render one observation card and keep independent provenance inspection", async () => {
    const source = `https://${A}/same-work`;
    const rows = [record(100, A, {source_url: source, application_record_id: 10}),
        record(10, A, {source_url: source, origin: "lab"}), record(11, A, {source_url: source,
            started_at: "2026-10-01T12:00:00Z"})];
    const f = fixture({read: (q) => q.get("view") === "platform"
        ? {...detailPayload(A, 1, 3), executions: rows} : undefined});
    await f.workspace.activate(true); await f.workspace.open(A);
    assert.equal(f.detail.querySelectorAll(".platform-ledger-work").length, 1);
    const cards = f.detail.querySelectorAll(".platform-ledger-observation");
    assert.equal(cards.length, 2, "Linked copy is provenance, not another independent observation");
    assert.equal(cards[0].children[0].dataset.ledgerInspect, "10", "Original application record is the primary card");
    assert.match(text(cards[0]), /关联副本/);
    assert.ok(cards[0].querySelector('[data-ledger-inspect="100"]'));
    assert.equal(cards[1].children[0].dataset.ledgerInspect, "11", "Later real retest remains independent");
    await f.click('[data-ledger-inspect="100"]');
    assert.equal(f.calls.at(-1).url.searchParams.get("execution_id"), "100");
    assert.equal(f.routeCalls.length, 0);
});

test("redacted query-bearing works compare by work_key and ID without restoring a source URL", async () => {
    const rows = [record(1, A, {source_url: "", work_key: "opaque-a", can_compare_work: true}),
        record(2, A, {source_url: "", work_key: "opaque-a", can_compare_work: true, provider_id: "gallery_dl_existing"}),
        record(3, A, {source_url: "", work_key: "opaque-b", can_compare_work: true})];
    const f = fixture({read: (q) => q.get("view") === "platform"
        ? {...detailPayload(A, 1, 3), executions: rows} : undefined});
    await f.workspace.activate(true); await f.workspace.open(A);
    assert.equal(f.detail.querySelectorAll(".platform-ledger-work").length, 2);
    await f.click('[data-ledger-work="1"]');
    const q = f.calls.at(-1).url.searchParams;
    assert.equal(q.get("work_id"), "1"); assert.equal(q.has("source_url"), false);
    assert.equal(q.has("work_key"), false);
    assert.doesNotMatch(text(f.detail), /opaque-a|opaque-b|https:\/\//);
});

test("top-level detail pagination remains compatible with the nested backend envelope", async () => {
    const f = fixture({read: (q) => {
        if (q.get("view") !== "platform") return undefined;
        const payload = detailPayload(A, Number(q.get("page") || 1), 21);
        const {paging, ...rest} = payload; return {...rest, ...paging};
    }});
    await f.workspace.activate(true); await f.workspace.open(A);
    await f.click('[data-ledger-detail-page="2"]');
    assert.equal(f.calls.at(-1).url.searchParams.get("page"), "2");
    assert.ok(f.detail.querySelector('[data-ledger-inspect="21"]'));
    assert.equal(f.detail.querySelector('[data-ledger-detail-page="3"]').disabled, true);
});
