(function () {
    const SETTINGS_KEY = "xhsDownloadSettings";
    const MAX_IMAGE_RETRIES = 3;
    const ordinaryMediaDownloadApi = window.AnimeImagesOrdinaryMediaDownload || null;
    const browserPhotoApi = window.AnimeImagesBrowserPhoto || null;
    const downloadReliabilityApi = window.AnimeImagesDownloadReliability || null;
    const douyinReliabilityApi = window.AnimeImagesDouyinReliability || null;
    const resultUrlSafetyApi = window.AnimeImagesResultUrlSafety || null;

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
            const settings = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "{}");
            return settings && typeof settings === "object" && !Array.isArray(settings)
                ? settings
                : {};
        } catch (error) {
            console.warn("Invalid XHS download settings; using page defaults.");
            return {};
        }
    }

    function retryImage(img) {
        const card = img.closest(".asset-card");
        let external = true;
        try {
            external = new URL(img.src, window.location.origin).origin !== window.location.origin;
        } catch (_error) {
            // An invalid preview URL cannot be retried safely.
        }
        if (external || img.getAttribute("referrerpolicy") === "no-referrer"
            || (card && (card.dataset.sourcePlatform === "upstream"
                || String(card.dataset.assetId || "").startsWith("upstream:")))) {
            if (img.dataset.previewFailed === "true") {
                return;
            }
            img.dataset.previewFailed = "true";
            img.onerror = null;
            img.hidden = true;
            img.style.display = "none";
            const status = img.ownerDocument.createElement("span");
            status.className = "text-muted";
            status.setAttribute("role", "status");
            Object.assign(status.style, {
                display: "grid", placeItems: "center", width: "100%", height: "220px",
                boxSizing: "border-box", padding: "48px 16px 24px", textAlign: "center",
            });
            status.textContent = "预览加载失败";
            img.insertAdjacentElement("afterend", status);
            return;
        }
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

    if (!window.retryImage) window.retryImage = retryImage;

    async function openUpstreamImage(button, requestRoute) {
        const card = button.closest(".asset-card");
        const status = card.querySelector("[data-upstream-image-link-status]");
        const item = getCardItem(card);
        const popup = window.open("about:blank", "_blank");
        if (!popup) {
            status.textContent = "浏览器阻止了新窗口，请允许弹窗后重试。";
            return;
        }
        popup.opener = null;
        const referrer = popup.document.createElement("meta");
        referrer.name = "referrer";
        referrer.content = "no-referrer";
        popup.document.head.appendChild(referrer);
        button.disabled = true;
        status.textContent = "正在验证链接授权...";
        try {
            const route = await requestRoute({media_items: [item], save_format: "original", base_filename: "image",
                intent: "resource_link"});
            const url = browserPhotoApi.validatedUpstreamImageUrl(item, route);
            popup.location.replace(url);
            status.textContent = "已打开直链；这是独立的页面访问，不代表已保存，也不保证可保存。";
        } catch (_error) {
            popup.close();
            status.textContent = "链接授权不可用或已失效，请重新解析后再试。";
        } finally {
            button.disabled = false;
        }
    }

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
        const logicalType = card.dataset.logicalType || card.dataset.assetKind || type;
        const sizeBytes = Number.parseInt(card.dataset.sizeBytes || "0", 10) || 0;
        const providerFields = {
            ...(card.dataset.downloadFilename ? {filename: card.dataset.downloadFilename} : {}),
            source_platform: card.dataset.sourcePlatform || "",
            asset_id: card.dataset.assetId || "",
            provider: card.dataset.provider || "",
            provider_resource_id: card.dataset.providerResourceId || "",
            provider_cache_key: card.dataset.providerCacheKey || "",
            provider_manifest_token: card.dataset.providerManifestToken || "",
            download_strategy: card.dataset.downloadStrategy || "",
            fallback_strategy: card.dataset.fallbackStrategy || "",
            cors_expected: card.dataset.corsExpected === "true",
            browser_direct_fetch_safe: card.dataset.browserDirectFetchSafe === "true",
            size_is_estimate: card.dataset.sizeIsEstimate === "true",
            requires_merge: card.dataset.requiresMerge === "true",
            requires_cookie: card.dataset.requiresCookie === "true",
        };

        if (type === "video") {
            // A live photo's motion carries its photo's number.
            const displayOrdinal = Number.parseInt(card.dataset.displayOrdinal || "0", 10) || 0;
            return {
                type: "video",
                logical_type: "video",
                url: card.dataset.url,
                cover_url: card.dataset.coverUrl || "",
                size_bytes: sizeBytes,
                ...(displayOrdinal ? {display_ordinal: displayOrdinal} : {}),
                ...providerFields,
            };
        }

        if (logicalType === "livephoto") {
            return {
                type: "livephoto",
                logical_type: "livephoto",
                asset_id: card.dataset.assetId || "",
                pair_token: card.dataset.livephotoPairToken || "",
                descriptor_url: card.dataset.livephotoDescriptorUrl || "",
                display_ordinal: Number.parseInt(card.dataset.displayOrdinal || "0", 10) || 0,
                filenameStem: card.dataset.downloadFilenameStem || "",
                source_platform: card.dataset.sourcePlatform || "",
                requires_merge: false,
                requires_cookie: false,
            };
        }

        return {
            type: "image",
            logical_type: "image",
            url: card.dataset.url,
            size_bytes: sizeBytes,
            display_ordinal: Number.parseInt(card.dataset.displayOrdinal || "0", 10) || 0,
            filenameStem: card.dataset.downloadFilenameStem || "",
            extensionHint: card.dataset.downloadExtensionHint || "",
            ...providerFields,
        };
    }

    function getItemKey(item) {
        if (item && item.logical_type === "livephoto") {
            return `livephoto:${item.asset_id || "invalid"}`;
        }
        return `${item.type}:${item.url}`;
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

    function downloadOperationCurrent(operation) {
        return !operation || (!(operation.signal && operation.signal.aborted)
            && (!operation.isCurrent || operation.isCurrent()));
    }

    function assertDownloadCurrent(operation) {
        if (!downloadOperationCurrent(operation)) {
            const error = new Error("Download operation is no longer current");
            error.name = "AbortError";
            throw error;
        }
    }

    async function saveZipBlob(blob, filename, useFilePicker, operation) {
        assertDownloadCurrent(operation);
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
            assertDownloadCurrent(operation);
            const writable = await handle.createWritable();
            try {
                assertDownloadCurrent(operation);
                await writable.write(blob);
                assertDownloadCurrent(operation);
                await writable.close();
            } catch (error) {
                if (typeof writable.abort === "function") await writable.abort().catch(() => {});
                throw error;
            }
            assertDownloadCurrent(operation);
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

    function itemRequiresServerProcessing(item) {
        const provider = item ? String(item.provider || "") : "";
        return ["kukutool_generic", "youtube_merge", "yt_dlp_merge"].includes(provider);
    }

    function selectedNeedsZipTask(items) {
        const selectedItems = items || [];
        return selectedItems.length > 1 || selectedItems.some(itemRequiresZipTask);
    }

    function selectedRequiresProviderProcessing(items) {
        return (items || []).some(itemRequiresServerProcessing);
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
        const batchPage = document.querySelector(".batch-parse-page");
        const homePage = batchPage ? null : document.querySelector(".xhs-download-page");
        const page = homePage || batchPage;

        if (!page) {
            return;
        }

        const searchForm = document.getElementById("hero-search-form");
        const resultsRegion = homePage ? document.getElementById("parse-results-region") : page;
        const pageHeading = document.getElementById("page-heading");
        const pageDescription = document.getElementById("page-description");
        const linkLabel = document.getElementById("media-link-label");
        const linkInput = document.getElementById("media-link-input");
        const pasteLinkIconBtn = document.getElementById("paste-link-icon-btn");
        const clearLinkBtn = document.getElementById("clear-link-btn");
        const ultraHdRequestInput = document.getElementById("ultra-hd-request-input");
        const youtubeMergeRequestInput = document.getElementById("youtube-merge-request-input");
        const downloadLocationSettingAvailable = (
            page.dataset.downloadLocationSettingAvailable === "true"
        );
        const downloadUrl = page.dataset.downloadUrl || page.dataset.batchDownloadUrl;
        const downloadPrepareUrl = page.dataset.downloadPrepareUrl;
        const douyinContinuationUrl = page.dataset.douyinContinuationUrl;
        const downloadTaskUrl = page.dataset.downloadTaskUrl || page.dataset.batchDownloadTaskUrl;
        const downloadAsyncEnabled = (page.dataset.downloadAsyncEnabled || page.dataset.batchDownloadAsyncEnabled) === "true";
        const showInternalDiagnostics = page.dataset.showInternalDiagnostics === "true";
        const presentationText = (publicText, diagnosticText) => (
            showInternalDiagnostics ? diagnosticText : publicText
        );
        if (resultUrlSafetyApi && searchForm) {
            resultUrlSafetyApi.scrubServerRenderedResultUrl({
                locationObject: window.location,
                historyObject: window.history,
                homeUrl: page.dataset.pageHomeUrl,
                resultsUrl: searchForm.action,
            });
        }
        const savedSettings = loadSettings();
        const projectSettingsApi = window.AnimeImagesProjectSettings
            || window.AnimeImagesParsePreferences
            || null;
        const renderedProjectDefaults = {
            parsing: {
                ultra_hd: ultraHdRequestInput
                    ? ultraHdRequestInput.value === "1"
                    : true,
                youtube_merge: youtubeMergeRequestInput
                    ? youtubeMergeRequestInput.value === "1"
                    : false,
            },
            download: {
                location_mode: downloadLocationSettingAvailable
                    && typeof savedSettings.use_file_picker === "boolean"
                    && savedSettings.use_file_picker
                    ? "ask_when_supported"
                    : "browser_default",
            },
        };
        let projectSettings = projectSettingsApi
            ? projectSettingsApi.loadProjectSettings({defaults: renderedProjectDefaults})
            : {version: 2, ...renderedProjectDefaults};

        let selected = [];
        let useFilePicker = (
            downloadLocationSettingAvailable
            && projectSettings.download.location_mode === "ask_when_supported"
        );
        let activeParseController = null;
        let parseRequestId = 0;
        let resultGeneration = 0;
        let downloadInProgress = false;
        let pickerDownloadInProgress = false;
        let legacyPickerRoot = null;
        let activePhotoController = null;
        let pendingPhotoServerFallback = false;
        let pendingProjectSettings = null;
        let lastDownloadProgress = 0;
        let ultraRetryDeadline = 0;
        let ultraRetryTimer = null;
        let ultraRetrySource = "";
        let livePhotoPairScriptPromise = null;
        let livePhotoScriptPromise = null;

        function getBaseFilename() {
            return page.dataset.baseFilename || "xhs_image";
        }

        function getCards() {
            return Array.from(resultsRegion ? resultsRegion.querySelectorAll(".asset-card") : [])
                .filter(card => !card.closest || !card.closest("[data-ytdlp-picker], [data-picker-preview-only]"));
        }

        function getPickerMedia() {
            const api = window.DokiToolYtdlpPicker;
            return api && resultsRegion ? api.mediaRoots(resultsRegion) : [];
        }

        function selectedPickerCount() {
            const api = window.DokiToolYtdlpPicker;
            return api ? getPickerMedia().filter(api.mediaSelected).length : 0;
        }

        function getSelectableCards() {
            return getCards().filter((card) => card.dataset.downloadSelectable !== "false");
        }

        function initializeLivePhotoPreview() {
            const api = window.AnimeImagesXhsLivePhotoPreview;
            if (!api || typeof api.ensureController !== "function") {
                return false;
            }
            return Boolean(api.ensureController(document));
        }

        function initializeLivePhotoPair() {
            const api = window.AnimeImagesXhsLivePhotoPair;
            if (!api || typeof api.ensureController !== "function") {
                return false;
            }
            return Boolean(api.ensureController(document));
        }

        function cancelLivePhotoActivity() {
            const previewApi = window.AnimeImagesXhsLivePhotoPreview;
            if (previewApi && typeof previewApi.cancelActive === "function") {
                previewApi.cancelActive();
            }
            const pairApi = window.AnimeImagesXhsLivePhotoPair;
            if (pairApi && typeof pairApi.cancelAll === "function") {
                pairApi.cancelAll();
            }
            if (activePhotoController) {
                activePhotoController.abort();
                activePhotoController = null;
            }
        }

        function safeLivePhotoScriptUrl(rawUrl, expectedPath) {
            try {
                const scriptUrl = new URL(rawUrl || "", window.location.origin);
                return (
                    scriptUrl.origin === window.location.origin
                    && scriptUrl.pathname.endsWith(expectedPath)
                ) ? scriptUrl : null;
            } catch (error) {
                return null;
            }
        }

        function ensureLivePhotoPair(previewRoot) {
            if (initializeLivePhotoPair()) {
                return Promise.resolve(true);
            }
            if (livePhotoPairScriptPromise) {
                return livePhotoPairScriptPromise;
            }
            const scriptUrl = safeLivePhotoScriptUrl(
                previewRoot.dataset.pairScriptUrl,
                "/js/xhs_livephoto_pair_download.js",
            );
            if (!scriptUrl) {
                return Promise.resolve(false);
            }
            const existingScript = document.querySelector("script[data-livephoto-pair-loader]");
            if (existingScript) {
                return Promise.resolve(initializeLivePhotoPair());
            }
            livePhotoPairScriptPromise = new Promise((resolve) => {
                const script = document.createElement("script");
                script.src = scriptUrl.toString();
                script.async = true;
                script.dataset.livephotoPairLoader = "true";
                script.addEventListener("load", () => {
                    resolve(initializeLivePhotoPair());
                }, {once: true});
                script.addEventListener("error", () => {
                    livePhotoPairScriptPromise = null;
                    resolve(false);
                }, {once: true});
                document.head.appendChild(script);
            });
            return livePhotoPairScriptPromise;
        }

        async function ensureLivePhotoPreview() {
            const previewRoot = resultsRegion
                ? resultsRegion.querySelector("[data-xhs-livephoto-preview]")
                : null;
            if (!previewRoot) {
                return false;
            }
            const pairReady = await ensureLivePhotoPair(previewRoot);
            if (!pairReady) {
                return false;
            }
            if (!previewRoot.dataset.previewScriptUrl) {
                return true;
            }
            if (initializeLivePhotoPreview()) {
                return true;
            }
            if (livePhotoScriptPromise) {
                return livePhotoScriptPromise;
            }

            const scriptUrl = safeLivePhotoScriptUrl(
                previewRoot.dataset.previewScriptUrl,
                "/js/xhs_livephoto_preview.js",
            );
            if (!scriptUrl) {
                return false;
            }

            const existingScript = document.querySelector("script[data-livephoto-loader]");
            if (existingScript) {
                return Promise.resolve(initializeLivePhotoPreview());
            }

            livePhotoScriptPromise = new Promise((resolve) => {
                const script = document.createElement("script");
                script.src = scriptUrl.toString();
                script.async = true;
                script.dataset.livephotoLoader = "true";
                script.addEventListener("load", () => {
                    resolve(initializeLivePhotoPreview());
                }, {once: true});
                script.addEventListener("error", () => {
                    livePhotoScriptPromise = null;
                    resolve(false);
                }, {once: true});
                document.head.appendChild(script);
            });
            return livePhotoScriptPromise;
        }

        function getStatusElement() {
            if (!homePage && legacyPickerRoot) return legacyPickerRoot.querySelector("[data-picker-status]");
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
                progressItems: resultsRegion ? resultsRegion.querySelector("#download-progress-items") : null,
                progressBytes: resultsRegion ? resultsRegion.querySelector("#download-progress-bytes") : null,
                progressSpeed: resultsRegion ? resultsRegion.querySelector("#download-progress-speed") : null,
                progressEta: resultsRegion ? resultsRegion.querySelector("#download-progress-eta") : null,
                blobWarning: progressWrap ? progressWrap.querySelector("[data-blob-download-warning]") : null,
                photoCancel: resultsRegion ? resultsRegion.querySelector("[data-photo-download-cancel]") : null,
                photoServerFallback: resultsRegion ? resultsRegion.querySelector("[data-photo-server-fallback]") : null,
            };
        }

        function setPhotoActionVisibility(options) {
            const settings = options || {};
            const {photoCancel, photoServerFallback} = getProgressElements();
            if (photoCancel) {
                photoCancel.hidden = !settings.cancel;
            }
            if (photoServerFallback) {
                photoServerFallback.hidden = !settings.serverFallback;
            }
        }

        function clearPhotoServerFallback() {
            pendingPhotoServerFallback = false;
            setPhotoActionVisibility({cancel: Boolean(activePhotoController)});
        }

        function isUltraHdEnabled() {
            return Boolean(
                page.dataset.ultraHdAvailable === "true"
                && ultraHdRequestInput
                && ultraHdRequestInput.value === "1"
            );
        }

        function isYoutubeMergeEnabled() {
            return Boolean(
                page.dataset.youtubeMergeAvailable === "true"
                && youtubeMergeRequestInput
                && youtubeMergeRequestInput.value === "1"
            );
        }

        function applyProjectSettings(settings) {
            projectSettings = settings;
            const parsing = settings && settings.parsing ? settings.parsing : {};
            if (ultraHdRequestInput) {
                ultraHdRequestInput.value = (
                    page.dataset.ultraHdAvailable === "true"
                    && parsing.ultra_hd
                ) ? "1" : "0";
            }
            if (youtubeMergeRequestInput) {
                youtubeMergeRequestInput.value = (
                    page.dataset.youtubeMergeAvailable === "true"
                    && parsing.youtube_merge
                ) ? "1" : "0";
            }
            useFilePicker = Boolean(
                downloadLocationSettingAvailable
                && settings
                && settings.download
                && settings.download.location_mode === "ask_when_supported"
            );
            if (!isUltraHdEnabled() && ultraRetryDeadline) {
                startUltraRetryCountdown(0);
            }
        }

        function queueOrApplyProjectSettings(settings) {
            if (!activeParseController && !downloadInProgress) {
                applyProjectSettings(settings);
            } else {
                pendingProjectSettings = settings;
            }
        }

        function applyPendingProjectSettings() {
            if (pendingProjectSettings && !activeParseController && !downloadInProgress) {
                const pending = pendingProjectSettings;
                pendingProjectSettings = null;
                applyProjectSettings(pending);
            }
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
            setStatus(getStatusElement(), `已选择 ${selected.length + selectedPickerCount()} 个资源`);
        }

        function updateSelectAllButton() {
            const selectAllBtn = getSelectAllButton();
            const cards = getSelectableCards();
            if (!selectAllBtn) {
                return;
            }

            const media = getPickerMedia();
            const allSelected = cards.length + media.length > 0
                && selected.length === cards.length && selectedPickerCount() === media.length;
            selectAllBtn.innerHTML = allSelected
                ? '<i class="bi bi-x-square me-1"></i>取消全选'
                : '<i class="bi bi-check2-square me-1"></i>全选';
        }

        function setDownloadButtonsBusy(isBusy, activeButton, label, controls) {
            if (!isBusy && pickerDownloadInProgress) return;
            (controls ? controls.buttons : getDownloadActionButtons()).forEach((button) => {
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
            (controls ? controls.cards : getCards()).forEach((card) => {
                const playbackButton = card.querySelector("[data-livephoto-toggle]");
                if (!playbackButton) {
                    return;
                }
                if (isBusy) {
                    if (!playbackButton.hasAttribute("data-download-busy-previous-disabled")) {
                        playbackButton.dataset.downloadBusyPreviousDisabled = playbackButton.disabled
                            ? "true"
                            : "false";
                    }
                    playbackButton.disabled = true;
                    playbackButton.setAttribute("aria-disabled", "true");
                    return;
                }
                if (playbackButton.hasAttribute("data-download-busy-previous-disabled")) {
                    playbackButton.disabled = (
                        playbackButton.dataset.downloadBusyPreviousDisabled === "true"
                    );
                    delete playbackButton.dataset.downloadBusyPreviousDisabled;
                }
                if (!playbackButton.disabled) {
                    playbackButton.removeAttribute("aria-disabled");
                }
            });
        }

        function resetDownloadProgressBaseline() {
            lastDownloadProgress = 0;
        }

        function hideDownloadProgress() {
            const {
                progressWrap,
                progressTrack,
                progressBar,
                progressPercent,
                progressItems,
                progressBytes,
                progressSpeed,
                progressEta,
                blobWarning,
            } = getProgressElements();
            if (progressWrap) {
                progressWrap.hidden = true;
                progressWrap.classList.remove("is-pending");
            }
            if (progressTrack) {
                progressTrack.setAttribute("aria-valuenow", "0");
                progressTrack.setAttribute("aria-valuetext", "0%");
            }
            if (progressBar) {
                progressBar.style.width = "0%";
            }
            if (progressPercent) {
                progressPercent.innerText = "0%";
            }
            if (progressBytes) {
                progressBytes.innerText = "0 B";
            }
            if (progressItems) {
                progressItems.innerText = "";
            }
            if (progressSpeed) {
                progressSpeed.innerText = "正在测速...";
            }
            if (progressEta) {
                progressEta.innerText = "";
            }
            if (blobWarning) {
                blobWarning.hidden = true;
            }
            lastDownloadProgress = 0;
            setPhotoActionVisibility({});
        }

        function setProgress(percent, options) {
            const rawValue = Math.max(0, Math.min(100, Math.round(percent || 0)));
            const allowDecrease = Boolean(options && options.allowDecrease);
            const value = allowDecrease ? rawValue : Math.max(rawValue, lastDownloadProgress);
            lastDownloadProgress = value;
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

        function applyOrdinaryDownloadProgress(event) {
            if (!ordinaryMediaDownloadApi || !event) {
                return;
            }
            const {
                progressWrap,
                progressTrack,
                progressBar,
                progressPercent,
                progressBytes,
                progressSpeed,
                progressEta,
                blobWarning,
            } = getProgressElements();
            const knownTotal = Number(event.totalBytes || 0) > 0;
            const completed = event.stage === "saving" || event.stage === "completed";
            if (completed) {
                setProgress(100);
            } else if (knownTotal && event.percent !== null) {
                setProgress(event.percent);
            } else if (event.stage === "fetching") {
                setProgressPending("下载中");
            }
            if (progressWrap) {
                progressWrap.hidden = false;
            }
            if (progressTrack && !knownTotal) {
                progressTrack.removeAttribute("aria-valuenow");
                progressTrack.setAttribute("aria-valuetext", "正在下载，文件大小未知");
            }
            if (progressBar && completed) {
                progressBar.style.width = "100%";
            }
            if (progressPercent && !knownTotal) {
                progressPercent.innerText = completed ? "100%" : "下载中";
            }
            if (progressBytes) {
                const received = ordinaryMediaDownloadApi.formatBytes(event.receivedBytes);
                progressBytes.innerText = knownTotal
                    ? `${received} / ${ordinaryMediaDownloadApi.formatBytes(event.totalBytes)}`
                    : `已接收 ${received}`;
            }
            if (progressSpeed) {
                progressSpeed.innerText = ordinaryMediaDownloadApi.formatRate(event.bytesPerSecond);
            }
            if (progressEta) {
                progressEta.innerText = event.etaSeconds
                    ? ordinaryMediaDownloadApi.formatEta(event.etaSeconds)
                    : (event.stage === "fetching" ? "正在测速..." : "");
            }
            if (blobWarning) {
                blobWarning.hidden = event.transport !== "browser_blob";
            }
        }

        function selectedSupportsBrowserPhoto(items, saveFormat) {
            const sourceItems = Array.isArray(items) ? items : [];
            const effectiveSaveFormat = sourceItems.some(
                (item) => item && item.logical_type === "livephoto",
            ) ? "original" : saveFormat;
            if (browserPhotoApi && typeof browserPhotoApi.supportsSelection === "function") {
                return browserPhotoApi.supportsSelection(sourceItems, effectiveSaveFormat);
            }
            return Boolean(
                sourceItems.length
                && String(effectiveSaveFormat || "original") === "original"
                && sourceItems.every((item) => (
                    item
                    && (
                        (item.logical_type === "image" && item.url)
                        || (
                            item.logical_type === "livephoto"
                            && item.asset_id
                            && item.pair_token
                            && item.descriptor_url
                        )
                        || (
                            item.logical_type === "video"
                            && sourceItems.length > 1
                            && item.source_platform === "xiaohongshu"
                            && item.browser_direct_fetch_safe === true
                            && item.url
                        )
                    )
                    && !item.requires_cookie
                    && !item.requires_merge
                ))
            );
        }

        function selectionRequiresZeroRelay(items) {
            const sourceItems = Array.isArray(items) ? items : [];
            return Boolean(
                sourceItems.length
                && sourceItems.every((item) => (
                    item && (
                        ["image", "livephoto"].includes(item.logical_type || item.type)
                        || ((item.logical_type || item.type) === "video" && item.source_platform === "xiaohongshu")
                    )
                ))
                && sourceItems.some((item) => (
                    item.logical_type === "livephoto"
                    || item.type === "livephoto"
                    || item.source_platform === "xiaohongshu"
                    || item.provider === "parsehub_xhs_images"
                    || String(item.asset_id || "").startsWith("upstream:")
                ))
            );
        }

        function applyPhotoDownloadProgress(event) {
            if (!browserPhotoApi || !event) {
                return;
            }
            const {
                progressWrap,
                progressTrack,
                progressBar,
                progressPercent,
                progressItems,
                progressBytes,
                progressSpeed,
                progressEta,
                blobWarning,
            } = getProgressElements();
            const completed = event.stage === "saving" || event.stage === "completed";
            const percent = completed
                ? 100
                : (event.totalKnown && event.percent !== null ? event.percent : null);
            if (progressWrap) {
                progressWrap.hidden = false;
                progressWrap.classList.toggle("is-pending", percent === null);
            }
            if (progressTrack) {
                if (percent === null) {
                    progressTrack.removeAttribute("aria-valuenow");
                    progressTrack.setAttribute("aria-valuetext", "正在下载图片，文件总量未知");
                } else {
                    progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
                    progressTrack.setAttribute("aria-valuetext", `${Math.round(percent)}%`);
                }
            }
            if (progressBar) {
                progressBar.style.width = percent === null ? "100%" : `${percent}%`;
            }
            if (progressPercent) {
                progressPercent.innerText = percent === null ? "下载中" : `${Math.round(percent)}%`;
            }
            if (progressItems) {
                progressItems.innerText = `${event.completedItems} / ${event.totalItems}`;
            }
            if (progressBytes) {
                progressBytes.innerText = event.totalKnown
                    ? `${browserPhotoApi.formatBytes(event.receivedBytes)} / ${browserPhotoApi.formatBytes(event.totalBytes)}`
                    : `已下载 ${browserPhotoApi.formatBytes(event.receivedBytes)}`;
            }
            if (progressSpeed) {
                progressSpeed.innerText = browserPhotoApi.formatRate(event.bytesPerSecond);
            }
            if (progressEta) {
                progressEta.innerText = event.etaSeconds
                    ? browserPhotoApi.formatEta(event.etaSeconds)
                    : "";
            }
            if (blobWarning) {
                blobWarning.hidden = false;
                blobWarning.innerText = "浏览器正在下载并打包图片，完成前请勿刷新或关闭本页。";
            }
            const stageMessages = {
                connecting: "正在连接素材资源...",
                resolving_livephoto: "正在准备实况素材...",
                fetching_still: "正在获取实况原图...",
                fetching_motion: "正在获取实况视频...",
                fetching: event.totalItems > 1
                    ? `浏览器正在处理素材 ${event.completedItems} / ${event.totalItems}`
                    : "浏览器正在获取素材...",
                assembling: "素材已获取完成，正在生成 ZIP...",
                saving: "正在交给浏览器保存...",
                completed: "已开始保存。",
            };
            if (stageMessages[event.stage]) {
                setStatus(getStatusElement(), stageMessages[event.stage]);
            }
        }

        async function requestSelectedLivePhotoDescriptor(item, signal) {
            await ensureLivePhotoPreview();
            const pairApi = window.AnimeImagesXhsLivePhotoPair;
            if (!pairApi || typeof pairApi.requestPairDescriptor !== "function") {
                const error = new Error("实况资源服务暂时不可用，请刷新页面后重试。");
                error.errorCode = "XHS_LIVEPHOTO_PAIR_UNAVAILABLE";
                throw error;
            }
            return pairApi.requestPairDescriptor(
                item.pair_token,
                item.descriptor_url,
                {
                    csrfToken: getCookie("csrftoken"),
                    signal,
                },
            );
        }

        function logicalDownloadErrorMessage(error, includesLivePhoto) {
            const errorCode = browserPhotoApi.safeErrorCode(error);
            if (
                errorCode === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE"
                || errorCode === "BROWSER_PHOTO_ITEM_TOO_LARGE"
                || errorCode === "BROWSER_LIVEPHOTO_PAIR_TOO_LARGE"
            ) {
                return "选中的素材总量过大，暂无法在浏览器中打包。";
            }
            if (includesLivePhoto) {
                const pairApi = window.AnimeImagesXhsLivePhotoPair;
                if (pairApi && typeof pairApi.safeErrorMessage === "function") {
                    return pairApi.safeErrorMessage(error, browserPhotoApi);
                }
            }
            return browserPhotoApi.safeErrorMessage(error);
        }

        async function downloadSelectedPhotos(payload) {
            const items = (payload.browserRouteItems || payload.media_items || selected)
                .map((item, index) => ({
                    ...item,
                    filenameStem: item.filenameStem || `${payload.base_filename || getBaseFilename()}_${String(index + 1).padStart(2, "0")}`,
                    display_ordinal: item.display_ordinal || index + 1,
                }))
                .sort((left, right) => left.display_ordinal - right.display_ordinal);
            const livePhotoItems = items.filter((item) => item.logical_type === "livephoto");
            const includesLivePhoto = livePhotoItems.length > 0;
            const includesVideo = items.some((item) => item.logical_type === "video");
            const zeroRelay = selectionRequiresZeroRelay(items);
            activePhotoController = new AbortController();
            const previewApi = window.AnimeImagesXhsLivePhotoPreview;
            if (previewApi && typeof previewApi.cancelActive === "function") {
                previewApi.cancelActive();
            }
            clearPhotoServerFallback();
            setPhotoActionVisibility({cancel: true});
            try {
                if (items.length === 1) {
                    let result;
                    if (items[0].logical_type === "livephoto") {
                        const descriptor = await requestSelectedLivePhotoDescriptor(
                            items[0],
                            activePhotoController.signal,
                        );
                        result = await browserPhotoApi.createLivePhotoPairArchive(descriptor, {
                            signal: activePhotoController.signal,
                            onProgress(snapshot) {
                                applyPhotoDownloadProgress({
                                    ...snapshot,
                                    totalItems: 1,
                                    completedItems: ["saving", "completed"].includes(snapshot.stage) ? 1 : 0,
                                });
                            },
                        });
                    } else {
                        result = await browserPhotoApi.downloadImage(items[0], {
                            filenameStem: items[0].filenameStem,
                            signal: activePhotoController.signal,
                            onProgress: applyPhotoDownloadProgress,
                        });
                    }
                    setStatus(
                        getStatusElement(),
                        `已交给浏览器保存：${result.filename}`,
                        "text-success",
                    );
                    return;
                }
                const result = includesLivePhoto || includesVideo
                    ? await browserPhotoApi.createLogicalSelectionArchive(items, {
                        baseFilename: payload.base_filename,
                        signal: activePhotoController.signal,
                        onProgress: applyPhotoDownloadProgress,
                        resolveLivePhoto: (item, options) => (
                            requestSelectedLivePhotoDescriptor(item, options.signal)
                        ),
                    })
                    : await browserPhotoApi.createArchive(items, {
                        baseFilename: payload.base_filename,
                        signal: activePhotoController.signal,
                        onProgress: applyPhotoDownloadProgress,
                    });
                const failureNote = result.failureCount
                    ? `，${result.failureCount} 张未能加入，详情见 failed.txt`
                    : "";
                setStatus(
                    getStatusElement(),
                    `已交给浏览器保存：${result.filename}${failureNote}`,
                    result.failureCount ? "text-muted" : "text-success",
                );
            } catch (error) {
                const errorCode = browserPhotoApi.safeErrorCode(error);
                const cancelled = errorCode === "BROWSER_PHOTO_CANCELLED";
                hideDownloadProgress();
                const progress = getProgressElements();
                for (const node of [progress.progressPercent, progress.progressSpeed, progress.progressEta, progress.blobWarning]) {
                    if (node) node.innerText = "";
                }
                if (!cancelled && items.length > 1 && !zeroRelay) {
                    pendingPhotoServerFallback = true;
                    setPhotoActionVisibility({serverFallback: true});
                    setStatus(
                        getStatusElement(),
                        `${browserPhotoApi.safeErrorMessage(error)} 可明确选择“使用服务器打包”。`,
                        "text-danger",
                    );
                    return;
                }
                if (!cancelled && zeroRelay) {
                    setStatus(
                        getStatusElement(),
                        items.some((item) => String(item.asset_id || "").startsWith("upstream:"))
                            ? browserPhotoApi.safeErrorMessage(error, {upstream: true})
                            : logicalDownloadErrorMessage(error, includesLivePhoto),
                        "text-danger",
                    );
                    return;
                }
                setStatus(
                    getStatusElement(),
                    browserPhotoApi.safeErrorMessage(error),
                    cancelled ? "text-muted" : "text-danger",
                );
            } finally {
                activePhotoController = null;
                setPhotoActionVisibility({serverFallback: pendingPhotoServerFallback});
            }
        }

        function setParseLoading(isLoading) {
            const submitBtn = document.getElementById("parse-submit-btn");
            const idleIcon = submitBtn ? submitBtn.querySelector(".hero-submit-idle") : null;
            const spinner = submitBtn ? submitBtn.querySelector(".hero-submit-spinner") : null;
            const label = submitBtn ? submitBtn.querySelector(".hero-submit-label") : null;

            const retrySeconds = douyinReliabilityApi
                ? douyinReliabilityApi.scopedRetrySeconds({
                    ultraEnabled: isUltraHdEnabled(),
                    currentSource: linkInput ? linkInput.value : "",
                    retrySource: ultraRetrySource,
                    deadlineMs: ultraRetryDeadline,
                })
                : 0;
            const retryApplies = Boolean(
                ultraRetrySource
                && String(linkInput && linkInput.value || "").trim()
                    === ultraRetrySource
                && isUltraHdEnabled()
            );
            if (submitBtn) {
                submitBtn.disabled = isLoading || retrySeconds > 0;
                submitBtn.classList.toggle("is-loading", isLoading);
            }
            if (idleIcon) {
                idleIcon.hidden = isLoading;
            }
            if (spinner) {
                spinner.hidden = !isLoading;
            }
            if (label) {
                label.innerText = isLoading
                    ? (isUltraHdEnabled() ? "正在获取超高清资源…" : "解析中")
                    : (retrySeconds > 0 && douyinReliabilityApi
                        ? douyinReliabilityApi.retryButtonLabel(retrySeconds)
                        : (retryApplies ? "重新尝试超高清" : "获取素材"));
            }
        }

        function startUltraRetryCountdown(value, source = "") {
            const seconds = douyinReliabilityApi
                ? douyinReliabilityApi.normalizeRetryAfterSeconds(value)
                : Math.max(0, Number.parseInt(value || 0, 10));
            if (ultraRetryTimer) {
                window.clearInterval(ultraRetryTimer);
                ultraRetryTimer = null;
            }
            if (!seconds) {
                ultraRetryDeadline = 0;
                ultraRetrySource = "";
                setParseLoading(false);
                return;
            }
            ultraRetryDeadline = Date.now() + (seconds * 1000);
            ultraRetrySource = String(source || (linkInput && linkInput.value) || "").trim();
            setParseLoading(false);
            ultraRetryTimer = window.setInterval(() => {
                if (Date.now() >= ultraRetryDeadline) {
                    window.clearInterval(ultraRetryTimer);
                    ultraRetryTimer = null;
                }
                setParseLoading(false);
            }, 250);
        }

        function applyContinuationNotice(payload, retrySource = "") {
            if (!resultsRegion || !payload) {
                return;
            }
            let message = String(payload.continuation_message || "");
            const retrySeconds = douyinReliabilityApi
                ? douyinReliabilityApi.normalizeRetryAfterSeconds(
                    payload.retry_after_seconds,
                )
                : 0;
            if (message && retrySeconds) {
                message = `${message} 约 ${retrySeconds} 秒后可重新尝试超高清。`;
            }
            const notice = resultsRegion.querySelector("[data-provider-notice-text]");
            if (message && notice) {
                notice.textContent = message;
            }
            if (retrySeconds) {
                startUltraRetryCountdown(retrySeconds, retrySource);
            } else if (
                payload.status === "success"
                && String(retrySource || "").trim() === ultraRetrySource
            ) {
                startUltraRetryCountdown(0);
            }
        }

        function showParseLoading() {
            resultGeneration += 1;
            if (!resultsRegion) {
                return;
            }
            selected = [];
            pendingPhotoServerFallback = false;
            cancelLivePhotoActivity();
            setPageHasResults(true);
            resultsRegion.innerHTML = `
                <div class="parse-loading-card">
                    <div class="spinner-border" role="status" aria-hidden="true"></div>
                    <div>
                        <div class="parse-loading-title">${isUltraHdEnabled() ? "正在获取超高清资源…" : "正在解析素材"}</div>
                        <div class="parse-loading-subtitle">结果会直接显示在当前页面</div>
                    </div>
                </div>
            `;
        }

        function showParseContinuationPending(message, attempt, maxPolls) {
            resultGeneration += 1;
            if (!resultsRegion) {
                return;
            }
            const progressText = showInternalDiagnostics
                ? (
                    attempt && maxPolls
                        ? `自动续查 ${attempt}/${maxPolls}`
                        : "正在等待已启动的解析完成"
                )
                : "正在获取高清资源";
            cancelLivePhotoActivity();
            resultsRegion.innerHTML = `
                <div class="parse-loading-card">
                    <div class="spinner-border" role="status" aria-hidden="true"></div>
                    <div>
                        <div class="parse-loading-title">${escapeHtml(message || "超高清资源仍在处理中")}</div>
                        <div class="parse-loading-subtitle">${escapeHtml(progressText)}</div>
                    </div>
                </div>
            `;
        }

        function showParseError(message) {
            resultGeneration += 1;
            if (!resultsRegion) {
                return;
            }
            selected = [];
            pendingPhotoServerFallback = false;
            cancelLivePhotoActivity();
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
                // The work itself, not the notice above it; notices alone keep the anchor.
                const target = (
                    resultsRegion.querySelector(".result-card")
                    || resultsRegion.querySelector(".results-scroll-anchor")
                    || resultsRegion
                );
                target.scrollIntoView({behavior: "smooth", block: "start"});
            });
        }

        function applyParsePayload(payload, retrySource = "") {
            resultGeneration += 1;
            if (!resultsRegion) {
                return;
            }
            applyTargetPage(payload.target_page);
            selected = [];
            pendingPhotoServerFallback = false;
            cancelLivePhotoActivity();
            resultsRegion.innerHTML = payload.html || "";
            void ensureLivePhotoPreview();
            page.dataset.baseFilename = payload.base_filename || "xhs_image";
            setPageHasResults(Boolean(payload.has_content || payload.has_results));
            updateSelectAllButton();
            if (payload.has_results) {
                scrollResultsIntoView();
            }
            applyContinuationNotice(payload, retrySource);
        }

        async function requestDouyinContinuation(token, signal) {
            if (!douyinContinuationUrl) {
                return {
                    status: "failed",
                    error_code: "DOUYIN_CONTINUATION_URL_MISSING",
                    message: "当前页面缺少解析续查接口，请重新尝试。",
                };
            }
            const response = await fetch(douyinContinuationUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify({continuation_token: token}),
                cache: "no-store",
                signal,
            });
            const contentType = response.headers.get("Content-Type") || "";
            if (!contentType.includes("application/json")) {
                await response.text();
                return {
                    status: "failed",
                    error_code: "DOUYIN_CONTINUATION_RESPONSE_INVALID",
                    message: "解析续查响应异常，请重新尝试。",
                };
            }
            return response.json();
        }

        function hasActiveResultDownload() {
            return downloadInProgress || pickerDownloadInProgress
                || Array.from(resultsRegion.querySelectorAll('[data-ytdlp-action="cancel"]'))
                    .some((button) => !button.hidden);
        }

        async function continueDouyinParse(initialPayload, signal, isCurrent) {
            if (!douyinReliabilityApi) {
                return {
                    status: "failed",
                    error_code: "DOUYIN_CONTINUATION_CLIENT_UNAVAILABLE",
                    message: "当前浏览器无法继续解析，请刷新页面后重试。",
                };
            }
            const preserveRenderedResult = Boolean(
                initialPayload
                && initialPayload.has_results
                && initialPayload.html
            );
            const outcome = await douyinReliabilityApi.runBoundedContinuation({
                initialPayload,
                signal,
                requestPoll: (token, pollContext) => (
                    requestDouyinContinuation(token, pollContext.signal)
                ),
                onPending: (payload, attempt, maxPolls) => {
                    if (isCurrent() && !preserveRenderedResult) {
                        showParseContinuationPending(
                            payload.message,
                            attempt,
                            maxPolls,
                        );
                    }
                },
            });
            return douyinReliabilityApi.resolveContinuationResult(
                initialPayload,
                outcome,
            );
        }

        function resetResults() {
            resultGeneration += 1;
            if (activeParseController) {
                activeParseController.abort();
                activeParseController = null;
            }
            selected = [];
            pendingPhotoServerFallback = false;
            cancelLivePhotoActivity();
            parseRequestId += 1;
            setParseLoading(false);
            page.dataset.baseFilename = "xhs_image";
            if (resultsRegion) {
                resultsRegion.innerHTML = "";
            }
            setPageHasResults(false);
            applyPendingProjectSettings();
        }

        async function readResponseBlobWithProgress(response, startPercent, endPercent, operation) {
            assertDownloadCurrent(operation);
            const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
            if (!response.body || !contentLength) {
                setProgressPending("下载中");
                const blob = await response.blob();
                assertDownloadCurrent(operation);
                setProgress(endPercent);
                return blob;
            }

            const reader = response.body.getReader();
            const chunks = [];
            let received = 0;

            try {
                while (true) {
                    assertDownloadCurrent(operation);
                    const {done, value} = await reader.read();
                    assertDownloadCurrent(operation);
                    if (done) {
                        break;
                    }
                    chunks.push(value);
                    received += value.length;
                    const ratio = received / contentLength;
                    setProgress(startPercent + (endPercent - startPercent) * ratio);
                }
            } finally {
                if (!downloadOperationCurrent(operation)) await reader.cancel().catch(() => {});
                reader.releaseLock();
            }

            setProgress(endPercent);
            return new Blob(chunks, {type: response.headers.get("Content-Type") || "application/zip"});
        }

        function directDownloadFilename(item, response, index, total, fallbackBase) {
            const suffix = total > 1 ? `_${index + 1}` : "";
            const extension = extensionFromContentType(
                response.headers.get("Content-Type") || "",
                item.type || "video",
            );
            return getDispositionFilename(response) || item.filename || `${fallbackBase}${suffix}.${extension}`;
        }

        function getServerDownloadFrame() {
            let frame = document.getElementById("xhs-server-download-frame");
            if (frame) {
                return frame;
            }

            frame = document.createElement("iframe");
            frame.id = "xhs-server-download-frame";
            frame.name = "xhs-server-download-frame";
            frame.hidden = true;
            frame.style.display = "none";
            document.body.appendChild(frame);
            return frame;
        }

        function directRouteHasOrdinaryMedia(items) {
            const directItems = (items || []).filter((item) => item && item.url);
            return Boolean(
                directItems.length === 1
                && ordinaryMediaDownloadApi
                && ordinaryMediaDownloadApi.isOrdinaryMediaItem(directItems[0])
            );
        }

        function ordinaryMediaFailure(error) {
            const publicMessage = ordinaryMediaDownloadApi
                ? ordinaryMediaDownloadApi.safeErrorMessage(error)
                : "当前浏览器无法保存文件，请重试下载。";
            const diagnostic = (
                showInternalDiagnostics
                && ordinaryMediaDownloadApi
                && typeof ordinaryMediaDownloadApi.safeFailureDiagnostic === "function"
            ) ? ordinaryMediaDownloadApi.safeFailureDiagnostic(error) : "";
            const wrapped = new Error(
                diagnostic ? `${publicMessage} (${diagnostic})` : publicMessage,
            );
            wrapped.errorCode = String(error && error.errorCode || "BROWSER_DOWNLOAD_FAILED");
            wrapped.streamingStarted = Boolean(error && error.streamingStarted);
            wrapped.status = error && error.status ? error.status : null;
            wrapped.isOrdinaryMediaFailure = true;
            return wrapped;
        }

        async function prepareServerSingleDownload(payload, operation) {
            assertDownloadCurrent(operation);
            if (!downloadPrepareUrl) {
                throw new Error("当前页面缺少单文件下载准备接口。");
            }
            const response = await fetch(downloadPrepareUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify({
                    ...payload,
                    download_mode: "server_single",
                }),
                cache: "no-store",
                signal: operation && operation.signal,
            });
            assertDownloadCurrent(operation);
            let rawPayload = {};
            try {
                rawPayload = await response.json();
            } catch (error) {
                rawPayload = {
                    status: "failed",
                    message: "下载准备响应格式异常，请稍后再试。",
                };
            }
            assertDownloadCurrent(operation);
            const prepared = downloadReliabilityApi
                ? downloadReliabilityApi.parsePreparePayload(
                    rawPayload,
                    response.ok ? "ready" : "failed",
                )
                : {
                    ok: Boolean(
                        response.ok
                        && rawPayload.download_url
                        && rawPayload.status_url
                    ),
                    downloadUrl: String(rawPayload.download_url || ""),
                    statusUrl: String(rawPayload.status_url || ""),
                    pollAfterMs: 500,
                    errorCode: String(rawPayload.error_code || ""),
                    message: String(rawPayload.message || "下载准备失败，请稍后再试。"),
                };
            if (!response.ok || !prepared.ok) {
                const error = new Error(prepared.message);
                error.errorCode = prepared.errorCode;
                throw error;
            }
            return prepared;
        }

        function sameOriginHandoffPath(value) {
            const parsed = new URL(String(value || ""), window.location.origin);
            if (parsed.origin !== window.location.origin) {
                throw new Error("下载通道来源无效，请重新点击下载。");
            }
            return `${parsed.pathname}${parsed.search}`;
        }

        function handoffPreparedDownload(prepared) {
            const frame = getServerDownloadFrame();
            if (!prepared || !prepared.downloadUrl) {
                throw new Error("下载准备结果缺少有效链接。");
            }
            const downloadUrl = sameOriginHandoffPath(prepared.downloadUrl);
            if (frame) {
                frame.src = downloadUrl;
            } else {
                window.location.assign(downloadUrl);
            }
        }

        async function prepareAndHandoffServerSingle(payload, warnings, operation) {
            assertDownloadCurrent(operation);
            setProgressPending("准备下载");
            setStatus(getStatusElement(), "正在签发安全下载通道...");
            const prepared = await prepareServerSingleDownload(payload, operation);
            assertDownloadCurrent(operation);
            const statusUrl = sameOriginHandoffPath(prepared.statusUrl);
            setProgressPending("连接下载");
            setStatus(
                getStatusElement(),
                presentationText(
                    "正在连接资源...",
                    "下载通道已签发，正在连接上游资源...",
                ),
            );
            handoffPreparedDownload(prepared);
            setStatus(
                getStatusElement(),
                presentationText(
                    "下载请求已交给浏览器，正在建立资源连接...",
                    "下载请求已交给浏览器，服务器正在建立资源连接...",
                ),
                "text-muted",
            );
            const statusResult = downloadReliabilityApi
                ? await downloadReliabilityApi.pollDownloadStatus({
                    statusUrl,
                    pollAfterMs: prepared.pollAfterMs,
                    startTimeoutMs: prepared.expiresInSeconds * 1000,
                    signal: operation && operation.signal,
                    onStreaming: () => {
                        assertDownloadCurrent(operation);
                        setProgressPending("传输中");
                        const {progressBytes, progressSpeed, progressEta} = getProgressElements();
                        if (progressBytes) progressBytes.innerText = "";
                        if (progressSpeed) progressSpeed.innerText = "";
                        if (progressEta) progressEta.innerText = "";
                        setStatus(
                            getStatusElement(),
                            "资源正在传输，请查看浏览器下载栏；尚未确认文件保存完成。",
                            "text-muted",
                        );
                    },
                    fetchStatus: async (url, init) => {
                        assertDownloadCurrent(operation);
                        const controller = new AbortController();
                        const signals = [init.signal, operation && operation.signal].filter(Boolean);
                        const abort = () => controller.abort();
                        const cleanup = () => signals.forEach(signal => signal.removeEventListener("abort", abort));
                        let readingBody = false;
                        signals.forEach(signal => signal.addEventListener("abort", abort, {once: true}));
                        if (signals.some(signal => signal.aborted)) abort();
                        try {
                            const response = await fetch(url, {...init, signal: controller.signal});
                            assertDownloadCurrent(operation);
                            if (response.status === 429 || response.status === 503) return response;
                            readingBody = true;
                            return {ok: response.ok, status: response.status, headers: response.headers,
                                async json() {
                                    try {
                                        assertDownloadCurrent(operation);
                                        const payload = await response.json();
                                        assertDownloadCurrent(operation);
                                        return payload;
                                    } finally { cleanup(); }
                                }};
                        } finally {
                            if (!readingBody) cleanup();
                        }
                    },
                    wait: async delay => {
                        assertDownloadCurrent(operation);
                        await sleep(delay);
                        assertDownloadCurrent(operation);
                    },
                })
                : {
                    ok: false,
                    status: "poll_timeout",
                    recovered: false,
                    message: "下载请求已交给浏览器，无法查询传输状态，请查看浏览器下载栏。",
                };
            assertDownloadCurrent(operation);
            const startDisposition = downloadReliabilityApi
                ? downloadReliabilityApi.downloadStartDisposition(
                    statusResult,
                )
                : "unconfirmed";
            if (startDisposition === "unconfirmed") {
                hideDownloadProgress();
                setStatus(
                    getStatusElement(),
                    `${statusResult.message}${routeWarningText(warnings)}`,
                    "text-muted",
                );
                return {
                    ...prepared,
                    status: statusResult.status,
                    recovered: false,
                    upstreamAttemptCount: 0,
                };
            }
            if (startDisposition !== "started") {
                const error = new Error(
                    statusResult.message
                    || "下载启动失败，请稍后重试。",
                );
                error.errorCode = statusResult.errorCode || "DOWNLOAD_FAILED";
                throw error;
            }
            hideDownloadProgress();
            setStatus(
                getStatusElement(),
                `${statusResult.recovered ? "资源已自动刷新，" : ""}服务器传输已结束，请在浏览器下载栏确认文件保存结果。${routeWarningText(warnings)}`,
                "text-success",
            );
            return {
                ...prepared,
                status: statusResult.status,
                recovered: Boolean(statusResult.recovered),
                upstreamAttemptCount: statusResult.upstreamAttemptCount || 0,
            };
        }

        async function writeResponseToFileWithProgress(response, fileHandle, startPercent, endPercent, operation) {
            assertDownloadCurrent(operation);
            const contentLength = Number.parseInt(response.headers.get("Content-Length") || "0", 10);
            if (!response.body || !contentLength) {
                const blob = await response.blob();
                assertDownloadCurrent(operation);
                const writable = await fileHandle.createWritable();
                try {
                    assertDownloadCurrent(operation);
                    await writable.write(blob);
                    assertDownloadCurrent(operation);
                    await writable.close();
                } catch (error) {
                    if (typeof writable.abort === "function") await writable.abort().catch(() => {});
                    throw error;
                }
                assertDownloadCurrent(operation);
                setProgress(endPercent);
                return;
            }

            const reader = response.body.getReader();
            const writable = await fileHandle.createWritable();
            let received = 0;

            try {
                while (true) {
                    assertDownloadCurrent(operation);
                    const {done, value} = await reader.read();
                    assertDownloadCurrent(operation);
                    if (done) {
                        break;
                    }
                    await writable.write(value);
                    assertDownloadCurrent(operation);
                    received += value.length;
                    const ratio = received / contentLength;
                    setProgress(startPercent + (endPercent - startPercent) * ratio);
                }
                await writable.close();
                assertDownloadCurrent(operation);
            } catch (error) {
                if (typeof writable.abort === "function") await writable.abort().catch(() => {});
                throw error;
            } finally {
                if (!downloadOperationCurrent(operation)) await reader.cancel().catch(() => {});
                reader.releaseLock();
            }
            setProgress(endPercent);
        }

        async function downloadDirectItem(item, index, total, onStatus, baseFilename, operation) {
            assertDownloadCurrent(operation);
            const startPercent = total > 1 ? (index / total) * 100 : 0;
            const endPercent = total > 1 ? ((index + 1) / total) * 100 : 100;
            if (onStatus) {
                onStatus("正在连接资源...");
            }
            const response = await fetch(item.url, {
                cache: "no-store",
                credentials: "omit",
                signal: operation && operation.signal,
            });
            assertDownloadCurrent(operation);

            if (!response.ok) {
                throw new Error(`直链下载失败（${response.status}）`);
            }

            const filename = directDownloadFilename(item, response, index, total, baseFilename);
            if (onStatus) {
                onStatus("下载通道已建立，正在读取文件...");
            }
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
                assertDownloadCurrent(operation);
                await writeResponseToFileWithProgress(response, handle, startPercent, endPercent, operation);
                assertDownloadCurrent(operation);
                return filename;
            }

            const blob = await readResponseBlobWithProgress(response, startPercent, endPercent, operation);
            assertDownloadCurrent(operation);
            if (onStatus) {
                onStatus("正在唤起浏览器下载...");
            }
            await saveZipBlob(blob, filename, false, operation);
            assertDownloadCurrent(operation);
            return filename;
        }

        async function downloadDirectRouteItems(items, baseFilename, operation) {
            assertDownloadCurrent(operation);
            const directItems = (items || []).filter((item) => item && item.url);
            if (directItems.length === 0) {
                throw new Error("没有可下载的直链资源");
            }

            const onStatus = (message) => {
                if (downloadOperationCurrent(operation)) setStatus(getStatusElement(), message);
            };
            onStatus("正在获取文件...");
            const filenames = [];
            for (let index = 0; index < directItems.length; index += 1) {
                assertDownloadCurrent(operation);
                filenames.push(await downloadDirectItem(directItems[index], index, directItems.length, onStatus, baseFilename, operation));
            }
            return filenames;
        }

        async function downloadOrdinaryMediaRouteItems(items, baseFilename, operation) {
            assertDownloadCurrent(operation);
            const directItems = (items || []).filter((item) => item && item.url);
            if (!ordinaryMediaDownloadApi || directItems.length !== 1) {
                throw new Error("当前资源无法由浏览器直接保存。");
            }
            resetDownloadProgressBaseline();
            const item = directItems[0];
            const result = await ordinaryMediaDownloadApi.download(item, {
                filename: item.filename || `${baseFilename}.mp4`,
                signal: operation && operation.signal,
                onStage(_stage, message) {
                    if (downloadOperationCurrent(operation)) setStatus(getStatusElement(), message);
                },
                onProgress(progress) {
                    if (downloadOperationCurrent(operation)) applyOrdinaryDownloadProgress(progress);
                },
            });
            assertDownloadCurrent(operation);
            return result;
        }

        async function postDownloadPayload(payload, operation) {
            assertDownloadCurrent(operation);
            return fetch(downloadUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify(payload),
                signal: operation && operation.signal,
            });
        }

        async function fetchRouteOnly(payload, operation) {
            const response = await postDownloadPayload({
                ...payload,
                route_only: true,
            }, operation);
            assertDownloadCurrent(operation);
            if (!response.ok) {
                throw new Error(await parseErrorResponse(response));
            }
            const route = await response.json();
            assertDownloadCurrent(operation);
            return route;
        }

        async function handleDirectRoute(routeData, baseFilename, operation) {
            assertDownloadCurrent(operation);
            const directItems = routeData.items || [];
            if (directItems.some((item) => String(item.asset_id || "").startsWith("upstream:"))
                && !directRouteHasOrdinaryMedia(directItems)) {
                throw new Error("下载组件不可用或资源授权已失效，请刷新页面或重新解析。");
            }
            if (directRouteHasOrdinaryMedia(directItems)) {
                try {
                    const result = await downloadOrdinaryMediaRouteItems(directItems, baseFilename, operation);
                    assertDownloadCurrent(operation);
                    setStatus(
                        getStatusElement(),
                        result.transport === 'browser_native'
                            ? result.message
                            : `已交给浏览器保存：${result.filename}${routeWarningText(routeData.warnings)}`,
                        "text-success",
                    );
                } catch (error) {
                    if (error && error.name === "AbortError") {
                        throw error;
                    }
                    console.warn(
                        "Ordinary browser download failed",
                        String(error && error.errorCode || "BROWSER_DOWNLOAD_FAILED"),
                    );
                    throw ordinaryMediaFailure(error);
                }
                return;
            }

            const filenames = await downloadDirectRouteItems(directItems, baseFilename, operation);
            assertDownloadCurrent(operation);
            setProgress(100);
            const completionLabel = showInternalDiagnostics
                ? (routeData.route_reason || "已开始保存")
                : "已开始保存";
            setStatus(
                getStatusElement(),
                `${completionLabel}：${filenames.join("、")}${routeWarningText(routeData.warnings)}`,
                "text-success",
            );
        }

        async function saveDownloadResponse(response, fallbackName, startPercent, endPercent, operation) {
            assertDownloadCurrent(operation);
            const filename = getDownloadFilename(response, fallbackName);
            const blob = await readResponseBlobWithProgress(response, startPercent, endPercent, operation);
            assertDownloadCurrent(operation);
            const savedMessage = await saveZipBlob(blob, filename, useFilePicker, operation);
            assertDownloadCurrent(operation);
            const savedCount = response.headers.get("X-Saved-Count") || "?";
            const failedCount = response.headers.get("X-Failed-Count") || "0";
            const variant = failedCount === "0" ? "text-success" : "text-muted";
            return {filename, savedMessage, savedCount, failedCount, variant};
        }

        function serverDownloadStatusMessage(result) {
            return `${result.savedMessage}：${result.filename}，成功 ${result.savedCount} 个，失败 ${result.failedCount} 个`;
        }

        async function waitForDownloadTask(taskStatusUrl, operation) {
            while (true) {
                assertDownloadCurrent(operation);
                const response = await fetch(taskStatusUrl, {cache: "no-store", signal: operation && operation.signal});
                assertDownloadCurrent(operation);
                if (!response.ok) {
                    throw new Error(await parseErrorResponse(response));
                }

                const task = await response.json();
                assertDownloadCurrent(operation);
                const progress = Number(task.progress || 0);
                if (progress > 0) {
                    setProgress(progress);
                } else {
                    setProgressPending(task.status === "pending" ? "排队中" : "后台打包中");
                }

                if (task.status === "failed") {
                    throw new Error(task.message || "下载失败");
                }

                if (task.status === "completed") {
                    return task;
                }

                await sleep(250);
            }
        }

        async function downloadWithTask(payload, operation) {
            assertDownloadCurrent(operation);
            if (!downloadTaskUrl) {
                throw new Error("当前页面缺少下载任务接口");
            }

            setProgressPending("创建任务");
            setStatus(getStatusElement(), "正在创建后台打包任务，请保持页面打开...");

            // Aborting this request cannot undo work already accepted by the server.
            const createResponse = await fetch(downloadTaskUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCookie("csrftoken"),
                },
                body: JSON.stringify(payload),
                signal: operation && operation.signal,
            });
            assertDownloadCurrent(operation);
            if (!createResponse.ok) {
                throw new Error(await parseErrorResponse(createResponse));
            }

            const created = await createResponse.json();
            assertDownloadCurrent(operation);
            const completed = await waitForDownloadTask(created.status_url, operation);
            assertDownloadCurrent(operation);
            const fileUrl = completed.file_url || created.file_url;

            setProgress(95);
            setStatus(getStatusElement(), "正在下载文件...");

            const fileResponse = await fetch(fileUrl, {cache: "no-store", signal: operation && operation.signal});
            assertDownloadCurrent(operation);
            if (!fileResponse.ok) {
                throw new Error(await parseErrorResponse(fileResponse));
            }

            const filename = getDownloadFilename(fileResponse, payload.base_filename || getBaseFilename());
            const blob = await readResponseBlobWithProgress(fileResponse, 95, 100, operation);
            assertDownloadCurrent(operation);
            const savedMessage = await saveZipBlob(blob, filename, useFilePicker, operation);
            assertDownloadCurrent(operation);
            const savedCount = fileResponse.headers.get("X-Saved-Count") || completed.saved_count || "?";
            const failedCount = fileResponse.headers.get("X-Failed-Count") || completed.failed_count || "0";
            const variant = String(failedCount) === "0" ? "text-success" : "text-muted";

            setProgress(100);
            setStatus(getStatusElement(), `${savedMessage}：${filename}，成功 ${savedCount} 个，失败 ${failedCount} 个`, variant);
        }

        async function handleDownloadClick(downloadBtn, forceAutoMode, options, chosenItems = selected) {
            const isCurrent = () => downloadOperationCurrent(options);
            if (!isCurrent()) return;
            if (chosenItems.length === 0) {
                window.alert("请先选择需要保存的资源");
                return;
            }
            if (downloadInProgress) {
                setStatus(getStatusElement(), "当前下载仍在处理中，请等待完成后再操作。", "text-muted");
                return;
            }

            downloadInProgress = true;
            const controls = {buttons: getDownloadActionButtons(), cards: getCards()};
            resetDownloadProgressBaseline();
            const busyLabel = forceAutoMode ? "智能处理中" : "保存中";
            setDownloadButtonsBusy(true, downloadBtn, busyLabel);
            setStatus(getStatusElement(), "正在准备下载...");
            const filenameBase = options && options.baseFilename || getBaseFilename();

            try {
                const forceServerPhotoArchive = Boolean(
                    options && options.forceServerPhotoArchive
                );
                const payload = {
                    media_items: chosenItems,
                    intent: "download",
                    base_filename: filenameBase,
                    download_mode: forceAutoMode ? "auto" : "browser",
                    save_dir: String(savedSettings.save_dir || "xhs").trim(),
                    save_format: savedSettings.save_format || "original",
                    quality: savedSettings.quality || 95,
                    video_save_mode: savedSettings.video_save_mode || "video",
                };
                if (forceServerPhotoArchive) {
                    payload.download_mode = "zip";
                    payload.archive_profile = "batch_images";
                    clearPhotoServerFallback();
                    setStatus(
                        getStatusElement(),
                        presentationText("正在准备图片 ZIP...", "服务器正在准备图片 ZIP..."),
                    );
                }

                if (chosenItems.some((item) => String(item.asset_id || "").startsWith("upstream:"))) {
                    const route = await fetchRouteOnly(payload, options);
                    if (!isCurrent()) return;
                    if (!route || route.route_type !== "direct"
                        || !route.items || !route.items.every((item) => item.browser_upstream_bound === true)) {
                        throw new Error(route && (route.message || route.route_reason) || "下载授权已失效，请重新解析。");
                    }
                    if (route.items.every((item) => item.type === "image")) {
                        if (!browserPhotoApi) {
                            throw new Error("图片下载组件不可用，请刷新页面后重试。");
                        }
                        await downloadSelectedPhotos({...payload, browserRouteItems: route.items});
                    } else {
                        await handleDirectRoute(route, filenameBase, options);
                    }
                    return;
                }

                if (
                    !forceServerPhotoArchive
                    && selectedSupportsBrowserPhoto(chosenItems, payload.save_format)
                ) {
                    if (!browserPhotoApi) {
                        const zeroRelay = selectionRequiresZeroRelay(chosenItems);
                        pendingPhotoServerFallback = !zeroRelay && chosenItems.length > 1;
                        setPhotoActionVisibility({serverFallback: pendingPhotoServerFallback});
                        setStatus(
                            getStatusElement(),
                            zeroRelay
                                ? "图片下载组件不可用，请刷新页面后重试。"
                                : pendingPhotoServerFallback
                                ? presentationText(
                                    "图片下载组件不可用，可选择“换一种方式打包”。",
                                    "浏览器图片下载组件不可用，可明确选择“使用服务器打包”。"
                                )
                                : "浏览器图片下载组件不可用，请刷新页面后重试。",
                            "text-danger",
                        );
                        return;
                    }
                    await downloadSelectedPhotos(payload);
                    return;
                }

                if (selectedSingleItem(chosenItems)) {
                    const routePreview = await fetchRouteOnly(payload, options);
                    if (!isCurrent()) return;
                    if (
                        routePreview
                        && routePreview.status === "routed"
                        && routePreview.route_type === "proxy"
                        && routePreview.download_strategy === "server_single_attachment"
                    ) {
                        await prepareAndHandoffServerSingle(
                            payload,
                            routePreview.warnings,
                            options,
                        );
                        return;
                    }
                    if (
                        routePreview
                        && routePreview.status === "routed"
                        && routePreview.route_type === "direct"
                    ) {
                        await handleDirectRoute(routePreview, filenameBase, options);
                        return;
                    }
                }

                if (
                    downloadAsyncEnabled
                    && downloadTaskUrl
                    && selectedNeedsZipTask(chosenItems)
                ) {
                    await downloadWithTask(payload, options);
                    return;
                }

                const requiresProviderProcessing = selectedRequiresProviderProcessing(chosenItems);
                const pendingLabel = requiresProviderProcessing
                    ? presentationText("准备文件", "服务端处理")
                    : (
                        payload.download_mode === "auto"
                            ? "判断中"
                            : (selectedSingleItem(chosenItems) ? "准备下载" : "打包中")
                    );
                setProgressPending(pendingLabel);
                if (requiresProviderProcessing) {
                    setStatus(
                        getStatusElement(),
                        showInternalDiagnostics
                            ? "Provider 资源正在服务端校验、恢复或合并，完成后会开始传输..."
                            : "正在准备所需文件，完成后会开始下载...",
                    );
                } else if (!selectedSingleItem(chosenItems)) {
                    setStatus(
                        getStatusElement(),
                        forceServerPhotoArchive
                            ? presentationText(
                                "正在打包 ZIP，文件开始传输后会显示接收进度...",
                                "服务器正在打包 ZIP，文件开始传输后会显示接收进度..."
                            )
                            : presentationText(
                                "正在生成所需文件，完成后会显示接收进度...",
                                "服务器正在生成所需文件，完成后会显示接收进度..."
                            ),
                    );
                }
                const response = await postDownloadPayload(payload, options);
                if (!isCurrent()) return;

                if (!response.ok) {
                    throw new Error(await parseErrorResponse(response));
                }

                const contentType = response.headers.get("Content-Type") || "";
                if (contentType.includes("application/json")) {
                    setProgress(0);
                    const data = await response.json();
                    if (!isCurrent()) return;
                    if (data.status === "routed" && data.route_type === "direct") {
                        await handleDirectRoute(data, filenameBase, options);
                        return;
                    }
                    if (data.status === "routed" && data.route_type === "proxy") {
                        setProgress(100);
                        setStatus(
                            getStatusElement(),
                            `${showInternalDiagnostics ? (data.route_reason || "已生成代理下载建议") : "下载已准备"}${routeWarningText(data.warnings)}`,
                            "text-muted",
                        );
                        return;
                    }
                    setProgress(100);
                    if (data.status === "success" && data.message) {
                        setStatus(getStatusElement(), data.message, "text-success");
                        return;
                    }
                    const failureMessage = data.message
                        || (
                            showInternalDiagnostics
                                ? data.route_reason
                                : "下载请求未返回可保存的文件，请刷新页面后重试。"
                        )
                        || "下载请求未返回可保存的文件，请刷新页面后重试。";
                    setStatus(getStatusElement(), failureMessage, "text-danger");
                    if (options && options.propagateError) throw new Error(failureMessage);
                    return;
                }

                if (response.headers.get("Content-Length")) {
                    setProgress(1);
                } else {
                    setProgressPending("下载中");
                }
                setStatus(
                    getStatusElement(),
                    presentationText("文件准备完成，正在下载...", "服务器处理完成，正在下载文件..."),
                );
                const result = await saveDownloadResponse(response, filenameBase, 0, 100, options);
                if (!isCurrent()) return;
                setStatus(getStatusElement(), serverDownloadStatusMessage(result), result.variant);
            } catch (error) {
                if (!isCurrent()) return;
                if (error && error.name === "AbortError") {
                    setProgress(0);
                    setStatus(getStatusElement(), "已取消保存位置选择");
                } else {
                    const errorCode = String(
                        error && error.errorCode
                            ? error.errorCode
                            : "DOWNLOAD_FAILED",
                    );
                    console.warn("Download request failed", errorCode);
                    if (
                        errorCode.startsWith("BROWSER_")
                        || Boolean(error && error.isOrdinaryMediaFailure)
                    ) {
                        hideDownloadProgress();
                    } else {
                        setProgress(0);
                    }
                    setStatus(
                        getStatusElement(),
                        error && error.message
                            ? error.message
                            : "保存失败，请稍后重试。",
                        "text-danger",
                    );
                }
                if (options && options.propagateError) throw error;
            } finally {
                downloadInProgress = false;
                setDownloadButtonsBusy(false, null, null, controls);
                if (isCurrent()) {
                    applyPendingProjectSettings();
                }
            }
        }

        window.AnimeImagesLegacySelectionDownload = async function (item, pickerRoot, operation) {
            assertDownloadCurrent(operation);
            if (!resultsRegion || !pickerRoot || !resultsRegion.contains(pickerRoot)
                || downloadInProgress || !item || item.selection_schema !== "service_selected_format_v1"
                || item.selection_delivery !== "legacy_manifest" || item.type !== "video"
                || !["tiantianjiexi_generic", "kukutool_generic", "tiantianjiexi", "kukutool"].includes(item.provider)
                || !item.provider_manifest_token || !item.asset_id) {
                throw new Error("当前无法下载所选资源，请等待已有任务结束或重新解析。");
            }
            // A private candidate descriptor supplies the original manifest.
            // Snapshot it without changing the user's other selected assets.
            const legacyStatus = resultsRegion.querySelector("[data-legacy-download-status]");
            if (legacyStatus) legacyStatus.hidden = false;
            legacyPickerRoot = pickerRoot;
            try {
                return await handleDownloadClick(null, true, {
                    propagateError: true,
                    baseFilename: String(item.filename || "video").replace(/\.[^.]+$/, ""),
                    signal: operation && operation.signal,
                    isCurrent: () => resultsRegion.contains(pickerRoot) && downloadOperationCurrent(operation),
                }, [Object.freeze({...item})]);
            } finally { legacyPickerRoot = null; }
        };

        // Batch shares only the authorized download bridge, not the home page controller.
        if (!homePage) return;

        async function downloadCurrentSelection(button) {
            if (pickerDownloadInProgress || downloadInProgress) return;
            const generation = resultGeneration;
            const anchor = resultsRegion.children[0];
            const isCurrent = () => generation === resultGeneration && resultsRegion.isConnected
                && anchor && resultsRegion.contains(anchor);
            const baseFilename = getBaseFilename();
            const api = window.DokiToolYtdlpPicker;
            const snapshot = api ? api.snapshotMedia(resultsRegion, document, window) : [];
            const oldItems = selected.map(item => Object.freeze({...item}));
            if (!snapshot.length) return handleDownloadClick(button, true, {baseFilename, isCurrent}, oldItems);
            pickerDownloadInProgress = true;
            const controls = {buttons: getDownloadActionButtons(), cards: getCards()};
            setDownloadButtonsBusy(true, button, "保存中");
            try {
                await api.downloadMedia(snapshot, isCurrent);
                if (oldItems.length && isCurrent()) {
                    await handleDownloadClick(button, true, {baseFilename, isCurrent}, oldItems);
                }
            } finally {
                pickerDownloadInProgress = false;
                setDownloadButtonsBusy(false, null, null, controls);
            }
        }

        function toggleAssetCard(card) {
            clearPhotoServerFallback();
            if (card.dataset.downloadSelectable === "false") {
                setStatus(
                    getStatusElement(),
                    "该项实况暂不可下载，请重新解析后再试。",
                    "text-danger",
                );
                return;
            }
            const item = getCardItem(card);
            const key = getItemKey(item);
            const exists = selected.some((selectedItem) => getItemKey(selectedItem) === key);

            if (exists) {
                selected = selected.filter((selectedItem) => getItemKey(selectedItem) !== key);
                setAssetCardSelected(card, false);
            } else {
                selected.push(item);
                setAssetCardSelected(card, true);
            }

            updateSelectedStatus();
            updateSelectAllButton();
        }

        function setAssetCardSelected(card, isSelected) {
            card.classList.toggle("selected", Boolean(isSelected));
            const selectionControl = card.querySelector("[data-asset-select-toggle]");
            if (selectionControl) {
                selectionControl.setAttribute("aria-pressed", isSelected ? "true" : "false");
            }
        }

        function toggleSelectAll() {
            clearPhotoServerFallback();
            const allCards = getCards();
            const cards = getSelectableCards();
            const media = getPickerMedia();
            const allSelected = selected.length === cards.length && selectedPickerCount() === media.length;
            selected = [];

            if (window.DokiToolYtdlpPicker) window.DokiToolYtdlpPicker.selectMedia(resultsRegion, !allSelected);

            allCards.forEach((card) => setAssetCardSelected(card, false));
            if (!allSelected) {
                cards.forEach((card) => {
                    setAssetCardSelected(card, true);
                    selected.push(getCardItem(card));
                });
            }

            updateSelectedStatus();
            updateSelectAllButton();
        }

        async function handleSearchSubmit(event) {
            if (!window.fetch || !window.AbortController || !resultsRegion) {
                return;
            }

            event.preventDefault();
            const currentSource = String(linkInput && linkInput.value || "").trim();
            if (
                isUltraHdEnabled()
                && currentSource === ultraRetrySource
                && Date.now() < ultraRetryDeadline
            ) {
                setParseLoading(false);
                return;
            }
            if (!linkInput || !linkInput.value.trim()) {
                linkInput && linkInput.focus();
                return;
            }

            if (ultraRetryDeadline && !isUltraHdEnabled()) {
                startUltraRetryCountdown(0);
            } else if (ultraRetryDeadline && currentSource !== ultraRetrySource) {
                startUltraRetryCountdown(0);
            } else if (ultraRetryDeadline && Date.now() >= ultraRetryDeadline) {
                startUltraRetryCountdown(0);
            }

            if (activeParseController) {
                activeParseController.abort();
            }

            const currentRequestId = parseRequestId + 1;
            parseRequestId = currentRequestId;
            const parseController = new AbortController();
            activeParseController = parseController;
            const isCurrent = () => currentRequestId === parseRequestId
                && activeParseController === parseController && !parseController.signal.aborted;
            let renderedFallback = false;
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
                    signal: parseController.signal,
                });
                let payload = await parseSearchResponse(response);
                if (!isCurrent()) {
                    return;
                }

                if (!response.ok) {
                    const error = new Error(
                        payload.message || "解析失败，请稍后再试。",
                    );
                    error.errorCode = payload.error_code || "PARSE_FAILED";
                    error.retryAfter = Number.parseInt(
                        response.headers.get("Retry-After")
                        || payload.retry_after_seconds
                        || 0,
                        10,
                    );
                    throw error;
                }

                if (payload.status === "continuation_pending") {
                    if (payload.has_results && payload.html) {
                        applyParsePayload(payload, currentSource);
                        renderedFallback = true;
                    }
                    payload = await continueDouyinParse(
                        payload,
                        parseController.signal,
                        isCurrent,
                    );
                    // Keep the active download's DOM and captured selection intact.
                    if (!isCurrent() || hasActiveResultDownload()) {
                        return;
                    }
                    if (renderedFallback && payload.continuation_status) {
                        applyContinuationNotice(payload, currentSource);
                        return;
                    }
                }
                if (
                    payload.status === "failed"
                    || payload.status === "timeout"
                    || payload.status === "error"
                ) {
                    const error = new Error(
                        payload.message || "解析失败，请稍后再试。",
                    );
                    error.errorCode = payload.error_code || "PARSE_FAILED";
                    throw error;
                }
                if (!payload.html) {
                    throw new Error(payload.message || "解析失败，请稍后再试。");
                }

                applyParsePayload(payload, currentSource);
            } catch (error) {
                if (!isCurrent() || (error && error.name === "AbortError")) {
                    return;
                }
                console.warn(
                    "Parse request failed",
                    error && error.errorCode
                        ? error.errorCode
                        : "PARSE_FAILED",
                );
                if (renderedFallback) {
                    if (!hasActiveResultDownload()) {
                        applyContinuationNotice({
                            continuation_message: "超高清解析暂未完成，仍可下载当前结果。",
                            retry_after_seconds: error.retryAfter,
                        }, currentSource);
                    }
                } else {
                    if (error.retryAfter) {
                        startUltraRetryCountdown(error.retryAfter, currentSource);
                    }
                    showParseError(error.message || "解析失败，请稍后再试。");
                }
            } finally {
                if (currentRequestId === parseRequestId) {
                    activeParseController = null;
                    setParseLoading(false);
                    applyPendingProjectSettings();
                }
            }
        }

        applyProjectSettings(projectSettings);

        if (projectSettingsApi) {
            projectSettingsApi.subscribeProjectSettings(queueOrApplyProjectSettings);
        }
        window.addEventListener("animeimages:project-settings-changed", (event) => {
            const settings = event && event.detail ? event.detail.settings : null;
            if (settings) {
                queueOrApplyProjectSettings(settings);
            }
        });

        if (searchForm) {
            searchForm.addEventListener("submit", handleSearchSubmit);
        }

        if (resultsRegion && homePage) {
            resultsRegion.addEventListener("change", event => {
                if (event.target.matches("[data-picker-media-selected]")) {
                    updateSelectedStatus();
                    updateSelectAllButton();
                }
            });
            resultsRegion.addEventListener("click", (event) => {
                const openImageButton = event.target.closest("[data-upstream-image-open]");
                if (openImageButton && resultsRegion.contains(openImageButton)) {
                    event.preventDefault();
                    void openUpstreamImage(openImageButton, fetchRouteOnly);
                    return;
                }
                const photoCancelButton = event.target.closest("[data-photo-download-cancel]");
                if (photoCancelButton && resultsRegion.contains(photoCancelButton)) {
                    event.preventDefault();
                    if (activePhotoController) {
                        activePhotoController.abort();
                    }
                    return;
                }

                const photoServerFallbackButton = event.target.closest("[data-photo-server-fallback]");
                if (photoServerFallbackButton && resultsRegion.contains(photoServerFallbackButton)) {
                    event.preventDefault();
                    if (pendingPhotoServerFallback) {
                        handleDownloadClick(
                            getDownloadButton(),
                            true,
                            {forceServerPhotoArchive: true},
                        );
                    }
                    return;
                }

                const selectAllBtn = event.target.closest("#select-all-btn");
                if (selectAllBtn && resultsRegion.contains(selectAllBtn)) {
                    toggleSelectAll();
                    return;
                }

                const downloadBtn = event.target.closest("#download-btn");
                if (downloadBtn && resultsRegion.contains(downloadBtn)) {
                    downloadCurrentSelection(downloadBtn);
                    return;
                }

                if (event.target.closest("[data-ytdlp-picker]")) return;

                if (event.target.closest("[data-livephoto-toggle]")) {
                    return;
                }

                const selectionControl = event.target.closest("[data-asset-select-toggle]");
                if (selectionControl && resultsRegion.contains(selectionControl)) {
                    event.preventDefault();
                    const selectionCard = selectionControl.closest(".asset-card");
                    if (selectionCard) {
                        toggleAssetCard(selectionCard);
                    }
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

        if (linkInput) {
            linkInput.addEventListener("input", () => {
                setParseLoading(Boolean(activeParseController));
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

        updateSelectAllButton();
        void ensureLivePhotoPreview();
        if (ordinaryMediaDownloadApi) {
            const interrupted = ordinaryMediaDownloadApi.consumeInterruptedDownloadMarker();
            const interruptionNotice = page.querySelector("[data-ordinary-download-interruption]");
            if (interrupted && interruptionNotice) {
                interruptionNotice.hidden = false;
            }
        }
    });
})();
