(function () {
    const SETTINGS_KEY = "xhsDownloadSettings";
    const MAX_IMAGE_RETRIES = 3;

    function getCookie(name) {
        let cookieValue = null;

        if (document.cookie && document.cookie !== "") {
            const cookies = document.cookie.split(";");

            for (let cookie of cookies) {
                cookie = cookie.trim();

                if (cookie.startsWith(name + "=")) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }

        return cookieValue;
    }

    function loadSettings() {
        try {
            return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
        } catch (error) {
            console.warn("Invalid XHS download settings, reset to defaults.", error);
            localStorage.removeItem(SETTINGS_KEY);
            return {};
        }
    }

    function saveSettings(settings) {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    }

    function retryImage(img) {
        let retry = Number.parseInt(img.dataset.retry || "0", 10);

        if (retry >= MAX_IMAGE_RETRIES) {
            return;
        }

        retry += 1;
        img.dataset.retry = String(retry);

        window.setTimeout(() => {
            const url = new URL(img.src, window.location.origin);
            url.searchParams.set("_retry", Date.now().toString());
            img.src = url.toString();
        }, retry * 1000);
    }

    window.retryImage = retryImage;

    function setStatus(statusElement, message, variant) {
        if (!statusElement) {
            return;
        }

        statusElement.innerText = message;
        statusElement.classList.remove("text-muted", "text-danger", "text-success");
        statusElement.classList.add(variant || "text-muted");
    }

    function getCardItem(card) {
        const type = card.dataset.type || "image";
        const sizeBytes = Number.parseInt(card.dataset.sizeBytes || "0", 10) || 0;
        const providerFields = {
            asset_id: card.dataset.assetId || "",
            provider: card.dataset.provider || "",
            provider_resource_id: card.dataset.providerResourceId || "",
            provider_cache_key: card.dataset.providerCacheKey || "",
            provider_manifest_token: card.dataset.providerManifestToken || "",
        };

        if (type === "video") {
            return {
                type: "video",
                url: card.dataset.url,
                cover_url: card.dataset.coverUrl || "",
                size_bytes: sizeBytes,
                ...providerFields,
            };
        }

        return {
            type: "image",
            url: card.dataset.url,
            size_bytes: sizeBytes,
            ...providerFields,
        };
    }

    function getItemKey(item) {
        return `${item.type}:${item.url}`;
    }

    function optionExists(select, value) {
        return Boolean(select) && Array.from(select.options).some((option) => option.value === value);
    }

    function decodeDispositionValue(value) {
        try {
            return decodeURIComponent(value.replace(/^UTF-8''/i, ""));
        } catch (error) {
            return value;
        }
    }

    function getDispositionFilename(response) {
        const disposition = response.headers.get("Content-Disposition") || "";
        const encodedMatch = disposition.match(/filename\*=([^;]+)/i);

        if (encodedMatch) {
            return decodeDispositionValue(encodedMatch[1].trim().replace(/^"|"$/g, ""));
        }

        const match = disposition.match(/filename="([^"]+)"/i);
        return match ? match[1] : "";
    }

    function getDownloadFilename(response, fallbackName) {
        return getDispositionFilename(response) || `${fallbackName || "xhs"}.zip`;
    }

    async function saveZipBlob(blob, filename, useFilePicker) {
        if (useFilePicker && window.showSaveFilePicker) {
            const isZip = filename.toLowerCase().endsWith(".zip");
            const handle = await window.showSaveFilePicker({
                suggestedName: filename,
                types: [
                    {
                        description: isZip ? "ZIP archive" : "Media file",
                        accept: isZip
                            ? {"application/zip": [".zip"]}
                            : {"video/mp4": [".mp4"], "application/octet-stream": [".mp4"]},
                    },
                ],
            });
            const writable = await handle.createWritable();
            await writable.write(blob);
            await writable.close();
            return "已保存到选择的位置";
        }

        const downloadUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = downloadUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(downloadUrl);
        return "已交给浏览器下载";
    }

    function extensionFromContentType(contentType, itemType) {
        const normalized = String(contentType || "").toLowerCase();
        if (normalized.includes("video/mp4")) {
            return "mp4";
        }
        if (normalized.includes("image/jpeg")) {
            return "jpg";
        }
        if (normalized.includes("image/png")) {
            return "png";
        }
        if (normalized.includes("image/webp")) {
            return "webp";
        }
        return itemType === "video" ? "mp4" : "jpg";
    }

    function routeWarningText(warnings) {
        return Array.isArray(warnings) && warnings.length > 0 ? ` ${warnings.join(" ")}` : "";
    }

    async function parseErrorResponse(response) {
        try {
            const data = await response.json();
            return data.message || "保存失败";
        } catch (error) {
            return "保存失败";
        }
    }

    function selectedSingleItem(items) {
        const selectedItems = items || [];
        return selectedItems.length === 1;
    }

    function itemRequiresZipTask(item) {
        const provider = item ? String(item.provider || "") : "";
        return ["kukutool_generic", "youtube_merge", "yt_dlp_merge"].includes(provider);
    }

    function selectedNeedsZipTask(items) {
        const selectedItems = items || [];
        return selectedItems.length > 1 || selectedItems.some(itemRequiresZipTask);
    }

    async function parseSearchResponse(response) {
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.includes("application/json")) {
            return response.json();
        }

        await response.text();
        if (response.ok) {
            throw new Error("解析响应格式异常，请稍后再试。");
        }
        throw new Error(`解析失败（${response.status}），请稍后再试。`);
    }

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    function escapeHtml(value) {
        const element = document.createElement("div");
        element.innerText = value || "";
        return element.innerHTML;
    }

    document.addEventListener("DOMContentLoaded", () => {
        const page = document.querySelector(".xhs-download-page");

        if (!page) {
            return;
        }

        const searchForm = document.getElementById("hero-search-form");
        const resultsRegion = document.getElementById("parse-results-region");
        const pageHeading = document.getElementById("page-heading");
        const pageDescription = document.getElementById("page-description");
        const linkLabel = document.getElementById("media-link-label");
        const linkInput = document.getElementById("media-link-input");
        const pasteLinkIconBtn = document.getElementById("paste-link-icon-btn");
        const clearLinkBtn = document.getElementById("clear-link-btn");
        const ultraHdRequestInput = document.getElementById("ultra-hd-request-input");
        const ultraHdSettingInput = document.getElementById("ultra-hd-setting");
        const youtubeMergeRequestInput = document.getElementById("youtube-merge-request-input");
        const youtubeMergeSettingInput = document.getElementById("youtube-merge-setting");
        const downloadModeInput = document.getElementById("download-mode");
        const browserDownloadSettings = document.getElementById("browser-download-settings");
        const serverSaveSettings = document.getElementById("server-save-settings");
        const saveDirInput = document.getElementById("save-dir");
        const browserDefaultLocationBtn = document.getElementById("browser-default-location-btn");
        const chooseDownloadLocationBtn = document.getElementById("choose-download-location-btn");
        const browserLocationHelp = document.getElementById("browser-location-help");
        const saveFormatInput = document.getElementById("save-format");
        const qualityInput = document.getElementById("quality");
        const saveSettingsBtn = document.getElementById("save-settings-btn");
        const videoSaveModeInput = document.getElementById("video-save-mode");
        const downloadUrl = page.dataset.downloadUrl;
        const downloadTaskUrl = page.dataset.downloadTaskUrl;
        const downloadAsyncEnabled = page.dataset.downloadAsyncEnabled === "true";
        const savedSettings = loadSettings();

        let selected = [];
        let useFilePicker = Boolean(savedSettings.use_file_picker);
        let downloadMode = savedSettings.download_mode || "browser";
        let activeParseController = null;
        let parseRequestId = 0;
        let downloadInProgress = false;

        function getBaseFilename() {
            return page.dataset.baseFilename || "xhs_image";
        }

        function getCards() {
            return Array.from(resultsRegion ? resultsRegion.querySelectorAll(".asset-card") : []);
        }

        function getStatusElement() {
            return resultsRegion ? resultsRegion.querySelector("#status") : null;
        }

        function getDownloadButton() {
            return resultsRegion ? resultsRegion.querySelector("#download-btn") : null;
        }

        function getDownloadActionButtons() {
            return [getDownloadButton()].filter(Boolean);
        }

        function getSelectAllButton() {
            return resultsRegion ? resultsRegion.querySelector("#select-all-btn") : null;
        }

        function getProgressElements() {
            const progressWrap = resultsRegion ? resultsRegion.querySelector("#download-progress-wrap") : null;
            return {
                progressWrap,
                progressTrack: progressWrap ? progressWrap.querySelector("[role='progressbar']") : null,
                progressBar: resultsRegion ? resultsRegion.querySelector("#download-progress-bar") : null,
                progressPercent: resultsRegion ? resultsRegion.querySelector("#download-progress-percent") : null,
            };
        }

        function isUltraHdEnabled() {
            return Boolean(ultraHdSettingInput && ultraHdSettingInput.checked && !ultraHdSettingInput.disabled);
        }

        function updateUltraHdRequestValue() {
            if (!ultraHdRequestInput) {
                return;
            }

            ultraHdRequestInput.value = isUltraHdEnabled() ? "1" : "0";
        }

        function isYoutubeMergeEnabled() {
            return Boolean(
                youtubeMergeSettingInput
                && youtubeMergeSettingInput.checked
                && !youtubeMergeSettingInput.disabled
            );
        }

        function updateYoutubeMergeRequestValue() {
            if (!youtubeMergeRequestInput) {
                return;
            }

            youtubeMergeRequestInput.value = isYoutubeMergeEnabled() ? "1" : "0";
        }

        function setPageHasResults(value) {
            page.classList.toggle("has-results", Boolean(value));
        }

        function replaceThemeClass(element, themeClass) {
            if (!element || !themeClass) {
                return;
            }
            Array.from(element.classList)
                .filter((className) => className.startsWith("theme-"))
                .forEach((className) => element.classList.remove(className));
            element.classList.add(themeClass);
        }

        function updateNavActive(platformKey) {
            document.querySelectorAll(".site-nav-link[data-nav-platform-key]").forEach((link) => {
                link.classList.toggle("active", link.dataset.navPlatformKey === platformKey);
            });
        }

        function applyTargetPage(targetPage) {
            if (!targetPage || !targetPage.key || targetPage.key === page.dataset.platformKey) {
                return;
            }

            page.dataset.platformKey = targetPage.key;
            replaceThemeClass(document.body, targetPage.theme_class);
            replaceThemeClass(page, targetPage.theme_class);
            updateNavActive(targetPage.key);

            if (targetPage.title) {
                document.title = targetPage.title;
            }
            if (pageHeading && targetPage.heading) {
                pageHeading.innerText = targetPage.heading;
            }
            if (pageDescription && targetPage.description) {
                pageDescription.innerText = targetPage.description;
            }
            if (linkLabel && targetPage.label) {
                linkLabel.innerText = targetPage.label;
            }
            if (linkInput && targetPage.placeholder) {
                linkInput.placeholder = targetPage.placeholder;
            }
            if (searchForm && targetPage.parse_action_url) {
                searchForm.action = targetPage.parse_action_url;
            }
            if (targetPage.home_url && window.location.pathname !== targetPage.home_url) {
                window.history.replaceState({}, "", targetPage.home_url);
            }
        }

        function updateSelectedStatus() {
            setStatus(getStatusElement(), `已选择 ${selected.length} 个资源`);
        }

        function updateSelectAllButton() {
            const selectAllBtn = getSelectAllButton();
            const cards = getCards();
            if (!selectAllBtn) {
                return;
            }

            const allSelected = cards.length > 0 && selected.length === cards.length;
            selectAllBtn.innerHTML = allSelected
                ? '<i class="bi bi-x-square me-1"></i>取消全选'
                : '<i class="bi bi-check2-square me-1"></i>全选';
        }

        function setDownloadButtonsBusy(isBusy, activeButton, label) {
            getDownloadActionButtons().forEach((button) => {
                if (!button.isConnected) {
                    return;
                }
                if (isBusy && !button.dataset.originalHtml) {
                    button.dataset.originalHtml = button.innerHTML;
                }
                button.disabled = isBusy;
                button.setAttribute("aria-busy", isBusy ? "true" : "false");
                if (isBusy && button === activeButton) {
                    const busyLabel = label || "处理中";
                    button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>${busyLabel}`;
                } else if (!isBusy && button.dataset.originalHtml) {
                    button.innerHTML = button.dataset.originalHtml;
                    delete button.dataset.originalHtml;
                }
            });
        }

        function updateDownloadLocationButtons() {
            if (!browserDefaultLocationBtn || !chooseDownloadLocationBtn) {
                return;
            }

            if (!window.showSaveFilePicker) {
                useFilePicker = false;
                chooseDownloadLocationBtn.disabled = true;
                chooseDownloadLocationBtn.title = "当前浏览器不支持选择保存位置，将使用默认下载路径";

                if (browserLocationHelp) {
                    browserLocationHelp.innerText = "当前浏览器不支持选择保存位置，将使用浏览器默认下载目录。";
                }
            } else {
                chooseDownloadLocationBtn.disabled = false;
                chooseDownloadLocationBtn.title = "";

                if (browserLocationHelp) {
                    browserLocationHelp.innerText = "默认使用浏览器下载目录；选择保存位置后，每次下载 ZIP 时会弹出保存窗口。";
                }
            }

            browserDefaultLocationBtn.classList.toggle("btn-brand", !useFilePicker);
            browserDefaultLocationBtn.classList.toggle("btn-outline-secondary", useFilePicker);
            browserDefaultLocationBtn.setAttribute("aria-pressed", String(!useFilePicker));

            chooseDownloadLocationBtn.classList.toggle("btn-brand", useFilePicker);
            chooseDownloadLocationBtn.classList.toggle("btn-outline-secondary", !useFilePicker);
            chooseDownloadLocationBtn.setAttribute("aria-pressed", String(useFilePicker));
        }

        function updateModeVisibility() {
            downloadMode = downloadModeInput ? downloadModeInput.value : "browser";

            if (browserDownloadSettings) {
                browserDownloadSettings.hidden = downloadMode !== "browser";
            }

            if (serverSaveSettings) {
                serverSaveSettings.hidden = downloadMode !== "server";
            }
        }

        function setProgress(percent) {
            const value = Math.max(0, Math.min(100, Math.round(percent || 0)));
            const {progressWrap, progressTrack, progressBar, progressPercent} = getProgressElements();
            if (progressWrap) {
                progressWrap.hidden = false;
                progressWrap.classList.remove("is-pending");
            }
            if (progressTrack) {
                progressTrack.setAttribute("aria-valuenow", String(value));
                progressTrack.setAttribute("aria-valuetext", `${value}%`);
            }
            if (progressBar) {
                progressBar.style.width = `${value}%`;
            }
            if (progressPercent) {
                progressPercent.innerText = `${value}%`;
            }
        }

        function setProgressPending(label) {
            const {progressWrap, progressTrack, progressBar, progressPercent} = getProgressElements();
            if (progressWrap) {
                progressWrap.hidden = false;
                progressWrap.classList.add("is-pending");
            }
            if (progressTrack) {
                progressTrack.removeAttribute("aria-valuenow");
                progressTrack.setAttribute("aria-valuetext", label || "准备中");
            }
            if (progressBar) {
                progressBar.style.width = "100%";
            }
            if (progressPercent) {
                progressPercent.innerText = label || "准备中";
            }
        }

        function setParseLoading(isLoading) {
            const submitBtn = document.getElementById("parse-submit-btn");
            const idleIcon = submitBtn ? submitBtn.querySelector(".hero-submit-idle") : null;
            const spinner = submitBtn ? submitBtn.querySelector(".hero-submit-spinner") : null;
            const label = submitBtn ? submitBtn.querySelector(".hero-submit-label") : null;

            if (submitBtn) {
                submitBtn.disabled = isLoading;
                submitBtn.classList.toggle("is-loading", isLoading);
            }
            if (idleIcon) {
                idleIcon.hidden = isLoading;
            }
            if (spinner) {
                spinner.hidden = !isLoading;
            }
            if (label) {
                label.innerText = isLoading ? "解析中" : "获取素材";
            }
        }

        function showParseLoading() {
            if (!resultsRegion) {
                return;
            }
            selected = [];
            setPageHasResults(true);
            resultsRegion.innerHTML = `
                <div class="parse-loading-card">
                    <div class="spinner-border" role="status" aria-hidden="true"></div>
                    <div>
                        <div class="parse-loading-title">正在解析素材</div>
                        <div class="parse-loading-subtitle">结果会直接显示在当前页面</div>
                    </div>
                </div>
            `;
        }

        function showParseError(message) {
            if (!resultsRegion) {
                return;
            }
            selected = [];
            setPageHasResults(true);
            page.dataset.baseFilename = "xhs_image";
            resultsRegion.innerHTML = `
                <div class="alert alert-danger parse-inline-alert">
                    <i class="bi bi-exclamation-triangle me-1"></i>
                    ${escapeHtml(message || "解析失败，请稍后再试。")}
                </div>
            `;
        }

        function scrollResultsIntoView() {
            if (!resultsRegion || !resultsRegion.innerHTML.trim()) {
                return;
            }

            window.requestAnimationFrame(() => {
                const target = (
                    resultsRegion.querySelector(".results-scroll-anchor")
                    || resultsRegion.querySelector(".result-card")
                    || resultsRegion
                );
                target.scrollIntoView({behavior: "smooth", block: "start"});
            });
        }

        function applyParsePayload(payload) {
            if (!resultsRegion) {
                return;
            }
            applyTargetPage(payload.target_page);
            selected = [];
            resultsRegion.innerHTML = payload.html || "";
            page.dataset.baseFilename = payload.base_filename || "xhs_image";
            setPageHasResults(Boolean(payload.has_content || payload.has_results));
            updateSelectAllButton();
            if (payload.has_results) {
                scrollResultsIntoView();
            }
        }

        function resetResults() {
            if (activeParseController) {
                activeParseController.abort();
                activeParseController = null;
            }
            selected = [];
            parseRequestId += 1;
            setParseLoading(false);
            page.dataset.baseFilename = "xhs_image";
            if (resultsRegion) {
                resultsRegion.innerHTML = "";
            }
            setPageHasResults(false);
        }

        async function readResponseBlobWithProgress(response, startPercent, endPercent) {
            const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
            if (!response.body || !contentLength) {
                const blob = await response.blob();
                setProgress(endPercent);
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
                chunks.push(value);
                received += value.length;
                const ratio = received / contentLength;
                setProgress(startPercent + (endPercent - startPercent) * ratio);
            }

            setProgress(endPercent);
            return new Blob(chunks, {type: response.headers.get("Content-Type") || "application/zip"});
        }

        function directDownloadFilename(item, response, index, total) {
            const fallbackBase = getBaseFilename();
            const suffix = total > 1 ? `_${index + 1}` : "";
            const extension = extensionFromContentType(
                response.headers.get("Content-Type") || "",
                item.type || "video",
            );
            return getDispositionFilename(response) || item.filename || `${fallbackBase}${suffix}.${extension}`;
        }

        async function writeResponseToFileWithProgress(response, fileHandle, startPercent, endPercent) {
            const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
            if (!response.body || !contentLength) {
                const blob = await response.blob();
                const writable = await fileHandle.createWritable();
                try {
                    await writable.write(blob);
                } finally {
                    await writable.close();
                }
                setProgress(endPercent);
                return;
            }

            const reader = response.body.getReader();
            const writable = await fileHandle.createWritable();
            let received = 0;

            try {
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) {
                        break;
                    }
                    await writable.write(value);
                    received += value.length;
                    const ratio = received / contentLength;
                    setProgress(startPercent + (endPercent - startPercent) * ratio);
                }
            } finally {
                await writable.close();
            }
            setProgress(endPercent);
        }

        async function downloadDirectItem(item, index, total) {
            const startPercent = total > 1 ? (index / total) * 100 : 0;
            const endPercent = total > 1 ? ((index + 1) / total) * 100 : 100;
            const response = await fetch(item.url, {
                cache: "no-store",
                credentials: "omit",
            });

            if (!response.ok) {
                throw new Error(`直链下载失败（${response.status}）`);
            }

            const filename = directDownloadFilename(item, response, index, total);
            if (useFilePicker && window.showSaveFilePicker) {
                const extension = filename.split(".").pop() || "mp4";
                const handle = await window.showSaveFilePicker({
                    suggestedName: filename,
                    types: [
                        {
                            description: item.type === "image" ? "Image file" : "Media file",
                            accept: item.type === "image"
                                ? {"image/*": [`.${extension}`]}
                                : {"video/mp4": [`.${extension}`], "application/octet-stream": [`.${extension}`]},
                        },
                    ],
                });
                await writeResponseToFileWithProgress(response, handle, startPercent, endPercent);
                return filename;
            }

            const blob = await readResponseBlobWithProgress(response, startPercent, endPercent);
            await saveZipBlob(blob, filename, false);
            return filename;
        }

        async function downloadDirectRouteItems(items) {
            const directItems = (items || []).filter((item) => item && item.url);
            if (directItems.length === 0) {
                throw new Error("没有可下载的直链资源");
            }

            setStatus(getStatusElement(), "正在通过浏览器直接下载源文件...");
            const filenames = [];
            for (let index = 0; index < directItems.length; index += 1) {
                filenames.push(await downloadDirectItem(directItems[index], index, directItems.length));
            }
            return filenames;
        }

        async function postDownloadPayload(payload) {
            return fetch(downloadUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify(payload),
            });
        }

        async function saveDownloadResponse(response, fallbackName, startPercent, endPercent) {
            const filename = getDownloadFilename(response, fallbackName);
            const blob = await readResponseBlobWithProgress(response, startPercent, endPercent);
            const savedMessage = await saveZipBlob(blob, filename, useFilePicker);
            const savedCount = response.headers.get("X-Saved-Count") || "?";
            const failedCount = response.headers.get("X-Failed-Count") || "0";
            const variant = failedCount === "0" ? "text-success" : "text-muted";
            return {filename, savedMessage, savedCount, failedCount, variant};
        }

        function serverDownloadStatusMessage(result) {
            return `${result.savedMessage}：${result.filename}，成功 ${result.savedCount} 个，失败 ${result.failedCount} 个`;
        }

        async function downloadServerSingleFallback(payload) {
            const fallbackPayload = {
                ...payload,
                download_mode: "proxy",
            };
            setProgressPending("切换中");
            setStatus(getStatusElement(), "直链下载不可用，正在切换服务器单文件下载...");

            const response = await postDownloadPayload(fallbackPayload);
            if (!response.ok) {
                throw new Error(await parseErrorResponse(response));
            }

            const contentType = response.headers.get("Content-Type") || "";
            if (contentType.includes("application/json")) {
                const data = await response.json();
                throw new Error(data.message || data.route_reason || "服务器兜底下载未返回文件");
            }

            return saveDownloadResponse(response, getBaseFilename(), 0, 100);
        }

        async function waitForDownloadTask(taskStatusUrl) {
            while (true) {
                const response = await fetch(taskStatusUrl, {cache: "no-store"});
                if (!response.ok) {
                    throw new Error(await parseErrorResponse(response));
                }

                const task = await response.json();
                setProgress(task.progress || 0);

                if (task.status === "failed") {
                    throw new Error(task.message || "下载失败");
                }

                if (task.status === "completed") {
                    return task;
                }

                await sleep(250);
            }
        }

        async function downloadWithTask(payload) {
            if (!downloadTaskUrl) {
                throw new Error("当前页面缺少下载任务接口");
            }

            setProgress(0);
            setStatus(getStatusElement(), "正在创建后台打包任务，请保持页面打开...");

            const createResponse = await fetch(downloadTaskUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify(payload),
            });

            if (!createResponse.ok) {
                throw new Error(await parseErrorResponse(createResponse));
            }

            const created = await createResponse.json();
            const completed = await waitForDownloadTask(created.status_url);
            const fileUrl = completed.file_url || created.file_url;

            setProgress(95);
            setStatus(getStatusElement(), "正在接收文件...");

            const fileResponse = await fetch(fileUrl, {cache: "no-store"});
            if (!fileResponse.ok) {
                throw new Error(await parseErrorResponse(fileResponse));
            }

            const filename = getDownloadFilename(fileResponse, getBaseFilename());
            const blob = await readResponseBlobWithProgress(fileResponse, 95, 100);
            const savedMessage = await saveZipBlob(blob, filename, useFilePicker);
            const savedCount = fileResponse.headers.get("X-Saved-Count") || completed.saved_count || "?";
            const failedCount = fileResponse.headers.get("X-Failed-Count") || completed.failed_count || "0";
            const variant = String(failedCount) === "0" ? "text-success" : "text-muted";

            setProgress(100);
            setStatus(getStatusElement(), `${savedMessage}：${filename}，成功 ${savedCount} 个，失败 ${failedCount} 个`, variant);
        }

        async function handleDownloadClick(downloadBtn, forceAutoMode) {
            if (selected.length === 0) {
                window.alert("请先选择需要保存的资源");
                return;
            }
            if (downloadInProgress) {
                setStatus(getStatusElement(), "当前下载仍在处理中，请等待完成后再操作。", "text-muted");
                return;
            }

            downloadInProgress = true;
            const busyLabel = forceAutoMode ? "智能处理中" : "保存中";
            setDownloadButtonsBusy(true, downloadBtn, busyLabel);
            setStatus(
                getStatusElement(),
                forceAutoMode ? "正在智能判断下载方式，请稍候..." : "正在保存，请稍候...",
            );

            try {
                const payload = {
                    media_items: selected,
                    base_filename: getBaseFilename(),
                    download_mode: forceAutoMode ? "auto" : (downloadModeInput ? downloadModeInput.value : "browser"),
                    save_dir: saveDirInput ? saveDirInput.value.trim() : "xhs",
                    save_format: saveFormatInput ? saveFormatInput.value : "original",
                    quality: qualityInput ? qualityInput.value || 95 : 95,
                    video_save_mode: videoSaveModeInput ? videoSaveModeInput.value : "video",
                };

                if (
                    downloadAsyncEnabled
                    && downloadTaskUrl
                    && selectedNeedsZipTask(selected)
                ) {
                    await downloadWithTask(payload);
                    return;
                }

                const pendingLabel = payload.download_mode === "auto"
                    ? "判断中"
                    : (selectedSingleItem(selected) ? "准备下载" : "打包中");
                setProgressPending(pendingLabel);
                const response = await postDownloadPayload(payload);

                if (!response.ok) {
                    throw new Error(await parseErrorResponse(response));
                }
                setProgress(0);

                const contentType = response.headers.get("Content-Type") || "";
                if (contentType.includes("application/json")) {
                    const data = await response.json();
                    if (data.status === "routed" && data.route_type === "direct") {
                        try {
                            const filenames = await downloadDirectRouteItems(data.items || []);
                            setProgress(100);
                            setStatus(
                                getStatusElement(),
                                `${data.route_reason || "已使用浏览器直链下载"}：${filenames.join("、")}${routeWarningText(data.warnings)}`,
                                "text-success",
                            );
                        } catch (directError) {
                            if (directError && directError.name === "AbortError") {
                                throw directError;
                            }
                            if (!selectedSingleItem(selected)) {
                                throw directError;
                            }
                            console.warn("Direct download failed, falling back to server proxy", directError);
                            const fallback = await downloadServerSingleFallback(payload);
                            setProgress(100);
                            setStatus(
                                getStatusElement(),
                                `${serverDownloadStatusMessage(fallback)}（已自动切换服务器下载）`,
                                fallback.variant,
                            );
                        }
                        return;
                    }
                    if (data.status === "routed" && data.route_type === "proxy") {
                        setProgress(100);
                        setStatus(
                            getStatusElement(),
                            `${data.route_reason || "已生成代理下载建议"}${routeWarningText(data.warnings)}`,
                            "text-muted",
                        );
                        return;
                    }
                    setProgress(100);
                    setStatus(getStatusElement(), data.message || "服务端保存完成", "text-success");
                    return;
                }

                const result = await saveDownloadResponse(response, getBaseFilename(), 0, 100);
                setStatus(getStatusElement(), serverDownloadStatusMessage(result), result.variant);
            } catch (error) {
                if (error && error.name === "AbortError") {
                    setStatus(getStatusElement(), "已取消保存位置选择");
                } else {
                    console.error(error);
                    setStatus(getStatusElement(), error.message || "保存失败，请查看浏览器控制台或 Django 后台日志", "text-danger");
                }
            } finally {
                downloadInProgress = false;
                setDownloadButtonsBusy(false);
            }
        }

        function toggleAssetCard(card) {
            const item = getCardItem(card);
            const key = getItemKey(item);
            const exists = selected.some((selectedItem) => getItemKey(selectedItem) === key);

            if (exists) {
                selected = selected.filter((selectedItem) => getItemKey(selectedItem) !== key);
                card.classList.remove("selected");
            } else {
                selected.push(item);
                card.classList.add("selected");
            }

            updateSelectedStatus();
            updateSelectAllButton();
        }

        function toggleSelectAll() {
            const cards = getCards();
            const allSelected = selected.length === cards.length;
            selected = [];

            cards.forEach((card) => {
                if (allSelected) {
                    card.classList.remove("selected");
                } else {
                    card.classList.add("selected");
                    selected.push(getCardItem(card));
                }
            });

            updateSelectedStatus();
            updateSelectAllButton();
        }

        async function handleSearchSubmit(event) {
            if (!window.fetch || !window.AbortController || !resultsRegion) {
                return;
            }

            event.preventDefault();
            updateUltraHdRequestValue();
            updateYoutubeMergeRequestValue();

            if (!linkInput || !linkInput.value.trim()) {
                linkInput && linkInput.focus();
                return;
            }

            if (activeParseController) {
                activeParseController.abort();
            }

            const currentRequestId = parseRequestId + 1;
            parseRequestId = currentRequestId;
            activeParseController = new AbortController();
            const formData = new FormData(searchForm);
            const params = new URLSearchParams(formData);
            params.delete("csrfmiddlewaretoken");
            const requestUrl = `${searchForm.action}?${params.toString()}`;

            setParseLoading(true);
            showParseLoading();

            try {
                const response = await fetch(requestUrl, {
                    headers: {
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    cache: "no-store",
                    signal: activeParseController.signal,
                });
                const payload = await parseSearchResponse(response);

                if (currentRequestId !== parseRequestId) {
                    return;
                }
                if (!payload.html) {
                    throw new Error(payload.message || "解析失败，请稍后再试。");
                }

                applyParsePayload(payload);
            } catch (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
                console.error(error);
                if (currentRequestId === parseRequestId) {
                    showParseError(error.message || "解析失败，请稍后再试。");
                }
            } finally {
                if (currentRequestId === parseRequestId) {
                    activeParseController = null;
                    setParseLoading(false);
                }
            }
        }

        if (ultraHdSettingInput) {
            ultraHdSettingInput.checked = Boolean(savedSettings.ultra_hd_enabled) && !ultraHdSettingInput.disabled;
            updateUltraHdRequestValue();
        }

        if (youtubeMergeSettingInput) {
            youtubeMergeSettingInput.checked = Boolean(savedSettings.youtube_merge_enabled)
                && !youtubeMergeSettingInput.disabled;
            updateYoutubeMergeRequestValue();
        }

        if (optionExists(downloadModeInput, downloadMode)) {
            downloadModeInput.value = downloadMode;
        } else {
            downloadMode = "browser";
            if (downloadModeInput) {
                downloadModeInput.value = "browser";
            }
        }

        if (savedSettings.save_format && saveFormatInput) {
            saveFormatInput.value = savedSettings.save_format;
        }

        if (savedSettings.quality && qualityInput) {
            qualityInput.value = savedSettings.quality;
        }

        if (savedSettings.video_save_mode && videoSaveModeInput) {
            videoSaveModeInput.value = savedSettings.video_save_mode;
        }

        if (savedSettings.save_dir && saveDirInput) {
            saveDirInput.value = savedSettings.save_dir;
        }

        if (searchForm) {
            searchForm.addEventListener("submit", handleSearchSubmit);
        }

        if (resultsRegion) {
            resultsRegion.addEventListener("click", (event) => {
                const selectAllBtn = event.target.closest("#select-all-btn");
                if (selectAllBtn && resultsRegion.contains(selectAllBtn)) {
                    toggleSelectAll();
                    return;
                }

                const downloadBtn = event.target.closest("#download-btn");
                if (downloadBtn && resultsRegion.contains(downloadBtn)) {
                    handleDownloadClick(downloadBtn);
                    return;
                }

                const card = event.target.closest(".asset-card");
                if (card && resultsRegion.contains(card)) {
                    toggleAssetCard(card);
                }
            });
        }

        if (clearLinkBtn && linkInput) {
            clearLinkBtn.addEventListener("click", () => {
                linkInput.value = "";
                linkInput.focus();
                resetResults();
            });
        }

        if (pasteLinkIconBtn && linkInput) {
            pasteLinkIconBtn.addEventListener("click", async () => {
                try {
                    if (!navigator.clipboard || !navigator.clipboard.readText) {
                        linkInput.focus();
                        return;
                    }
                    const text = await navigator.clipboard.readText();
                    if (text) {
                        linkInput.value = text.trim();
                    }
                } catch (error) {
                    linkInput.focus();
                }
            });
        }

        if (downloadModeInput) {
            downloadModeInput.addEventListener("change", updateModeVisibility);
        }

        if (browserDefaultLocationBtn) {
            browserDefaultLocationBtn.addEventListener("click", () => {
                useFilePicker = false;
                updateDownloadLocationButtons();
            });
        }

        if (chooseDownloadLocationBtn) {
            chooseDownloadLocationBtn.addEventListener("click", () => {
                if (!window.showSaveFilePicker) {
                    return;
                }

                useFilePicker = true;
                updateDownloadLocationButtons();
            });
        }

        if (saveSettingsBtn) {
            saveSettingsBtn.addEventListener("click", () => {
                saveSettings({
                    download_mode: downloadModeInput ? downloadModeInput.value : "browser",
                    save_dir: saveDirInput ? saveDirInput.value.trim() : "xhs",
                    save_format: saveFormatInput ? saveFormatInput.value : "original",
                    quality: qualityInput ? qualityInput.value || 95 : 95,
                    video_save_mode: videoSaveModeInput ? videoSaveModeInput.value : "video",
                    use_file_picker: useFilePicker,
                    ultra_hd_enabled: isUltraHdEnabled(),
                    youtube_merge_enabled: isYoutubeMergeEnabled(),
                });
                updateUltraHdRequestValue();
                updateYoutubeMergeRequestValue();

                setStatus(getStatusElement(), "设置已保存", "text-success");
            });
        }

        updateDownloadLocationButtons();
        updateModeVisibility();
        updateSelectAllButton();
    });
})();
