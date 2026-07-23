"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
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

test("permanent pending state ends after five polls", async () => {
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
    assert.equal(
        result.error_code,
        "DOUYIN_CONTINUATION_CLIENT_LIMIT",
    );
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
