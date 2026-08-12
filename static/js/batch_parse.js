// Batch parse page behavior. Results stay in the current DOM and are never persisted.

(function () {
    "use strict";

    const ordinaryMediaDownloadApi = typeof window !== "undefined"
        ? window.AnimeImagesOrdinaryMediaDownload || null
        : null;

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

    function getBatchDownloadProgress(button) {
        const item = button ? button.closest(".batch-direct-item") : null;
        const wrap = item ? item.querySelector("[data-batch-download-progress]") : null;
        return {
            wrap,
            track: wrap ? wrap.querySelector("[role='progressbar']") : null,
            bar: wrap ? wrap.querySelector(".download-progress-bar") : null,
            percent: wrap ? wrap.querySelector("[data-download-progress-percent]") : null,
            bytes: wrap ? wrap.querySelector("[data-download-progress-bytes]") : null,
            speed: wrap ? wrap.querySelector("[data-download-progress-speed]") : null,
            eta: wrap ? wrap.querySelector("[data-download-progress-eta]") : null,
            blobWarning: wrap ? wrap.querySelector("[data-blob-download-warning]") : null,
        };
    }

    function resetBatchDownloadProgress(button) {
        const progress = getBatchDownloadProgress(button);
        if (progress.wrap) {
            progress.wrap.hidden = false;
            progress.wrap.classList.remove("is-pending");
        }
        if (progress.track) {
            progress.track.setAttribute("aria-valuenow", "0");
            progress.track.setAttribute("aria-valuetext", "0%");
        }
        if (progress.bar) {
            progress.bar.style.width = "0%";
        }
        if (progress.percent) {
            progress.percent.textContent = "0%";
        }
        if (progress.bytes) {
            progress.bytes.textContent = "0 B";
        }
        if (progress.speed) {
            progress.speed.textContent = "正在测速...";
        }
        if (progress.eta) {
            progress.eta.textContent = "";
        }
        if (progress.blobWarning) {
            progress.blobWarning.hidden = true;
        }
    }

    function applyBatchDownloadProgress(button, event) {
        if (!ordinaryMediaDownloadApi || !event) {
            return;
        }
        const progress = getBatchDownloadProgress(button);
        const knownTotal = Number(event.totalBytes || 0) > 0;
        const completed = event.stage === "saving" || event.stage === "completed";
        const percentValue = completed
            ? 100
            : (knownTotal && event.percent !== null ? Math.max(0, Math.min(100, event.percent)) : null);
        if (progress.wrap) {
            progress.wrap.hidden = false;
            progress.wrap.classList.toggle("is-pending", percentValue === null);
        }
        if (progress.track) {
            if (percentValue === null) {
                progress.track.removeAttribute("aria-valuenow");
                progress.track.setAttribute("aria-valuetext", "正在下载，文件大小未知");
            } else {
                progress.track.setAttribute("aria-valuenow", String(Math.round(percentValue)));
                progress.track.setAttribute("aria-valuetext", `${Math.round(percentValue)}%`);
            }
        }
        if (progress.bar) {
            progress.bar.style.width = percentValue === null ? "100%" : `${percentValue}%`;
        }
        if (progress.percent) {
            progress.percent.textContent = percentValue === null
                ? "下载中"
                : `${Math.round(percentValue)}%`;
        }
        if (progress.bytes) {
            const received = ordinaryMediaDownloadApi.formatBytes(event.receivedBytes);
            progress.bytes.textContent = knownTotal
                ? `${received} / ${ordinaryMediaDownloadApi.formatBytes(event.totalBytes)}`
                : `已接收 ${received}`;
        }
        if (progress.speed) {
            progress.speed.textContent = ordinaryMediaDownloadApi.formatRate(event.bytesPerSecond);
        }
        if (progress.eta) {
            progress.eta.textContent = event.etaSeconds
                ? ordinaryMediaDownloadApi.formatEta(event.etaSeconds)
                : (event.stage === "fetching" ? "正在测速..." : "");
        }
        if (progress.blobWarning) {
            progress.blobWarning.hidden = event.transport !== "browser_blob";
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
        const activeDownloadsPanel = batchPage.querySelector("[data-ordinary-active-downloads]");
        if (ordinaryMediaDownloadApi && activeDownloadsPanel) {
            ordinaryMediaDownloadApi.mountActiveDownloadPanel(activeDownloadsPanel).catch(() => {
                activeDownloadsPanel.hidden = true;
            });
        }
        if (ordinaryMediaDownloadApi) {
            const interrupted = ordinaryMediaDownloadApi.consumeInterruptedDownloadMarker();
            const interruptionNotice = batchPage.querySelector("[data-ordinary-download-interruption]");
            if (interrupted && interruptionNotice) {
                interruptionNotice.hidden = false;
            }
        }

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
                const item = {
                    type: "video",
                    url,
                    filename,
                    provider: directDownloadButton.getAttribute("data-batch-direct-provider") || "",
                    size_bytes: directDownloadButton.getAttribute("data-batch-direct-size-bytes") || "0",
                    size_is_estimate: (
                        directDownloadButton.getAttribute("data-batch-direct-size-is-estimate") === "true"
                    ),
                    browser_direct_fetch_safe: (
                        directDownloadButton.getAttribute("data-batch-browser-fetch-safe") === "true"
                    ),
                    requires_merge: (
                        directDownloadButton.getAttribute("data-batch-requires-merge") === "true"
                    ),
                    requires_cookie: (
                        directDownloadButton.getAttribute("data-batch-requires-cookie") === "true"
                    ),
                };
                if (
                    !ordinaryMediaDownloadApi
                    || !ordinaryMediaDownloadApi.isOrdinaryMediaItem(item)
                ) {
                    showBatchDownloadButtonMessage(directDownloadButton, "请重新解析");
                    return;
                }
                setBatchDownloadButtonLoading(directDownloadButton, true, "正在准备下载...");
                resetBatchDownloadProgress(directDownloadButton);
                try {
                    await ordinaryMediaDownloadApi.download(item, {
                        filename,
                        onStage(_stage, message) {
                            setBatchDownloadButtonLoading(directDownloadButton, true, message);
                        },
                        onProgress(progressEvent) {
                            applyBatchDownloadProgress(directDownloadButton, progressEvent);
                        },
                    });
                    setBatchDownloadButtonLoading(directDownloadButton, false);
                    showBatchDownloadButtonMessage(directDownloadButton, "已交给浏览器保存");
                } catch (error) {
                    console.warn(
                        "Batch ordinary browser download failed",
                        String(error && error.errorCode || "BROWSER_DOWNLOAD_FAILED"),
                    );
                    setBatchDownloadButtonLoading(directDownloadButton, false);
                    showBatchDownloadButtonMessage(directDownloadButton, "重试下载");
                    directDownloadButton.title = ordinaryMediaDownloadApi.safeErrorMessage(error);
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
        buildEnabledPreferencePatch,
        commitEnabledParsePreferences,
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
