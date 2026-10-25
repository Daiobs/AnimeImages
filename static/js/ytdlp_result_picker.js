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
    const controllers = new WeakMap();
    let instanceSequence = 0;

    function selected(root) {
        const radio = root.querySelector("[data-picker-radio]:checked");
        if (!radio) return null;
        return Object.freeze({id: radio.value, token: radio.dataset.token, kind: radio.dataset.kind,
            provider: (root.dataset || {}).provider || "yt_dlp_generic", schema: (root.dataset || {}).selectionSchema || SCHEMA,
            canLegacy: radio.dataset.canLegacy === "true", legacyAssetId: radio.dataset.legacyAssetId || "",
            formatId: radio.dataset.formatId,
            label: radio.dataset.label || "", size: radio.dataset.sizeLabel || "大小未知",
            canDownload: radio.dataset.canDownload === "true", canPreview: radio.dataset.canPreview === "true",
            canCheck: radio.dataset.canCheck === "true", canOpen: radio.dataset.canOpen === "true",
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
        if (typeof value !== "string" || !value.trim()) throw new Error("操作入口无效。");
        const url = new URL(value, windowObject.location.href);
        if (url.origin !== windowObject.location.origin || !["http:", "https:"].includes(url.protocol)
                || url.username || url.password || url.hash) throw new Error("操作入口无效。");
        return url.href;
    }
    function csrf(documentObject) {
        const field = documentObject.querySelector('input[name="csrfmiddlewaretoken"]');
        if (field) return field.value;
        const match = String(documentObject.cookie || "").match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }
    function validateItem(item, selection, delivery = "direct") {
        if (!item || (item.selection_delivery || "direct") !== delivery) throw new Error("返回交付方式与本次选择不一致，未开始下载。");
        if (delivery === "legacy_manifest") {
            validateLinkItem(item, selection);
            if (selection.schema !== "service_selected_format_v1"
                    || !["tiantianjiexi_generic", "kukutool_generic", "tiantianjiexi", "kukutool"].includes(selection.provider)
                    || selection.canLegacy !== true || !selection.legacyAssetId || item.asset_id !== selection.legacyAssetId
                    || selection.kind !== "video" || typeof selection.formatId !== "string"
                    || item.browser_upstream_bound === true || item.browser_direct_fetch_safe === true
                    || item.requires_merge === true || item.requires_cookie === true
                    || ["asset_id", "provider_resource_id", "provider_cache_key", "provider_manifest_token", "source_platform"]
                        .some(key => typeof item[key] !== "string" || !item[key].trim() || /[\u0000-\u001f\u007f]/.test(item[key]))
                    || item.asset_id.startsWith("upstream:")) throw new Error("旧资源交付授权不完整，未开始下载。");
            return item;
        }
        if (delivery !== "direct") throw new Error("交付方式无效，未开始下载。");
        const cookieCompatible = item && (item.requires_cookie === false
            || (item.requires_cookie == null && item.request_context_status === "no_known_requirement"));
        if (!item || item.selection_schema !== (selection.schema || SCHEMA) || item.provider !== (selection.provider || "yt_dlp_generic")
                || item.candidate_id !== selection.id || item.type !== selection.kind
                || (selection.formatId !== undefined && item.selected_format_id !== selection.formatId)
                || item.browser_upstream_bound !== true || item.browser_direct_fetch_safe !== true
                || item.requires_merge !== false || !cookieCompatible
                || item.request_context_status === "required") throw new Error("返回格式与本次选择不一致，未开始下载。");
        if (["tiantianjiexi", "kukutool"].includes(item.provider)) {
            validateLinkItem(item, selection);
            if (selection.schema !== "service_selected_format_v1" || selection.kind !== "video"
                    || !selection.legacyAssetId || item.asset_id !== selection.legacyAssetId
                    || item.source_platform !== "douyin"
                    || typeof item.provider_resource_id !== "string" || !/^\d{8,24}$/.test(item.provider_resource_id)
                    || item.provider_cache_key !== `douyin:${item.provider}:validated:${item.provider_resource_id}`
                    || typeof item.provider_manifest_token !== "string" || !item.provider_manifest_token
                    || /[\s\\\u0000-\u001f\u007f]/.test(item.provider_manifest_token)) {
                throw new Error("返回资源授权与本次选择不一致，未开始下载。");
            }
            const assetPrefix = `douyin:${item.provider_resource_id}:video:${item.provider}`;
            if (!(item.asset_id === assetPrefix || item.asset_id.startsWith(`${assetPrefix}:format:`)
                    && /^[a-f0-9]{24}$/.test(item.asset_id.slice(`${assetPrefix}:format:`.length)))) {
                throw new Error("返回资源身份与本次选择不一致，未开始下载。");
            }
        }
        if (item.source_platform === "bilibili") {
            validateLinkItem(item, selection);
            if (selection.schema !== "service_selected_format_v1" || item.provider !== "tiantianjiexi_generic"
                    || selection.kind !== "video" || !selection.legacyAssetId || item.asset_id !== selection.legacyAssetId
                    || !/^bilibili:service:BV[A-Za-z0-9]{10}:p[1-9][0-9]*:video:[a-f0-9]{24}$/.test(item.asset_id)
                    || !/^[a-f0-9]{64}$/.test(item.provider_resource_id)
                    || item.provider_cache_key !== `bilibili:service:${item.provider}:${item.provider_resource_id}`
                    || typeof item.provider_manifest_token !== "string" || !item.provider_manifest_token
                    || /[\s\\\u0000-\u001f\u007f]/.test(item.provider_manifest_token)) {
                throw new Error("返回资源授权与本次选择不一致，未开始下载。");
            }
        }
        sourceHttps(item.url);
        return item;
    }
    function validateLinkItem(item, selection) {
        if (!item || item.selection_schema !== (selection.schema || SCHEMA) || item.provider !== (selection.provider || "yt_dlp_generic")
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
        if (action === "open") {
            const item = validateLinkItem(payload.item, selection);
            if (item.type !== "video") throw new Error("此格式不支持新标签页播放。");
            sourceHttps(item.url);
            return item;
        }
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
        return validateItem(payload.item, selection, action === "legacy" ? "legacy_manifest" : "direct");
    }
    async function resolveLink(root, selection, environment, signal) {
        if (!selection || !selection.id || !selection.token) throw new Error("未选择可核验的格式，请重新解析。");
        return descriptor(root, selection, "link", environment, signal);
    }

    function mediaRoots(scope) {
        return Array.from(scope.querySelectorAll("[data-ytdlp-picker][data-picker-media]"));
    }
    function mediaSelected(root) {
        const checkbox = root.querySelector("[data-picker-media-selected], [data-picker-legacy-selected]");
        return !checkbox || checkbox.checked;
    }
    function legacyMediaNodes(scope) {
        return Array.from(scope.querySelectorAll("[data-batch-download-item]"))
            .filter(node => !node.closest("[data-ytdlp-picker], [data-picker-preview-only]")
                && node.dataset.downloadSelectable !== "false"
                && (node.dataset.type === "image" && node.querySelector("[data-batch-image-download]")
                    || node.dataset.type === "video" && node.querySelector("[data-batch-direct-download]")));
    }
    function ensureLegacyMediaSelection(scope, documentObject) {
        if (!scope.querySelector("[data-picker-select-all]")) return;
        legacyMediaNodes(scope).forEach(node => {
            if (node.querySelector("[data-picker-legacy-selected]")) return;
            const label = documentObject.createElement("label");
            label.setAttribute("class", "quality-media-select");
            const checkbox = documentObject.createElement("input");
            checkbox.setAttribute("type", "checkbox");
            checkbox.setAttribute("data-picker-legacy-selected", "");
            checkbox.setAttribute("aria-label", node.dataset.type === "image" ? "选择图片资源" : "选择视频资源");
            checkbox.checked = true;
            label.appendChild(checkbox);
            const summary = node.querySelector(".batch-direct-summary");
            if (summary) {
                label.classList.add("position-static");
                label.classList.add("flex-shrink-0");
                summary.insertBefore(label, summary.children[0] || null);
            } else {
                node.classList.add("position-relative");
                node.appendChild(label);
            }
        });
    }
    function selectMedia(scope, checked) {
        mediaRoots(scope).concat(legacyMediaNodes(scope)).forEach(root => {
            const checkbox = root.querySelector("[data-picker-media-selected], [data-picker-legacy-selected]");
            if (checkbox) checkbox.checked = checked;
            if (controllers.has(root)) controllers.get(root).reconcile();
        });
    }
    function snapshotMedia(scope, documentObject, windowObject) {
        return mediaRoots(scope).filter(mediaSelected).map(root => {
            const controller = mount(root, documentObject, windowObject);
            return Object.freeze({controller, selection: selected(root),
                descriptorRoot: Object.freeze({dataset: Object.freeze({selectionUrl: root.dataset.selectionUrl})})});
        });
    }
    async function downloadMedia(snapshot, isCurrent = () => true) {
        for (const entry of snapshot) {
            if (!isCurrent()) break;
            await entry.controller.save(entry.selection, entry.descriptorRoot, false, isCurrent);
        }
    }
    function refreshMediaSelectAll(scope) {
        const button = scope.querySelector("[data-picker-select-all]");
        if (!button) return;
        const roots = mediaRoots(scope).concat(legacyMediaNodes(scope));
        const label = roots.length && roots.every(mediaSelected) ? "取消全选" : "全选";
        if (button.textContent !== label) button.textContent = label;
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
        let status, download, legacy, copy, cancel, progress, playerRegion, playerSlot;
        function bindControls() {
            status = root.querySelector("[data-picker-status]");
            download = root.querySelector('[data-ytdlp-action="download"]');
            legacy = root.querySelector('[data-ytdlp-action="legacy"]');
            copy = root.querySelector('[data-ytdlp-action="copy"]');
            cancel = root.querySelector('[data-ytdlp-action="cancel"]');
            progress = root.querySelector("[data-picker-progress]");
            playerRegion = root.querySelector("[data-picker-player-region]");
            playerSlot = root.querySelector("[data-picker-player]");
        }
        bindControls();
        let generation = 0, busy = false, destroyed = false, activeController = null;
        let activeCheck = null;
        let activeLegacy = false;
        let activeOpen = null;
        let coverController = null, player = null;
        const groupName = `doki-ytdlp-instance-${++instanceSequence}`;
        let knownRadios = [], rememberedId = null, knownMediaCheckbox = null, rememberedMediaSelected = true;
        function revealSelection() {
            const radio = root.querySelector("[data-picker-radio]:checked");
            if (!radio) return;
            root.querySelectorAll("details").forEach(disclosure => {
                if (disclosure.contains(radio)) disclosure.open = true;
            });
        }
        function reconcile() {
            if (activeOpen && !openApplicable(activeOpen)) abandonOpen();
            const checkbox = root.querySelector("[data-picker-media-selected]");
            if (checkbox !== knownMediaCheckbox) {
                if (checkbox && knownMediaCheckbox) checkbox.checked = rememberedMediaSelected;
                knownMediaCheckbox = checkbox;
            }
            if (checkbox) rememberedMediaSelected = checkbox.checked;
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
            const legacyAvailable = selection && selection.canLegacy
                && typeof windowObject.AnimeImagesLegacySelectionDownload === "function";
            if (selection) rememberedId = selection.id;
            revealSelection();
            root.querySelectorAll("[data-picker-radio]").forEach(radio => {
                radio.disabled = busy;
                radio.closest("[data-picker-option]").classList.toggle("is-selected", radio.checked);
            });
            if (download) {
                const checkable = selection && !selection.canDownload && !legacyAvailable && selection.kind === "video" && selection.canCheck;
                download.disabled = busy || !selection || !(selection.canDownload && ordinary || !selection.canDownload && legacyAvailable || checkable);
                download.textContent = busy ? "正在处理…" : checkable ? "检查所选视频"
                    : `↓ 下载所选${selection && selection.kind === "audio" ? "音频" : "视频"}`;
            }
            if (legacy) {
                legacy.hidden = !(selection && selection.canDownload && legacyAvailable);
                legacy.disabled = busy || legacy.hidden;
                legacy.textContent = "下载所选视频";
            }
            if (copy) copy.disabled = busy || !selection;
            root.querySelectorAll('[data-ytdlp-action="preview"]').forEach(button => { button.disabled = busy; });
            root.querySelectorAll('[data-ytdlp-action="open"]').forEach(button => {
                button.disabled = busy || Boolean(activeOpen) || !selection;
                const restricted = selection && (selection.kind !== "video" || !selection.canOpen);
                button.setAttribute("aria-disabled", String(button.disabled || Boolean(restricted)));
                button.title = restricted ? selection.limitation || "此格式暂不能在新标签页播放。" : "在新标签页播放所选视频";
                button.setAttribute("aria-busy", String(Boolean(activeOpen)));
            });
            if (cancel) {
                cancel.hidden = !busy;
                cancel.textContent = activeLegacy ? "停止等待" : "取消当前下载";
            }
            const label = root.querySelector("[data-picker-size]");
            if (label) label.textContent = selection ? selection.size : "大小未知";
            const limitation = root.querySelector("[data-picker-limitation]");
            if (limitation) {
                limitation.textContent = selection && selection.limitation || "";
                limitation.hidden = !limitation.textContent;
            }
        }
        function start(legacyOperation = false) {
            if (busy || destroyed) return null;
            abandonOpen();
            activeLegacy = legacyOperation;
            busy = true; generation += 1;
            activeController = new windowObject.AbortController();
            refresh();
            return {generation, signal: activeController.signal};
        }
        function current(operation) { return !destroyed && operation.generation === generation; }
        function finish(operation) {
            if (!current(operation)) return;
            busy = false; activeController = null; activeCheck = null; activeLegacy = false; refresh();
        }
        function sameSelection(selection, snapshot = false) {
            const now = selected(root);
            const radio = selection.radio;
            return root.isConnected && root.contains(radio) && radio.value === selection.id
                && radio.dataset.token === selection.token && radio.dataset.formatId === selection.formatId
                && (snapshot || now && now.radio === radio);
        }
        function closeOpenWindow(operation) {
            try { if (operation.popup && !operation.popup.closed) operation.popup.close(); } catch (_) { /* Detached window. */ }
        }
        function openApplicable(operation) {
            const now = selected(root);
            return !destroyed && activeOpen === operation && !operation.controller.signal.aborted
                && sameSelection(operation.selection) && now.canOpen && now.kind === "video"
                && now.provider === operation.selection.provider && now.schema === operation.selection.schema
                && root.dataset.selectionUrl === operation.endpoint
                && root.querySelector('[data-ytdlp-action="open"]') === operation.button;
        }
        function abandonOpen() {
            if (!activeOpen) return;
            const operation = activeOpen;
            activeOpen = null;
            operation.controller.abort();
            closeOpenWindow(operation);
            say(""); refresh();
        }
        async function openSelected(button) {
            const selection = selected(root);
            if (destroyed || busy || activeOpen || !selection || !sameSelection(selection)) return;
            if (!selection.canOpen || selection.kind !== "video") {
                say(selection.limitation || "此格式暂不能在新标签页播放。");
                return;
            }
            const operation = {selection, button, endpoint: root.dataset.selectionUrl,
                controller: new windowObject.AbortController(), popup: null};
            activeOpen = operation;
            let navigated = false;
            try {
                // Retain a window handle for cleanup; isolate it before awaiting authorization.
                operation.popup = windowObject.open("about:blank", "_blank");
                const popup = operation.popup;
                if (!popup) throw new Error("请允许新标签页后重试。");
                popup.opener = null;
                if (popup.opener !== null) throw new Error("无法安全打开新标签页。");
                const meta = popup.document.createElement("meta");
                meta.name = "referrer"; meta.content = "no-referrer";
                popup.document.head.appendChild(meta);
                refresh(); say("正在核验所选视频…");
                const descriptorRoot = {dataset: {selectionUrl: operation.endpoint}};
                const item = await descriptor(descriptorRoot, selection, "open", environment, operation.controller.signal);
                if (!openApplicable(operation)) return;
                if (popup.closed) { say("新标签页已关闭，未打开视频。"); return; }
                popup.location.replace(item.url);
                navigated = true;
                say("已打开所选视频链接。");
            } catch (error) {
                if (openApplicable(operation)) say(error.message || "未能打开所选视频，请重新解析。");
            } finally {
                if (!navigated) closeOpenWindow(operation);
                if (activeOpen === operation) { activeOpen = null; refresh(); }
            }
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
        async function save(selection = selected(root), descriptorRoot = root, forceLegacy = false, isCurrent = () => true) {
            const snapshot = descriptorRoot !== root;
            if (!isCurrent() || !selection || busy || destroyed || !sameSelection(selection, snapshot)) return;
            const handler = windowObject.AnimeImagesLegacySelectionDownload;
            if (forceLegacy || !selection.canDownload && selection.canLegacy && typeof handler === "function") {
                if (selection.canLegacy && typeof handler === "function") await saveLegacy(selection, handler, descriptorRoot, isCurrent);
                return;
            }
            if (selection && !selection.canDownload && selection.kind === "video" && selection.canCheck && !busy) {
                await checkSelected(selection);
                return;
            }
            if (!selection.canDownload || !ordinary) {
                say(selection.limitation || "当前格式暂不可下载。");
                return;
            }
            closePreview();
            const operation = start();
            if (!operation) return;
            say("正在准备所选格式…");
            try {
                const item = await descriptor(descriptorRoot, selection, "download", environment, operation.signal);
                if (!isCurrent() || !current(operation) || operation.signal.aborted || !sameSelection(selection, snapshot)) return;
                if (progress) { progress.hidden = false; progress.removeAttribute("value"); }
                const saved = await ordinary.download(item, {
                    filename: item.filename, signal: operation.signal,
                    onStage(stage, message) {
                        if (!current(operation)) return;
                        if (stage === "updating" || stage === "handed_off") { say(message); return; }
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
                if (current(operation)) say(saved && saved.transport === 'browser_native'
                    ? saved.message : "已交给浏览器保存，请在下载列表中确认。");
            } catch (error) {
                const cancellationUnknown = error && error.errorCode === "STREAMING_CANCEL_STATE_UNKNOWN";
                if (current(operation)) say(cancellationUnknown ? "尚未确认下载已停止，请查看浏览器下载列表；未自动重试。" :
                    operation.signal.aborted ? "下载已取消，未自动重试。" :
                    (error && error.errorCode && ordinary.safeErrorMessage ? ordinary.safeErrorMessage(error) : error.message || "下载未完成。"));
            } finally {
                if (progress && current(operation)) progress.hidden = true;
                finish(operation);
            }
        }
        function waitWhileActive(promise, signal) {
            return new Promise((resolve, reject) => {
                const abort = () => { const error = new Error("已停止等待。"); error.name = "AbortError"; reject(error); };
                signal.addEventListener("abort", abort, {once: true});
                Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
                if (signal.aborted) abort();
            });
        }
        async function saveLegacy(selection, handler, descriptorRoot = root, isCurrent = () => true) {
            closePreview();
            const operation = start(true);
            if (!operation) return;
            const signal = operation.signal;
            const applicable = () => isCurrent() && current(operation) && !signal.aborted && sameSelection(selection, descriptorRoot !== root);
            say("正在核验所选资源…");
            try {
                const item = await waitWhileActive(descriptor(descriptorRoot, selection, "legacy", environment, signal), signal);
                if (!applicable()) return;
                // The home controller owns routing and status; this bridge never starts another transport.
                await waitWhileActive(handler(item, root, Object.freeze({isCurrent: applicable, signal})), signal);
                if (applicable()) say("下载流程已处理，请查看下载状态。");
            } catch (error) {
                if (current(operation) && sameSelection(selection, descriptorRoot !== root)) say(signal.aborted
                    ? "已停止等待；下载可能仍在进行，请查看下载状态。未自动重试。"
                    : error.message || "下载未完成，未自动重试。");
            } finally { finish(operation); }
        }
        async function preview() {
            if (busy) return;
            let selection = selected(root);
            if (!selection || !selection.canPreview) {
                const radio = Array.from(root.querySelectorAll("[data-picker-radio]")).find(r => r.value === root.dataset.previewId);
                if (!radio) { say("暂无可直接预览的格式；下载选择未改变。"); return; }
                selection = {id: radio.value, token: radio.dataset.token, kind: radio.dataset.kind,
                    formatId: radio.dataset.formatId, label: radio.dataset.label,
                    legacyAssetId: radio.dataset.legacyAssetId || "",
                    provider: root.dataset.provider || "yt_dlp_generic",
                    schema: root.dataset.selectionSchema || SCHEMA};
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
            if (!image) return;
            coverController = new windowObject.AbortController();
            const timer = windowObject.setTimeout(() => coverController && coverController.abort(), 8000);
            try {
                const source = await descriptor(root, {id: root.dataset.firstId, token: root.dataset.firstToken}, "cover", environment, coverController.signal);
                if (destroyed || coverController.signal.aborted) return;
                // The authorized cover is display-only, like the existing card
                // image. It does not need CORS access or a complete image Blob.
                image.addEventListener("error", () => { image.hidden = true; }, {once: true});
                image.referrerPolicy = "no-referrer";
                image.src = source; image.hidden = false;
            } catch (_) { /* Decoration failures never abort parsing or format selection. */ }
            finally { windowObject.clearTimeout(timer); coverController = null; }
        }
        function handleChange(event) {
            if (event.target.matches("[data-picker-media-selected]")) {
                rememberedMediaSelected = event.target.checked;
                return;
            }
            if (!event.target.matches("[data-picker-radio]")) return;
            abandonOpen();
            if (activeCheck) abandonCheck();
            if (busy) return;
            generation += 1; closePreview(); say(""); refresh();
        }
        function handleClick(event) {
            const artwork = event.target.closest("[data-picker-preview-only]");
            if (artwork && root.contains(artwork) && !event.target.closest("input, label, button, a, video")) {
                const checkbox = root.querySelector("[data-picker-media-selected]");
                if (checkbox && !checkbox.disabled) checkbox.click();
                return;
            }
            const more = event.target.closest("[data-picker-more]");
            if (more && root.contains(more)) {
                const group = root.querySelector(`[data-picker-group="${more.dataset.pickerMore === "audio" ? "audio" : "video"}"]`);
                const hidden = Array.from(group.querySelectorAll("[data-picker-option][hidden]"));
                hidden.slice(0, PAGE_SIZE).forEach(row => { row.hidden = false; });
                more.hidden = hidden.length <= PAGE_SIZE;
                return;
            }
            const button = event.target.closest("[data-ytdlp-action]");
            if (button && root.contains(button) && button.dataset.ytdlpAction === "open") {
                event.preventDefault(); event.stopPropagation();
                if (!button.disabled) void openSelected(button);
                return;
            }
            if (!button || !root.contains(button) || button.disabled) return;
            event.preventDefault();
            if (button.dataset.ytdlpAction === "download") save();
            else if (button.dataset.ytdlpAction === "legacy") save(selected(root), root, true);
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
        const controller = {reconcile, save, destroy() {
            destroyed = true; generation += 1;
            abandonOpen();
            if (activeController) activeController.abort();
            if (coverController) coverController.abort();
            closePreview();
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
            documentObject.querySelectorAll(".batch-result-card").forEach(scope => {
                ensureLegacyMediaSelection(scope, documentObject);
                refreshMediaSelectAll(scope);
            });
        }
        scan();
        documentObject.addEventListener("change", event => {
            if (!event.target.matches("[data-picker-media-selected], [data-picker-legacy-selected]")) return;
            const scope = event.target.closest(".batch-result-card");
            if (scope) refreshMediaSelectAll(scope);
        });
        documentObject.addEventListener("click", async event => {
            const button = event.target.closest("[data-picker-download-all], [data-picker-select-all]");
            const scope = button && button.closest(".batch-result-card");
            if (!scope || button.disabled) return;
            event.preventDefault();
            ensureLegacyMediaSelection(scope, documentObject);
            if (button.matches("[data-picker-select-all]")) {
                const roots = mediaRoots(scope).concat(legacyMediaNodes(scope));
                selectMedia(scope, !roots.every(mediaSelected));
                refreshMediaSelectAll(scope);
                return;
            }
            const snapshot = snapshotMedia(scope, documentObject, windowObject);
            const legacyApi = windowObject.AnimeImagesBatchSelectionDownload;
            const oldItems = legacyApi ? legacyApi.snapshot(scope) : [];
            const anchor = scope.children[0];
            const isCurrent = () => scope.isConnected && anchor && scope.contains(anchor);
            if (!snapshot.length && !oldItems.length) {
                const status = scope.querySelector("[data-picker-status]");
                if (status) status.textContent = "请先选择需要保存的资源。";
                return;
            }
            button.disabled = true;
            try {
                await downloadMedia(snapshot, isCurrent);
                if (oldItems.length && isCurrent()) await legacyApi.save(oldItems, scope, isCurrent);
            } catch (error) {
                const status = scope.querySelector("[data-picker-status]");
                if (isCurrent() && status) status.textContent = error.message || "下载未完成，未自动重试。";
            }
            finally { if (button.isConnected) button.disabled = false; }
        });
        if (windowObject.MutationObserver) {
            const observer = new windowObject.MutationObserver(scan);
            observer.observe(documentObject.body, {childList: true, subtree: true});
        }
    }
    return Object.freeze({SCHEMA, PAGE_SIZE, selected, sourceHttps, sourceLink, sameOriginPath,
        csrf, validateItem, validateLinkItem, descriptor, resolveLink, mount, attach,
        mediaRoots, mediaSelected, selectMedia, snapshotMedia, downloadMedia, legacyMediaNodes});
}));
