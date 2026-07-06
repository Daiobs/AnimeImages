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

        batchPage.addEventListener("click", async (event) => {
            const copyButton = event.target.closest("[data-copy-text]");
            if (copyButton) {
                await copyText(copyButton.getAttribute("data-copy-text") || "");
                flashButton(copyButton, "已复制");
                return;
            }

            const retryButton = event.target.closest("[data-retry-link]");
            if (retryButton) {
                const input = document.getElementById("batch-links");
                if (input) {
                    input.value = retryButton.getAttribute("data-retry-link") || "";
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
