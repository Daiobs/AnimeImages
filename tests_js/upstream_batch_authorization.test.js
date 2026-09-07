"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const {authorizeBatchUpstreamItems} = require("../static/js/batch_parse.js");

const item = {type: "image", url: "https://cdn.example/image.webp", asset_id: "upstream:job:image:1",
    provider: "gallery_dl_generic", source_platform: "upstream", provider_manifest_token: "signed"};
const page = {dataset: {batchDownloadUrl: "/media/download-images/"}, querySelector: () => ({value: "csrf"})};

test("Batch requires exact server-issued binding before upstream browser fetch", async () => {
    const oldFetch = global.fetch;
    const oldWindow = global.window;
    global.window = {location: {origin: "https://product.example"}};
    let payload;
    global.fetch = async (url, options) => {
        assert.equal(url, "https://product.example/media/download-images/");
        payload = JSON.parse(options.body);
        return {ok: true, json: async () => ({route_type: "direct", items: [{...item, browser_upstream_bound: true}]})};
    };
    try {
        const result = await authorizeBatchUpstreamItems(page, [item]);
        assert.equal(result[0].browser_upstream_bound, true);
        assert.equal(payload.route_only, true);
        assert.equal(payload.media_items[0].provider_manifest_token, "signed");
        global.fetch = async () => ({ok: true, json: async () => ({route_type: "direct", items: [{...item, url: "https://other.example/image", browser_upstream_bound: true}]})});
        await assert.rejects(authorizeBatchUpstreamItems(page, [item]));
        global.fetch = async () => ({ok: true, json: async () => ({route_type: "direct", items: [item]})});
        await assert.rejects(authorizeBatchUpstreamItems(page, [item]));
        global.fetch = async () => ({ok: true, json: async () => ({route_type: "zip", items: [{...item, browser_upstream_bound: true}]})});
        await assert.rejects(authorizeBatchUpstreamItems(page, [item]));
    } finally { global.fetch = oldFetch; global.window = oldWindow; }
});

test("Mature browser items do not acquire a new authorization request", async () => {
    const items = [{type: "image", url: "https://cdn.example/legacy.jpg"}];
    assert.equal(await authorizeBatchUpstreamItems(null, items), items);
});
