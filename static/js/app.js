// Project-wide JavaScript hook. Keep page-specific behavior guarded by DOM checks.

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
        if (filter === "success" || filter === "failed") {
            return status === filter || (filter === "failed" && status === "skipped");
        }
        if (filter === "video" || filter === "image") {
            return postType.indexOf(filter) !== -1;
        }
        return platform.indexOf(filter) !== -1 || platformName.indexOf(filter) !== -1;
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
        const ultraInput = batchPage.querySelector('input[name="ultra_hd"]');
        const youtubeInput = batchPage.querySelector('input[name="youtube_merge"]');
        const draftKey = "animeimages.batchParseDraft.v1";

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
            batchForm.addEventListener("submit", (event) => {
                if (!batchForm.checkValidity()) {
                    if (typeof batchForm.reportValidity === "function") {
                        batchForm.reportValidity();
                    }
                    return;
                }
                event.preventDefault();
                if (batchForm.dataset.submitting === "true") {
                    return;
                }
                batchForm.dataset.submitting = "true";
                writeDraft();
                submitButton.disabled = true;
                submitButton.classList.add("is-loading");
                if (submitLabel) {
                    submitLabel.textContent = "正在批量解析...";
                }
                window.setTimeout(() => {
                    HTMLFormElement.prototype.submit.call(batchForm);
                }, 120);
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
                const input = batchTextarea || document.getElementById("batch-links");
                if (input) {
                    input.value = retryButton.getAttribute("data-retry-link") || "";
                    writeDraft();
                    input.focus();
                    input.scrollIntoView({behavior: "smooth", block: "center"});
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
                const filter = filterButton.getAttribute("data-batch-filter") || "all";
                batchPage.querySelectorAll("[data-batch-filter]").forEach((button) => {
                    button.classList.toggle("is-active", button === filterButton);
                });
                batchPage.querySelectorAll(".batch-result-card").forEach((card) => {
                    card.classList.toggle("is-hidden", !cardMatchesFilter(card, filter));
                });
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initBatchParsePage);
    } else {
        initBatchParsePage();
    }
}());
