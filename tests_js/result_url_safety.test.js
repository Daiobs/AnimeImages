"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const safety = require("../static/js/result_url_safety.js");

function location(pathname, search) {
    return {
        origin: "https://download.example.test",
        pathname,
        search: search || "",
    };
}

function history() {
    const calls = [];
    return {
        state: {safe: true},
        calls,
        replaceState(...args) {
            calls.push(args);
        },
    };
}

test("server-rendered result URL is replaced by its platform home path", () => {
    const historyObject = history();
    const changed = safety.scrubServerRenderedResultUrl({
        locationObject: location("/tiktok/results/", "?q=sensitive-source&ultra_hd=1"),
        historyObject,
        homeUrl: "/tiktok/",
        resultsUrl: "/tiktok/results/",
    });

    assert.equal(changed, true);
    assert.deepEqual(historyObject.calls, [[{safe: true}, "", "/tiktok/"]]);
    assert.doesNotMatch(JSON.stringify(historyObject.calls), /sensitive-source/);
});

test("all supported result routes can be scrubbed without hard-coded platform logic", () => {
    const routes = [
        ["/results/", "/"],
        ["/xhs/results/", "/xhs/"],
        ["/tiktok/results/", "/tiktok/"],
        ["/bilibili/results/", "/bilibili/"],
    ];

    for (const [resultsUrl, homeUrl] of routes) {
        const historyObject = history();
        assert.equal(safety.scrubServerRenderedResultUrl({
            locationObject: location(resultsUrl, "?q=source"),
            historyObject,
            homeUrl,
            resultsUrl,
        }), true);
        assert.equal(historyObject.calls[0][2], homeUrl);
    }
});

test("home and unrelated pages are not rewritten", () => {
    const historyObject = history();
    assert.equal(safety.scrubServerRenderedResultUrl({
        locationObject: location("/tiktok/"),
        historyObject,
        homeUrl: "/tiktok/",
        resultsUrl: "/tiktok/results/",
    }), false);
    assert.equal(historyObject.calls.length, 0);
});

test("cross-origin or malformed route configuration fails closed", () => {
    for (const values of [
        {homeUrl: "https://other.example.test/", resultsUrl: "/results/"},
        {homeUrl: "/", resultsUrl: "https://other.example.test/results/"},
        {homeUrl: "http://[", resultsUrl: "/results/"},
    ]) {
        const historyObject = history();
        assert.equal(safety.scrubServerRenderedResultUrl({
            locationObject: location("/results/", "?q=source"),
            historyObject,
            ...values,
        }), false);
        assert.equal(historyObject.calls.length, 0);
    }
});
