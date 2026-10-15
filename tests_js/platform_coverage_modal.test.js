"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/js/platform_coverage_modal.js"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
}

function element(tagName = "div") {
    const listeners = new Map();
    const attributes = new Map();
    let text = "";
    return {
        tagName, children: [], dataset: {},
        get textContent() { return text + this.children.map(child => child.textContent).join(""); },
        set textContent(value) { text = value; this.children = []; },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        removeAttribute(name) { attributes.delete(name); },
        append(child) { this.children.push(child); },
        replaceChildren(...children) { text = ""; this.children = children; },
        addEventListener(name, callback) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(callback);
        },
        dispatch(name, target = this) {
            const event = {target, defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; }};
            for (const callback of listeners.get(name) || []) callback(event);
            return event;
        },
    };
}

function harness({coverageUrl = "/platform-coverage/", pathname = "/batch/"} = {}) {
    const body = element();
    const modal = element();
    const requests = [];
    const sections = new Map();
    modal.dataset.coverageUrl = coverageUrl;
    modal.querySelector = selector => selector === "[data-coverage-content]" ? body : null;
    vm.runInNewContext(source, {
        document: {
            getElementById: id => id === "platformCoverageModal" ? modal : null,
            createElement: element,
        },
        location: {origin: "https://site.test", pathname, href: `https://site.test${pathname}`},
        URL, URLSearchParams, AbortController,
        FormData: class { constructor(form) { return form.entries; } },
        DOMParser: class {
            parseFromString(html, type) {
                assert.equal(type, "text/html");
                return {querySelector: selector => selector === ".upstream-coverage" ? sections.get(html) : null};
            }
        },
        // Deliberately allow completion after abort to exercise stale-response guards.
        fetch(url, options) {
            const pending = deferred();
            requests.push({...pending, url: String(url), options});
            return pending.promise;
        },
    });
    function response(label) {
        const section = element("section");
        section.textContent = label;
        section.setAttribute("aria-labelledby", "coverage-title");
        section.headerRemoved = false;
        section.querySelector = selector => selector === ".coverage-header > div" ? {
            remove() { section.headerRemoved = true; },
        } : null;
        sections.set(label, section);
        return {section, ok: true, text: async () => label};
    }
    return {body, modal, requests, response};
}

for (const failure of ["network", "http", "missing section", "response body"]) {
    test(`load failure (${failure}) clears busy state and retries the same URL`, async () => {
        const {modal, body, requests, response} = harness({coverageUrl: "/platform-coverage/?state=available"});
        modal.dispatch("show.bs.modal");
        assert.equal(body.getAttribute("aria-busy"), "true");
        assert.equal(requests[0].options.credentials, "same-origin");
        assert.equal(requests[0].options.signal.aborted, false);
        if (failure === "network") requests[0].reject(new TypeError("network failed"));
        if (failure === "http") requests[0].resolve({ok: false});
        if (failure === "missing section") requests[0].resolve({ok: true, text: async () => "login page"});
        if (failure === "response body") requests[0].resolve({ok: true, text: async () => { throw new Error("read failed"); }});
        await flush();
        assert.equal(body.getAttribute("aria-busy"), null);
        assert.equal(body.children.length, 1);
        const retry = body.children[0];
        assert.equal(retry.tagName, "button");
        assert.equal(retry.type, "button");
        assert.ok(body.textContent.length > retry.textContent.length);
        retry.dispatch("click");
        assert.equal(requests.length, 2);
        assert.equal(requests[1].url, requests[0].url);
        assert.notEqual(requests[1].options.signal, requests[0].options.signal);
        assert.equal(body.children.length, 0);
        assert.equal(body.getAttribute("aria-busy"), "true");
        const recovered = response("recovered coverage");
        requests[1].resolve(recovered);
        await flush();
        assert.deepEqual(body.children, [recovered.section]);
        assert.equal(body.getAttribute("aria-busy"), null);
        assert.equal(recovered.section.headerRemoved, true);
        assert.equal(recovered.section.getAttribute("aria-labelledby"), null);
    });
}

for (const outcome of ["success", "failure", "abort"]) {
    test(`closing aborts loading and ignores a late ${outcome}; reopening loads afresh`, async () => {
        const {modal, body, requests, response} = harness();
        modal.dispatch("show.bs.modal");
        modal.dispatch("hidden.bs.modal");
        assert.equal(requests[0].options.signal.aborted, true);
        const closedText = body.textContent;
        if (outcome === "success") requests[0].resolve(response("closed result"));
        else requests[0].reject(Object.assign(new Error("cancelled"), {
            name: outcome === "abort" ? "AbortError" : "Error",
        }));
        await flush();
        assert.equal(body.textContent, closedText);
        assert.equal(body.children.length, 0);
        modal.dispatch("show.bs.modal");
        assert.equal(requests.length, 2);
        assert.equal(requests[1].options.signal.aborted, false);
        const fresh = response("reopened result");
        requests[1].resolve(fresh);
        await flush();
        assert.deepEqual(body.children, [fresh.section]);
        assert.equal(body.getAttribute("aria-busy"), null);
    });
}

for (const outcome of ["success", "failure"]) {
    for (const oldFinishesFirst of [true, false]) {
        test(`stale ${outcome} cannot change newer content or busy state (${oldFinishesFirst ? "old" : "new"} finishes first)`, async () => {
            const {modal, body, requests, response} = harness();
            modal.dispatch("show.bs.modal");
            const oldBody = deferred();
            requests[0].resolve({ok: true, text: () => oldBody.promise});
            await flush();
            modal.dispatch("hidden.bs.modal");
            modal.dispatch("show.bs.modal");
            assert.equal(requests[0].options.signal.aborted, true);
            const fresh = response("new result");
            const old = response("old result");
            async function finishOld() {
                if (outcome === "success") oldBody.resolve("old result");
                else oldBody.reject(new Error("late body failure"));
                await flush();
            }
            if (oldFinishesFirst) {
                const loadingText = body.textContent;
                await finishOld();
                assert.equal(body.textContent, loadingText);
                assert.equal(body.children.length, 0);
                assert.equal(body.getAttribute("aria-busy"), "true");
            }
            requests[1].resolve(fresh);
            await flush();
            if (!oldFinishesFirst) await finishOld();
            assert.deepEqual(body.children, [fresh.section]);
            assert.equal(body.getAttribute("aria-busy"), null);
            assert.equal(old.section.headerRemoved, false);
        });
    }
}

test("search and delegated pagination stay on the coverage URL, not the hosting page", async () => {
    const {modal, body, requests, response} = harness();
    modal.dispatch("show.bs.modal");
    const form = {action: "/", entries: [["coverage", "1"], ["search", "a+b & video"], ["state", "available"]]};
    const submit = body.dispatch("submit", {closest: selector => selector === ".coverage-filters" ? form : null});
    assert.equal(submit.defaultPrevented, true);
    assert.equal(requests[0].options.signal.aborted, true);
    const searchUrl = new URL(requests[1].url);
    assert.equal(searchUrl.origin, "https://site.test");
    assert.equal(searchUrl.pathname, "/platform-coverage/");
    assert.deepEqual([...searchUrl.searchParams], form.entries);
    assert.equal(searchUrl.searchParams.has("page"), false);
    requests[1].resolve(response("search results"));
    await flush();
    const link = element("a");
    link.setAttribute("href", "?coverage=1&search=a%2Bb+%26+video&state=available&page=2");
    const click = body.dispatch("click", {closest: selector => selector === ".coverage-pagination a" ? link : null});
    assert.equal(click.defaultPrevented, true);
    const pageUrl = new URL(requests[2].url);
    assert.equal(pageUrl.origin, searchUrl.origin);
    assert.equal(pageUrl.pathname, searchUrl.pathname);
    for (const [key, value] of form.entries) assert.equal(pageUrl.searchParams.get(key), value);
    assert.equal(pageUrl.searchParams.get("page"), "2");
    assert.equal(requests[2].options.credentials, "same-origin");
    requests[2].resolve(response("page two"));
    requests[0].resolve(response("stale initial page"));
    await flush();
    assert.equal(body.textContent, "page two");
    assert.equal(body.getAttribute("aria-busy"), null);
    for (const type of ["submit", "click"]) {
        assert.equal(body.dispatch(type, {closest: () => null}).defaultPrevented, false);
    }
    assert.equal(requests.length, 3);
});
