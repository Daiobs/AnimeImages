// Batch parse page behavior. Results stay in the current DOM and are never persisted.

(function () {
    "use strict";

    const ordinaryMediaDownloadApi = typeof window !== "undefined"
        ? window.AnimeImagesOrdinaryMediaDownload || null
        : null;
    const browserPhotoApi = typeof window !== "undefined"
        ? window.AnimeImagesBrowserPhoto || null
        : null;
    const BATCH_SYNC_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024;
    const batchPhotoControllers = new WeakMap();

    function showBatchInternalDiagnostics(node) {
        const page = node && typeof node.closest === "function"
            ? node.closest(".batch-parse-page")
            : (
                typeof document !== "undefined"
                    ? document.querySelector(".batch-parse-page")
                    : null
            );
        return Boolean(page && page.dataset.showInternalDiagnostics === "true");
    }

    function batchPresentationText(node, publicText, diagnosticText) {
        return showBatchInternalDiagnostics(node) ? diagnosticText : publicText;
    }

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
            try {
                await navigator.clipboard.writeText(value);
                return;
            } catch (error) {
                // Browser permission failures can still use the local selection fallback.
            }
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

    function collectBatchResourceLinks(root) {
        const scope = root || document;
        const resources = [];
        const seenUrls = new Set();
        Array.from(scope.querySelectorAll(".batch-result-card")).forEach((card) => {
            if (normalizeBatchValue(card.dataset.status) !== "success") {
                return;
            }
            const nodes = Array.from(card.querySelectorAll("[data-batch-download-item]"));
            nodes.sort((left, right) => {
                const rank = (node) => normalizeBatchValue(node.dataset.type) === "video" ? 0 : 1;
                return rank(left) - rank(right);
            });
            nodes.forEach((node) => {
                const type = normalizeBatchValue(node.dataset.type);
                const url = String(node.dataset.url || "").trim();
                if (!url || seenUrls.has(url) || (type !== "video" && type !== "image")) {
                    return;
                }
                seenUrls.add(url);
                const filenameStem = String(
                    node.dataset.downloadFilenameStem
                    || (type === "video" ? "video" : "image")
                );
                const extensionHint = String(node.dataset.downloadExtensionHint || "");
                resources.push({
                    type,
                    url,
                    filename: String(
                        node.dataset.downloadFilename
                        || (extensionHint ? `${filenameStem}.${extensionHint}` : filenameStem)
                    ),
                });
            });
        });
        return resources;
    }

    function buildBatchLinksText(resources) {
        return (resources || []).map((item) => item.url).join("\n");
    }

    function sanitizeBatchResourceFilename(filename) {
        if (ordinaryMediaDownloadApi && ordinaryMediaDownloadApi.sanitizeFilename) {
            return ordinaryMediaDownloadApi.sanitizeFilename(filename);
        }
        const value = String(filename || "download")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, " ")
            .trim();
        return value || "download";
    }

    function buildBatchAria2Text(resources, sanitizeFilename) {
        const sanitizer = typeof sanitizeFilename === "function"
            ? sanitizeFilename
            : sanitizeBatchResourceFilename;
        return (resources || [])
            .map((item) => `${item.url}\n  out=${sanitizer(item.filename)}`)
            .join("\n");
    }

    function batchBrowserPhotoItem(node) {
        if (!node || !node.dataset) {
            return null;
        }
        const url = String(node.dataset.url || "").trim();
        if (!url || normalizeBatchValue(node.dataset.type) !== "image") {
            return null;
        }
        return {
            type: "image",
            url,
            filenameStem: String(node.dataset.downloadFilenameStem || "image"),
            extensionHint: String(node.dataset.downloadExtensionHint || ""),
            requires_cookie: node.dataset.requiresCookie === "true",
            requires_merge: node.dataset.requiresMerge === "true",
        };
    }

    async function downloadBatchImage(button, options) {
        const item = button ? button.closest("[data-batch-download-item]") : null;
        const photoItem = batchBrowserPhotoItem(item);
        if (!browserPhotoApi || !photoItem) {
            throw new Error("图片下载地址不可用，请重新解析。");
        }
        return browserPhotoApi.downloadImage(photoItem, options || {});
    }

    function batchPhotoMediaItem(node) {
        if (!node || !node.dataset) {
            return null;
        }
        const url = String(node.dataset.url || "").trim();
        if (!url || normalizeBatchValue(node.dataset.type) !== "image") {
            return null;
        }
        const item = {
            type: "image",
            url,
        };
        const fields = [
            ["sizeBytes", "size_bytes"],
            ["assetId", "asset_id"],
            ["provider", "provider"],
            ["providerResourceId", "provider_resource_id"],
            ["providerCacheKey", "provider_cache_key"],
            ["providerManifestToken", "provider_manifest_token"],
        ];
        fields.forEach(([datasetKey, payloadKey]) => {
            const value = String(node.dataset[datasetKey] || "").trim();
            if (value) {
                item[payloadKey] = value;
            }
        });
        return item;
    }

    function buildBatchPhotoArchivePayload(section) {
        const mediaItems = section
            ? Array.from(section.querySelectorAll("[data-batch-photo-item]"))
                .map(batchPhotoMediaItem)
                .filter(Boolean)
            : [];
        return {
            media_items: mediaItems,
            base_filename: String(
                section && section.dataset ? section.dataset.baseFilename || "images" : "images"
            ),
            download_mode: "zip",
            save_format: "original",
            quality: 95,
            video_save_mode: "video",
            archive_profile: "batch_images",
        };
    }

    function buildBatchBrowserPhotoItems(section) {
        return section
            ? Array.from(section.querySelectorAll("[data-batch-photo-item]"))
                .map(batchBrowserPhotoItem)
                .filter(Boolean)
            : [];
    }

    function safeSameOriginDownloadUrl(value, origin) {
        const parsed = new URL(String(value || ""), origin);
        if (parsed.origin !== origin) {
            throw new Error("下载文件地址无效，请重新打包。");
        }
        return parsed.href;
    }

    function parseBatchArchiveFilename(response, fallback) {
        const disposition = String(response.headers.get("Content-Disposition") || "");
        const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        if (encoded) {
            try {
                return decodeURIComponent(encoded[1]);
            } catch (error) {
                // Fall through to the safe local filename.
            }
        }
        const basic = disposition.match(/filename="?([^";]+)"?/i);
        return basic ? basic[1] : fallback;
    }

    async function parseBatchArchiveError(response) {
        try {
            const payload = await response.json();
            return String(payload.message || "图片 ZIP 下载失败，请稍后重试。");
        } catch (error) {
            return `图片 ZIP 下载失败（HTTP ${response.status}）。`;
        }
    }

    function assertBatchSyncArchiveSize(sizeBytes) {
        const size = Number(sizeBytes || 0);
        if (Number.isFinite(size) && size > BATCH_SYNC_ARCHIVE_MAX_BYTES) {
            throw new Error("图片 ZIP 过大，无法在当前浏览器安全接收。请分批下载图片。");
        }
    }

    function setBatchPhotoArchiveState(section, state, options) {
        const config = options || {};
        const button = section ? section.querySelector("[data-batch-photo-download-all]") : null;
        const label = button ? button.querySelector("[data-batch-photo-archive-label]") : null;
        const status = section ? section.querySelector("[data-batch-photo-archive-status]") : null;
        const progressWrap = section ? section.querySelector("[data-batch-photo-archive-progress]") : null;
        const progressTrack = progressWrap ? progressWrap.querySelector("[role='progressbar']") : null;
        const progressBar = progressWrap ? progressWrap.querySelector(".download-progress-bar") : null;
        const progressLabel = progressWrap
            ? progressWrap.querySelector("[data-batch-photo-archive-progress-label]")
            : null;
        const progressItems = progressWrap
            ? progressWrap.querySelector("[data-batch-photo-archive-items]")
            : null;
        const progressBytes = progressWrap
            ? progressWrap.querySelector("[data-batch-photo-archive-bytes]")
            : null;
        const progressSpeed = progressWrap
            ? progressWrap.querySelector("[data-batch-photo-archive-speed]")
            : null;
        const progressEta = progressWrap
            ? progressWrap.querySelector("[data-batch-photo-archive-eta]")
            : null;
        const cancelButton = section
            ? section.querySelector("[data-batch-photo-archive-cancel]")
            : null;
        const fallbackButton = section
            ? section.querySelector("[data-batch-photo-server-fallback]")
            : null;
        const busy = state === "loading";
        const progressKnown = config.progress !== null && config.progress !== undefined;
        const progress = progressKnown
            ? Math.max(0, Math.min(100, Number(config.progress || 0)))
            : null;
        if (button) {
            button.disabled = busy;
            button.classList.toggle("is-loading", busy);
        }
        if (label) {
            label.textContent = busy
                ? String(config.buttonLabel || "正在下载并打包...")
                : "下载全部图片";
        }
        if (status) {
            status.hidden = !config.message;
            status.textContent = String(config.message || "");
            status.classList.toggle("is-error", state === "error");
            status.classList.toggle("is-success", state === "success");
        }
        if (progressWrap) {
            progressWrap.hidden = !busy;
            progressWrap.classList.toggle("is-pending", !progressKnown);
        }
        if (progressTrack) {
            if (progressKnown) {
                progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
                progressTrack.setAttribute("aria-valuetext", `${Math.round(progress)}%`);
            } else {
                progressTrack.removeAttribute("aria-valuenow");
                progressTrack.setAttribute("aria-valuetext", "正在下载图片，总量未知");
            }
        }
        if (progressBar) {
            progressBar.style.width = progressKnown ? `${progress}%` : "100%";
        }
        if (progressLabel) {
            progressLabel.textContent = String(config.progressLabel || "正在准备图片 ZIP...");
        }
        if (progressItems) {
            progressItems.textContent = String(config.itemsLabel || progressItems.textContent || "");
        }
        if (progressBytes) {
            progressBytes.textContent = String(config.bytesLabel || "0 B");
        }
        if (progressSpeed) {
            progressSpeed.textContent = String(config.speedLabel || "正在测速...");
        }
        if (progressEta) {
            progressEta.textContent = String(config.etaLabel || "");
        }
        if (cancelButton) {
            cancelButton.hidden = !(busy && config.allowCancel !== false);
        }
        if (fallbackButton) {
            fallbackButton.hidden = !config.showFallback;
        }
    }

    function applyBatchPhotoArchiveProgress(section, event) {
        if (!browserPhotoApi || !event) {
            return;
        }
        const labels = {
            connecting: "正在连接图片资源...",
            fetching: `正在下载图片 ${event.completedItems} / ${event.totalItems}`,
            assembling: "正在生成 ZIP...",
            saving: "正在交给浏览器保存...",
            completed: "已开始下载 ZIP。",
        };
        setBatchPhotoArchiveState(section, "loading", {
            progress: event.stage === "saving" || event.stage === "completed"
                ? 100
                : (event.totalKnown ? event.percent : null),
            progressLabel: labels[event.stage] || "正在下载并打包图片...",
            itemsLabel: `${event.completedItems} / ${event.totalItems}`,
            bytesLabel: event.totalKnown
                ? `${browserPhotoApi.formatBytes(event.receivedBytes)} / ${browserPhotoApi.formatBytes(event.totalBytes)}`
                : `已下载 ${browserPhotoApi.formatBytes(event.receivedBytes)}`,
            speedLabel: browserPhotoApi.formatRate(event.bytesPerSecond),
            etaLabel: event.etaSeconds ? browserPhotoApi.formatEta(event.etaSeconds) : "",
            allowCancel: event.stage !== "saving" && event.stage !== "completed",
        });
    }

    function triggerBatchArchiveDownload(url, filename) {
        const link = document.createElement("a");
        link.href = url;
        if (filename) {
            link.download = filename;
        }
        link.rel = "noopener noreferrer";
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    async function waitForBatchPhotoArchiveTask(statusUrl, section) {
        const safeStatusUrl = safeSameOriginDownloadUrl(statusUrl, window.location.origin);
        for (let attempt = 0; attempt < 2400; attempt += 1) {
            const response = await fetch(safeStatusUrl, {
                cache: "no-store",
                credentials: "same-origin",
            });
            if (!response.ok) {
                throw new Error(await parseBatchArchiveError(response));
            }
            const task = await response.json();
            if (task.status === "failed") {
                throw new Error(task.message || "图片 ZIP 打包失败，请稍后重试。");
            }
            const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
            setBatchPhotoArchiveState(section, "loading", {
                progress,
                progressLabel: task.message || "正在打包图片 ZIP...",
            });
            if (task.status === "completed") {
                return task;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 250));
        }
        throw new Error("图片 ZIP 打包等待超时，请稍后重试。");
    }

    async function downloadBatchPhotoArchiveWithTask(batchPage, section, payload, csrfToken) {
        const taskUrl = String(batchPage.dataset.batchDownloadTaskUrl || "");
        const response = await fetch(taskUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            throw new Error(await parseBatchArchiveError(response));
        }
        const created = await response.json();
        const completed = await waitForBatchPhotoArchiveTask(created.status_url, section);
        const fileUrl = safeSameOriginDownloadUrl(
            completed.file_url || created.file_url,
            window.location.origin
        );
        const knownSize = Number(completed.size_bytes || 0);
        if (knownSize > BATCH_SYNC_ARCHIVE_MAX_BYTES) {
            triggerBatchArchiveDownload(fileUrl, completed.filename || "");
        } else {
            const fileResponse = await fetch(fileUrl, {
                cache: "no-store",
                credentials: "same-origin",
            });
            if (!fileResponse.ok) {
                throw new Error(await parseBatchArchiveError(fileResponse));
            }
            const blob = await readBatchPhotoArchiveResponse(fileResponse, section);
            const objectUrl = URL.createObjectURL(blob);
            triggerBatchArchiveDownload(objectUrl, completed.filename || "images.zip");
            window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
        }
        const savedCount = Number(completed.saved_count || 0);
        const failedCount = Number(completed.failed_count || 0);
        return {
            savedCount,
            failedCount,
            message: failedCount
                ? `浏览器已开始下载 ZIP：成功 ${savedCount} 张，失败 ${failedCount} 张。`
                : (
                    knownSize > BATCH_SYNC_ARCHIVE_MAX_BYTES
                        ? `ZIP 文件较大，已交给浏览器下载，共 ${savedCount} 张图片；页面不再显示传输速度。`
                        : `浏览器已开始下载 ZIP，共 ${savedCount} 张图片。`
                ),
        };
    }

    function formatBatchArchiveBytes(value) {
        if (browserPhotoApi) {
            return browserPhotoApi.formatBytes(value);
        }
        const bytes = Math.max(0, Number(value || 0));
        return bytes < 1024 * 1024
            ? `${(bytes / 1024).toFixed(1)} KiB`
            : `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    }

    async function readBatchPhotoArchiveResponse(response, section) {
        const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
        const startedAt = Date.now();
        const samples = [{at: startedAt, bytes: 0}];
        try {
            assertBatchSyncArchiveSize(contentLength);
        } catch (error) {
            if (response.body && typeof response.body.cancel === "function") {
                try {
                    await response.body.cancel();
                } catch (cancelError) {
                    // The response is already rejected; cancellation remains best-effort.
                }
            }
            throw error;
        }
        if (!response.body || !response.body.getReader) {
            if (!contentLength) {
                throw new Error("当前浏览器无法安全接收大小未知的图片 ZIP，请分批下载图片。");
            }
            const blob = await response.blob();
            assertBatchSyncArchiveSize(blob.size);
            return blob;
        }
        const reader = response.body.getReader();
        const chunks = [];
        let received = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) {
                break;
            }
            received += value.byteLength;
            try {
                assertBatchSyncArchiveSize(received);
            } catch (error) {
                try {
                    await reader.cancel();
                } catch (cancelError) {
                    // The bounded-memory error is authoritative.
                }
                throw error;
            }
            chunks.push(value);
            const now = Date.now();
            samples.push({at: now, bytes: received});
            while (samples.length > 2 && samples[1].at <= now - 2500) {
                samples.shift();
            }
            const first = samples[0];
            const sampleDuration = Math.max(0, now - first.at);
            const bytesPerSecond = sampleDuration > 0
                ? ((received - first.bytes) * 1000) / sampleDuration
                : 0;
            const progress = contentLength ? (received / contentLength) * 100 : null;
            const etaSeconds = (
                contentLength > received
                && now - startedAt >= 1500
                && bytesPerSecond > 0
            ) ? (contentLength - received) / bytesPerSecond : null;
            setBatchPhotoArchiveState(section, "loading", {
                progress,
                progressLabel: contentLength
                    ? batchPresentationText(
                        section,
                        `正在接收 ZIP ${Math.round(progress)}%`,
                        `正在接收服务器 ZIP ${Math.round(progress)}%`
                    )
                    : batchPresentationText(section, "正在接收 ZIP...", "正在接收服务器 ZIP..."),
                bytesLabel: contentLength
                    ? `${formatBatchArchiveBytes(received)} / ${formatBatchArchiveBytes(contentLength)}`
                    : `已接收 ${formatBatchArchiveBytes(received)}`,
                speedLabel: bytesPerSecond > 0
                    ? `${formatBatchArchiveBytes(bytesPerSecond)}/s`
                    : "正在测速...",
                etaLabel: etaSeconds && browserPhotoApi
                    ? browserPhotoApi.formatEta(etaSeconds)
                    : "",
                buttonLabel: batchPresentationText(section, "ZIP 下载中...", "服务器 ZIP 下载中..."),
                allowCancel: false,
            });
        }
        return new Blob(chunks, {type: "application/zip"});
    }

    async function downloadBatchPhotoArchiveSync(batchPage, section, payload, csrfToken) {
        const response = await fetch(String(batchPage.dataset.batchDownloadUrl || ""), {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            throw new Error(await parseBatchArchiveError(response));
        }
        const contentType = String(response.headers.get("Content-Type") || "");
        if (!contentType.includes("application/zip")) {
            throw new Error("图片 ZIP 响应格式异常，请稍后重试。");
        }
        const fallbackName = `${sanitizeBatchResourceFilename(payload.base_filename)}_images.zip`;
        const filename = parseBatchArchiveFilename(response, fallbackName);
        const blob = await readBatchPhotoArchiveResponse(response, section);
        const objectUrl = URL.createObjectURL(blob);
        triggerBatchArchiveDownload(objectUrl, filename);
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        const savedCount = Number(response.headers.get("X-Saved-Count") || 0);
        const failedCount = Number(response.headers.get("X-Failed-Count") || 0);
        return {
            savedCount,
            failedCount,
            message: failedCount
                ? `浏览器已开始下载 ZIP：成功 ${savedCount} 张，失败 ${failedCount} 张。`
                : `浏览器已开始下载 ZIP，共 ${savedCount} 张图片。`,
        };
    }

    async function downloadBatchPhotoArchiveServer(batchPage, batchForm, button) {
        const section = button ? button.closest("[data-batch-photo-section]") : null;
        const payload = buildBatchPhotoArchivePayload(section);
        const csrfInput = batchForm
            ? batchForm.querySelector('input[name="csrfmiddlewaretoken"]')
            : null;
        const csrfToken = csrfInput ? String(csrfInput.value || "") : "";
        if (!section || payload.media_items.length < 2 || !csrfToken) {
            throw new Error("当前图片列表无法打包，请重新解析。");
        }
        setBatchPhotoArchiveState(section, "loading", {
            progress: 2,
            progressLabel: batchPresentationText(
                section,
                "正在准备图片 ZIP...",
                "服务器正在准备图片 ZIP..."
            ),
            buttonLabel: batchPresentationText(section, "正在打包...", "服务器正在打包..."),
            allowCancel: false,
        });
        try {
            const useAsync = (
                batchPage.dataset.batchDownloadAsyncEnabled === "true"
                && Boolean(batchPage.dataset.batchDownloadTaskUrl)
            );
            const result = useAsync
                ? await downloadBatchPhotoArchiveWithTask(
                    batchPage,
                    section,
                    payload,
                    csrfToken
                )
                : await downloadBatchPhotoArchiveSync(
                    batchPage,
                    section,
                    payload,
                    csrfToken
                );
            setBatchPhotoArchiveState(section, "success", {message: result.message});
        } catch (error) {
            setBatchPhotoArchiveState(section, "error", {
                message: String(error && error.message || "图片 ZIP 下载失败，请稍后重试。"),
                showFallback: true,
            });
        }
    }

    async function downloadBatchPhotoArchive(batchPage, button) {
        const section = button ? button.closest("[data-batch-photo-section]") : null;
        const items = buildBatchBrowserPhotoItems(section);
        if (!section || items.length < 2) {
            throw new Error("当前图片列表无法由浏览器打包，请重新解析。");
        }
        if (!browserPhotoApi) {
            setBatchPhotoArchiveState(section, "error", {
                message: batchPresentationText(
                    section,
                    "图片下载组件不可用，可选择“换一种方式打包”。",
                    "浏览器图片下载组件不可用，可明确选择“使用服务器打包”。"
                ),
                showFallback: true,
            });
            return;
        }
        const controller = new AbortController();
        batchPhotoControllers.set(section, controller);
        setBatchPhotoArchiveState(section, "loading", {
            progress: null,
            progressLabel: "正在连接图片资源...",
            itemsLabel: `0 / ${items.length}`,
            bytesLabel: "已下载 0 B",
            allowCancel: true,
        });
        try {
            const result = await browserPhotoApi.createArchive(items, {
                baseFilename: String(section.dataset.baseFilename || "images"),
                signal: controller.signal,
                onProgress(event) {
                    applyBatchPhotoArchiveProgress(section, event);
                },
            });
            const failureNote = result.failureCount
                ? `，${result.failureCount} 张未能加入，详情见 failed.txt`
                : "";
            setBatchPhotoArchiveState(section, "success", {
                message: `浏览器已开始下载 ${result.filename}${failureNote}`,
            });
        } catch (error) {
            const errorCode = browserPhotoApi.safeErrorCode(error);
            if (errorCode === "BROWSER_PHOTO_CANCELLED") {
                setBatchPhotoArchiveState(section, "idle", {
                    message: "图片下载已取消。",
                });
                return;
            }
            setBatchPhotoArchiveState(section, "error", {
                message: `${browserPhotoApi.safeErrorMessage(error)} ${batchPresentationText(
                    section,
                    "可选择“换一种方式打包”。",
                    "可明确选择“使用服务器打包”。"
                )}`,
                showFallback: true,
            });
        } finally {
            batchPhotoControllers.delete(section);
        }
    }

    async function downloadBatchImageWithUi(button) {
        const itemNode = button ? button.closest("[data-batch-photo-item]") : null;
        const section = itemNode ? itemNode.closest("[data-batch-photo-section]") : null;
        if (!itemNode || !section || !browserPhotoApi) {
            throw new Error("图片下载地址不可用，请重新解析。");
        }
        const controller = new AbortController();
        batchPhotoControllers.set(section, controller);
        setBatchDownloadButtonLoading(button, true, "正在下载...");
        let completionLabel = "";
        try {
            const result = await downloadBatchImage(button, {
                signal: controller.signal,
                onProgress(event) {
                    applyBatchPhotoArchiveProgress(section, event);
                },
            });
            setBatchPhotoArchiveState(section, "success", {
                message: `已交给浏览器保存：${result.filename}`,
            });
            completionLabel = "已开始保存";
            return result;
        } catch (error) {
            const message = browserPhotoApi.safeErrorMessage(error);
            setBatchPhotoArchiveState(section, "error", {message});
            completionLabel = "重试下载";
            throw error;
        } finally {
            batchPhotoControllers.delete(section);
            setBatchDownloadButtonLoading(button, false);
            if (completionLabel) {
                showBatchDownloadButtonMessage(button, completionLabel);
            }
        }
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
        const galleryImages = Array.from(card.querySelectorAll(".batch-photo-preview img"));
        galleryImages.forEach((image, index) => {
            image.setAttribute("loading", index === 0 ? "eager" : "lazy");
            if (index === 0) {
                image.setAttribute("fetchpriority", "high");
            } else {
                image.removeAttribute("fetchpriority");
            }
        });
        const image = galleryImages[0] || card.querySelector(".batch-result-cover img");
        if (!image) {
            return;
        }
        image.setAttribute("loading", "eager");
        image.setAttribute("fetchpriority", "high");
    }

    function getBatchPhotoItems(item) {
        const section = item && item.closest
            ? item.closest("[data-batch-photo-section]")
            : null;
        return section
            ? Array.from(section.querySelectorAll("[data-batch-photo-item]"))
            : [];
    }

    function getBatchLightboxNavigation(item) {
        const items = getBatchPhotoItems(item);
        const index = items.indexOf(item);
        return {
            index,
            total: items.length,
            previousItem: index > 0 ? items[index - 1] : null,
            nextItem: index >= 0 && index < items.length - 1 ? items[index + 1] : null,
        };
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
        const lightbox = batchPage.querySelector("[data-batch-photo-lightbox]");
        const lightboxImage = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-image]")
            : null;
        const lightboxLoading = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-loading]")
            : null;
        const lightboxStatus = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-status]")
            : null;
        const lightboxIndex = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-index]")
            : null;
        const lightboxPrevious = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-prev]")
            : null;
        const lightboxNext = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-next]")
            : null;
        const lightboxDownload = lightbox
            ? lightbox.querySelector("[data-batch-lightbox-download]")
            : null;
        const lightboxModal = (
            lightbox
            && window.bootstrap
            && window.bootstrap.Modal
        )
            ? window.bootstrap.Modal.getOrCreateInstance(lightbox)
            : null;
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
        let activeLightboxItem = null;

        function setLightboxStatus(message) {
            if (!lightboxStatus) {
                return;
            }
            lightboxStatus.hidden = !message;
            lightboxStatus.textContent = message || "";
        }

        function renderLightboxItem(item) {
            if (!item || !lightboxImage) {
                return;
            }
            const navigation = getBatchLightboxNavigation(item);
            if (navigation.index < 0) {
                return;
            }
            const preview = item.querySelector("[data-batch-lightbox-open]");
            const cardPreview = preview
                ? String(preview.dataset.cardPreviewSrc || "")
                : "";
            const lightboxPreview = preview
                ? String(preview.dataset.lightboxPreviewSrc || cardPreview)
                : cardPreview;
            activeLightboxItem = item;
            if (lightboxIndex) {
                lightboxIndex.textContent = `${navigation.index + 1} / ${navigation.total}`;
            }
            if (lightboxPrevious) {
                lightboxPrevious.disabled = !navigation.previousItem;
            }
            if (lightboxNext) {
                lightboxNext.disabled = !navigation.nextItem;
            }
            if (lightboxDownload) {
                lightboxDownload.hidden = !item.querySelector("[data-batch-image-download]");
            }
            if (lightboxLoading) {
                lightboxLoading.hidden = false;
            }
            setLightboxStatus("");
            lightboxImage.hidden = true;
            lightboxImage.alt = `图片 ${navigation.index + 1}`;
            lightboxImage.dataset.cardPreviewSrc = cardPreview;
            lightboxImage.dataset.fallbackAttempted = "false";
            lightboxImage.onload = () => {
                if (lightboxLoading) {
                    lightboxLoading.hidden = true;
                }
                lightboxImage.hidden = false;
            };
            lightboxImage.onerror = () => {
                const fallback = String(lightboxImage.dataset.cardPreviewSrc || "");
                if (
                    fallback
                    && lightboxImage.dataset.fallbackAttempted !== "true"
                    && lightboxImage.src !== fallback
                ) {
                    lightboxImage.dataset.fallbackAttempted = "true";
                    setLightboxStatus("高清预览加载失败，仍可下载原资源。");
                    lightboxImage.src = fallback;
                    return;
                }
                if (lightboxLoading) {
                    lightboxLoading.hidden = true;
                }
                lightboxImage.hidden = true;
                setLightboxStatus("图片预览加载失败，仍可使用下载按钮保存资源。");
            };
            lightboxImage.src = lightboxPreview;
        }

        function openLightboxItem(item) {
            renderLightboxItem(item);
            if (lightboxModal) {
                lightboxModal.show();
            }
        }

        function moveLightbox(direction) {
            if (!activeLightboxItem) {
                return;
            }
            const navigation = getBatchLightboxNavigation(activeLightboxItem);
            const target = direction < 0
                ? navigation.previousItem
                : navigation.nextItem;
            if (target) {
                renderLightboxItem(target);
            }
        }

        prioritizeFirstBatchPreview(
            batchPage.querySelector(".batch-result-card:not(.is-hidden)")
        );
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

        if (lightbox) {
            lightbox.addEventListener("hidden.bs.modal", () => {
                activeLightboxItem = null;
                setLightboxStatus("");
            });
        }

        document.addEventListener("keydown", (event) => {
            if (!activeLightboxItem || event.defaultPrevented) {
                return;
            }
            if (event.key === "ArrowLeft") {
                event.preventDefault();
                moveLightbox(-1);
            } else if (event.key === "ArrowRight") {
                event.preventDefault();
                moveLightbox(1);
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
                    applyPendingParsePreferences();
                }
            });
        }

        batchPage.addEventListener("click", async (event) => {
            const lightboxOpen = event.target.closest("[data-batch-lightbox-open]");
            if (lightboxOpen) {
                event.preventDefault();
                const item = lightboxOpen.closest("[data-batch-photo-item]");
                if (item) {
                    openLightboxItem(item);
                }
                return;
            }

            const photoDownloadAllButton = event.target.closest("[data-batch-photo-download-all]");
            if (photoDownloadAllButton) {
                event.preventDefault();
                if (!photoDownloadAllButton.disabled) {
                    await downloadBatchPhotoArchive(
                        batchPage,
                        photoDownloadAllButton
                    );
                }
                return;
            }

            const photoArchiveCancelButton = event.target.closest("[data-batch-photo-archive-cancel]");
            if (photoArchiveCancelButton) {
                event.preventDefault();
                const section = photoArchiveCancelButton.closest("[data-batch-photo-section]");
                const controller = section ? batchPhotoControllers.get(section) : null;
                if (controller) {
                    controller.abort();
                }
                return;
            }

            const photoServerFallbackButton = event.target.closest("[data-batch-photo-server-fallback]");
            if (photoServerFallbackButton) {
                event.preventDefault();
                if (!photoServerFallbackButton.disabled) {
                    await downloadBatchPhotoArchiveServer(
                        batchPage,
                        batchForm,
                        photoServerFallbackButton
                    );
                }
                return;
            }

            const lightboxPreviousButton = event.target.closest("[data-batch-lightbox-prev]");
            if (lightboxPreviousButton) {
                event.preventDefault();
                moveLightbox(-1);
                return;
            }

            const lightboxNextButton = event.target.closest("[data-batch-lightbox-next]");
            if (lightboxNextButton) {
                event.preventDefault();
                moveLightbox(1);
                return;
            }

            const lightboxDownloadButton = event.target.closest("[data-batch-lightbox-download]");
            if (lightboxDownloadButton) {
                event.preventDefault();
                const imageButton = activeLightboxItem
                    ? activeLightboxItem.querySelector("[data-batch-image-download]")
                    : null;
                if (imageButton) {
                    try {
                        const result = await downloadBatchImageWithUi(imageButton);
                        setLightboxStatus(`已交给浏览器保存：${result.filename}`);
                    } catch (error) {
                        setLightboxStatus("图片下载未能启动，请关闭预览后重试。");
                    }
                }
                return;
            }

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
                const resources = collectBatchResourceLinks(batchPage);
                const mode = exportButton.getAttribute("data-batch-export");
                if (!resources.length) {
                    flashButton(exportButton, "无可复制资源");
                    return;
                }
                if (mode === "copy-links") {
                    try {
                        await copyText(buildBatchLinksText(resources));
                        flashButton(exportButton, "已复制");
                    } catch (error) {
                        flashButton(exportButton, "复制失败");
                    }
                    return;
                }
                if (mode === "aria2") {
                    downloadTextFile(
                        "aria2.txt",
                        buildBatchAria2Text(resources)
                    );
                    return;
                }
                downloadTextFile("links.txt", buildBatchLinksText(resources));
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

            const imageDownloadButton = event.target.closest("[data-batch-image-download]");
            if (imageDownloadButton) {
                event.preventDefault();
                try {
                    await downloadBatchImageWithUi(imageDownloadButton);
                } catch (error) {
                    imageDownloadButton.title = String(error && error.message || "图片下载失败");
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
        collectBatchResourceLinks,
        buildBatchLinksText,
        buildBatchAria2Text,
        batchPhotoMediaItem,
        buildBatchPhotoArchivePayload,
        safeSameOriginDownloadUrl,
        getBatchPhotoItems,
        getBatchLightboxNavigation,
        assertBatchSyncArchiveSize,
        readBatchPhotoArchiveResponse,
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
