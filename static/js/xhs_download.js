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

        if (type === "video") {
            return {
                type: "video",
                url: card.dataset.url,
                cover_url: card.dataset.coverUrl || "",
            };
        }

        return {
            type: "image",
            url: card.dataset.url,
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

    function getDownloadFilename(response, fallbackName) {
        const disposition = response.headers.get("Content-Disposition") || "";
        const encodedMatch = disposition.match(/filename\*=([^;]+)/i);

        if (encodedMatch) {
            return decodeDispositionValue(encodedMatch[1].trim().replace(/^"|"$/g, ""));
        }

        const match = disposition.match(/filename="([^"]+)"/i);
        return match ? match[1] : `${fallbackName || "xhs"}.zip`;
    }

    async function saveZipBlob(blob, filename, useFilePicker) {
        if (useFilePicker && window.showSaveFilePicker) {
            const handle = await window.showSaveFilePicker({
                suggestedName: filename,
                types: [
                    {
                        description: "ZIP archive",
                        accept: {"application/zip": [".zip"]},
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

    async function parseErrorResponse(response) {
        try {
            const data = await response.json();
            return data.message || "保存失败";
        } catch (error) {
            return "保存失败";
        }
    }

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    document.addEventListener("DOMContentLoaded", () => {
        const page = document.querySelector(".xhs-download-page");

        if (!page) {
            return;
        }

        const cards = document.querySelectorAll(".asset-card");
        const downloadBtn = document.getElementById("download-btn");
        const selectAllBtn = document.getElementById("select-all-btn");
        const status = document.getElementById("status");
        const linkInput = document.getElementById("media-link-input");
        const pasteLinkIconBtn = document.getElementById("paste-link-icon-btn");
        const clearLinkBtn = document.getElementById("clear-link-btn");
        const ultraHdRequestInput = document.getElementById("ultra-hd-request-input");
        const ultraHdSettingInput = document.getElementById("ultra-hd-setting");
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
        const baseFilename = page.dataset.baseFilename || "xhs_image";
        const downloadUrl = page.dataset.downloadUrl;
        const downloadTaskUrl = page.dataset.downloadTaskUrl;
        const progressWrap = document.getElementById("download-progress-wrap");
        const progressTrack = progressWrap ? progressWrap.querySelector("[role='progressbar']") : null;
        const progressBar = document.getElementById("download-progress-bar");
        const progressPercent = document.getElementById("download-progress-percent");
        const savedSettings = loadSettings();

        let selected = [];
        let useFilePicker = Boolean(savedSettings.use_file_picker);
        let downloadMode = savedSettings.download_mode || "browser";

        function isUltraHdEnabled() {
            return Boolean(ultraHdSettingInput && ultraHdSettingInput.checked && !ultraHdSettingInput.disabled);
        }

        function updateUltraHdRequestValue() {
            if (!ultraHdRequestInput) {
                return;
            }

            ultraHdRequestInput.value = isUltraHdEnabled() ? "1" : "0";
        }

        if (ultraHdSettingInput) {
            ultraHdSettingInput.checked = Boolean(savedSettings.ultra_hd_enabled) && !ultraHdSettingInput.disabled;
            updateUltraHdRequestValue();
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

        function updateSelectedStatus() {
            setStatus(status, `已选择 ${selected.length} 个资源`);
        }

        function updateSelectAllButton() {
            if (!selectAllBtn) {
                return;
            }

            const allSelected = cards.length > 0 && selected.length === cards.length;
            selectAllBtn.innerHTML = allSelected
                ? '<i class="bi bi-x-square me-1"></i>取消全选'
                : '<i class="bi bi-check2-square me-1"></i>全选';
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
            if (progressWrap) {
                progressWrap.hidden = false;
            }
            if (progressTrack) {
                progressTrack.setAttribute("aria-valuenow", String(value));
            }
            if (progressBar) {
                progressBar.style.width = `${value}%`;
            }
            if (progressPercent) {
                progressPercent.innerText = `${value}%`;
            }
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

                await sleep(500);
            }
        }

        async function downloadWithTask(payload) {
            if (!downloadTaskUrl) {
                throw new Error("当前页面缺少下载任务接口");
            }

            setProgress(0);
            setStatus(status, "正在准备下载...");

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
            setStatus(status, "正在接收文件...");

            const fileResponse = await fetch(fileUrl, {cache: "no-store"});
            if (!fileResponse.ok) {
                throw new Error(await parseErrorResponse(fileResponse));
            }

            const filename = getDownloadFilename(fileResponse, baseFilename);
            const blob = await readResponseBlobWithProgress(fileResponse, 95, 100);
            const savedMessage = await saveZipBlob(blob, filename, useFilePicker);
            const savedCount = fileResponse.headers.get("X-Saved-Count") || completed.saved_count || "?";
            const failedCount = fileResponse.headers.get("X-Failed-Count") || completed.failed_count || "0";
            const variant = String(failedCount) === "0" ? "text-success" : "text-muted";

            setProgress(100);
            setStatus(status, `${savedMessage}：${filename}，成功 ${savedCount} 个，失败 ${failedCount} 个`, variant);
        }

        cards.forEach((card) => {
            card.addEventListener("click", () => {
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
            });
        });

        if (selectAllBtn) {
            selectAllBtn.addEventListener("click", () => {
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
            });
        }

        if (clearLinkBtn && linkInput) {
            clearLinkBtn.addEventListener("click", () => {
                linkInput.value = "";
                linkInput.focus();
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
                });
                updateUltraHdRequestValue();

                setStatus(status, "设置已保存", "text-success");
            });
        }

        if (downloadBtn) {
            downloadBtn.addEventListener("click", async () => {
                if (selected.length === 0) {
                    window.alert("请先选择需要保存的资源");
                    return;
                }

                setStatus(status, "正在保存，请稍候...");
                downloadBtn.disabled = true;

                try {
                    const payload = {
                        media_items: selected,
                        base_filename: baseFilename,
                        download_mode: downloadModeInput ? downloadModeInput.value : "browser",
                        save_dir: saveDirInput ? saveDirInput.value.trim() : "xhs",
                        save_format: saveFormatInput ? saveFormatInput.value : "original",
                        quality: qualityInput ? qualityInput.value || 95 : 95,
                        video_save_mode: videoSaveModeInput ? videoSaveModeInput.value : "video",
                    };

                    if (payload.download_mode === "browser" && downloadTaskUrl) {
                        await downloadWithTask(payload);
                        return;
                    }

                    setProgress(0);
                    const response = await fetch(downloadUrl, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRFToken": getCookie("csrftoken"),
                        },
                        body: JSON.stringify(payload),
                    });

                    if (!response.ok) {
                        throw new Error(await parseErrorResponse(response));
                    }

                    const contentType = response.headers.get("Content-Type") || "";
                    if (contentType.includes("application/json")) {
                        const data = await response.json();
                        setProgress(100);
                        setStatus(status, data.message || "服务端保存完成", "text-success");
                        return;
                    }

                    const filename = getDownloadFilename(response, baseFilename);
                    const blob = await readResponseBlobWithProgress(response, 0, 100);
                    const savedMessage = await saveZipBlob(blob, filename, useFilePicker);
                    const savedCount = response.headers.get("X-Saved-Count") || "?";
                    const failedCount = response.headers.get("X-Failed-Count") || "0";
                    const variant = failedCount === "0" ? "text-success" : "text-muted";
                    setStatus(status, `${savedMessage}：${filename}，成功 ${savedCount} 个，失败 ${failedCount} 个`, variant);
                } catch (error) {
                    if (error && error.name === "AbortError") {
                        setStatus(status, "已取消保存位置选择");
                    } else {
                        console.error(error);
                        setStatus(status, error.message || "保存失败，请查看浏览器控制台或 Django 后台日志", "text-danger");
                    }
                } finally {
                    downloadBtn.disabled = false;
                }
            });
        }

        updateDownloadLocationButtons();
        updateModeVisibility();
        updateSelectAllButton();
    });
})();
