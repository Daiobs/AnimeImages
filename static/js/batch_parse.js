// Batch parse page behavior. Results stay in the current DOM and are never persisted.

(function () {
    "use strict";

    function buildEnabledPreferencePatch(ultraInput, youtubeInput) {
        const patch = {};
        if (ultraInput && !ultraInput.disabled) {
            patch.ultra_hd = Boolean(ultraInput.checked);
        }
        if (youtubeInput && !youtubeInput.disabled) {
            patch.youtube_merge = Boolean(youtubeInput.checked);
        }
        return patch;
    }

    function commitEnabledParsePreferences(
        parsePreferencesApi,
        currentPreferences,
        ultraInput,
        youtubeInput,
        renderedDefaults
    ) {
        const patch = buildEnabledPreferencePatch(ultraInput, youtubeInput);
        if (parsePreferencesApi && typeof parsePreferencesApi.update === "function") {
            return parsePreferencesApi.update(patch, {defaults: renderedDefaults});
        }
        return {
            version: 1,
            ultra_hd: Boolean(currentPreferences && currentPreferences.ultra_hd),
            youtube_merge: Boolean(currentPreferences && currentPreferences.youtube_merge),
            ...patch,
        };
    }

    function fallbackCopy(text) {
        const area = document.createElement("textarea");
        area.value = text || "";
        area.setAttribute("readonly", "readonly");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        try {
            document.execCommand("copy");
        } finally {
            document.body.removeChild(area);
        }
    }

    async function copyText(text) {
        const value = text || "";
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(value);
            return;
        }
        fallbackCopy(value);
    }

    function flashButton(button, label) {
        if (!button) {
            return;
        }
        const original = button.textContent;
        button.textContent = label;
        window.setTimeout(() => {
            button.textContent = original;
        }, 1300);
    }

    function downloadTextFile(filename, content) {
        const blob = new Blob([content], {type: "text/plain;charset=utf-8"});
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function sanitizeFilename(filename) {
        const fallback = "video.mp4";
        const value = String(filename || fallback)
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, " ")
            .trim();
        return value || fallback;
    }

    function saveBlob(filename, blob) {
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = sanitizeFilename(filename);
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function batchDispositionFilename(response) {
        const disposition = String(
            response && response.headers
                ? response.headers.get("Content-Disposition") || ""
                : ""
        );
        const encoded = disposition.match(/filename\*=([^;]+)/i);
        const quoted = disposition.match(/filename="([^"]+)"/i);
        const value = encoded ? encoded[1] : (quoted ? quoted[1] : "");
        if (!value) {
            return "";
        }
        const cleaned = value.trim().replace(/^"|"$/g, "").replace(/^UTF-8''/i, "");
        try {
            return decodeURIComponent(cleaned);
        } catch (error) {
            return cleaned;
        }
    }

    function getCookie(name) {
        const prefix = `${name}=`;
        const match = String(document.cookie || "")
            .split(";")
            .map((value) => value.trim())
            .find((value) => value.startsWith(prefix));
        return match ? decodeURIComponent(match.slice(prefix.length)) : "";
    }

    function batchBooleanValue(value) {
        return value === true || String(value || "").toLowerCase() === "true";
    }

    function buildBatchDownloadItem(itemNode) {
        const dataset = itemNode && itemNode.dataset ? itemNode.dataset : {};
        return {
            type: dataset.type === "video" ? "video" : "image",
            url: String(dataset.url || ""),
            cover_url: String(dataset.coverUrl || ""),
            size_bytes: Number.parseInt(dataset.sizeBytes || "0", 10) || 0,
            asset_id: String(dataset.assetId || ""),
            provider: String(dataset.provider || ""),
            provider_resource_id: String(dataset.providerResourceId || ""),
            provider_cache_key: String(dataset.providerCacheKey || ""),
            provider_manifest_token: String(dataset.providerManifestToken || ""),
            download_strategy: String(dataset.downloadStrategy || ""),
            fallback_strategy: String(dataset.fallbackStrategy || ""),
            cors_expected: batchBooleanValue(dataset.corsExpected),
            browser_direct_fetch_safe: batchBooleanValue(
                dataset.browserDirectFetchSafe
            ),
            browser_native_handoff_capable: batchBooleanValue(
                dataset.browserNativeHandoffCapable
            ),
            requires_merge: batchBooleanValue(dataset.requiresMerge),
            requires_cookie: batchBooleanValue(dataset.requiresCookie),
        };
    }

    function buildBatchDownloadPayload(itemNode) {
        const dataset = itemNode && itemNode.dataset ? itemNode.dataset : {};
        return {
            media_items: [buildBatchDownloadItem(itemNode)],
            base_filename: String(dataset.baseFilename || "media"),
            platform_key: String(dataset.platformKey || ""),
            download_mode: "auto",
        };
    }

    function batchDownloadRouteDisposition(route, reliabilityApi) {
        if (!route || route.status !== "routed") {
            return "blocked";
        }
        if (route.route_type === "direct") {
            return "direct";
        }
        if (
            reliabilityApi
            && typeof reliabilityApi.shouldUsePreparedServerDownload === "function"
            && reliabilityApi.shouldUsePreparedServerDownload(route)
        ) {
            return "prepared";
        }
        if (route.route_type === "proxy" || route.route_type === "zip") {
            return "server";
        }
        return "blocked";
    }

    function validatedDirectRouteItems(route) {
        const items = Array.isArray(route && route.items) ? route.items : [];
        if (!items.length) {
            throw new Error("下载路由没有返回可用资源。");
        }
        return items.map((item) => {
            const value = String(item && item.url || "");
            let parsed;
            try {
                parsed = new URL(value);
            } catch (error) {
                throw new Error("下载路由返回了无效资源。");
            }
            if (!(["http:", "https:"].includes(parsed.protocol))) {
                throw new Error("下载路由返回了无效资源。");
            }
            return item;
        });
    }

    function batchDirectRouteUsesBlob(items, reliabilityApi) {
        const directItems = Array.isArray(items) ? items : [];
        if (directItems.length !== 1) {
            return false;
        }
        const item = directItems[0] || {};
        if (String(item.type || "").toLowerCase() === "image") {
            return true;
        }
        return Boolean(
            String(item.provider || "").trim()
            && reliabilityApi
            && typeof reliabilityApi.browserDirectFetchBlobSafe === "function"
            && reliabilityApi.browserDirectFetchBlobSafe(item)
        );
    }

    function batchDirectRouteUsesNativeHandoff(items, reliabilityApi) {
        const directItems = Array.isArray(items) ? items : [];
        if (directItems.length !== 1) {
            return false;
        }
        const item = directItems[0] || {};
        return Boolean(
            String(item.type || "").toLowerCase() === "video"
            && reliabilityApi
            && typeof reliabilityApi.browserNativeHandoffSafe === "function"
            && reliabilityApi.browserNativeHandoffSafe(item)
        );
    }

    async function executePreparedBatchDownload(settings, route) {
        const reliabilityApi = settings.reliabilityApi;
        settings.onPhase("preparing");
        const prepared = await settings.prepare(route);
        settings.onPhase("connecting");
        await settings.handoffPrepared(prepared);
        const statusResult = await settings.pollStatus(prepared);
        const startDisposition = reliabilityApi.downloadStartDisposition(
            statusResult
        );
        if (startDisposition === "failed") {
            throw new Error(
                statusResult.message || "下载启动失败，请稍后重试。"
            );
        }
        return {
            kind: "prepared",
            handoffConfirmed: startDisposition === "started",
            unconfirmed: startDisposition === "unconfirmed",
            statusResult,
            route,
        };
    }

    async function executeBatchDownload(options) {
        const settings = options && typeof options === "object" ? options : {};
        const reliabilityApi = settings.reliabilityApi || null;
        const onPhase = typeof settings.onPhase === "function"
            ? settings.onPhase
            : (() => {});
        onPhase("routing");
        const route = await settings.fetchRoute();
        const disposition = batchDownloadRouteDisposition(route, reliabilityApi);

        if (disposition === "direct") {
            const directItems = validatedDirectRouteItems(route);
            onPhase("direct_handoff");
            try {
                const handoffResult = await settings.handoffDirect(directItems);
                return {
                    kind: "direct",
                    completed: Boolean(
                        handoffResult && handoffResult.completed
                    ),
                    handoffIssued: true,
                    handoffConfirmed: false,
                    route,
                };
            } catch (error) {
                if (!settings.fallbackDirectToPrepared || !reliabilityApi) {
                    throw error;
                }
                onPhase("direct_fallback");
                return executePreparedBatchDownload(
                    {...settings, onPhase},
                    route
                );
            }
        }

        if (disposition === "prepared") {
            return executePreparedBatchDownload(
                {...settings, onPhase},
                route
            );
        }

        if (disposition === "server") {
            onPhase("server_download");
            await settings.downloadServer(route);
            return {kind: "server", handoffConfirmed: true, route};
        }

        throw new Error(
            String(route && (route.message || route.route_reason) || "")
            || "当前资源没有可用的安全下载路径。"
        );
    }

    function getBatchDownloadFrame(id) {
        let frame = document.getElementById(id);
        if (frame) {
            return frame;
        }
        frame = document.createElement("iframe");
        frame.id = id;
        frame.name = id;
        frame.hidden = true;
        frame.style.display = "none";
        document.body.appendChild(frame);
        return frame;
    }

    function handoffBatchDirectUrl(url, filename) {
        const frame = getBatchDownloadFrame("batch-direct-download-frame");
        const link = document.createElement("a");
        link.href = url;
        link.target = frame.name;
        link.rel = "noopener noreferrer";
        link.referrerPolicy = "no-referrer";
        link.download = sanitizeFilename(filename);
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    function handoffBatchDirectItems(items, itemNode) {
        const fallbackFilename = String(
            itemNode && itemNode.dataset.downloadFilename || "media"
        );
        validatedDirectRouteItems({items}).forEach((item, index) => {
            const filename = String(
                item.filename
                || (items.length > 1
                    ? `${fallbackFilename}_${index + 1}`
                    : fallbackFilename)
            );
            handoffBatchDirectUrl(item.url, filename);
        });
    }

    async function downloadBatchDirectItems(items, itemNode, onProgress) {
        const directItems = validatedDirectRouteItems({items});
        if (directItems.length !== 1) {
            throw new Error("当前浏览器直连只支持单个资源。");
        }
        const response = await fetch(directItems[0].url, {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
        });
        if (!response.ok) {
            throw new Error("浏览器直连失败，正在切换安全下载通道。");
        }
        const contentType = String(response.headers.get("Content-Type") || "");
        if (contentType.toLowerCase().includes("text/html")) {
            throw new Error("浏览器直连未返回媒体文件。");
        }
        const blob = await readBlobWithProgress(response, onProgress);
        const filename = batchDispositionFilename(response) || String(
            directItems[0].filename
            || (itemNode && itemNode.dataset.downloadFilename)
            || "media"
        );
        saveBlob(filename, blob);
    }

    function sameOriginBatchHandoffPath(value) {
        const parsed = new URL(String(value || ""), window.location.origin);
        if (parsed.origin !== window.location.origin) {
            throw new Error("下载通道来源无效，请重新点击下载。");
        }
        return `${parsed.pathname}${parsed.search}`;
    }

    function handoffPreparedBatchDownload(prepared) {
        if (!prepared || !prepared.downloadUrl) {
            throw new Error("下载准备结果缺少有效链接。");
        }
        const frame = getBatchDownloadFrame("batch-server-download-frame");
        frame.src = sameOriginBatchHandoffPath(prepared.downloadUrl);
    }

    async function postBatchDownloadJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCookie("csrftoken"),
            },
            body: JSON.stringify(payload),
        });
        let data = {};
        try {
            data = await response.json();
        } catch (error) {
            data = {};
        }
        if (!response.ok) {
            throw new Error(
                String(data.message || "下载请求失败，请稍后重试。")
            );
        }
        return data;
    }

    async function prepareBatchServerDownload(url, payload, reliabilityApi) {
        if (!reliabilityApi) {
            throw new Error("下载可靠性组件未加载，请刷新页面后重试。");
        }
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCookie("csrftoken"),
            },
            body: JSON.stringify({
                ...payload,
                download_mode: "server_single",
            }),
        });
        let rawPayload = {};
        try {
            rawPayload = await response.json();
        } catch (error) {
            rawPayload = {
                status: "failed",
                message: "下载准备响应格式异常，请稍后再试。",
            };
        }
        const prepared = reliabilityApi.parsePreparePayload(
            rawPayload,
            "failed"
        );
        if (!response.ok || !prepared.ok) {
            throw new Error(prepared.message);
        }
        return prepared;
    }

    async function readBlobWithProgress(response, onProgress) {
        const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
        if (!response.body || !contentLength) {
            if (onProgress) {
                onProgress(null, "正在接收文件...");
            }
            return response.blob();
        }

        const reader = response.body.getReader();
        const chunks = [];
        let received = 0;

        while (true) {
            const {done, value} = await reader.read();
            if (done) {
                break;
            }
            chunks.push(value);
            received += value.length;
            if (onProgress) {
                onProgress(Math.min(100, Math.round((received / contentLength) * 100)));
            }
        }
        return new Blob(chunks, {type: response.headers.get("Content-Type") || "application/octet-stream"});
    }

    async function downloadBatchServerResponse(
        url,
        payload,
        itemNode,
        onProgress
    ) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCookie("csrftoken"),
            },
            body: JSON.stringify(payload),
        });
        const contentType = String(response.headers.get("Content-Type") || "");
        if (contentType.includes("application/json")) {
            let data = {};
            try {
                data = await response.json();
            } catch (error) {
                data = {};
            }
            if (
                response.ok
                && data.status === "routed"
                && data.route_type === "direct"
            ) {
                handoffBatchDirectItems(validatedDirectRouteItems(data), itemNode);
                return;
            }
            throw new Error(
                String(data.message || data.route_reason || "下载请求失败，请稍后重试。")
            );
        }
        if (!response.ok) {
            throw new Error("下载请求失败，请稍后重试。");
        }
        const blob = await readBlobWithProgress(response, onProgress);
        const filename = batchDispositionFilename(response) || String(
            itemNode && itemNode.dataset.downloadFilename || "media"
        );
        saveBlob(filename, blob);
    }

    function setBatchDownloadButtonLoading(button, loading, label) {
        if (!button) {
            return;
        }
        const labelNode = button.querySelector(".batch-download-label");
        if (!button.dataset.originalLabel && labelNode) {
            button.dataset.originalLabel = labelNode.textContent || "下载";
        }
        button.disabled = Boolean(loading);
        button.classList.toggle("is-loading", Boolean(loading));
        if (labelNode) {
            labelNode.textContent = loading
                ? (label || "下载中...")
                : (button.dataset.originalLabel || "下载");
        }
    }

    function showBatchDownloadButtonMessage(button, message) {
        const labelNode = button ? button.querySelector(".batch-download-label") : null;
        if (!labelNode) {
            return;
        }
        const original = button.dataset.originalLabel || labelNode.textContent || "下载";
        labelNode.textContent = message;
        window.setTimeout(() => {
            if (!button.classList.contains("is-loading")) {
                labelNode.textContent = original;
            }
        }, 1300);
    }

    function getBatchDownloadProgressElements(button) {
        const item = button ? button.closest("[data-batch-download-item]") : null;
        const progress = item ? item.querySelector("[data-batch-download-progress]") : null;
        return {
            progress,
            bar: progress ? progress.querySelector("[data-batch-download-progress-bar]") : null,
            label: progress ? progress.querySelector("[data-batch-download-progress-label]") : null,
        };
    }

    function updateBatchDownloadProgress(button, percent, label) {
        const elements = getBatchDownloadProgressElements(button);
        if (!elements.progress) {
            return;
        }
        elements.progress.hidden = false;
        const hasPercent = typeof percent === "number" && Number.isFinite(percent);
        elements.progress.classList.toggle("is-indeterminate", !hasPercent);
        if (elements.bar) {
            elements.bar.style.width = hasPercent ? `${Math.max(0, Math.min(100, percent))}%` : "45%";
        }
        if (elements.label) {
            elements.label.textContent = label || (hasPercent ? `${percent}%` : "正在下载...");
        }
    }

    function collectBatchVideoLinks() {
        return Array.from(document.querySelectorAll("[data-batch-direct-url]"))
            .map((node) => ({
                url: node.getAttribute("data-batch-direct-url") || "",
                filename: node.getAttribute("data-batch-direct-filename") || "video.mp4",
            }))
            .filter((item) => item.url);
    }

    function normalizeBatchValue(value) {
        return String(value || "").toLowerCase();
    }

    function cardMatchesFilter(card, filter) {
        if (filter === "all") {
            return true;
        }
        const status = normalizeBatchValue(card.dataset.status);
        const postType = normalizeBatchValue(card.dataset.postType);
        const platform = normalizeBatchValue(card.dataset.platform);
        const platformName = normalizeBatchValue(card.dataset.platformName);
        if (["success", "failed", "timeout", "skipped"].includes(filter)) {
            return status === filter;
        }
        if (filter === "video" || filter === "image") {
            return postType.indexOf(filter) !== -1;
        }
        return platform.indexOf(filter) !== -1 || platformName.indexOf(filter) !== -1;
    }

    function parseBatchCount(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }

    function updateBatchSummaryNode(summary, key, label, value, suffix) {
        const node = summary.querySelector(`[data-batch-summary-key="${key}"]`);
        if (node) {
            node.textContent = `${label} ${value}${suffix || ""}`;
        }
    }

    function applyActiveBatchFilter(batchPage) {
        const activeButton = batchPage.querySelector("[data-batch-filter].is-active");
        const filter = activeButton ? activeButton.getAttribute("data-batch-filter") : "all";
        batchPage.querySelectorAll(".batch-result-card").forEach((card) => {
            card.classList.toggle("is-hidden", !cardMatchesFilter(card, filter || "all"));
        });
    }

    function recomputeBatchSummary(batchPage, wallDurationMs, replaceWallDuration) {
        const summary = batchPage.querySelector("[data-batch-summary]");
        if (!summary) {
            return;
        }
        const values = {
            total_count: 0,
            success_count: 0,
            failed_count: 0,
            timeout_count: 0,
            skipped_count: 0,
            video_count: 0,
            image_count: 0,
            total_duration_ms: 0,
        };
        const providerCounts = new Map();
        batchPage.querySelectorAll(".batch-result-card").forEach((card) => {
            const status = normalizeBatchValue(card.dataset.status);
            values.total_count += 1;
            if (Object.prototype.hasOwnProperty.call(values, `${status}_count`)) {
                values[`${status}_count`] += 1;
            }
            values.video_count += parseBatchCount(card.dataset.videoCount);
            values.image_count += parseBatchCount(card.dataset.imageCount);
            values.total_duration_ms += parseBatchCount(card.dataset.durationMs);
            if (status === "success" && card.dataset.providerLabel) {
                const provider = card.dataset.providerLabel;
                providerCounts.set(provider, (providerCounts.get(provider) || 0) + 1);
            }
        });

        const expectedTotal = parseBatchCount(summary.dataset.batchExpectedTotal);
        values.total_count = Math.max(values.total_count, expectedTotal);
        const previousWallDuration = parseBatchCount(summary.dataset.batchWallDurationMs);
        const nextWallDuration = parseBatchCount(wallDurationMs);
        const batchWallDuration = replaceWallDuration
            ? nextWallDuration
            : previousWallDuration + nextWallDuration;
        summary.dataset.batchWallDurationMs = String(batchWallDuration);
        updateBatchSummaryNode(summary, "total_count", "总链接", values.total_count);
        updateBatchSummaryNode(summary, "success_count", "成功", values.success_count);
        updateBatchSummaryNode(summary, "failed_count", "失败", values.failed_count);
        updateBatchSummaryNode(summary, "timeout_count", "超时", values.timeout_count);
        updateBatchSummaryNode(summary, "skipped_count", "跳过", values.skipped_count);
        updateBatchSummaryNode(summary, "video_count", "视频", values.video_count);
        updateBatchSummaryNode(summary, "image_count", "图片", values.image_count);
        updateBatchSummaryNode(summary, "total_duration_ms", "行耗时合计", values.total_duration_ms, "ms");
        updateBatchSummaryNode(summary, "batch_wall_duration_ms", "页面累计墙钟耗时", batchWallDuration, "ms");

        const providerSummary = summary.querySelector("[data-batch-provider-summary]");
        if (providerSummary) {
            providerSummary.replaceChildren();
            providerCounts.forEach((count, provider) => {
                const chip = document.createElement("span");
                chip.className = "batch-summary-chip";
                chip.textContent = `${provider} ${count}`;
                providerSummary.appendChild(chip);
            });
        }
    }

    function setBatchRetryLoading(button, loading) {
        if (!button) {
            return;
        }
        button.disabled = Boolean(loading);
        button.classList.toggle("is-loading", Boolean(loading));
        const label = button.querySelector("[data-batch-retry-label]");
        if (label) {
            label.textContent = loading ? "正在重新解析..." : "重新解析此链接";
        }
    }

    function replaceBatchResultCard(card, cardHtml) {
        const replacement = parseBatchResultCard(cardHtml);
        card.replaceWith(replacement);
        return replacement;
    }

    function prioritizeFirstBatchPreview(card) {
        if (!card) {
            return;
        }
        const image = card.querySelector(".batch-result-cover img");
        if (!image) {
            return;
        }
        image.setAttribute("loading", "eager");
        image.setAttribute("fetchpriority", "high");
    }

    function parseBatchResultCard(cardHtml) {
        const template = document.createElement("template");
        template.innerHTML = String(cardHtml || "").trim();
        const replacement = template.content.firstElementChild;
        if (!replacement || !replacement.classList.contains("batch-result-card")) {
            throw new Error("重试结果格式无效");
        }
        prioritizeFirstBatchPreview(replacement);
        return replacement;
    }

    function appendBatchResultCard(resultList, cardHtml) {
        const card = parseBatchResultCard(cardHtml);
        resultList.appendChild(card);
        return card;
    }

    function setBatchProgressStatus(batchPage, message, tone) {
        const status = batchPage.querySelector("[data-batch-progress-status]");
        if (!status) {
            return;
        }
        const results = batchPage.querySelector("[data-batch-results]");
        if (results && message) {
            results.hidden = false;
        }
        status.hidden = !message;
        status.classList.toggle("is-error", tone === "error");
        status.classList.toggle("is-complete", tone === "complete");
        status.textContent = message || "";
    }

    function resetBatchProgressResults(batchPage) {
        const results = batchPage.querySelector("[data-batch-results]");
        const resultList = batchPage.querySelector("[data-batch-result-list]");
        const summary = batchPage.querySelector("[data-batch-summary]");
        if (results) {
            results.hidden = false;
        }
        if (resultList) {
            resultList.replaceChildren();
        }
        if (summary) {
            summary.dataset.batchWallDurationMs = "0";
            summary.dataset.batchExpectedTotal = "0";
        }
        recomputeBatchSummary(batchPage, 0, true);
        applyActiveBatchFilter(batchPage);
    }

    async function fetchBatchJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            body: payload,
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        let result = {};
        try {
            result = await response.json();
        } catch (error) {
            result = {};
        }
        if (!response.ok || !result.ok) {
            throw new Error(result.message || `HTTP ${response.status}`);
        }
        return result;
    }

    async function runProgressiveBatchParse(batchPage, batchForm) {
        const batchStartUrl = batchPage.dataset.batchStartUrl || "";
        const batchItemUrl = batchPage.dataset.batchItemUrl || "";
        const resultList = batchPage.querySelector("[data-batch-result-list]");
        const csrfInput = batchForm.querySelector('input[name="csrfmiddlewaretoken"]');
        if (!batchStartUrl || !batchItemUrl || !resultList || !csrfInput || !window.fetch) {
            return false;
        }

        setBatchProgressStatus(batchPage, "正在准备逐条解析...", "progress");
        const batchStart = await fetchBatchJson(batchStartUrl, new FormData(batchForm));
        const totalCount = parseBatchCount(batchStart.total_count);
        const batchRunToken = String(batchStart.batch_run_token || "");
        if (!totalCount || !batchRunToken) {
            throw new Error("批量解析任务格式无效，请重新提交。");
        }

        resetBatchProgressResults(batchPage);
        const summary = batchPage.querySelector("[data-batch-summary]");
        if (summary) {
            summary.dataset.batchExpectedTotal = String(totalCount);
        }
        recomputeBatchSummary(batchPage, 0, true);
        const batchStartedAt = window.performance.now();

        for (let index = 0; index < totalCount; index += 1) {
            setBatchProgressStatus(
                batchPage,
                `已完成 ${index}/${totalCount}，正在解析第 ${index + 1} 条...`,
                "progress"
            );
            const itemPayload = new FormData();
            itemPayload.set("csrfmiddlewaretoken", csrfInput.value || "");
            itemPayload.set("batch_run_token", batchRunToken);
            itemPayload.set("index", String(index));
            const itemResult = await fetchBatchJson(batchItemUrl, itemPayload);
            appendBatchResultCard(resultList, itemResult.card_html);
            recomputeBatchSummary(
                batchPage,
                Math.round(window.performance.now() - batchStartedAt),
                true
            );
            applyActiveBatchFilter(batchPage);
        }

        setBatchProgressStatus(
            batchPage,
            `解析完成，共返回 ${totalCount} 条结果。`,
            "complete"
        );
        return true;
    }

    function showBatchRetryError(card, message) {
        const alert = card.querySelector("[data-batch-row-message]")
            || card.querySelector("[data-batch-retry-request-error]");
        if (alert) {
            alert.classList.remove("d-none");
            alert.classList.remove("alert-warning");
            alert.classList.add("alert-danger");
            alert.textContent = message || "重新解析请求失败，请稍后重试。";
        }
    }

    function effectiveCheckboxValue(input) {
        return Boolean(input && input.checked && !input.disabled);
    }

    async function retryBatchResultCard(
        batchPage,
        batchForm,
        retryButton,
        ultraInput,
        youtubeInput,
        onIdle
    ) {
        const card = retryButton.closest(".batch-result-card");
        const retryUrl = batchPage.dataset.batchRetryUrl || "";
        const csrfInput = batchForm ? batchForm.querySelector('input[name="csrfmiddlewaretoken"]') : null;
        if (!card || !retryUrl || !csrfInput) {
            return;
        }
        const payload = new FormData();
        payload.set("csrfmiddlewaretoken", csrfInput.value || "");
        payload.set("link", retryButton.getAttribute("data-retry-link") || "");
        payload.set("ultra_hd", effectiveCheckboxValue(ultraInput) ? "1" : "0");
        payload.set("youtube_merge", effectiveCheckboxValue(youtubeInput) ? "1" : "0");
        setBatchRetryLoading(retryButton, true);
        try {
            const response = await fetch(retryUrl, {
                method: "POST",
                body: payload,
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.message || `HTTP ${response.status}`);
            }
            replaceBatchResultCard(card, result.card_html);
            recomputeBatchSummary(batchPage, result.retry_wall_duration_ms);
            applyActiveBatchFilter(batchPage);
        } catch (error) {
            showBatchRetryError(card, error.message);
            recomputeBatchSummary(batchPage, 0);
            applyActiveBatchFilter(batchPage);
        } finally {
            if (retryButton.isConnected) {
                setBatchRetryLoading(retryButton, false);
            }
            if (typeof onIdle === "function") {
                onIdle();
            }
        }
    }

    function initBatchParsePage() {
        const batchPage = document.querySelector(".batch-parse-page");
        if (!batchPage) {
            return;
        }
        const downloadReliabilityApi = window.AnimeImagesDownloadReliability || null;
        const batchDownloadRouteUrl = batchPage.dataset.batchDownloadRouteUrl || "";
        const batchDownloadPrepareUrl = batchPage.dataset.batchDownloadPrepareUrl || "";
        const batchForm = batchPage.querySelector("form.batch-form");
        const batchTextarea = batchPage.querySelector("#batch-links");
        const submitButton = batchPage.querySelector(".batch-submit-btn");
        const submitLabel = batchPage.querySelector(".batch-submit-label");
        const ultraInput = batchPage.querySelector('input[name="ultra_hd"][type="checkbox"]');
        const youtubeInput = batchPage.querySelector('input[name="youtube_merge"][type="checkbox"]');
        const ultraHiddenInput = batchPage.querySelector("#batch-ultra-hd-request-input");
        const youtubeHiddenInput = batchPage.querySelector("#batch-youtube-merge-request-input");
        const settingsModal = batchPage.querySelector("#batchSettingsModal");
        const saveSettingsButton = batchPage.querySelector("#batch-save-settings-btn");
        const parsePreferencesApi = window.AnimeImagesParsePreferences || null;
        const renderedParseDefaults = {
            ultra_hd: effectiveCheckboxValue(ultraInput),
            youtube_merge: effectiveCheckboxValue(youtubeInput),
        };
        let parsePreferences = parsePreferencesApi
            ? parsePreferencesApi.load({defaults: renderedParseDefaults})
            : {version: 1, ...renderedParseDefaults};
        let pendingParsePreferences = null;
        let settingsModalOpen = false;
        let settingsSaveCommitted = false;

        prioritizeFirstBatchPreview(
            batchPage.querySelector(".batch-result-card:not(.is-hidden)")
        );

        function readDraft() {
            if (parsePreferencesApi) {
                return parsePreferencesApi.loadBatchDraft({
                    defaultLinks: batchTextarea ? batchTextarea.value : "",
                });
            }
            try {
                const draft = JSON.parse(
                    window.localStorage.getItem("animeimages.batchParseDraft.v2") || "{}"
                );
                return draft && draft.version === 2 && typeof draft.links === "string"
                    ? {version: 2, links: draft.links}
                    : {version: 2, links: ""};
            } catch (error) {
                return {version: 2, links: ""};
            }
        }

        function writeDraft() {
            const links = batchTextarea ? batchTextarea.value : "";
            if (parsePreferencesApi) {
                parsePreferencesApi.saveBatchDraft(links);
                return;
            }
            try {
                window.localStorage.setItem("animeimages.batchParseDraft.v2", JSON.stringify({
                    version: 2,
                    links,
                }));
            } catch (error) {
                // localStorage may be disabled; batch parsing still works without draft persistence.
            }
        }

        function restoreDraft() {
            const draft = readDraft();
            if (batchTextarea && !batchTextarea.value && draft.links) {
                batchTextarea.value = draft.links;
            }
        }

        function applyParsePreferences(preferences) {
            parsePreferences = preferences;
            if (parsePreferencesApi) {
                parsePreferencesApi.applyToControls(parsePreferences, {
                    ultraHdInput: ultraInput,
                    ultraHdHiddenInput: ultraHiddenInput,
                    youtubeMergeInput: youtubeInput,
                    youtubeMergeHiddenInput: youtubeHiddenInput,
                });
                return;
            }
            if (ultraHiddenInput) {
                ultraHiddenInput.value = effectiveCheckboxValue(ultraInput) ? "1" : "0";
            }
            if (youtubeHiddenInput) {
                youtubeHiddenInput.value = effectiveCheckboxValue(youtubeInput) ? "1" : "0";
            }
        }

        function saveBatchParsePreferences() {
            pendingParsePreferences = null;
            applyParsePreferences(commitEnabledParsePreferences(
                parsePreferencesApi,
                parsePreferences,
                ultraInput,
                youtubeInput,
                renderedParseDefaults
            ));
            settingsSaveCommitted = true;
        }

        function batchOperationInProgress() {
            const retryInProgress = Boolean(batchPage.querySelector(".batch-retry-btn.is-loading"));
            const batchInProgress = Boolean(
                batchForm && batchForm.dataset.submitting === "true"
            );
            return batchInProgress || retryInProgress;
        }

        function applyPendingParsePreferences() {
            if (pendingParsePreferences && !batchOperationInProgress()) {
                const pending = pendingParsePreferences;
                pendingParsePreferences = null;
                if (settingsModalOpen) {
                    parsePreferences = pending;
                } else {
                    applyParsePreferences(pending);
                }
            }
        }

        applyParsePreferences(parsePreferences);
        restoreDraft();

        if (parsePreferencesApi) {
            parsePreferencesApi.subscribe((preferences) => {
                if (batchOperationInProgress()) {
                    pendingParsePreferences = preferences;
                } else if (settingsModalOpen) {
                    parsePreferences = preferences;
                } else {
                    applyParsePreferences(preferences);
                }
            });
        }

        if (batchTextarea) {
            batchTextarea.addEventListener("input", writeDraft);
        }

        if (settingsModal) {
            settingsModal.addEventListener("show.bs.modal", () => {
                settingsModalOpen = true;
                settingsSaveCommitted = false;
                applyPendingParsePreferences();
                applyParsePreferences(parsePreferences);
            });
            settingsModal.addEventListener("hidden.bs.modal", () => {
                settingsModalOpen = false;
                if (!settingsSaveCommitted) {
                    applyPendingParsePreferences();
                }
                applyParsePreferences(parsePreferences);
                settingsSaveCommitted = false;
                applyPendingParsePreferences();
            });
        }

        if (saveSettingsButton) {
            saveSettingsButton.addEventListener("click", saveBatchParsePreferences);
        }

        if (batchForm && submitButton) {
            batchForm.addEventListener("submit", async (event) => {
                if (!batchForm.checkValidity()) {
                    if (typeof batchForm.reportValidity === "function") {
                        batchForm.reportValidity();
                    }
                    return;
                }
                if (batchForm.dataset.submitting === "true") {
                    event.preventDefault();
                    return;
                }
                const supportsProgressiveBatch = Boolean(
                    batchPage.dataset.batchStartUrl
                    && batchPage.dataset.batchItemUrl
                    && window.fetch
                );
                if (!supportsProgressiveBatch) {
                    writeDraft();
                    submitButton.disabled = true;
                    submitButton.classList.add("is-loading");
                    if (submitLabel) {
                        submitLabel.textContent = "正在批量解析...";
                    }
                    return;
                }
                event.preventDefault();
                batchForm.dataset.submitting = "true";
                writeDraft();
                submitButton.disabled = true;
                submitButton.classList.add("is-loading");
                if (submitLabel) {
                    submitLabel.textContent = "正在逐条解析...";
                }
                try {
                    await runProgressiveBatchParse(batchPage, batchForm);
                } catch (error) {
                    setBatchProgressStatus(
                        batchPage,
                        error.message || "批量解析请求失败，已完成的结果会保留。",
                        "error"
                    );
                } finally {
                    batchForm.dataset.submitting = "false";
                    submitButton.disabled = false;
                    submitButton.classList.remove("is-loading");
                    if (submitLabel) {
                        submitLabel.textContent = "开始批量解析";
                    }
                    applyPendingParsePreferences();
                }
            });
        }

        batchPage.addEventListener("click", async (event) => {
            const copyButton = event.target.closest("[data-copy-text]");
            if (copyButton) {
                await copyText(copyButton.getAttribute("data-copy-text") || "");
                flashButton(copyButton, "已复制");
                return;
            }

            const retryButton = event.target.closest("[data-retry-link]");
            if (retryButton) {
                event.preventDefault();
                if (!retryButton.disabled) {
                    await retryBatchResultCard(
                        batchPage,
                        batchForm,
                        retryButton,
                        ultraInput,
                        youtubeInput,
                        applyPendingParsePreferences
                    );
                }
                return;
            }

            const exportButton = event.target.closest("[data-batch-export]");
            if (exportButton) {
                const links = collectBatchVideoLinks();
                const mode = exportButton.getAttribute("data-batch-export");
                if (!links.length) {
                    flashButton(exportButton, "无直链");
                    return;
                }
                if (mode === "copy-links") {
                    await copyText(links.map((item) => item.url).join("\n"));
                    flashButton(exportButton, "已复制");
                    return;
                }
                if (mode === "aria2") {
                    downloadTextFile(
                        "aria2.txt",
                        links.map((item) => `${item.url}\n  out=${item.filename}`).join("\n")
                    );
                    return;
                }
                downloadTextFile("links.txt", links.map((item) => item.url).join("\n"));
                return;
            }

            const downloadButton = event.target.closest("[data-batch-download]");
            if (downloadButton) {
                event.preventDefault();
                if (downloadButton.disabled) {
                    return;
                }
                const itemNode = downloadButton.closest("[data-batch-download-item]");
                if (
                    !itemNode
                    || !batchDownloadRouteUrl
                    || !batchDownloadPrepareUrl
                    || !downloadReliabilityApi
                ) {
                    showBatchDownloadButtonMessage(downloadButton, "下载不可用");
                    return;
                }
                const payload = buildBatchDownloadPayload(itemNode);
                setBatchDownloadButtonLoading(downloadButton, true, "准备下载...");
                updateBatchDownloadProgress(downloadButton, null, "准备下载...");
                try {
                    const outcome = await executeBatchDownload({
                        reliabilityApi: downloadReliabilityApi,
                        fetchRoute: () => postBatchDownloadJson(
                            batchDownloadRouteUrl,
                            {...payload, route_only: true}
                        ),
                        prepare: () => prepareBatchServerDownload(
                            batchDownloadPrepareUrl,
                            payload,
                            downloadReliabilityApi
                        ),
                        handoffDirect: (items) => {
                            if (batchDirectRouteUsesBlob(
                                items,
                                downloadReliabilityApi
                            )) {
                                return downloadBatchDirectItems(
                                    items,
                                    itemNode,
                                    (percent, label) => {
                                        if (typeof percent === "number") {
                                            updateBatchDownloadProgress(
                                                downloadButton,
                                                percent,
                                                `${percent}%`
                                            );
                                        } else {
                                            updateBatchDownloadProgress(
                                                downloadButton,
                                                null,
                                                label || "正在接收文件..."
                                            );
                                        }
                                    }
                                ).then(() => ({
                                    completed: true,
                                    mode: "fetch_blob",
                                }));
                            }
                            if (!batchDirectRouteUsesNativeHandoff(
                                items,
                                downloadReliabilityApi
                            )) {
                                throw new Error(
                                    "当前资源没有经过验证的浏览器直连能力。"
                                );
                            }
                            handoffBatchDirectItems(items, itemNode);
                            return {
                                completed: false,
                                mode: "native_handoff",
                            };
                        },
                        fallbackDirectToPrepared: true,
                        handoffPrepared: (prepared) => {
                            handoffPreparedBatchDownload(prepared);
                        },
                        pollStatus: (prepared) => downloadReliabilityApi.pollDownloadStatus({
                            statusUrl: sameOriginBatchHandoffPath(prepared.statusUrl),
                            pollAfterMs: prepared.pollAfterMs,
                        }),
                        downloadServer: () => downloadBatchServerResponse(
                            batchDownloadRouteUrl,
                            payload,
                            itemNode,
                            (percent, label) => {
                                if (typeof percent === "number") {
                                    updateBatchDownloadProgress(
                                        downloadButton,
                                        percent,
                                        `${percent}%`
                                    );
                                } else {
                                    updateBatchDownloadProgress(
                                        downloadButton,
                                        null,
                                        label || "正在接收文件..."
                                    );
                                }
                            }
                        ),
                        onPhase: (phase) => {
                            const labels = {
                                routing: "正在确认下载路径...",
                                preparing: "正在签发安全下载通道...",
                                connecting: "正在连接资源...",
                                direct_handoff: "正在交给浏览器...",
                                direct_fallback: "正在切换安全下载通道...",
                                server_download: "正在接收文件...",
                            };
                            const label = labels[phase] || "准备下载...";
                            setBatchDownloadButtonLoading(downloadButton, true, label);
                            updateBatchDownloadProgress(downloadButton, null, label);
                        },
                    });
                    setBatchDownloadButtonLoading(downloadButton, false);
                    if (outcome.kind === "direct" && !outcome.completed) {
                        updateBatchDownloadProgress(
                            downloadButton,
                            null,
                            "已交给浏览器下载管理器；请查看下载栏"
                        );
                        showBatchDownloadButtonMessage(downloadButton, "已交接");
                    } else if (outcome.unconfirmed) {
                        updateBatchDownloadProgress(
                            downloadButton,
                            null,
                            outcome.statusResult.message
                        );
                        showBatchDownloadButtonMessage(downloadButton, "仍在连接");
                    } else {
                        updateBatchDownloadProgress(
                            downloadButton,
                            100,
                            "浏览器已接管"
                        );
                        showBatchDownloadButtonMessage(downloadButton, "浏览器已接管");
                    }
                } catch (error) {
                    updateBatchDownloadProgress(
                        downloadButton,
                        0,
                        error && error.message
                            ? error.message
                            : "下载失败，请稍后重试。"
                    );
                    setBatchDownloadButtonLoading(downloadButton, false);
                    showBatchDownloadButtonMessage(downloadButton, "重新下载");
                }
                return;
            }

            const filterButton = event.target.closest("[data-batch-filter]");
            if (filterButton) {
                batchPage.querySelectorAll("[data-batch-filter]").forEach((button) => {
                    button.classList.toggle("is-active", button === filterButton);
                });
                applyActiveBatchFilter(batchPage);
            }
        });
    }

    const testApi = Object.freeze({
        batchDirectRouteUsesBlob,
        batchDirectRouteUsesNativeHandoff,
        batchDownloadRouteDisposition,
        buildBatchDownloadItem,
        buildBatchDownloadPayload,
        buildEnabledPreferencePatch,
        commitEnabledParsePreferences,
        executeBatchDownload,
        validatedDirectRouteItems,
    });
    if (typeof module === "object" && module.exports) {
        module.exports = testApi;
    }
    if (typeof document === "undefined") {
        return;
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initBatchParsePage);
    } else {
        initBatchParsePage();
    }
}());
