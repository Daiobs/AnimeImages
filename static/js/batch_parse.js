// Batch parse page behavior. Results stay in the current DOM and are never persisted.

(function () {
    "use strict";

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

    function handoffBatchDirectUrl(url, filename) {
        const link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.download = sanitizeFilename(filename);
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        link.remove();
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

    async function downloadBatchDirectUrl(url, filename, onStatus, onProgress) {
        if (onStatus) {
            onStatus("正在连接资源...");
        }
        const response = await fetch(url, {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.toLowerCase().includes("text/html")) {
            throw new Error("上游返回了网页而不是媒体文件");
        }
        if (onStatus) {
            onStatus("下载通道已建立...");
        }
        const blob = await readBlobWithProgress(response, onProgress);
        if (onStatus) {
            onStatus("正在唤起浏览器下载...");
        }
        saveBlob(filename, blob);
    }

    function setBatchDownloadButtonLoading(button, loading, label) {
        if (!button) {
            return;
        }
        const labelNode = button.querySelector(".batch-download-label");
        if (!button.dataset.originalLabel && labelNode) {
            button.dataset.originalLabel = labelNode.textContent || "浏览器下载";
        }
        button.disabled = Boolean(loading);
        button.classList.toggle("is-loading", Boolean(loading));
        if (labelNode) {
            labelNode.textContent = loading
                ? (label || "下载中...")
                : (button.dataset.originalLabel || "浏览器下载");
        }
    }

    function showBatchDownloadButtonMessage(button, message) {
        const labelNode = button ? button.querySelector(".batch-download-label") : null;
        if (!labelNode) {
            return;
        }
        const original = button.dataset.originalLabel || labelNode.textContent || "浏览器下载";
        labelNode.textContent = message;
        window.setTimeout(() => {
            if (!button.classList.contains("is-loading")) {
                labelNode.textContent = original;
            }
        }, 1300);
    }

    function getBatchDownloadProgressElements(button) {
        const item = button ? button.closest(".batch-direct-item") : null;
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

    function resetBatchDownloadProgress(button, label) {
        const elements = getBatchDownloadProgressElements(button);
        if (!elements.progress) {
            return;
        }
        elements.progress.classList.remove("is-indeterminate");
        if (elements.bar) {
            elements.bar.style.width = "0%";
        }
        if (elements.label) {
            elements.label.textContent = label || "准备下载...";
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

    async function retryBatchResultCard(batchPage, batchForm, retryButton, ultraInput, youtubeInput) {
        const card = retryButton.closest(".batch-result-card");
        const retryUrl = batchPage.dataset.batchRetryUrl || "";
        const csrfInput = batchForm ? batchForm.querySelector('input[name="csrfmiddlewaretoken"]') : null;
        if (!card || !retryUrl || !csrfInput) {
            return;
        }
        const payload = new FormData();
        payload.set("csrfmiddlewaretoken", csrfInput.value || "");
        payload.set("link", retryButton.getAttribute("data-retry-link") || "");
        payload.set("ultra_hd", ultraInput && ultraInput.checked ? "1" : "0");
        payload.set("youtube_merge", youtubeInput && youtubeInput.checked ? "1" : "0");
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
        }
    }

    function initBatchParsePage() {
        const batchPage = document.querySelector(".batch-parse-page");
        if (!batchPage) {
            return;
        }
        const batchForm = batchPage.querySelector("form.batch-form");
        const batchTextarea = batchPage.querySelector("#batch-links");
        const submitButton = batchPage.querySelector(".batch-submit-btn");
        const submitLabel = batchPage.querySelector(".batch-submit-label");
        const ultraInput = batchPage.querySelector('input[name="ultra_hd"][type="checkbox"]');
        const youtubeInput = batchPage.querySelector('input[name="youtube_merge"][type="checkbox"]');
        const draftKey = "animeimages.batchParseDraft.v1";

        prioritizeFirstBatchPreview(
            batchPage.querySelector(".batch-result-card:not(.is-hidden)")
        );

        function readDraft() {
            try {
                return JSON.parse(window.localStorage.getItem(draftKey) || "{}");
            } catch (error) {
                return {};
            }
        }

        function writeDraft() {
            try {
                window.localStorage.setItem(draftKey, JSON.stringify({
                    links: batchTextarea ? batchTextarea.value : "",
                    ultra_hd: ultraInput ? ultraInput.checked : false,
                    youtube_merge: youtubeInput ? youtubeInput.checked : false,
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
            if (batchTextarea && batchTextarea.value) {
                if (ultraInput && !ultraInput.disabled && typeof draft.ultra_hd === "boolean") {
                    ultraInput.checked = draft.ultra_hd;
                }
                if (youtubeInput && !youtubeInput.disabled && typeof draft.youtube_merge === "boolean") {
                    youtubeInput.checked = draft.youtube_merge;
                }
            }
        }

        restoreDraft();

        if (batchTextarea) {
            batchTextarea.addEventListener("input", writeDraft);
        }
        [ultraInput, youtubeInput].forEach((input) => {
            if (input) {
                input.addEventListener("change", writeDraft);
            }
        });

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
                        youtubeInput
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

            const directDownloadButton = event.target.closest("[data-batch-direct-download]");
            if (directDownloadButton) {
                event.preventDefault();
                if (directDownloadButton.disabled) {
                    return;
                }
                const url = directDownloadButton.getAttribute("data-batch-direct-url") || "";
                const filename = directDownloadButton.getAttribute("data-batch-direct-filename") || "video.mp4";
                if (!url) {
                    showBatchDownloadButtonMessage(directDownloadButton, "无直链");
                    return;
                }
                setBatchDownloadButtonLoading(directDownloadButton, true, "正在连接资源...");
                updateBatchDownloadProgress(directDownloadButton, 0, "正在连接资源...");
                try {
                    await downloadBatchDirectUrl(url, filename, (label) => {
                        setBatchDownloadButtonLoading(directDownloadButton, true, label);
                        updateBatchDownloadProgress(directDownloadButton, null, label);
                    }, (percent, label) => {
                        if (typeof percent === "number") {
                            updateBatchDownloadProgress(directDownloadButton, percent, `${percent}%`);
                            return;
                        }
                        updateBatchDownloadProgress(directDownloadButton, null, label || "正在下载...");
                    });
                    updateBatchDownloadProgress(directDownloadButton, 100, "100%");
                    setBatchDownloadButtonLoading(directDownloadButton, false);
                    showBatchDownloadButtonMessage(directDownloadButton, "已交给浏览器");
                } catch (error) {
                    console.warn("Batch direct browser download failed", error);
                    updateBatchDownloadProgress(
                        directDownloadButton,
                        null,
                        "Blob 不可用，正在改用直链..."
                    );
                    handoffBatchDirectUrl(url, filename);
                    resetBatchDownloadProgress(directDownloadButton, "已交给浏览器处理");
                    setBatchDownloadButtonLoading(directDownloadButton, false);
                    showBatchDownloadButtonMessage(directDownloadButton, "已改用直链");
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

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initBatchParsePage);
    } else {
        initBatchParsePage();
    }
}());
