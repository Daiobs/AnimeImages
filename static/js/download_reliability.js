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
        PROVIDER_MANIFEST_EXPIRED: "解析结果已过期，请重新解析后下载。",
        PROXY_FAILED_RECOVERY_BLOCKED: "资源连接失败，自动恢复未成功，请重新解析后再试。",
        RATE_LIMIT_CACHE_ERROR: "下载状态服务暂时不可用，请稍后再试。",
        UPSTREAM_SSL_ERROR: "上游资源连接异常，请稍后重试。",
        UPSTREAM_CONNECTION_ERROR: "上游资源暂时无法连接，请稍后重试。",
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
        "下载请求已交给浏览器，服务器仍在连接资源；"
        + "请查看浏览器下载栏，若未开始可重新尝试。";
    const DEFAULT_STATUS_POLL_INTERVAL_MS = 500;
    const MAX_STATUS_POLL_INTERVAL_MS = 1000;
    const MAX_STATUS_POLL_DURATION_MS = 15000;
    const MAX_STATUS_POLL_COUNT = 30;
    const BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES = 32 * 1024 * 1024;
    const DIRECT_ROUTE_SELECTED = "DIRECT_ROUTE_SELECTED";
    const DIRECT_LAUNCH_ISSUED = "DIRECT_LAUNCH_ISSUED";
    const DIRECT_DOWNLOAD_STARTED = "DIRECT_DOWNLOAD_STARTED";

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

    function browserNativeHandoffSafe(item) {
        if (!item || typeof item !== "object") {
            return false;
        }
        return Boolean(
            booleanValue(item.browser_native_handoff_safe)
            && String(item.browser_direct_mode || "") === "native_handoff"
            && !booleanValue(item.requires_merge)
            && !booleanValue(item.requires_cookie)
        );
    }

    function browserNativeOpenCandidate(item) {
        if (!item || typeof item !== "object") {
            return false;
        }
        return Boolean(
            String(item.type || "").toLowerCase() === "video"
            && booleanValue(item.browser_native_handoff_capable)
            && !booleanValue(item.requires_merge)
            && !booleanValue(item.requires_cookie)
        );
    }

    function directLaunchError(message, errorCode) {
        const error = new Error(message);
        error.errorCode = errorCode;
        return error;
    }

    function preopenBrowserDirectContext(openWindow) {
        const opener = typeof openWindow === "function" ? openWindow : null;
        if (!opener) {
            return {ok: false, target: null, issued: false};
        }
        let target = null;
        try {
            target = opener("about:blank", "_blank");
            if (target) {
                target.opener = null;
            }
        } catch (error) {
            target = null;
        }
        return {
            ok: Boolean(target),
            target,
            issued: false,
        };
    }

    function closeBrowserDirectContext(context) {
        if (!context || !context.target || context.issued) {
            return false;
        }
        try {
            if (!context.target.closed && typeof context.target.close === "function") {
                context.target.close();
            }
        } catch (error) {
            return false;
        }
        return true;
    }

    function issueBrowserDirectOpen(context, item) {
        if (!context || !context.ok || !context.target) {
            throw directLaunchError(
                "浏览器阻止了新标签页，请允许弹出窗口后重试，或使用兼容下载。",
                "DIRECT_POPUP_BLOCKED",
            );
        }
        if (!browserNativeHandoffSafe(item)) {
            throw directLaunchError(
                "当前资源没有经过验证的浏览器高速直连能力。",
                "DIRECT_ROUTE_NOT_VALIDATED",
            );
        }
        let parsed;
        try {
            parsed = new URL(String(item.url || ""));
        } catch (error) {
            throw directLaunchError(
                "下载路由返回了无效资源。",
                "DIRECT_URL_INVALID",
            );
        }
        if (!["http:", "https:"].includes(parsed.protocol)) {
            throw directLaunchError(
                "下载路由返回了无效资源。",
                "DIRECT_URL_INVALID",
            );
        }
        try {
            context.target.location.replace(parsed.href);
        } catch (error) {
            throw directLaunchError(
                "浏览器未能打开原始资源，请重试或使用兼容下载。",
                "DIRECT_LAUNCH_FAILED",
            );
        }
        context.issued = true;
        try {
            if (typeof context.target.focus === "function") {
                context.target.focus();
            }
        } catch (error) {
            // Navigation was issued successfully; focus is best-effort only.
        }
        return {
            state: DIRECT_LAUNCH_ISSUED,
            mode: "native_open",
            completed: false,
            downloadStarted: false,
        };
    }

    function browserDirectFetchBlobSafe(item) {
        if (!item || typeof item !== "object") {
            return false;
        }
        const sizeBytes = Number.parseInt(
            item.size_bytes || item.sizeBytes || "0",
            10,
        ) || 0;
        return Boolean(
            browserDirectFetchSafe(item)
            && sizeBytes > 0
            && sizeBytes <= BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES
            && !booleanValue(item.requires_merge)
            && !booleanValue(item.requires_cookie)
        );
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
        return ["streaming", "completed", "failed", "expired"].includes(
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

    function downloadStartDisposition(result) {
        const status = String(result && result.status || "");
        if (result && result.streamStarted) {
            return "started";
        }
        if (["streaming", "completed"].includes(status)) {
            return "started";
        }
        if (status === "poll_timeout") {
            return "unconfirmed";
        }
        return "failed";
    }

    function fetchStatusWithTimeout(fetchStatus, statusUrl, timeoutMs) {
        const controller = typeof AbortController === "function"
            ? new AbortController()
            : null;
        return new Promise((resolve) => {
            let settled = false;
            const finish = (value) => {
                if (settled) {
                    return;
                }
                settled = true;
                windowClearTimeout(timer);
                resolve(value);
            };
            const timer = windowSetTimeout(() => {
                if (controller) {
                    controller.abort();
                }
                finish(null);
            }, timeoutMs);

            Promise.resolve(fetchStatus(statusUrl, {
                method: "GET",
                cache: "no-store",
                credentials: "same-origin",
                signal: controller ? controller.signal : undefined,
            })).then(finish, () => finish(null));
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
        const intervalMs = boundedInteger(
            settings.pollAfterMs,
            DEFAULT_STATUS_POLL_INTERVAL_MS,
            250,
            MAX_STATUS_POLL_INTERVAL_MS,
        );
        const maxDurationMs = boundedInteger(
            settings.maxDurationMs,
            MAX_STATUS_POLL_DURATION_MS,
            50,
            MAX_STATUS_POLL_DURATION_MS,
        );
        const maxPolls = boundedInteger(
            settings.maxPolls,
            MAX_STATUS_POLL_COUNT,
            1,
            MAX_STATUS_POLL_COUNT,
        );
        const startedAt = now();

        for (let attempt = 0; attempt < maxPolls; attempt += 1) {
            if (attempt > 0) {
                await wait(intervalMs);
            }
            if ((now() - startedAt) >= maxDurationMs) {
                return statusPollTimeoutResult();
            }

            const remainingDurationMs = Math.max(
                1,
                maxDurationMs - (now() - startedAt),
            );
            const response = await fetchStatusWithTimeout(
                fetchStatus,
                statusUrl,
                Math.min(2000, remainingDurationMs),
            );
            if (!response) {
                if ((now() - startedAt) >= maxDurationMs) {
                    return statusPollTimeoutResult();
                }
                continue;
            }

            let payload = {};
            try {
                payload = await response.json();
            } catch (error) {
                payload = {};
            }
            if (response.status === 429 || response.status === 503) {
                continue;
            }

            const parsed = parseDownloadStatusPayload(
                payload,
                response.status,
            );
            if (parsed.streamStarted) {
                return {
                    ...parsed,
                    ok: true,
                    finalStatus: parsed.status,
                    status: (
                        parsed.status === "completed"
                            ? "completed"
                            : "streaming"
                    ),
                };
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
        }
        return statusPollTimeoutResult();
    }

    return Object.freeze({
        DEFAULT_STATUS_POLL_INTERVAL_MS,
        BROWSER_DIRECT_FETCH_BLOB_MAX_BYTES,
        DIRECT_DOWNLOAD_STARTED,
        DIRECT_LAUNCH_ISSUED,
        DIRECT_ROUTE_SELECTED,
        DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
        DOWNLOAD_STATUS_STATES,
        ERROR_MESSAGES,
        MAX_STATUS_POLL_COUNT,
        MAX_STATUS_POLL_DURATION_MS,
        browserDirectFetchSafe,
        browserDirectFetchBlobSafe,
        browserNativeHandoffSafe,
        browserNativeOpenCandidate,
        closeBrowserDirectContext,
        downloadStartDisposition,
        errorMessage,
        isDownloadStartTerminal,
        parseDownloadStatusPayload,
        parsePreparePayload,
        pollDownloadStatus,
        preopenBrowserDirectContext,
        issueBrowserDirectOpen,
        shouldUsePreparedServerDownload,
    });
}));
