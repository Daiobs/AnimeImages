(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesDownloadReliability = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    const ERROR_MESSAGES = Object.freeze({
        DOWNLOAD_HANDOFF_EXPIRED: "下载链接已过期，请重新点击下载。",
        PROXY_FAILED_RECOVERY_BLOCKED: "资源连接失败，自动恢复未成功，请重新解析后再试。",
        RATE_LIMIT_CACHE_ERROR: "下载状态服务暂时不可用，请稍后再试。",
        UPSTREAM_SSL_ERROR: "资源连接异常，请稍后重试。",
        UPSTREAM_CONNECTION_ERROR: "资源暂时无法连接，请稍后重试。",
        DOWNLOAD_CONNECTION_CLOSED: "下载连接已提前关闭，尚未确认文件传输完成。请查看浏览器下载栏。",
        TIANTIANJIEXI_REFRESH_TIMEOUT: "资源刷新超时，请重新解析后再试。",
        TIANTIANJIEXI_REFRESH_FAILED: "资源已失效，自动刷新失败，请重新解析。",
        TIANTIANJIEXI_RESOURCE_EXPIRED: "资源已失效，请重新解析后再试。",
        RATE_LIMITED: "请求过于频繁，请稍后再试。",
    });
    const DOWNLOAD_STATUS_STATES = Object.freeze([
        "issued",
        "connecting",
        "refreshing",
        "streaming",
        "completed",
        "failed",
        "expired",
    ]);
    const DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE =
        "下载请求已交给浏览器，本次状态查询已结束，尚未确认传输完成；"
        + "请查看浏览器下载栏，请勿重复点击下载。";
    const DEFAULT_STATUS_POLL_INTERVAL_MS = 500;
    const MAX_STATUS_POLL_INTERVAL_MS = 1000;
    const DEFAULT_START_OBSERVATION_MS = 60000;
    const STREAM_STATUS_POLL_INTERVAL_MS = 2000;
    const STATUS_FAILURE_OBSERVATION_MS = 15000;
    const MAX_BUSY_BACKOFF_MS = 4000;

    function booleanValue(value) {
        return value === true || String(value || "").toLowerCase() === "true";
    }

    function browserDirectFetchSafe(item) {
        if (!item || typeof item !== "object") {
            return false;
        }
        if (!String(item.provider || "").trim()) {
            return true;
        }
        return booleanValue(item.browser_direct_fetch_safe);
    }

    function shouldUsePreparedServerDownload(route) {
        return Boolean(
            route
            && route.status === "routed"
            && route.route_type === "proxy"
            && route.download_strategy === "server_single_attachment"
        );
    }

    function errorMessage(errorCode, fallbackMessage) {
        return ERROR_MESSAGES[String(errorCode || "").toUpperCase()]
            || String(fallbackMessage || "").trim()
            || "下载失败，请稍后再试。";
    }

    function boundedInteger(value, fallback, minimum, maximum) {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return Math.max(minimum, Math.min(maximum, parsed));
    }

    function parsePreparePayload(payload, fallbackStatus) {
        const data = payload && typeof payload === "object" ? payload : {};
        const errorCode = String(data.error_code || "").toUpperCase();
        const status = String(data.status || fallbackStatus || "failed");
        return {
            ok: Boolean(
                data.ok
                && status === "issued"
                && data.download_url
                && data.status_url
            ),
            status,
            downloadUrl: String(data.download_url || ""),
            statusUrl: String(data.status_url || ""),
            pollAfterMs: boundedInteger(
                data.poll_after_ms,
                DEFAULT_STATUS_POLL_INTERVAL_MS,
                250,
                MAX_STATUS_POLL_INTERVAL_MS,
            ),
            expiresInSeconds: boundedInteger(
                data.expires_in_seconds,
                60,
                1,
                300,
            ),
            errorCode,
            message: errorMessage(errorCode, data.message),
        };
    }

    function parseDownloadStatusPayload(payload, responseStatus) {
        const data = payload && typeof payload === "object" ? payload : {};
        let status = String(data.status || "").toLowerCase();
        if (!DOWNLOAD_STATUS_STATES.includes(status)) {
            status = Number(responseStatus) === 410 ? "expired" : "failed";
        }
        const errorCode = String(data.error_code || "").toUpperCase();
        return {
            ok: Boolean(data.ok),
            status,
            recovered: Boolean(data.recovered),
            streamStarted: Boolean(data.stream_started),
            upstreamAttemptCount: boundedInteger(
                data.upstream_attempt_count,
                0,
                0,
                2,
            ),
            errorCode,
            message: errorMessage(errorCode, data.message),
        };
    }

    function isDownloadStartTerminal(status) {
        return ["completed", "failed", "expired"].includes(
            String(status || ""),
        );
    }

    function statusPollTimeoutResult() {
        return {
            ok: false,
            status: "poll_timeout",
            recovered: false,
            streamStarted: false,
            upstreamAttemptCount: 0,
            errorCode: "DOWNLOAD_STATUS_POLL_TIMEOUT",
            message: DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
        };
    }

    function retryAfterDelayMs(response, epochMs) {
        const raw = response && response.headers && typeof response.headers.get === "function"
            ? response.headers.get("Retry-After") : null;
        if (typeof raw !== "string") return null;
        const value = raw.trim();
        if (!value || value.length > 128) return null;
        if (/^\d+$/.test(value)) {
            // Saturate huge values; never wrap a long delay into an immediate retry.
            return Math.min(Number(value) * 1000, Number.MAX_SAFE_INTEGER);
        }
        // Do not let Date.parse interpret negative/decimal seconds as calendar dates.
        if (!/^[A-Za-z]{3,9},?\s.*\sGMT$/i.test(value)) return null;
        const date = Date.parse(value);
        return Number.isFinite(date) ? Math.max(0, date - epochMs) : null;
    }

    function statusPollBackoffResult(httpStatus, retryAfterMs) {
        return {
            ...statusPollTimeoutResult(),
            httpStatus,
            retryAfterMs,
            message: "下载状态查询暂时繁忙，本次自动查询已暂停。"
                + "请先查看浏览器下载栏，稍后再查询；请勿反复点击下载。",
        };
    }

    function downloadStartDisposition(result) {
        const status = String(result && result.status || "");
        if (["failed", "expired"].includes(status)) {
            return "failed";
        }
        if (status === "poll_timeout") {
            return "unconfirmed";
        }
        if (result && result.streamStarted) {
            return "started";
        }
        if (["streaming", "completed"].includes(status)) {
            return "started";
        }
        return "failed";
    }

    function fetchStatusWithTimeout(fetchStatus, statusUrl, timeoutMs, signal) {
        const controller = typeof AbortController === "function"
            ? new AbortController()
            : null;
        return new Promise((resolve, reject) => {
            let settled = false;
            const finish = (value, error) => {
                if (settled) {
                    return;
                }
                settled = true;
                windowClearTimeout(timer);
                if (signal) signal.removeEventListener("abort", abort);
                if (error && error.name === "AbortError") reject(error);
                else resolve(value);
            };
            const timer = windowSetTimeout(() => {
                finish(null);
                if (controller) {
                    controller.abort();
                }
            }, timeoutMs);
            const abort = () => {
                finish(null);
                if (controller) controller.abort();
            };
            if (signal) signal.addEventListener("abort", abort, {once: true});
            if (signal && signal.aborted) { abort(); return; }

            Promise.resolve(fetchStatus(statusUrl, {
                method: "GET",
                cache: "no-store",
                credentials: "same-origin",
                signal: controller ? controller.signal : undefined,
            })).then(async (response) => ({
                response,
                payload: [429, 503].includes(response.status) ? null : await response.json(),
            })).then(finish, (error) => finish(null, error));
        });
    }

    const windowSetTimeout = (
        typeof setTimeout === "function"
            ? setTimeout
            : (() => 0)
    );
    const windowClearTimeout = (
        typeof clearTimeout === "function"
            ? clearTimeout
            : (() => {})
    );

    async function pollDownloadStatus(options) {
        const settings = options && typeof options === "object" ? options : {};
        const statusUrl = String(settings.statusUrl || "");
        const fetchStatus = settings.fetchStatus
            || (
                typeof fetch === "function"
                    ? fetch.bind(globalThis)
                    : null
            );
        if (!statusUrl || !fetchStatus) {
            return statusPollTimeoutResult();
        }

        const wait = settings.wait || (
            (delay) => new Promise((resolve) => setTimeout(resolve, delay))
        );
        const now = settings.now || (() => Date.now());
        const epochNow = settings.epochNow || (() => Date.now());
        const intervalMs = boundedInteger(
            settings.pollAfterMs,
            DEFAULT_STATUS_POLL_INTERVAL_MS,
            250,
            MAX_STATUS_POLL_INTERVAL_MS,
        );
        const startTimeoutMs = boundedInteger(
            settings.startTimeoutMs,
            DEFAULT_START_OBSERVATION_MS,
            50,
            300000,
        );
        const startedAt = now();
        let nextDelayMs = intervalMs;
        let busyCount = 0;
        let busyResult = null;
        let lastStreaming = null;
        let failureStartedAt = null;
        const unfinishedResult = () => ({
            ...(busyResult || statusPollTimeoutResult()),
            streamStarted: Boolean(lastStreaming),
            recovered: Boolean(lastStreaming && lastStreaming.recovered),
            upstreamAttemptCount: lastStreaming ? lastStreaming.upstreamAttemptCount : 0,
        });

        const doc = settings.document || (typeof document !== "undefined" ? document : null);
        const page = settings.window || (typeof window !== "undefined" ? window : null);
        const observer = new AbortController();
        let pageLeft = false;
        const leavePage = () => { pageLeft = true; observer.abort(); };
        const cancel = () => observer.abort();
        if (page) page.addEventListener("pagehide", leavePage);
        if (settings.signal) settings.signal.addEventListener("abort", cancel, {once: true});
        const checkCancelled = () => {
            if (settings.signal && settings.signal.aborted) {
                throw Object.assign(new Error("Status observation cancelled"), {name: "AbortError"});
            }
        };
        const remainingObservationMs = () => Math.min(
            lastStreaming ? Infinity : startTimeoutMs - (now() - startedAt),
            failureStartedAt === null ? Infinity : STATUS_FAILURE_OBSERVATION_MS - (now() - failureStartedAt),
        );
        const noteFailure = () => { if (failureStartedAt === null) failureStartedAt = now(); };

        try {
            for (let attempt = 0; ; attempt += 1) {
                checkCancelled();
                if (pageLeft) return unfinishedResult();
                if (doc && doc.hidden) {
                    await new Promise(resolve => {
                        const resume = () => {
                            if (doc.hidden && !observer.signal.aborted) return;
                            doc.removeEventListener("visibilitychange", resume);
                            observer.signal.removeEventListener("abort", resume);
                            resolve();
                        };
                        doc.addEventListener("visibilitychange", resume);
                        observer.signal.addEventListener("abort", resume, {once: true});
                        resume();
                    });
                    checkCancelled();
                    if (pageLeft) return unfinishedResult();
                }
                if (remainingObservationMs() <= 0) return unfinishedResult();
                if (attempt > 0) {
                    if (nextDelayMs >= remainingObservationMs()) return unfinishedResult();
                    await wait(nextDelayMs);
                    checkCancelled();
                    if (pageLeft) return unfinishedResult();
                    if (doc && doc.hidden) continue;
                    if (remainingObservationMs() <= 0) return unfinishedResult();
                }
                const observation = await fetchStatusWithTimeout(
                    fetchStatus,
                    statusUrl,
                    Math.max(1, Math.min(2000, remainingObservationMs())),
                    observer.signal,
                );
                checkCancelled();
                if (pageLeft) return unfinishedResult();
                if (!observation) {
                    noteFailure();
                    continue;
                }
                const {response, payload} = observation;

                if (response.status === 429 || response.status === 503) {
                    noteFailure();
                    busyCount += 1;
                    const retryAfterMs = retryAfterDelayMs(response, epochNow());
                    const fallbackMs = Math.min(MAX_BUSY_BACKOFF_MS, 1000 * (2 ** Math.min(busyCount - 1, 2)));
                    nextDelayMs = Math.max(intervalMs, retryAfterMs === null ? fallbackMs : retryAfterMs);
                    busyResult = statusPollBackoffResult(response.status, nextDelayMs);
                    // Bound status-service outages, not healthy media transfer duration.
                    continue;
                }
                nextDelayMs = lastStreaming ? STREAM_STATUS_POLL_INTERVAL_MS : intervalMs;
                busyCount = 0;
                busyResult = null;
                // A broken status response is not evidence that the media transfer failed.
                if (!payload || !DOWNLOAD_STATUS_STATES.includes(String(payload.status || "").toLowerCase())) {
                    noteFailure();
                    continue;
                }
                if (!response.ok && lastStreaming) return unfinishedResult();
                failureStartedAt = null;

                const parsed = parseDownloadStatusPayload(
                    payload,
                    response.status,
                );
                if (parsed.status === "expired" && lastStreaming) return unfinishedResult();
                if (["failed", "expired"].includes(parsed.status)) {
                    return parsed;
                }
                if (isDownloadStartTerminal(parsed.status)) {
                    return parsed;
                }
                if (!response.ok) {
                    return {
                        ...parsed,
                        ok: false,
                        status: response.status === 410 ? "expired" : "failed",
                    };
                }
                if (parsed.streamStarted || parsed.status === "streaming") {
                    lastStreaming = {...parsed, streamStarted: true};
                    nextDelayMs = STREAM_STATUS_POLL_INTERVAL_MS;
                    if (typeof settings.onStreaming === "function") {
                        settings.onStreaming(lastStreaming);
                    }
                }
            }
        } finally {
            if (page) page.removeEventListener("pagehide", leavePage);
            if (settings.signal) settings.signal.removeEventListener("abort", cancel);
        }
    }

    return Object.freeze({
        DEFAULT_STATUS_POLL_INTERVAL_MS,
        DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
        DOWNLOAD_STATUS_STATES,
        ERROR_MESSAGES,
        DEFAULT_START_OBSERVATION_MS,
        STATUS_FAILURE_OBSERVATION_MS,
        browserDirectFetchSafe,
        downloadStartDisposition,
        errorMessage,
        isDownloadStartTerminal,
        parseDownloadStatusPayload,
        parsePreparePayload,
        pollDownloadStatus,
        retryAfterDelayMs,
        shouldUsePreparedServerDownload,
    });
}));
