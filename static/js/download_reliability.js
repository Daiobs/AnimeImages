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
    const BROWSER_DIRECT_HANDOFF_VERSION = 2;
    const BROWSER_DIRECT_DELIVERY_MODE = "filesystem_stream";
    const DIRECT_ROUTE_SELECTED = "DIRECT_ROUTE_SELECTED";
    const DIRECT_STATES = Object.freeze({
        IDLE: "idle",
        CHOOSING_DESTINATION: "choosing_destination",
        CONNECTING: "connecting",
        WRITING: "writing",
        COMPLETED: "completed",
        CANCELLED: "cancelled",
        FAILED: "failed",
    });

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

    function validDirectUrl(value) {
        let parsed;
        try {
            parsed = new URL(String(value || ""));
        } catch (error) {
            return false;
        }
        return ["http:", "https:"].includes(parsed.protocol);
    }

    function browserDirectStreamingSafe(item) {
        if (!item || typeof item !== "object") {
            return false;
        }
        const provider = String(item.provider || "").trim();
        const providerIdentityComplete = !provider || Boolean(
            String(item.asset_id || "").trim()
            && String(item.provider_resource_id || "").trim()
            && String(item.provider_cache_key || "").trim()
            && String(item.provider_manifest_token || "").trim()
        );
        return Boolean(
            String(item.type || "").toLowerCase() === "video"
            && validDirectUrl(item.url)
            && Number.parseInt(
                item.browser_direct_handoff_version || item.version || "0",
                10,
            ) === BROWSER_DIRECT_HANDOFF_VERSION
            && String(
                item.browser_direct_delivery_mode
                || item.browser_direct_mode
                || "",
            ) === BROWSER_DIRECT_DELIVERY_MODE
            && (
                booleanValue(item.browser_streaming_direct_capable)
                || booleanValue(item.browser_streaming_direct_safe)
            )
            && providerIdentityComplete
            && !booleanValue(item.requires_merge)
            && !booleanValue(item.requires_cookie)
        );
    }

    function directDownloadError(message, errorCode, cause) {
        const error = new Error(message);
        error.errorCode = errorCode;
        if (cause) {
            error.cause = cause;
        }
        return error;
    }

    function browserDirectHandoffExpired(item, nowMilliseconds) {
        const expiresAt = Number.parseInt(
            item && (
                item.browser_direct_expires_at
                || item.expires_at
            ) || "0",
            10,
        ) || 0;
        if (!expiresAt) {
            return false;
        }
        const now = Number.isFinite(nowMilliseconds)
            ? nowMilliseconds
            : Date.now();
        return expiresAt <= Math.floor(now / 1000);
    }

    function sanitizeDirectFilename(value) {
        const cleaned = String(value || "video.mp4")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 180);
        return cleaned || "video.mp4";
    }

    function directPickerOptions(item) {
        const filename = sanitizeDirectFilename(
            item.filename || item.download_filename || "video.mp4",
        );
        const suffix = filename.match(/\.[a-z0-9]{1,8}$/i);
        const extension = suffix ? suffix[0].toLowerCase() : ".mp4";
        return {
            suggestedName: suffix ? filename : `${filename}.mp4`,
            types: [{
                description: "Video file",
                accept: {
                    "video/mp4": [extension],
                },
            }],
        };
    }

    function directClock(options) {
        if (options && typeof options.now === "function") {
            return options.now;
        }
        if (
            typeof performance !== "undefined"
            && typeof performance.now === "function"
        ) {
            return performance.now.bind(performance);
        }
        return Date.now;
    }

    function notifyDirectState(callback, state, details) {
        if (typeof callback !== "function") {
            return;
        }
        try {
            callback(state, details || {});
        } catch (error) {
            // UI observers must not interrupt media streaming.
        }
    }

    function directExpectedSizes(item, response) {
        const values = [];
        const responseSize = Number.parseInt(
            response.headers.get("Content-Length") || "0",
            10,
        ) || 0;
        const itemSize = Number.parseInt(
            item.size_bytes || item.sizeBytes || "0",
            10,
        ) || 0;
        if (responseSize > 0) {
            values.push(responseSize);
        }
        if (itemSize > 0 && !booleanValue(item.size_is_estimate)) {
            values.push(itemSize);
        }
        return [...new Set(values)];
    }

    async function streamBrowserDirectDownload(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        if (!browserDirectStreamingSafe(item)) {
            throw directDownloadError(
                "当前资源没有经过验证的浏览器流式直连能力。",
                "DIRECT_HANDOFF_NOT_VALIDATED",
            );
        }
        const picker = settings.showSaveFilePicker || (
            typeof showSaveFilePicker === "function"
                ? showSaveFilePicker.bind(globalThis)
                : null
        );
        if (!picker) {
            throw directDownloadError(
                "当前浏览器不支持流式保存，请使用最新版 Chrome 后重试。",
                "DIRECT_BROWSER_API_UNAVAILABLE",
            );
        }
        const fetchMedia = settings.fetch || (
            typeof fetch === "function" ? fetch.bind(globalThis) : null
        );
        if (!fetchMedia) {
            throw directDownloadError(
                "当前浏览器不支持直接下载，请使用最新版 Chrome 后重试。",
                "DIRECT_FETCH_UNAVAILABLE",
            );
        }

        const now = directClock(settings);
        const startedAt = now();
        notifyDirectState(
            settings.onState,
            DIRECT_STATES.CHOOSING_DESTINATION,
        );
        let fileHandle;
        try {
            fileHandle = await picker(directPickerOptions(item));
        } catch (error) {
            if (error && error.name === "AbortError") {
                notifyDirectState(settings.onState, DIRECT_STATES.CANCELLED);
                return {
                    state: DIRECT_STATES.CANCELLED,
                    completed: false,
                    cancelled: true,
                    receivedBytes: 0,
                    writtenBytes: 0,
                };
            }
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "DIRECT_PICKER_FAILED",
            });
            throw directDownloadError(
                "无法选择保存位置，请重试。",
                "DIRECT_PICKER_FAILED",
                error,
            );
        }

        if (!fileHandle || typeof fileHandle.createWritable !== "function") {
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "DIRECT_WRITER_UNAVAILABLE",
            });
            throw directDownloadError(
                "浏览器无法写入所选文件，请重试。",
                "DIRECT_WRITER_UNAVAILABLE",
            );
        }

        if (browserDirectHandoffExpired(item, settings.nowEpochMs)) {
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "PROVIDER_MANIFEST_EXPIRED",
            });
            throw directDownloadError(
                "解析结果已过期，请重新解析后下载。",
                "PROVIDER_MANIFEST_EXPIRED",
            );
        }

        notifyDirectState(settings.onState, DIRECT_STATES.CONNECTING);
        let response;
        try {
            response = await fetchMedia(String(item.url), {
                method: "GET",
                credentials: "omit",
                cache: "no-store",
            });
        } catch (error) {
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "DIRECT_NETWORK_FAILED",
            });
            throw directDownloadError(
                "无法连接资源，请重试下载；若仍失败，请重新解析。",
                "DIRECT_NETWORK_FAILED",
                error,
            );
        }
        if (!response || !response.ok) {
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "DIRECT_HTTP_FAILED",
            });
            throw directDownloadError(
                "资源暂时不可用，请重新解析后再试。",
                "DIRECT_HTTP_FAILED",
            );
        }
        if (!response.body || typeof response.body.getReader !== "function") {
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: "DIRECT_BODY_UNAVAILABLE",
            });
            throw directDownloadError(
                "浏览器无法读取资源流，请使用最新版 Chrome 重试。",
                "DIRECT_BODY_UNAVAILABLE",
            );
        }
        const reader = response.body.getReader();
        let writer = null;
        let writerClosed = false;
        let receivedBytes = 0;
        let writtenBytes = 0;
        let firstChunkAt = 0;
        let writerStartedAt = 0;
        const expectedSizes = directExpectedSizes(item, response);
        const expectedBytes = expectedSizes[0] || 0;
        try {
            writer = await fileHandle.createWritable();
            writerStartedAt = now();
            notifyDirectState(settings.onState, DIRECT_STATES.WRITING, {
                expectedBytes,
            });
            while (true) {
                const chunk = await reader.read();
                if (chunk.done) {
                    break;
                }
                if (!chunk.value || !chunk.value.byteLength) {
                    continue;
                }
                if (!firstChunkAt) {
                    firstChunkAt = now();
                }
                receivedBytes += chunk.value.byteLength;
                await writer.write(chunk.value);
                writtenBytes += chunk.value.byteLength;
                if (typeof settings.onProgress === "function") {
                    const percent = expectedBytes > 0
                        ? Math.min(99, Math.floor(
                            (writtenBytes / expectedBytes) * 100,
                        ))
                        : null;
                    settings.onProgress({
                        expectedBytes,
                        percent,
                        receivedBytes,
                        writtenBytes,
                    });
                }
            }
            if (
                receivedBytes !== writtenBytes
                || expectedSizes.some((size) => size !== writtenBytes)
            ) {
                throw directDownloadError(
                    "文件大小校验失败，请重新解析后重试。",
                    "DIRECT_SIZE_MISMATCH",
                );
            }
            await writer.close();
            writerClosed = true;
        } catch (error) {
            try {
                await reader.cancel();
            } catch (cancelError) {
                // The stream may already be closed after a read failure.
            }
            if (writer && !writerClosed && typeof writer.abort === "function") {
                try {
                    await writer.abort();
                } catch (abortError) {
                    // Preserve the original stream or writer failure.
                }
            }
            const failure = error && error.errorCode
                ? error
                : directDownloadError(
                    "下载未完成，请重试；若仍失败，请重新解析。",
                    "DIRECT_STREAM_FAILED",
                    error,
                );
            notifyDirectState(settings.onState, DIRECT_STATES.FAILED, {
                errorCode: failure.errorCode,
            });
            throw failure;
        }

        const completedAt = now();
        notifyDirectState(settings.onState, DIRECT_STATES.COMPLETED, {
            expectedBytes,
            receivedBytes,
            writtenBytes,
        });
        return {
            state: DIRECT_STATES.COMPLETED,
            completed: true,
            cancelled: false,
            expectedBytes,
            firstChunkLatencyMs: firstChunkAt ? firstChunkAt - startedAt : null,
            receivedBytes,
            totalDurationMs: completedAt - startedAt,
            writerStartLatencyMs: writerStartedAt - startedAt,
            writtenBytes,
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
        BROWSER_DIRECT_DELIVERY_MODE,
        BROWSER_DIRECT_HANDOFF_VERSION,
        DIRECT_ROUTE_SELECTED,
        DIRECT_STATES,
        DOWNLOAD_STATUS_POLL_TIMEOUT_MESSAGE,
        DOWNLOAD_STATUS_STATES,
        ERROR_MESSAGES,
        MAX_STATUS_POLL_COUNT,
        MAX_STATUS_POLL_DURATION_MS,
        browserDirectFetchSafe,
        browserDirectFetchBlobSafe,
        browserDirectHandoffExpired,
        browserDirectStreamingSafe,
        downloadStartDisposition,
        errorMessage,
        isDownloadStartTerminal,
        parseDownloadStatusPayload,
        parsePreparePayload,
        pollDownloadStatus,
        streamBrowserDirectDownload,
        shouldUsePreparedServerDownload,
    });
}));
