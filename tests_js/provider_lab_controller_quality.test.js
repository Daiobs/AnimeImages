"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");
const {Node, documentFor} = require("./helpers/lab_dom.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('non-JSON diagnostics retain stage, status and MIME category without response text', async () => {
    const fs = require('node:fs');
    const vm = require('node:vm');
    const path = require('node:path');
    const source = fs.readFileSync(path.join(__dirname, '../xhs_download/services/platforms/kukutool_provider.js'), 'utf8');
    const context = vm.createContext({readText: async () => '<html>secret account body</html>'});
    vm.runInContext(source.slice(source.indexOf('async function readJson('), source.indexOf('async function readText(')), context);
    vm.runInContext(source.slice(source.indexOf('function safeFailureDetail('), source.indexOf('main().catch(')), context);
    for (const phase of ['AUTH', 'PARSE']) {
        for (const [mime, category] of [['text/html; charset=utf-8', 'HTML'], ['application/json', 'JSON'],
            ['text/plain', 'TEXT'], ['', 'MISSING'], ['application/octet-stream; secret=value', 'OTHER']]) {
            for (const status of [200, 403, 502]) {
                await assert.rejects(context.readJson({status, headers: {get: () => mime}}, phase), error => {
                    const detail = context.safeFailureDetail(error);
                    assert.equal(detail, `SERVICE_JSON_${phase}_HTTP_${status}_TYPE_${category}`);
                    assert.doesNotMatch(detail, /secret|account|charset/);
                    return true;
                });
            }
        }
    }
    context.readText = async () => '{"ok":true}';
    assert.equal((await context.readJson({status: 200}, 'AUTH')).ok, true);
    assert.match(lab.errorMessage('PROVIDER_UPSTREAM_FAILED', 'SERVICE_JSON_AUTH_HTTP_200_TYPE_HTML'), /上游授权.*HTTP 200.*HTML/);
    assert.match(lab.errorMessage('PROVIDER_UPSTREAM_FAILED', 'SERVICE_BUSINESS_CODE_403'), /不是 HTTP 状态/);
    assert.match(lab.errorMessage('PROVIDER_RESULT_EMPTY', 'SERVICE_EMPTY_RESULT'), /没有可用媒体/);
    assert.equal(lab.errorMessage('PROVIDER_UPSTREAM_FAILED', 'SERVICE_JSON_AUTH_HTTP_200_TYPE_HTML secret'), lab.errorMessage('PROVIDER_UPSTREAM_FAILED'));
});

test('helper diagnostics contain only fixed codes, never upstream bodies', () => {
    const fs = require('node:fs');
    const vm = require('node:vm');
    const path = require('node:path');
    const source = fs.readFileSync(path.join(__dirname, '../xhs_download/services/platforms/kukutool_provider.js'), 'utf8');
    const start = source.indexOf('function safeFailureDetail(');
    const end = source.indexOf('main().catch(', start);
    const context = vm.createContext({});
    vm.runInContext(source.slice(start, end), context);
    for (const [message, expected] of [
        ['parse failed (403): secret token', 'SERVICE_HTTP_403'],
        ['auth failed (200): private body', 'SERVICE_AUTH_REJECTED'],
        ['parse failed (200): private body', 'SERVICE_PARSE_REJECTED'],
        ['upstream returned invalid JSON (200)', 'SERVICE_INVALID_JSON'],
        ['upstream redirect left the allowed origin', 'SERVICE_REDIRECT_REJECTED'],
        ['fetch failed', 'SERVICE_NETWORK_ERROR'],
        ['https://private.invalid/?secret=value', 'SERVICE_HELPER_FAILED'],
    ]) assert.equal(context.safeFailureDetail({ message }), expected);
});

test("safe failure details distinguish execution stages from admin permissions", () => {
    assert.match(lab.errorMessage("PROVIDER_TIMEOUT", "MATCH_DEADLINE"), /匹配阶段/);
    assert.match(lab.errorMessage("PROVIDER_TIMEOUT", "PARSE_DEADLINE"), /解析阶段/);
    assert.match(lab.errorMessage("PROVIDER_POLICY_REJECTED", "NESTED_AGGREGATOR_TLS_BYPASS_BLOCKED"), /TLS/);
    assert.equal(lab.errorMessage("PROVIDER_TIMEOUT", "secret"), lab.errorMessage("PROVIDER_TIMEOUT"));
    assert.match(lab.errorMessage("PROVIDER_UPSTREAM_FAILED", "SERVICE_HTTP_418"), /HTTP 418/);
    assert.match(lab.errorMessage("PROVIDER_UPSTREAM_FAILED", "SERVICE_HELPER_FAILED"), /不能据此判定 Node 缺失/);
    assert.match(lab.errorMessage("PROVIDER_UPSTREAM_FAILED", "SERVICE_INVALID_JSON"), /JSON/);
    for (const detail of ["SERVICE_HTTP_403 secret", "https://private.invalid", "__proto__"]) {
        assert.equal(lab.errorMessage("PROVIDER_UPSTREAM_FAILED", detail), lab.errorMessage("PROVIDER_UPSTREAM_FAILED"));
    }
});

function comparisonFixture(ids) {
    const root = new Node("main", {"data-provider-lab": "", "data-run-url": "/run", "data-resource-url": "/resource"});
    const form = new Node("form", {"data-provider-lab-form": ""});
    const source = new Node("textarea"); source.value = "https://source.example/one";
    form.elements = {source}; form.append(source);
    form.append(new Node("input", {name: "csrfmiddlewaretoken", value: "csrf"}), new Node("button", {type: "submit"}),
        new Node("button", {"data-lab-cancel": ""}));
    root.append(form, new Node("p", {"data-provider-lab-form-message": ""}), new Node("p", {"data-lab-current-selection": ""}),
        new Node("p", {"data-lab-comparison-warning": ""}));
    ids.forEach((id) => {
        const input = new Node("input", {name: "providers", value: id}); input.checked = true; form.append(input);
        const card = new Node("article", {"data-provider-card": id});
        for (const name of ["provider-status-label", "provider-assets", "provider-error", "provider-content", "lab-retry"]) {
            card.append(new Node(name === "lab-retry" ? "button" : "div", {[`data-${name}`]: ""}));
        }
        root.append(card);
    });
    return {root, form, doc: documentFor(root)};
}

test("controller preserves four independent results and current candidate selection never writes a route", async () => {
    const fixture = comparisonFixture(["A", "B", "C", "D"]);
    const calls = [];
    const controller = lab.createController(fixture.doc, {window: {}, navigator: {}, fetch: async (url, options) => {
        calls.push(url);
        const {provider_id: id} = JSON.parse(options.body);
        return {ok: true, async json() { return {provider_id: id, request_id: id, status: id === "D" ? "not_configured" : "success",
            metadata: {}, assets: id === "D" ? [] : [{asset_id: id, height: id === "B" ? 2160 : 1080, width: 1920,
                media_type: "video", delivery_state: id === "B" ? "requires_merge" : "unknown"}]}; }};
    }});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    assert.equal(controller.resultByProvider.size, 4);
    assert.equal(controller.resultByProvider.get("B").status, "success");
    assert.equal(controller.currentSelection, null);
    const card = fixture.root.querySelector('[data-provider-card="B"]');
    const choose = card.querySelector("[data-lab-select]");
    await fixture.root.listeners.click({target: choose, preventDefault() {}});
    assert.equal(controller.currentSelection.providerId, "B");
    assert.equal(controller.currentSelection.asset.delivery_state, "requires_merge");
    assert.equal(calls.length, 4);
    assert.equal(calls.every((url) => url === "/run"), true);
    assert.match(fixture.root.querySelector("[data-lab-current-selection]").textContent, /未修改平台路线/);
    controller.destroy();
});

function routeFixture(canConfigure = true) {
    const panel = new Node("section", {"data-lab-route-editor": "", "data-can-configure": String(canConfigure)});
    for (const name of ["status", "scope", "order", "add", "confirm", "save", "reset", "fields"]) {
        panel.append(new Node(name === "add" ? "select" : "div", {[`data-route-${name}`]: ""}));
    }
    for (const name of ["max_height", "preferred_video_codec", "preferred_container", "route_videodl_mode", "route_videodl_backend"]) {
        panel.append(new Node("input", {name}));
    }
    return {panel, doc: documentFor(panel)};
}

test("shipped script retries initialization when interactive DOM has not mounted the Lab yet", async () => {
    const fs = require("node:fs"), vm = require("node:vm");
    const doc = documentFor(new Node("div")); doc.readyState = "interactive";
    const calls = [];
    const context = {document: doc, location: {origin: "http://local.example"}, navigator: {},
        URL, AbortController, setTimeout, clearTimeout,
        fetch: async (url, options) => { calls.push({url, options}); return {ok: true, json: async () => ({ok: true, batches: []})}; }};
    vm.runInNewContext(fs.readFileSync(require.resolve("../static/js/provider_lab.js"), "utf8"), context);
    assert.equal(typeof doc.listeners.DOMContentLoaded, "function", "actual bootstrap must propagate null to schedule a DOM-ready retry");
    const f = comparisonFixture(["A"]);
    const tab = new Node("button", {"data-lab-tab": "history", "aria-pressed": "false"});
    const instant = new Node("button", {"data-lab-tab": "instant", "aria-pressed": "true"});
    const panel = new Node("section", {"data-lab-history": ""}); panel.hidden = true;
    for (const name of ["batches", "executions", "status", "heading", "host", "order"]) {
        panel.append(new Node("div", {[`data-history-${name}`]: ""}));
    }
    f.root.append(tab, instant, panel); doc.append(f.root);
    assert.equal(tab.closest("form"), null);
    doc.readyState = "complete"; doc.listeners.DOMContentLoaded();
    const controller = f.root.__animeImagesProviderLabController;
    assert.ok(controller);
    await f.root.listeners.click({target: tab}); await flush();
    assert.equal(tab.getAttribute("aria-pressed"), "true"); assert.equal(panel.hidden, false);
    assert.equal(calls.length, 1);
    assert.equal(new URL(calls[0].url).pathname, "/admin/provider-lab/history/");
    assert.equal(calls[0].options.method, undefined);
    await f.root.listeners.click({target: instant});
    assert.equal(panel.hidden, true); assert.equal(calls.length, 1);
    controller.destroy();
});

test("retained tail 2160p is reachable and selection survives quality sort and 64-item expansion", async () => {
    const f = comparisonFixture(["A"]);
    const assets = Array.from({length: 130}, (_, i) => ({asset_id: `v${i}`, media_type: "video",
        width: i === 129 ? null : 1920, height: i === 129 ? 2160 : 720}));
    const controller = lab.createController(f.doc, {window: {}, fetch: async () => ({ok: true,
        json: async () => ({status: "success", request_id: "r", assets, metadata: {returned_count: 130, retained_count: 130}})})});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    const card = f.root.querySelector('[data-provider-card="A"]');
    const rows = () => card.querySelectorAll(".provider-lab-asset");
    assert.equal(rows().filter((row) => !row.hidden).length, 64);
    const sort = new Node("select", {"data-lab-format-order": ""}); card.append(sort); sort.value = "resolution";
    f.root.listeners.change({target: sort});
    assert.equal(rows()[0].dataset.assetId, "v129");
    await f.root.listeners.click({target: rows()[0].querySelector("[data-lab-select]"), preventDefault() {}});
    await f.root.listeners.click({target: card.querySelector("[data-lab-expand]"), preventDefault() {}});
    assert.equal(rows().filter((row) => !row.hidden).length, 128);
    sort.value = "source"; f.root.listeners.change({target: sort});
    assert.equal(controller.currentSelection.asset.asset_id, "v129");
    assert.equal(rows()[129].querySelector("[data-lab-select]").getAttribute("aria-pressed"), "true");
    controller.destroy();
});

const route = {source_host: "source.example", applicable: true, active: true, revision: 4,
    provider_order: ["yt_dlp_generic"], allowed_provider_ids: ["yt_dlp_generic", "parsehub_generic"],
    provider_options: {}, format_preferences: {max_height: 1080}};

test("route controller saves explicit reusable choice with revision, then DELETE resets it", async () => {
    const {panel, doc} = routeFixture();
    const writes = [];
    const editor = lab.createRouteEditor(doc, {url: "/provider-lab/routes/", csrf: () => "csrf",
        getSource: () => "https://source.example/post", getSelection: () => ({providerId: "parsehub_repo"}),
        window: {location: {origin: "https://local.example"}, confirm: () => true},
        fetch: async (_url, options) => {
            if (!options.method) return {ok: true, json: async () => ({ok: true, route})};
            writes.push(options);
            return {ok: true, json: async () => ({ok: true, route: {...route, revision: 4 + writes.length,
                active: options.method !== "DELETE"}})};
        }});
    await editor.load(); editor.selectProvider("parsehub_repo");
    assert.equal(writes.length, 0);
    panel.querySelector("[data-route-confirm]").checked = true;
    await editor.persist(false);
    assert.equal(writes[0].headers["X-CSRFToken"], "csrf");
    const payload = JSON.parse(writes[0].body);
    assert.equal(payload.expected_revision, 4);
    assert.deepEqual(payload.provider_order, ["parsehub_generic"]);
    assert.deepEqual(payload.format_preferences, {max_height: 1080});
    await editor.persist(true);
    assert.equal(writes[1].method, "DELETE");
    assert.deepEqual(JSON.parse(writes[1].body), {source_url: "https://source.example/post", expected_revision: 5});
});

test("test-all derives descriptors, includes unavailable and unapproved, and does not wait for a second success", async () => {
    const f = comparisonFixture(["A", "B", "C", "paid", "missing", "animeimages_current"]);
    const descriptors = ["A", "B", "C", "paid", "missing", "animeimages_current"].map((id) => ({provider_id: id,
        execution_provider_id: id, available: id !== "missing", baseline: id === "animeimages_current",
        requires_confirmation: id === "paid" ? ["paid_service", "external_sharing"] : [], not_configured_hint: "运行依赖未配置"}));
    const calls = [];
    const controller = lab.createController(f.doc, {window: {}, descriptors, fetch: async (_url, options) => {
        const id = JSON.parse(options.body).provider_id; calls.push(id);
        return {ok: true, json: async () => ({provider_id: id, executed: true, status: id === "B" ? "unsupported" : "success", assets: []})};
    }});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    assert.deepEqual(calls.sort(), ["A", "B", "C"]);
    assert.equal(controller.resultByProvider.get("paid").status, "skipped");
    assert.equal(controller.resultByProvider.get("missing").status, "skipped");
    assert.deepEqual(controller.runStats, {included: 5, executed: 3, success: 2, unsupported: 1,
        failed: 0, timeout: 0, notrun: 2});
    assert.equal(controller.currentSelection, null);
    controller.destroy();
});

test("late not-configured response never counts as both executed and not run", async () => {
    const f = comparisonFixture(["A"]);
    const controller = lab.createController(f.doc, {window: {},
        descriptors: [{provider_id: "A", available: true, requires_confirmation: []}],
        fetch: async () => ({ok: true, json: async () => ({status: "not_configured", executed: true, assets: []})})});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    assert.equal(controller.runStats.executed, 0);
    assert.equal(controller.runStats.notrun, 1);
    controller.destroy();
});

test("explicit paid confirmation enables only that retry with backend confirmation contract", async () => {
    const f = comparisonFixture(["paid"]);
    const consent = new Node("input", {name: "provider_authorizations", value: "paid"}); f.form.append(consent);
    const calls = [];
    const controller = lab.createController(f.doc, {window: {}, descriptors: [{provider_id: "paid", available: true,
        requires_confirmation: ["paid_service", "external_sharing"]}], fetch: async (_url, options) => {
        calls.push(JSON.parse(options.body));
        return {ok: true, json: async () => ({status: "success", executed: true, assets: []})};
    }});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    assert.equal(calls.length, 0);
    consent.checked = true;
    const retry = f.root.querySelector("[data-lab-retry]");
    await f.root.listeners.click({target: retry, preventDefault() {}}); await flush(); await flush();
    assert.deepEqual(calls[0].confirmations, {paid_service: true, external_sharing: true});
    assert.equal(controller.runStats.executed, 1); assert.equal(controller.runStats.notrun, 0);
    controller.destroy();
});

test("advanced selection remains explicit and server rejection does not count an upstream execution", async () => {
    const f = comparisonFixture(["A", "B"]);
    f.form.querySelector('input[value="B"]').checked = false;
    const calls = [];
    const controller = lab.createController(f.doc, {window: {}, descriptors: ["A", "B"].map((id) => ({provider_id: id, available: true, requires_confirmation: []})),
        fetch: async (_url, options) => { calls.push(JSON.parse(options.body).provider_id);
            return {ok: true, json: async () => ({status: "failed", executed: false, unexecuted_reason: "PROVIDER_DISABLED"})}; }});
    await controller.submit({preventDefault() {}, submitter: {hasAttribute: (name) => name === "data-lab-selected"}});
    await flush(); await flush();
    assert.deepEqual(calls, ["A"]);
    assert.deepEqual(controller.runStats, {included: 1, executed: 0, success: 0, unsupported: 0, failed: 0, timeout: 0, notrun: 1});
    assert.equal(controller.currentSelection, null);
    controller.destroy();
});

test("route uses descriptor execution mapping for a sole service success, never the composite baseline", async () => {
    const {panel, doc} = routeFixture(); const writes = [];
    const editor = lab.createRouteEditor(doc, {url: "/routes", csrf: () => "csrf", getSource: () => "https://source.example/one",
        descriptors: [{provider_id: "service_lab", execution_provider_id: "service_executor", route_selectable: true},
            {provider_id: "animeimages_current", execution_provider_id: "service_executor", baseline: true}],
        window: {location: {origin: "https://local.example"}}, fetch: async (_url, options) => {
            if (options.method) writes.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({ok: true, route: {...route, allowed_provider_ids: ["service_executor"]}})};
        }});
    await editor.load(); editor.selectProvider("animeimages_current");
    assert.match(panel.querySelector("[data-route-status]").textContent, /不能保存/);
    editor.selectProvider("service_lab"); assert.equal(writes.length, 0);
    panel.querySelector("[data-route-confirm]").checked = true; await editor.persist(false);
    assert.deepEqual(writes[0].provider_order, ["service_executor"]);
});

test("route 409 invalidates authority and requires explicit reread", async () => {
    const {panel, doc} = routeFixture();
    let writes = 0;
    const editor = lab.createRouteEditor(doc, {url: "/routes", csrf: () => "csrf", getSource: () => "source",
        window: {location: {origin: "https://local.example"}}, fetch: async (_url, options) => {
            if (!options.method) return {ok: true, json: async () => ({ok: true, route})};
            writes += 1; return {ok: false, status: 409, json: async () => ({ok: false, error_code: "REVISION_CONFLICT"})};
        }});
    await editor.load(); panel.querySelector("[data-route-confirm]").checked = true;
    await editor.persist(false); await editor.persist(false);
    assert.equal(writes, 1); assert.equal(editor.route, null);
    assert.equal(panel.querySelector("[data-route-save]").disabled, true);
    assert.match(panel.querySelector("[data-route-status]").textContent, /不会覆盖/);
});

test("configuration permission cannot be bypassed by invoking save directly", async () => {
    const {panel, doc} = routeFixture(false);
    let writes = 0;
    const editor = lab.createRouteEditor(doc, {url: "/routes", csrf: () => "csrf", getSource: () => "source",
        window: {location: {origin: "https://local.example"}}, fetch: async (_url, options) => {
            if (options.method) writes += 1;
            return {ok: true, json: async () => ({ok: true, route})};
        }});
    await editor.load(); panel.querySelector("[data-route-confirm]").checked = true;
    await editor.persist(false); assert.equal(writes, 0);
});

test("history route saves evidence only on explicit save and never guesses missing videodl options", async () => {
    const {panel, doc} = routeFixture(); const writes = [];
    panel.append(new Node("p", {"data-route-reference": ""}), new Node("p", {"data-route-confirmation-summary": ""}));
    const evidence = {id: 73, batch: {label: "Scout batch", created_at: "2026-09-02"}, started_at: "2026-09-01", version: {app: "old"}, options: null};
    const editor = lab.createRouteEditor(doc, {url: "/provider-lab/routes/", csrf: () => "csrf",
        getSource: () => "https://source.example/history", getSelection: () => ({historyExecution: evidence}),
        descriptors: [{provider_id: "videodl_repo", execution_provider_id: "videodl_repo", available: true, route_selectable: true}],
        window: {location: {origin: "http://local.example"}}, fetch: async (_url, options) => {
            if (options.method) writes.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({ok: true, route: {...route, allowed_provider_ids: ["videodl_repo"]}})};
        }});
    await editor.load(); editor.selectProvider("videodl_repo", evidence);
    assert.equal(writes.length, 0);
    assert.equal(panel.querySelector('[name="route_videodl_mode"]').value, "");
    assert.match(panel.querySelector("[data-route-reference]").textContent, /Scout batch.*2026-09-02.*2026-09-01.*old/);
    panel.querySelector("[data-route-confirm]").checked = true;
    await editor.persist(false); assert.equal(writes.length, 0);
    assert.match(panel.querySelector("[data-route-status]").textContent, /显式设置/);
    panel.querySelector('[name="route_videodl_mode"]').value = "dedicated_only";
    await editor.persist(false);
    assert.equal(writes[0].reference_execution_id, 73);
    assert.equal(writes[0].source_url, "https://source.example/history");
    assert.equal(writes[0].expected_revision, 4);
    assert.deepEqual(writes[0].provider_options, {videodl_repo: {mode: "dedicated_only"}});
});

test("history cannot trust imported route_selectable for removed or currently forbidden providers", async () => {
    const {panel, doc} = routeFixture(); let writes = 0;
    const editor = lab.createRouteEditor(doc, {url: "/routes", csrf: () => "csrf", getSource: () => "source",
        descriptors: [{provider_id: "parsehub_repo", available: false, route_selectable: false}],
        window: {location: {origin: "http://local.example"}}, fetch: async () => {
            writes += 1; return {ok: true, json: async () => ({ok: true, route})};
        }});
    await editor.load(); editor.selectProvider("parsehub_repo", {id: 1, route_selectable: true});
    assert.match(panel.querySelector("[data-route-status]").textContent, /当前 Provider 配置不允许/);
    editor.selectProvider("removed", {id: 2, route_selectable: true});
    assert.match(panel.querySelector("[data-route-status]").textContent, /当前 Provider 配置不允许/);
    assert.equal(writes, 1);
});

test("single history run keeps original source/provider and requires current sharing authorization", async () => {
    const f = comparisonFixture(["paid"]); const calls = [];
    const consent = new Node("input", {name: "provider_authorizations", value: "paid"}); f.form.append(consent);
    const controller = lab.createController(f.doc, {window: {}, descriptors: [{provider_id: "paid", available: true,
        requires_confirmation: ["external_sharing"]}], fetch: async (url, options) => {
        calls.push({url, body: JSON.parse(options.body)}); return {ok: true, json: async () => ({status: "success", executed: true, assets: []})};
    }});
    assert.equal((await controller.runOne("paid", "https://original.example/work", "csrf", null, undefined, 73)).executed, false);
    assert.equal(calls.length, 0); consent.checked = true;
    await controller.runOne("paid", "https://original.example/work", "csrf", null, undefined, 73);
    assert.deepEqual(calls, [{url: "/run", body: {source: "https://original.example/work", provider_id: "paid",
        history_execution_id: 73, confirmations: {external_sharing: true}}}]);
    controller.destroy();
});

test("instant parse success remains success when its independent history write fails", async () => {
    const f = comparisonFixture(["A"]); let calls = 0;
    const controller = lab.createController(f.doc, {window: {}, fetch: async () => {
        calls += 1; return {ok: true, json: async () => ({status: "success", executed: true,
            assets: [], history: {saved: false, error_code: "HISTORY_SAVE_FAILED"}})};
    }});
    await controller.submit({preventDefault() {}}); await flush(); await flush();
    const card = f.root.querySelector('[data-provider-card="A"]');
    assert.equal(card.dataset.providerStatus, "success");
    assert.equal(controller.resultByProvider.get("A").status, "success");
    assert.equal(card.querySelector("[data-provider-error]").hidden, true);
    assert.match(card.querySelector("[data-history-receipt]").textContent, /HISTORY_SAVE_FAILED.*不会自动重跑/);
    assert.equal(calls, 1);
    controller.destroy();
});

test("history route save reports current provider rejection and mismatched reference explicitly", async () => {
    for (const [code, message] of [["HISTORY_PROVIDER_UNAVAILABLE", /拟保存的上游当前已禁用或未配置/],
        ["INVALID_HISTORY_REFERENCE", /历史参考记录与当前来源不匹配/]]) {
        const {panel, doc} = routeFixture(); let writes = 0;
        const editor = lab.createRouteEditor(doc, {url: "/routes", csrf: () => "csrf", getSource: () => "https://source.example/work",
            descriptors: [{provider_id: "parsehub_repo", execution_provider_id: "parsehub_generic", available: true, route_selectable: true}],
            window: {location: {origin: "http://local.example"}}, fetch: async (_url, options) => {
                if (!options.method) return {ok: true, json: async () => ({ok: true, route})};
                writes += 1;
                assert.equal(JSON.parse(options.body).reference_execution_id, 73);
                return {ok: false, status: 422, json: async () => ({ok: false, error_code: code})};
            }});
        await editor.load(); editor.selectProvider("parsehub_repo", {id: 73});
        panel.querySelector("[data-route-confirm]").checked = true;
        await editor.persist(false);
        assert.match(panel.querySelector("[data-route-status]").textContent, message);
        assert.match(panel.querySelector("[data-route-status]").textContent, /路线未保存/);
        assert.equal(writes, 1); assert.equal(editor.route.revision, 4);
    }
});
