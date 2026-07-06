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
