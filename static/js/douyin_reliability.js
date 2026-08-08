(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesDouyinReliability = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    const MAX_POLLS = 5;
    const DEFAULT_POLL_AFTER_MS = 1500;

    function abortError() {
        const error = new Error("The operation was aborted.");
        error.name = "AbortError";
        return error;
    }

    function boundedPollDelay(value) {
        const parsed = Number.parseInt(value || DEFAULT_POLL_AFTER_MS, 10);
        if (!Number.isFinite(parsed)) {
            return DEFAULT_POLL_AFTER_MS;
        }
        return Math.max(250, Math.min(3000, parsed));
    }

    function waitForPoll(ms, signal) {
        return new Promise((resolve, reject) => {
            if (signal && signal.aborted) {
                reject(abortError());
                return;
            }
            const timer = setTimeout(resolve, boundedPollDelay(ms));
            if (signal) {
                signal.addEventListener("abort", () => {
                    clearTimeout(timer);
                    reject(abortError());
                }, {once: true});
            }
        });
    }

    function normalizeContinuationPayload(payload) {
        const data = payload && typeof payload === "object" ? payload : {};
        return {
            status: String(data.status || "failed"),
            continuationToken: String(data.continuation_token || ""),
            pollAfterMs: boundedPollDelay(data.poll_after_ms),
            message: String(data.message || ""),
            errorCode: String(data.error_code || ""),
            payload: data,
        };
    }

    function resolveContinuationResult(initialPayload, outcomePayload) {
        const initial = initialPayload && typeof initialPayload === "object"
            ? initialPayload
            : {};
        const outcome = outcomePayload && typeof outcomePayload === "object"
            ? outcomePayload
            : {};
        const hasFallback = Boolean(initial.has_results && initial.html);
        const outcomeStatus = String(outcome.status || "failed");
        if (
            hasFallback
            && ["failed", "timeout", "error"].includes(outcomeStatus)
        ) {
            return {
                ...initial,
                status: "success",
                continuation_status: outcomeStatus,
                continuation_error_code: String(
                    outcome.error_code || "",
                ),
                continuation_message: String(outcome.message || ""),
            };
        }
        return outcome;
    }

    async function runBoundedContinuation(options) {
        const settings = options || {};
        const requestPoll = settings.requestPoll;
        if (typeof requestPoll !== "function") {
            throw new TypeError("requestPoll is required");
        }
        const wait = typeof settings.wait === "function"
            ? settings.wait
            : waitForPoll;
        const maxPolls = Math.max(
            1,
            Math.min(
                MAX_POLLS,
                Number.parseInt(settings.maxPolls || MAX_POLLS, 10),
            ),
        );
        const signal = settings.signal || null;
        let current = normalizeContinuationPayload(settings.initialPayload);
        const token = current.continuationToken;
        if (current.status !== "continuation_pending") {
            return current.payload;
        }
        if (!token) {
            return {
                status: "failed",
                error_code: "DOUYIN_CONTINUATION_TOKEN_MISSING",
                message: "解析续查凭据缺失，请重新尝试。",
            };
        }

        for (let attempt = 0; attempt < maxPolls; attempt += 1) {
            if (signal && signal.aborted) {
                throw abortError();
            }
            if (typeof settings.onPending === "function") {
                settings.onPending(
                    current.payload,
                    attempt + 1,
                    maxPolls,
                );
            }
            await wait(current.pollAfterMs, signal);
            const responsePayload = await requestPoll(token, {
                attempt: attempt + 1,
                maxPolls,
                signal,
            });
            current = normalizeContinuationPayload(responsePayload);
            if (current.status !== "continuation_pending") {
                return current.payload;
            }
        }
        return {
            status: "timeout",
            error_code: "DOUYIN_CONTINUATION_CLIENT_LIMIT",
            message: "超高清解析续查已结束，请重新尝试。",
        };
    }

    return Object.freeze({
        DEFAULT_POLL_AFTER_MS,
        MAX_POLLS,
        boundedPollDelay,
        normalizeContinuationPayload,
        resolveContinuationResult,
        runBoundedContinuation,
    });
}));
