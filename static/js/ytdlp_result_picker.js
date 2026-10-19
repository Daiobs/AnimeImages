(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.DokiToolYtdlpPicker = api;
    if (typeof document !== "undefined") {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => api.attach(document, root), {once: true});
        else api.attach(document, root);
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";
    const SCHEMA = "ytdlp_selected_format_v1";
    const PAGE_SIZE = 6;
    const COVER_MAX_BYTES = 2 * 1024 * 1024;
    const controllers = new WeakMap();
    let instanceSequence = 0;

    function selected(root) {
        const radio = root.querySelector("[data-picker-radio]:checked");
        if (!radio) return null;
        return Object.freeze({id: radio.value, token: radio.dataset.token, kind: radio.dataset.kind,
            formatId: radio.dataset.formatId,
            label: radio.dataset.label || "", size: radio.dataset.sizeLabel || "大小未知",
            canDownload: radio.dataset.canDownload === "true", canPreview: radio.dataset.canPreview === "true",
            canCheck: radio.dataset.canCheck === "true",
            limitation: radio.dataset.limitation || "", radio});
    }
    function sourceLink(value) {
        if (typeof value !== "string" || /[\\\s\u0000-\u001f\u007f]/.test(value)) throw new Error("资源地址无效。");
        const url = new URL(value);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash
                || (url.port && !["80", "443"].includes(url.port))) throw new Error("资源地址无效。");
        // The fresh endpoint validates public targets (including DNS/IP), not the cached DOM.
        return value;
    }
    function sourceHttps(value) {
        if (typeof value !== "string" || /[\\\s\u0000-\u001f\u007f]/.test(value)) throw new Error("资源地址无效。");
        const url = new URL(value);
        if (url.protocol !== "https:" || url.username || url.password || url.hash || (url.port && url.port !== "443")) throw new Error("资源地址无效。");
        return value;
    }
    function sameOriginPath(value, windowObject) {
        const url = new URL(value, windowObject.location.href);
        if (url.origin !== windowObject.location.origin || !["http:", "https:"].includes(url.protocol)) throw new Error("操作入口无效。");
        return url.href;
    }
    function csrf(documentObject) {
        const field = documentObject.querySelector('input[name="csrfmiddlewaretoken"]');
        if (field) return field.value;
        const match = String(documentObject.cookie || "").match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }
    function validateItem(item, selection) {
        if (!item || item.selection_schema !== SCHEMA || item.provider !== "yt_dlp_generic"
                || item.candidate_id !== selection.id || item.type !== selection.kind
                || (selection.formatId !== undefined && item.selected_format_id !== selection.formatId)
                || item.browser_upstream_bound !== true || item.browser_direct_fetch_safe !== true
                || item.requires_merge !== false || item.requires_cookie !== false) throw new Error("返回格式与本次选择不一致，未开始下载。");
        sourceHttps(item.url);
        return item;
    }
    function validateLinkItem(item, selection) {
        if (!item || item.selection_schema !== SCHEMA || item.provider !== "yt_dlp_generic"
                || item.candidate_id !== selection.id || item.type !== selection.kind
                || !["video", "audio"].includes(item.type)
                || typeof item.selected_format_id !== "string"
                || (selection.formatId !== undefined && item.selected_format_id !== selection.formatId)
                || typeof item.filename !== "string" || !item.filename.trim()
                || /[\u0000-\u001f\u007f]/.test(item.filename)) throw new Error("返回格式与本次选择不一致，已跳过。");
        sourceLink(item.url);
        return Object.freeze({...item});
    }
    async function descriptor(root, selection, action, environment, signal) {
        const {documentObject, windowObject, fetchImpl} = environment;
        const endpoint = sameOriginPath(root.dataset.selectionUrl, windowObject);
        const response = await fetchImpl(endpoint, {
            method: "POST", credentials: "same-origin", cache: "no-store", signal,
            headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(documentObject)},
            body: JSON.stringify({candidate_id: selection.id, resource_token: selection.token, action}),
        });
        let payload;
        try { payload = await response.json(); } catch (_) { throw new Error("资源准备失败，请稍后再试。"); }
        if (!response.ok || !payload || payload.status !== "success") throw new Error(
            payload && typeof payload.message === "string" ? payload.message : "资源授权已失效或访问受限，请重新解析。");
        if (action === "cover") return sourceHttps(payload.url);
        if (action === "link") return validateLinkItem(payload.item, selection);
        if (action === "check") {
            const candidate = payload.candidate;
            if (!candidate || candidate.candidate_id !== selection.id || candidate.format_id !== selection.formatId
                    || typeof candidate.format_id !== "string"
                    || ![candidate.can_download, candidate.can_preview, candidate.can_check].every(value => typeof value === "boolean")
                    || !payload.check || typeof payload.check.reason !== "string") {
                throw new Error("检查结果与本次选择不一致，未更新格式。");
            }
            return {candidate, check: payload.check};
        }
        return validateItem(payload.item, selection);
    }
    async function resolveLink(root, selection, environment, signal) {
        if (!selection || !selection.id || !selection.token) throw new Error("未选择可核验的格式，请重新解析。");
        return descriptor(root, selection, "link", environment, signal);
    }

    function mount(root, documentObject, windowObject, options) {
        if (controllers.has(root)) {
            const controller = controllers.get(root);
            controller.reconcile();
            return controller;
        }
        const settings = options || {};
        const fetchImpl = settings.fetchImpl || windowObject.fetch.bind(windowObject);
        const ordinary = settings.ordinary || windowObject.AnimeImagesOrdinaryMediaDownload;
        const environment = {documentObject, windowObject, fetchImpl};
        let status, download, copy, cancel, progress, playerRegion, playerSlot;
        function bindControls() {
            status = root.querySelector("[data-picker-status]");
            download = root.querySelector('[data-ytdlp-action="download"]');
            copy = root.querySelector('[data-ytdlp-action="copy"]');
            cancel = root.querySelector('[data-ytdlp-action="cancel"]');
            progress = root.querySelector("[data-picker-progress]");
            playerRegion = root.querySelector("[data-picker-player-region]");
            playerSlot = root.querySelector("[data-picker-player]");
        }
        bindControls();
        let generation = 0, busy = false, destroyed = false, activeController = null;
        let activeCheck = null;
        let coverController = null, coverObjectUrl = "", player = null;
        const groupName = `doki-ytdlp-instance-${++instanceSequence}`;
        let knownRadios = [], rememberedId = null;
        function revealSelection() {
            const radio = root.querySelector("[data-picker-radio]:checked");
            if (!radio) return;
            root.querySelectorAll("details").forEach(disclosure => {
                if (disclosure.contains(radio)) disclosure.open = true;
            });
        }
        function reconcile() {
            const radios = Array.from(root.querySelectorAll("[data-picker-radio]"));
            if (radios.length === knownRadios.length
                    && radios.every((radio, index) => radio === knownRadios[index] && radio.name === groupName)
                    && (!radios.length || radios.some(radio => radio.checked))) {
                revealSelection();
                return;
            }
            const choice = radios.find(radio => radio.value === rememberedId)
                || radios.find(radio => radio.checked) || radios.find(radio => radio.defaultChecked) || radios[0];
            if (activeCheck) abandonCheck();
            // Templates intentionally have no name: cached copies cannot disturb live groups before mounting.
            radios.forEach(radio => { radio.name = groupName; radio.checked = false; });
            if (choice) { choice.checked = true; rememberedId = choice.value; }
            knownRadios = radios;
            bindControls();
            refresh();
        }
        function say(message) { if (status) status.textContent = message; }
        function closePreview() {
            if (player) {
                player.pause(); player.removeAttribute("src"); player.load(); player.remove(); player = null;
            }
            if (playerRegion) playerRegion.hidden = true;
        }
        function refresh() {
            const selection = selected(root);
            if (selection) rememberedId = selection.id;
            revealSelection();
            root.querySelectorAll("[data-picker-radio]").forEach(radio => {
                radio.disabled = busy;
                radio.closest("[data-picker-option]").classList.toggle("is-selected", radio.checked);
            });
            if (download) {
                const checkable = selection && !selection.canDownload && selection.kind === "video" && selection.canCheck;
                download.disabled = busy || !selection || !(selection.canDownload && ordinary || checkable);
                download.textContent = busy ? "正在处理…" : checkable ? "检查所选视频"
                    : `↓ 下载所选${selection && selection.kind === "audio" ? "音频" : "视频"}`;
            }
            if (copy) copy.disabled = busy || !selection;
            root.querySelectorAll('[data-ytdlp-action="preview"]').forEach(button => { button.disabled = busy; });
            if (cancel) cancel.hidden = !busy;
            const label = root.querySelector("[data-picker-size]");
            if (label) label.textContent = selection ? selection.size : "大小未知";
            const limitation = root.querySelector("[data-picker-limitation]");
            if (limitation) limitation.textContent = selection && selection.limitation
                || "保留源文件，不转码。实际保存取决于来源的浏览器访问条件。";
        }
        function start() {
            if (busy || destroyed) return null;
            busy = true; generation += 1;
            activeController = new windowObject.AbortController();
            refresh();
            return {generation, signal: activeController.signal};
        }
        function current(operation) { return !destroyed && operation.generation === generation; }
        function finish(operation) {
            if (!current(operation)) return;
            busy = false; activeController = null; activeCheck = null; refresh();
        }
        function sameSelection(selection) {
            const now = selected(root);
            return root.isConnected && root.contains(selection.radio) && now && now.radio === selection.radio
                && now.id === selection.id && now.token === selection.token && now.formatId === selection.formatId;
        }
        function abandonCheck(showMessage = false) {
            if (!activeCheck) return;
            // Aborting local waiting does not prove that the server's bounded check stopped.
            const radio = activeCheck.radio;
            const message = "已停止等待检查结果；远端检查可能仍在进行。本页不再重复检查此格式，请重新解析后再试。";
            radio.dataset.canCheck = "false";
            radio.dataset.limitation = message;
            const limitation = radio.closest("[data-picker-option]").querySelector("[data-picker-option-limitation]");
            if (limitation) { limitation.textContent = message; limitation.hidden = false; }
            generation += 1;
            if (activeController) activeController.abort();
            busy = false; activeController = null; activeCheck = null;
            say(showMessage ? message : "");
        }
        async function checkSelected(selection) {
            const operation = start();
            if (!operation) return;
            activeCheck = selection;
            closePreview(); say("正在检查所选视频…");
            const applicable = () => current(operation) && !operation.signal.aborted && sameSelection(selection);
            try {
                const result = await descriptor(root, selection, "check", environment, operation.signal);
                if (!applicable()) return;
                const {candidate} = result;
                const radio = selection.radio;
                radio.dataset.canDownload = String(candidate.can_download);
                radio.dataset.canPreview = String(candidate.can_preview);
                radio.dataset.canCheck = String(candidate.can_download && candidate.can_check);
                radio.dataset.limitation = typeof candidate.limitation === "string" ? candidate.limitation : "";
                if (!candidate.can_download && !radio.dataset.limitation) radio.dataset.limitation = "检查未确认可下载，未开始下载。";
                if (typeof candidate.size_label === "string") radio.dataset.sizeLabel = candidate.size_label;
                const row = radio.closest("[data-picker-option]");
                const detail = row.querySelector("[data-picker-detail]");
                if (detail) detail.textContent = typeof candidate.detail_label === "string" ? candidate.detail_label : radio.dataset.limitation;
                const limitation = row.querySelector("[data-picker-option-limitation]");
                if (limitation) {
                    limitation.textContent = radio.dataset.limitation;
                    limitation.hidden = !radio.dataset.limitation;
                }
                const size = row.querySelector("[data-picker-option-size]");
                if (size) size.textContent = radio.dataset.sizeLabel || "大小未知";
                say(candidate.can_download ? "检查完成，可再次点击下载所选视频。" : radio.dataset.limitation);
            } catch (error) {
                if (applicable()) say(error.message || "检查未完成，未开始下载。");
            } finally { finish(operation); }
        }
        async function save() {
            const selection = selected(root);
            if (selection && !selection.canDownload && selection.kind === "video" && selection.canCheck && !busy) {
                await checkSelected(selection);
                return;
            }
            if (!selection || !selection.canDownload || !ordinary || busy) return;
            closePreview();
            const operation = start();
            if (!operation) return;
            say("正在准备所选格式…");
            try {
                const item = await descriptor(root, selection, "download", environment, operation.signal);
                if (!current(operation) || operation.signal.aborted) return;
                if (progress) { progress.hidden = false; progress.removeAttribute("value"); }
                await ordinary.download(item, {
                    filename: item.filename, signal: operation.signal,
                    onStage(stage) {
                        if (!current(operation)) return;
                        const messages = {preparing: "正在准备浏览器保存…", fetching: "正在接收所选源文件…", saving: "正在交给浏览器保存…", completed: "已交给浏览器保存，请在下载列表中确认。"};
                        say(messages[stage] || "正在处理所选格式…");
                    },
                    onProgress(event) {
                        if (!current(operation) || !progress) return;
                        const percent = event && event.percent;
                        if (Number.isFinite(percent)) progress.value = percent;
                        else progress.removeAttribute("value");
                    },
                });
                if (current(operation)) say("已交给浏览器保存，请在下载列表中确认。");
            } catch (error) {
                if (current(operation)) say(operation.signal.aborted ? "下载已取消，未自动重试。" :
                    (error && error.errorCode && ordinary.safeErrorMessage ? ordinary.safeErrorMessage(error) : error.message || "下载未完成。"));
            } finally {
                if (progress && current(operation)) progress.hidden = true;
                finish(operation);
            }
        }
        async function preview() {
            if (busy) return;
            let selection = selected(root);
            if (!selection || !selection.canPreview) {
                const radio = Array.from(root.querySelectorAll("[data-picker-radio]")).find(r => r.value === root.dataset.previewId);
                if (!radio) { say("暂无可直接预览的格式；下载选择未改变。"); return; }
                selection = {id: radio.value, token: radio.dataset.token, kind: radio.dataset.kind,
                    formatId: radio.dataset.formatId, label: radio.dataset.label};
            }
            const operation = start();
            if (!operation) return;
            closePreview(); say("正在准备预览；下载选择未改变…");
            try {
                const item = await descriptor(root, selection, "preview", environment, operation.signal);
                if (!current(operation) || operation.signal.aborted) return;
                player = documentObject.createElement(item.type === "audio" ? "audio" : "video");
                player.controls = true; player.preload = "none"; player.crossOrigin = "anonymous";
                player.setAttribute("playsinline", ""); player.setAttribute("referrerpolicy", "no-referrer");
                player.addEventListener("error", () => say("浏览器无法预览此来源或格式；不会自动重试或改用服务器。"));
                player.src = item.url; playerSlot.appendChild(player); playerRegion.hidden = false;
                root.querySelector("[data-picker-preview-note]").textContent = `预览 ${selection.label || "可播放格式"}，不改变下载选择。`;
                say("预览已准备，点击播放器播放。");
            } catch (error) { if (current(operation)) say(operation.signal.aborted ? "已取消预览准备。" : error.message || "预览不可用。"); }
            finally { finish(operation); }
        }
        async function copySelected() {
            const selection = selected(root);
            if (!selection || busy) return;
            const operation = start();
            if (!operation) return;
            say("正在核验所选链接…");
            try {
                const item = await resolveLink(root, selection, environment, operation.signal);
                if (!current(operation) || operation.signal.aborted) return;
                await windowObject.navigator.clipboard.writeText(item.url);
                say("已复制所选格式链接；访问条件及有效期以来源为准。");
            } catch (error) { if (current(operation)) say(`未能复制链接：${error.message || "请检查剪贴板权限或重新解析。"}`); }
            finally { finish(operation); }
        }
        async function cover() {
            const image = root.querySelector('[data-cover-available="true"]');
            if (!image || !ordinary || !ordinary.readBlobWithinLimit) return;
            coverController = new windowObject.AbortController();
            const timer = windowObject.setTimeout(() => coverController && coverController.abort(), 8000);
            try {
                const source = await descriptor(root, {id: root.dataset.firstId, token: root.dataset.firstToken}, "cover", environment, coverController.signal);
                const response = await fetchImpl(source, {method: "GET", credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store", redirect: "error", mode: "cors", signal: coverController.signal});
                const type = String(response.headers.get("Content-Type") || "").split(";", 1)[0].toLowerCase();
                if (!response.ok || response.redirected || ["opaque", "opaqueredirect"].includes(response.type)
                        || (response.url && response.url !== source) || !["image/jpeg", "image/png", "image/webp", "image/avif", "image/gif"].includes(type)
                        || Number(response.headers.get("Content-Length") || 0) > COVER_MAX_BYTES) {
                    if (response.body && response.body.cancel) await response.body.cancel();
                    return;
                }
                const blob = await ordinary.readBlobWithinLimit(response, COVER_MAX_BYTES, type);
                if (destroyed || !blob.size) return;
                coverObjectUrl = windowObject.URL.createObjectURL(blob);
                image.addEventListener("error", () => { image.hidden = true; }, {once: true});
                image.src = coverObjectUrl; image.hidden = false;
            } catch (_) { /* Decoration failures never abort parsing or format selection. */ }
            finally { windowObject.clearTimeout(timer); coverController = null; }
        }
        function handleChange(event) {
            if (!event.target.matches("[data-picker-radio]")) return;
            if (activeCheck) abandonCheck();
            if (busy) return;
            generation += 1; closePreview(); say(""); refresh();
        }
        function handleClick(event) {
            const more = event.target.closest("[data-picker-more]");
            if (more && root.contains(more)) {
                const group = root.querySelector(`[data-picker-group="${more.dataset.pickerMore === "audio" ? "audio" : "video"}"]`);
                const hidden = Array.from(group.querySelectorAll("[data-picker-option][hidden]"));
                hidden.slice(0, PAGE_SIZE).forEach(row => { row.hidden = false; });
                more.hidden = hidden.length <= PAGE_SIZE;
                return;
            }
            const button = event.target.closest("[data-ytdlp-action]");
            if (!button || !root.contains(button) || button.disabled) return;
            event.preventDefault();
            if (button.dataset.ytdlpAction === "download") save();
            else if (button.dataset.ytdlpAction === "preview") preview();
            else if (button.dataset.ytdlpAction === "copy") copySelected();
            else if (button.dataset.ytdlpAction === "close-preview") closePreview();
            else if (button.dataset.ytdlpAction === "cancel" && activeController) {
                if (activeCheck) {
                    abandonCheck(true);
                    refresh();
                } else activeController.abort();
            }
        }
        root.addEventListener("change", handleChange); root.addEventListener("click", handleClick);
        reconcile(); cover();
        const controller = {reconcile, destroy() {
            destroyed = true; generation += 1;
            if (activeController) activeController.abort();
            if (coverController) coverController.abort();
            closePreview();
            if (coverObjectUrl) windowObject.URL.revokeObjectURL(coverObjectUrl);
            root.removeEventListener("change", handleChange); root.removeEventListener("click", handleClick);
            controllers.delete(root);
        }};
        controllers.set(root, controller);
        return controller;
    }
    function attach(documentObject, windowObject, options) {
        if (documentObject.__dokiYtdlpPickerAttached) return;
        documentObject.__dokiYtdlpPickerAttached = true;
        const mounted = new Map();
        function scan() {
            documentObject.querySelectorAll("[data-ytdlp-picker]").forEach(root => {
                if (!mounted.has(root)) mounted.set(root, mount(root, documentObject, windowObject, options));
                else mounted.get(root).reconcile();
            });
            mounted.forEach((controller, root) => {
                if (!root.isConnected) { controller.destroy(); mounted.delete(root); }
            });
        }
        scan();
        if (windowObject.MutationObserver) {
            const observer = new windowObject.MutationObserver(scan);
            observer.observe(documentObject.body, {childList: true, subtree: true});
        }
    }
    return Object.freeze({SCHEMA, PAGE_SIZE, COVER_MAX_BYTES, selected, sourceHttps, sourceLink, sameOriginPath,
        csrf, validateItem, validateLinkItem, descriptor, resolveLink, mount, attach});
}));
