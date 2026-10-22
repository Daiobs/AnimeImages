"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const reliability = require("../static/js/douyin_reliability.js");

test("pending parse polls a bounded cache-only contract until success", async () => {
    const responses = [
        {
            status: "continuation_pending",
            poll_after_ms: 1,
        },
        {
            status: "success",
            html: "<div>ready</div>",
        },
    ];
    let pollCount = 0;
    let waitCount = 0;
    const result = await reliability.runBoundedContinuation({
        initialPayload: {
            status: "continuation_pending",
            continuation_token: "opaque-token",
            poll_after_ms: 1,
        },
        wait: async () => {
            waitCount += 1;
        },
        requestPoll: async (token) => {
            assert.equal(token, "opaque-token");
            const response = responses[pollCount];
            pollCount += 1;
            return response;
        },
    });

    assert.equal(result.status, "success");
    assert.equal(pollCount, 2);
    assert.equal(waitCount, 2);
});

test("terminal failure stops polling and preserves the safe error code", async () => {
    let pollCount = 0;
    const result = await reliability.runBoundedContinuation({
        initialPayload: {
            status: "continuation_pending",
            continuation_token: "opaque-token",
        },
        wait: async () => {},
        requestPoll: async () => {
            pollCount += 1;
            return {
                status: "failed",
                error_code: "DOUYIN_CONTINUATION_FAILED",
                message: "解析未能完成。",
            };
        },
    });

    assert.equal(result.status, "failed");
    assert.equal(result.error_code, "DOUYIN_CONTINUATION_FAILED");
    assert.equal(pollCount, 1);
});

test("permanent pending state uses the bounded continuation poll window", async () => {
    let pollCount = 0;
    const result = await reliability.runBoundedContinuation({
        initialPayload: {
            status: "continuation_pending",
            continuation_token: "opaque-token",
        },
        wait: async () => {},
        requestPoll: async () => {
            pollCount += 1;
            return {status: "continuation_pending"};
        },
    });

    assert.equal(pollCount, reliability.MAX_POLLS);
    assert.equal(result.status, "timeout");
    assert.doesNotMatch(result.message, /已返回|普通清晰度|已保留/);
    assert.equal(
        result.error_code,
        "DOUYIN_CONTINUATION_CLIENT_LIMIT",
    );
});

test("retry countdown values and labels stay local and bounded", () => {
    assert.equal(reliability.normalizeRetryAfterSeconds("8"), 8);
    assert.equal(reliability.normalizeRetryAfterSeconds(-3), 0);
    assert.equal(reliability.normalizeRetryAfterSeconds(120), 60);
    assert.equal(
        reliability.retryButtonLabel(8),
        "重新尝试超高清（8s）",
    );
    assert.equal(
        reliability.retryButtonLabel(0),
        "重新尝试超高清",
    );
    assert.equal(reliability.scopedRetrySeconds({
        ultraEnabled: true,
        currentSource: "source-a",
        retrySource: "source-a",
        deadlineMs: 9000,
        nowMs: 2000,
    }), 7);
    assert.equal(reliability.scopedRetrySeconds({
        ultraEnabled: true,
        currentSource: "source-b",
        retrySource: "source-a",
        deadlineMs: 9000,
        nowMs: 2000,
    }), 0);
    assert.equal(reliability.scopedRetrySeconds({
        ultraEnabled: false,
        currentSource: "source-a",
        retrySource: "source-a",
        deadlineMs: 9000,
        nowMs: 2000,
    }), 0);
});

test("missing token fails without making a poll request", async () => {
    let pollCount = 0;
    const result = await reliability.runBoundedContinuation({
        initialPayload: {status: "continuation_pending"},
        wait: async () => {},
        requestPoll: async () => {
            pollCount += 1;
            return {};
        },
    });

    assert.equal(result.status, "failed");
    assert.equal(pollCount, 0);
});

test("abort ends an active continuation without another request", async () => {
    const controller = new AbortController();
    controller.abort();
    let pollCount = 0;

    await assert.rejects(
        reliability.runBoundedContinuation({
            initialPayload: {
                status: "continuation_pending",
                continuation_token: "opaque-token",
            },
            signal: controller.signal,
            requestPoll: async () => {
                pollCount += 1;
                return {};
            },
        }),
        (error) => error.name === "AbortError",
    );
    assert.equal(pollCount, 0);
});

test("a bounded continuation failure preserves an already rendered fallback", () => {
    const initial = {
        status: "continuation_pending",
        continuation_token: "opaque-token",
        has_results: true,
        html: "<div>1080p fallback</div>",
    };
    const resolved = reliability.resolveContinuationResult(initial, {
        status: "timeout",
        error_code: "DOUYIN_CONTINUATION_CLIENT_LIMIT",
        message: "bounded timeout",
    });

    assert.equal(resolved.status, "success");
    assert.equal(resolved.html, initial.html);
    assert.equal(resolved.continuation_status, "timeout");
    assert.equal(
        resolved.continuation_error_code,
        "DOUYIN_CONTINUATION_CLIENT_LIMIT",
    );
});

test("a bounded continuation failure preserves retry cooldown metadata", () => {
    const initial = {
        status: "continuation_pending",
        has_results: true,
        html: "<div>native fallback</div>",
    };
    const resolved = reliability.resolveContinuationResult(initial, {
        status: "failed",
        message: "ultra unavailable",
        retry_after_seconds: 7,
    });

    assert.equal(resolved.status, "success");
    assert.equal(resolved.retry_after_seconds, 7);
    assert.equal(resolved.continuation_message, "已保留当前资源。ultra unavailable");
});

test("a completed continuation replaces the rendered fallback", () => {
    const completed = {
        status: "success",
        html: "<div>4K upgrade</div>",
    };
    const resolved = reliability.resolveContinuationResult({
        status: "continuation_pending",
        has_results: true,
        html: "<div>1080p fallback</div>",
    }, completed);

    assert.deepEqual(resolved, completed);
});

function deferred() {
    let resolve, reject;
    const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
    return {promise, resolve, reject};
}

for (const phase of ["wait", "poll"]) {
    test(`abort during ${phase} rejects even when the awaited operation ignores cancellation`, async () => {
        const controller = new AbortController();
        const gate = deferred();
        const entered = deferred();
        let polls = 0;
        const result = reliability.runBoundedContinuation({
            initialPayload: {status: "continuation_pending", continuation_token: "token"},
            signal: controller.signal,
            wait: async () => {
                if (phase === "wait") { entered.resolve(); await gate.promise; }
            },
            requestPoll: async () => {
                polls += 1;
                entered.resolve();
                return gate.promise;
            },
        });
        await entered.promise;
        controller.abort();
        gate.resolve({status: "success", html: "late upgrade"});
        await assert.rejects(result, {name: "AbortError"});
        assert.equal(polls, phase === "wait" ? 0 : 1);
    });
}

function resultPayload(name, extra = {}) {
    return {status: "success", has_results: true, html: `<article>${name}</article>`,
        base_filename: name, ...extra};
}

const fallbackPayload = () => resultPayload("fallback", {
    status: "continuation_pending", continuation_token: "token",
});
const flush = () => new Promise(resolve => setImmediate(resolve));

// Minimal result-region DOM double: every innerHTML write replaces the nodes,
// so tests detect selection loss and invalidation of download anchors.
function continuationHome(options = {}) {
    function node() {
        return {dataset: {}, style: {}, listeners: {}, children: [], hidden: false,
            isConnected: true, innerText: "", innerHTML: "", textContent: "",
            classList: {add() {}, remove() {}, toggle() {}},
            addEventListener(name, callback) { this.listeners[name] = callback; },
            setAttribute(name, value) { this[name] = value; },
            removeAttribute(name) { delete this[name]; },
            querySelector() { return null; }, focus() {}, scrollIntoView() {}};
    }
    const region = node(), page = node(), form = node(), input = node();
    const clear = node(), submit = node();
    const writes = [], requests = [], downloadRequests = [];
    let controls = {}, html = "", card = null;
    Object.defineProperty(region, "innerHTML", {
        get: () => html,
        set(value) {
            html = value;
            writes.push(value);
            controls = {};
            card = null;
            region.children = [];
            if (value.startsWith("<article>")) {
                card = node();
                card.dataset = {type: "video", url: "https://cdn.example/fallback.mp4", assetId: "douyin:video:1"};
                for (const selector of ["#status", "#download-btn", "#select-all-btn", "[data-provider-notice-text]"]) {
                    controls[selector] = node();
                }
                region.children = [card, ...Object.values(controls)];
            }
        },
    });
    const pickerCancel = node();
    pickerCancel.hidden = true;
    region.contains = value => region.children.includes(value);
    region.querySelector = selector => controls[selector] || null;
    region.querySelectorAll = selector => selector === ".asset-card" ? (card ? [card] : [])
        : selector === '[data-ytdlp-action="cancel"]' ? [pickerCancel] : [];
    page.dataset = {downloadUrl: "/download", douyinContinuationUrl: "/continuation"};
    form.action = "/parse";
    const elements = {"hero-search-form": form, "parse-results-region": region,
        "media-link-input": input, "clear-link-btn": clear, "parse-submit-btn": submit};
    let ready;
    const document = {
        cookie: "csrftoken=test",
        querySelector: selector => selector === ".xhs-download-page" ? page : null,
        getElementById: id => elements[id] || null,
        addEventListener(name, callback) { if (name === "DOMContentLoaded") ready = callback; },
        createElement: node,
    };
    const json = payload => new Response(JSON.stringify(payload), {headers: {"Content-Type": "application/json"}});
    const fetch = async (url, request) => {
        requests.push({url, ...request});
        if (url === "/continuation") return json(await options.poll(request.signal));
        if (url === "/download") {
            downloadRequests.push(JSON.parse(request.body));
            await options.download?.();
            return json({status: "success", message: "download finished"});
        }
        assert.match(url, /^\/parse\?/);
        return json(await (options.parse ? options.parse(url) : fallbackPayload()));
    };
    const window = {AbortController, fetch, location: {origin: "https://site.test"},
        localStorage: {getItem: () => "{}"}, addEventListener() {},
        requestAnimationFrame: callback => callback(), setTimeout, clearTimeout,
        setInterval: () => 1, clearInterval() {}, alert: message => assert.fail(message),
        AnimeImagesDouyinReliability: {...reliability,
            runBoundedContinuation: settings => options.ignoreAbort
                ? settings.requestPoll("token", {signal: settings.signal})
                : reliability.runBoundedContinuation({...settings, wait: async () => {}})},
    };
    if (options.pickerDownload) {
        window.DokiToolYtdlpPicker = {mediaRoots: () => [], snapshotMedia: () => [{}],
            downloadMedia: options.pickerDownload};
    }
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/js/xhs_download.js"), "utf8"), {
        window, document, fetch, AbortController, URLSearchParams,
        FormData: class { [Symbol.iterator]() { return [["url", input.value]][Symbol.iterator](); } },
        console: {warn() {}},
    });
    ready();
    function click(target, selector) {
        region.listeners.click({preventDefault() {}, target: {closest: value => value === selector ? target : null}});
    }
    return {region, page, writes, requests, downloadRequests, pickerCancel, submitButton: submit,
        control: selector => controls[selector],
        submit(source = "same-source") { input.value = source; return form.listeners.submit({preventDefault() {}}); },
        clear: () => clear.listeners.click(),
        select: () => click(card, ".asset-card"),
        download: () => click(controls["#download-btn"], "#download-btn"),
    };
}

test("home shows a real failure when continuation ends without any fallback", async () => {
    const home = continuationHome({
        parse: () => ({status: "continuation_pending", continuation_token: "token", has_results: false}),
        poll: async () => ({status: "failed", error_code: "DOUYIN_CONTINUATION_FAILED",
            message: "超高清解析本次未完成，请稍后重试。"}),
    });
    await home.submit();
    assert.match(home.region.innerHTML, /alert-danger/);
    assert.doesNotMatch(home.region.innerHTML, /已返回|已保留|普通清晰度/);
    assert.equal(home.submitButton.disabled, false);
    assert.equal(home.downloadRequests.length, 0);
});

test("home renders fallback immediately and upgrades only the current idle result", async () => {
    const poll = deferred();
    const home = continuationHome({poll: () => poll.promise});
    const parsing = home.submit();
    await flush();
    assert.equal(home.region.innerHTML, fallbackPayload().html);
    assert.equal(home.requests.filter(request => request.url === "/continuation").length, 1);
    poll.resolve(resultPayload("upgrade"));
    await parsing;
    assert.equal(home.region.innerHTML, resultPayload("upgrade").html);
    assert.equal(home.page.dataset.baseFilename, "upgrade");
    assert.equal(home.submitButton.disabled, false);
});

for (const outcome of ["failed", "timeout", "error", "unchanged", "rejected", "invalid"]) {
    test(`home ${outcome} continuation preserves fallback nodes and selection`, async () => {
        const poll = deferred();
        const home = continuationHome({poll: () => poll.promise});
        const parsing = home.submit();
        await flush();
        home.select();
        const anchor = home.region.children[0], writeCount = home.writes.length;
        if (outcome === "rejected") poll.reject(new TypeError("mock transport failure"));
        else poll.resolve({status: outcome === "invalid" ? "success" : outcome, message: "upgrade unavailable"});
        await parsing;
        assert.equal(home.region.children[0], anchor);
        assert.equal(home.writes.length, writeCount);
        assert.equal(home.page.dataset.baseFilename, "fallback");
        assert.match(home.control("#status").innerText, /1/);
        assert.ok(home.control("[data-provider-notice-text]").textContent);
        home.download();
        await flush();
        assert.equal(home.downloadRequests[0].media_items[0].asset_id, "douyin:video:1");
    });
}

for (const action of ["clear", "new-parse"]) {
    for (const outcome of ["success", "failed", "rejected"]) {
        test(`home ignores late ${outcome} poll after ${action}, even if polling ignores abort`, async () => {
            const poll = deferred();
            let parses = 0;
            const home = continuationHome({poll: () => poll.promise, ignoreAbort: true,
                parse: () => ++parses === 1 ? fallbackPayload() : resultPayload("new-result")});
            const oldParse = home.submit();
            await flush();
            const signal = home.requests.find(request => request.url === "/continuation").signal;
            if (action === "clear") home.clear();
            else await home.submit(); // Same source still represents a distinct parse.
            const expected = home.region.innerHTML, writes = home.writes.length;
            assert.equal(signal.aborted, true);
            if (outcome === "rejected") poll.reject(new Error("late failure"));
            else poll.resolve(resultPayload("late", {status: outcome}));
            await oldParse;
            assert.equal(home.region.innerHTML, expected);
            assert.equal(home.writes.length, writes);
            assert.equal(home.page.dataset.baseFilename, action === "clear" ? "xhs_image" : "new-result");
            assert.equal(home.submitButton.disabled, false);
        });
    }
}

for (const outcome of ["success", "failed", "rejected"]) {
    test(`home ${outcome} continuation cannot disturb an active fallback download`, async () => {
        const poll = deferred(), download = deferred();
        const home = continuationHome({poll: () => poll.promise, download: () => download.promise});
        const parsing = home.submit();
        await flush();
        home.select();
        home.download();
        await flush();
        assert.equal(home.downloadRequests.length, 1);
        const anchor = home.region.children[0], status = home.control("#status").innerText;
        if (outcome === "rejected") poll.reject(new Error("mock failure"));
        else poll.resolve(resultPayload("upgrade", {status: outcome}));
        await parsing;
        assert.equal(home.region.children[0], anchor);
        assert.equal(home.page.dataset.baseFilename, "fallback");
        assert.equal(home.control("#status").innerText, status);
        assert.equal(home.control("#download-btn").disabled, true);
        download.resolve();
        await flush();
        assert.equal(home.downloadRequests.length, 2, "the original download remains current and finishes");
        assert.equal(home.control("#status").innerText, "download finished");
        assert.equal(home.control("#download-btn").disabled, false);
        assert.equal(home.region.children[0], anchor, "no deferred replacement is queued");
    });
}

test("home does not replace fallback during aggregate picker download", async () => {
    const poll = deferred(), download = deferred();
    let isDownloadCurrent;
    const home = continuationHome({poll: () => poll.promise,
        pickerDownload: async (_snapshot, isCurrent) => { isDownloadCurrent = isCurrent; await download.promise; }});
    const parsing = home.submit();
    await flush();
    home.download();
    const anchor = home.region.children[0];
    poll.resolve(resultPayload("upgrade"));
    await parsing;
    assert.equal(home.region.children[0], anchor);
    assert.equal(isDownloadCurrent(), true);
    assert.equal(home.control("#download-btn").disabled, true);
    download.resolve();
    await flush();
    assert.equal(home.control("#download-btn").disabled, false);
    assert.equal(home.region.children[0], anchor);
});

test("home respects the individual picker cancellation control's existing busy state", async () => {
    const poll = deferred();
    const home = continuationHome({poll: () => poll.promise});
    const parsing = home.submit();
    await flush();
    const anchor = home.region.children[0];
    home.pickerCancel.hidden = false;
    poll.resolve(resultPayload("upgrade"));
    await parsing;
    assert.equal(home.region.children[0], anchor);
    assert.equal(home.page.dataset.baseFilename, "fallback");
});
