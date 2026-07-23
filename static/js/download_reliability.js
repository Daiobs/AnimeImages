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
        PROXY_FAILED_RECOVERY_BLOCKED: "资源连接失败，自动恢复未成功，请重新解析后再试。",
        UPSTREAM_SSL_ERROR: "上游资源连接异常，请稍后重试。",
        UPSTREAM_CONNECTION_ERROR: "上游资源暂时无法连接，请稍后重试。",
        TIANTIANJIEXI_REFRESH_TIMEOUT: "资源刷新超时，请重新解析后再试。",
        TIANTIANJIEXI_REFRESH_FAILED: "资源已失效，自动刷新失败，请重新解析。",
        TIANTIANJIEXI_RESOURCE_EXPIRED: "资源已失效，请重新解析后再试。",
        RATE_LIMITED: "请求过于频繁，请稍后再试。",
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

    function parsePreparePayload(payload, fallbackStatus) {
        const data = payload && typeof payload === "object" ? payload : {};
        const errorCode = String(data.error_code || "").toUpperCase();
        return {
            ok: Boolean(data.ok && data.status === "ready" && data.download_url),
            status: String(data.status || fallbackStatus || "failed"),
            downloadUrl: String(data.download_url || ""),
            recovered: Boolean(data.recovered),
            errorCode,
            message: errorMessage(errorCode, data.message),
        };
    }

    return Object.freeze({
        ERROR_MESSAGES,
        browserDirectFetchSafe,
        errorMessage,
        parsePreparePayload,
        shouldUsePreparedServerDownload,
    });
}));
