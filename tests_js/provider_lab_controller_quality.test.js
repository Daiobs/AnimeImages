"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const lab = require("../static/js/provider_lab.js");
const {Node, documentFor} = require("./helpers/lab_dom.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

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
    assert.deepEqual(payload.provider_order, ["parsehub_generic", "yt_dlp_generic"]);
    assert.deepEqual(payload.format_preferences, {max_height: 1080});
    await editor.persist(true);
    assert.equal(writes[1].method, "DELETE");
    assert.deepEqual(JSON.parse(writes[1].body), {source_url: "https://source.example/post", expected_revision: 5});
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
