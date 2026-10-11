(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.AnimeImagesUpstreamCandidates = api;
    if (typeof document !== "undefined") api.attach(document, root);
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    function permittedUrl(value, opening) {
        if (typeof value !== "string" || /[\\\s\u0000-\u001f\u007f]/.test(value)) throw new Error("invalid_candidate");
        const parsed = new URL(String(value || ""));
        if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password
            || (parsed.port && !["80", "443"].includes(parsed.port))) throw new Error("invalid_candidate");
        if (opening && parsed.protocol !== "https:") throw new Error("http_copy_only");
        // Public-target validation belongs to the fresh descriptor endpoint, including DNS/IP checks.
        return String(value);
    }

    async function act(button, options) {
        if (button.disabled) return;
        const row = button.closest("[data-upstream-candidate]");
        const section = button.closest("[data-upstream-candidates]");
        if (!row || !section) return;
        const status = row.querySelector("[data-candidate-status]");
        const windowObject = options.window;
        const opening = button.dataset.candidateAction === "open";
        const popup = opening ? windowObject.open("about:blank", "_blank") : null;
        if (opening && !popup) { status.textContent = "请允许新窗口后重试。"; return; }
        if (popup) {
            popup.opener = null;
            const meta = popup.document.createElement("meta");
            meta.name = "referrer";
            meta.content = "no-referrer";
            popup.document.head.appendChild(meta);
        }
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        status.textContent = "正在验证候选访问权限...";
        try {
            const endpoint = new URL(section.dataset.candidateUrl, windowObject.location.origin);
            if (endpoint.origin !== windowObject.location.origin) throw new Error("invalid_candidate");
            const response = await options.fetch(endpoint.href, {
                method: "POST", credentials: "same-origin", cache: "no-store",
                headers: {"Content-Type": "application/json", "X-CSRFToken": options.csrf},
                body: JSON.stringify({resource_token: row.dataset.resourceToken, candidate_id: row.dataset.candidateId}),
            });
            if (!response.ok) throw new Error("candidate_access_denied");
            const payload = await response.json();
            if (payload.status !== "success") throw new Error("candidate_access_denied");
            const url = permittedUrl(payload.url, opening);
            if (popup) {
                popup.location.replace(url);
                status.textContent = "已打开候选链接；不代表已保存，也不保证浏览器可读取。";
            } else {
                await options.navigator.clipboard.writeText(url);
                status.textContent = url.startsWith("http:")
                    ? "已复制。HTTP 候选仅供查看信息，不自动打开或下载。"
                    : "已复制候选链接；未计为下载或保存。";
            }
        } catch (error) {
            if (popup) popup.close();
            status.textContent = error.message === "http_copy_only"
                ? "HTTP 候选仅允许复制，不自动打开；协议未被改写。"
                : "候选操作未完成，请重试或重新解析。";
        } finally {
            button.disabled = false;
            button.setAttribute("aria-busy", "false");
        }
    }

    function pageCandidates(section, order, more) {
        const list = section.querySelector(".upstream-candidate-list");
        if (!list) return;
        if (!section.__candidateSourceRows) section.__candidateSourceRows = Array.from(list.querySelectorAll("[data-upstream-candidate]"));
        const source = section.__candidateSourceRows;
        section.__candidateLimit = more ? (section.__candidateLimit || 64) + 64 : 64;
        if (order) section.__candidateOrder = order;
        let rows = source;
        if (section.__candidateOrder === "resolution") {
            const videos = source.filter((row) => row.dataset.mediaType === "video")
                .map((row, index) => ({row, index})).sort((a, b) => {
                    const known = (value) => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : -1;
                    return known(b.row.dataset.height) - known(a.row.dataset.height)
                        || known(b.row.dataset.width) - known(a.row.dataset.width) || a.index - b.index;
                }).map(({row}) => row);
            let index = 0;
            rows = source.map((row) => row.dataset.mediaType === "video" ? videos[index++] : row);
        }
        rows.forEach((row, index) => { list.append(row); row.hidden = index >= section.__candidateLimit; });
        const shown = Math.min(rows.length, section.__candidateLimit);
        const counts = section.querySelector("[data-candidate-counts]");
        if (counts) counts.textContent = `已显示 ${shown} · 待展开 ${rows.length - shown}`;
        const expand = section.querySelector("[data-candidates-expand]");
        if (expand) { expand.hidden = shown >= rows.length; expand.textContent = `再显示 ${Math.min(64, rows.length - shown)} 项`; }
    }

    function attach(documentObject, windowObject) {
        if (documentObject.__upstreamCandidatesAttached) return;
        documentObject.__upstreamCandidatesAttached = true;
        documentObject.addEventListener("change", (event) => {
            if (event.target.matches?.("[data-candidates-order]")) {
                pageCandidates(event.target.closest("[data-upstream-candidates]"), event.target.value);
            }
        });
        documentObject.addEventListener("click", (event) => {
            const compare = event.target.closest?.("[data-compare-candidate-source]");
            if (compare) {
                const batch = compare.closest(".batch-result-card");
                const source = batch?.querySelector(".batch-result-source")?.textContent
                    || documentObject.querySelector("#media-link-input")?.value || "";
                const target = new URL(compare.href, windowObject.location.origin);
                if (target.origin !== windowObject.location.origin) { event.preventDefault(); return; }
                target.hash = new URLSearchParams({source: source.trim()}).toString();
                compare.href = target.href;
                return;
            }
            const expand = event.target.closest?.("[data-candidates-expand]");
            if (expand) {
                pageCandidates(expand.closest("[data-upstream-candidates]"), null, true);
                return;
            }
            const button = event.target.closest?.("[data-candidate-action]");
            if (!button) return;
            event.preventDefault();
            const token = documentObject.querySelector('input[name="csrfmiddlewaretoken"]');
            void act(button, {window: windowObject, fetch: windowObject.fetch.bind(windowObject),
                navigator: windowObject.navigator, csrf: token?.value || ""});
        });
    }

    return {permittedUrl, act, attach, pageCandidates};
}));
