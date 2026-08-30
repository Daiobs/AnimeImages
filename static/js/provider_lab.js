(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesProviderLab = api;
    }
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const STATUS_LABELS = Object.freeze({
        waiting: "WAITING",
        running: "RUNNING",
        success: "SUCCESS",
        unsupported: "UNSUPPORTED",
        not_configured: "NOT CONFIGURED",
        timeout: "TIMEOUT",
        failed: "FAILED",
    });
    const ERROR_MESSAGES = Object.freeze({
        PROVIDER_NOT_SELECTED: "请选择有效的 Provider。",
        PROVIDER_NOT_CONFIGURED: "当前环境未配置该 Provider。",
        PROVIDER_UNSUPPORTED: "该 Provider 不支持当前链接。",
        PROVIDER_TIMEOUT: "该 Provider 在限定时间内未返回。",
        PROVIDER_OUTPUT_TOO_LARGE: "Provider 返回结构超过安全上限。",
        PROVIDER_INVALID_JSON: "Provider 返回格式无效。",
        PROVIDER_SCHEMA_INVALID: "Provider 返回结构未通过校验。",
        PROVIDER_PROCESS_FAILED: "Provider 执行失败。",
        PROVIDER_RESULT_EMPTY: "Provider 未返回可比较的资源。",
        PROVIDER_LICENSE_RESTRICTED: "该 Provider 仅限显式启用的非商业本地研究。",
        PROVIDER_RATE_LIMITED: "请求过于频繁，请稍后重试。",
        PROVIDER_POLICY_REJECTED: "当前受控执行选项未通过安全策略。",
    });
    const ATTEMPT_OUTCOME_LABELS = Object.freeze({
        success: "成功",
        failed: "失败",
        timeout: "超时",
        unsupported: "不支持",
        empty: "无结果",
        policy_rejected: "策略拒绝",
        skipped: "跳过",
    });

    function safeStatus(value) {
        const status = String(value || "").toLowerCase();
        return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? status : "failed";
    }

    function selectedProviderIds(inputs, maximum) {
        const values = Array.from(inputs || [])
            .filter((input) => Boolean(input && input.checked && !input.disabled))
            .map((input) => String(input.value || ""))
            .filter(Boolean);
        const unique = Array.from(new Set(values));
        if (!unique.length || unique.length > Number(maximum || 5)) {
            return [];
        }
        return unique;
    }

    function readVideodlInventory(documentObject) {
        const node = documentObject.querySelector("#videodl-backend-inventory");
        if (!node) { return {status: "unavailable", items: []}; }
        try {
            const payload = JSON.parse(node.textContent || "{}");
            return {
                status: String(payload.status || "unavailable"),
                items: Array.isArray(payload.items) ? payload.items : [],
            };
        } catch (_error) {
            return {status: "unavailable", items: []};
        }
    }

    function videodlOptionError(message) {
        const error = new Error("videodl_options_invalid");
        error.userMessage = message;
        return error;
    }

    function videodlOptions(form, inventory) {
        const mode = String(form.elements.videodl_mode?.value || "");
        const category = String(form.elements.videodl_backend_category?.value || "");
        const exactBackend = String(form.elements.videodl_specific_backend?.value || "");
        const confirmed = Boolean(form.elements.videodl_external_sharing_confirmed?.checked);
        const commonIds = Array.from(
            form.querySelectorAll('input[name="videodl_common_backends"]:checked'),
        ).map((item) => String(item.value || "")).filter(Boolean);
        const available = new Set(
            (inventory?.items || [])
                .filter((item) => Boolean(item && item.available))
                .map((item) => `${item.backend_category}:${item.backend_id}`),
        );
        let backendIds = [];
        let backendCategory = "";
        let externalSharingConfirmed = false;

        if (mode === "specific_backend") {
            backendCategory = category;
            backendIds = exactBackend ? [exactBackend] : [];
            if (!backendIds.length || !available.has(`${backendCategory}:${exactBackend}`)) {
                throw videodlOptionError("请选择可用的固定 Backend。");
            }
            externalSharingConfirmed = backendCategory === "common" && confirmed;
            if (backendCategory === "common" && !externalSharingConfirmed) {
                throw videodlOptionError("使用 Common Backend 前必须确认外部共享。");
            }
        } else if (mode === "dedicated_only") {
            backendCategory = "dedicated";
        } else if (mode === "common_only" || mode === "reviewed_auto") {
            backendCategory = "common";
            backendIds = Array.from(new Set(commonIds));
            if (mode === "common_only" && !backendIds.length) {
                throw videodlOptionError("Common Only 至少选择一个 Common Backend。");
            }
            if (backendIds.some((backendId) => !available.has(`common:${backendId}`))) {
                throw videodlOptionError("所选 Common Backend 当前不可用。");
            }
            externalSharingConfirmed = backendIds.length > 0 && confirmed;
            if (backendIds.length && !externalSharingConfirmed) {
                throw videodlOptionError("使用 Common Backend 前必须确认外部共享。");
            }
        } else if (mode !== "legacy_auto") {
            throw videodlOptionError("请选择有效的 videodl 执行模式。");
        }
        return {
            mode,
            backend_ids: backendIds,
            backend_category: backendCategory,
            external_sharing_confirmed: externalSharingConfirmed,
        };
    }

    async function runWithConcurrency(items, maximum, worker) {
        const queue = Array.from(items || []);
        const limit = Math.max(1, Math.min(queue.length || 1, Number(maximum || 1)));
        const results = new Array(queue.length);
        let cursor = 0;
        async function consume() {
            while (cursor < queue.length) {
                const index = cursor;
                cursor += 1;
                try {
                    results[index] = await worker(queue[index], index);
                } catch (error) {
                    results[index] = {error};
                }
            }
        }
        await Promise.all(Array.from({length: limit}, consume));
        return results;
    }

    function errorMessage(code) {
        return ERROR_MESSAGES[String(code || "")] || "Provider 执行失败。";
    }

    function formatBytes(value) {
        const size = Number(value || 0);
        if (!Number.isFinite(size) || size <= 0) {
            return "大小未知";
        }
        if (size >= 1024 * 1024 * 1024) {
            return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
        }
        if (size >= 1024 * 1024) {
            return `${(size / (1024 * 1024)).toFixed(1)} MB`;
        }
        if (size >= 1024) {
            return `${(size / 1024).toFixed(1)} KB`;
        }
        return `${Math.round(size)} B`;
    }

    function formatDuration(value) {
        const seconds = Number(value || 0);
        return Number.isFinite(seconds) && seconds > 0 ? `${seconds.toFixed(1)}s` : "时长未知";
    }

    function jsonFilename(result) {
        const provider = String(result && result.provider_id || "provider")
            .replace(/[^A-Za-z0-9_-]+/g, "-")
            .slice(0, 64);
        return `${provider || "provider"}-sanitized-result.json`;
    }

    function csrfToken(form) {
        return String(form.querySelector('[name="csrfmiddlewaretoken"]')?.value || "");
    }

    async function requestJson(fetchImpl, url, token, body) {
        const response = await fetchImpl(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": token,
            },
            body: JSON.stringify(body),
        });
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {status: "failed", error_code: "PROVIDER_INVALID_JSON"};
        }
        if (!response.ok) {
            const error = new Error("provider_lab_request_failed");
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function setText(card, field, value) {
        const node = card.querySelector(`[data-field="${field}"]`);
        if (node) {
            node.textContent = String(value || "-");
        }
    }

    function setCardStatus(card, status) {
        const safe = safeStatus(status);
        card.dataset.providerStatus = safe;
        const label = card.querySelector("[data-provider-status-label]");
        if (label) {
            label.textContent = STATUS_LABELS[safe];
        }
    }

    function resetCard(card) {
        setCardStatus(card, "waiting");
        ["claimed_platform", "selected_backend", "result_kind", "elapsed_ms", "title", "author"]
            .forEach((field) => setText(card, field, "-"));
        setText(card, "asset_count", "0");
        setText(card, "relationship_count", "0");
        const content = card.querySelector("[data-provider-content]");
        const error = card.querySelector("[data-provider-error]");
        const assets = card.querySelector("[data-provider-assets]");
        const attempts = card.querySelector("[data-provider-attempts]");
        const attemptRows = card.querySelector("[data-provider-attempt-rows]");
        const raw = card.querySelector("[data-provider-raw]");
        if (content) { content.hidden = true; content.textContent = ""; }
        if (error) { error.hidden = true; error.textContent = ""; }
        if (assets) { assets.replaceChildren(); }
        if (attempts) { attempts.hidden = true; }
        if (attemptRows) { attemptRows.replaceChildren(); }
        if (raw) { raw.hidden = true; }
        card.querySelectorAll("[data-copy-json], [data-download-json]")
            .forEach((button) => { button.disabled = true; });
    }

    function metaFragment(documentObject, asset) {
        const fragment = documentObject.createDocumentFragment();
        const values = [
            ["类型", asset.media_type],
            ["角色", asset.role],
            ["格式", asset.declared_extension || "未知"],
            ["尺寸", asset.width && asset.height ? `${asset.width}×${asset.height}` : "未知"],
            ["时长", formatDuration(asset.duration_seconds)],
            ["大小", formatBytes(asset.size_bytes)],
            ["质量", asset.quality_label || "未知"],
            ["Codec", asset.codec || "未知"],
            ["Host", asset.host_class || "未知"],
            ["合并", asset.requires_merge ? "需要" : "否"],
            ["独立音频", asset.audio_asset_present ? "存在" : "否"],
            ["Header", asset.headers_present ? "需要" : "否"],
            ["Cookie", asset.requires_cookie ? "需要" : "否"],
        ];
        values.forEach(([label, value]) => {
            const span = documentObject.createElement("span");
            const strong = documentObject.createElement("strong");
            strong.textContent = `${label} `;
            span.append(strong, documentObject.createTextNode(String(value || "-")));
            fragment.append(span);
        });
        return fragment;
    }

    function renderAssets(documentObject, container, result) {
        container.replaceChildren();
        (result.assets || []).forEach((asset) => {
            const row = documentObject.createElement("div");
            row.className = "provider-lab-asset";
            const meta = documentObject.createElement("div");
            meta.className = "provider-lab-asset-meta";
            meta.append(metaFragment(documentObject, asset));
            const button = documentObject.createElement("button");
            button.type = "button";
            button.textContent = "浏览器测试";
            button.dataset.labInspect = "1";
            button.dataset.resourceToken = String(asset.resource_token || "");
            button.dataset.requestId = String(result.request_id || "");
            button.dataset.providerId = String(result.provider_id || "");
            button.dataset.assetId = String(asset.asset_id || "");
            button.dataset.mediaType = String(asset.media_type || "unknown");
            button.disabled = !asset.resource_token;
            const inspection = documentObject.createElement("div");
            inspection.className = "provider-lab-inspection";
            inspection.dataset.labInspection = "1";
            inspection.textContent = "尚未请求媒体描述符。";
            row.append(meta, button, inspection);
            container.append(row);
        });
    }

    function attemptDisplayRows(attempts) {
        return (Array.isArray(attempts) ? attempts : []).slice(0, 12).map((attempt) => ({
            ordinal: Number(attempt.ordinal || 0),
            backend: String(attempt.backend_id || "-"),
            category: String(attempt.backend_category || "-"),
            outcome: ATTEMPT_OUTCOME_LABELS[String(attempt.outcome || "")] || "失败",
            elapsed: `${Math.max(0, Number(attempt.elapsed_ms || 0))} ms`,
            candidates: Math.max(0, Number(attempt.candidate_count || 0)),
            selected: attempt.selected ? "已选择" : "-",
            failure: String(attempt.safe_failure_code || ""),
            externalSharing: Boolean(attempt.external_sharing),
        }));
    }

    function renderAttempts(documentObject, card, result) {
        const section = card.querySelector("[data-provider-attempts]");
        const body = card.querySelector("[data-provider-attempt-rows]");
        if (!section || !body) { return; }
        body.replaceChildren();
        const rows = attemptDisplayRows(result?.attempts);
        rows.forEach((attempt) => {
            const row = documentObject.createElement("tr");
            if (attempt.selected) { row.classList.add("is-selected"); }
            [
                attempt.ordinal,
                attempt.backend,
                attempt.category === "common" ? "Common" : "Dedicated",
                attempt.failure ? `${attempt.outcome} · ${attempt.failure}` : attempt.outcome,
                attempt.elapsed,
                attempt.candidates,
                attempt.selected,
            ].forEach((value) => {
                const cell = documentObject.createElement("td");
                cell.textContent = String(value);
                row.append(cell);
            });
            body.append(row);
        });
        section.hidden = rows.length === 0;
    }

    function renderResult(card, result, documentObject) {
        const status = safeStatus(result && result.status);
        setCardStatus(card, status);
        setText(card, "claimed_platform", result.claimed_platform || "-");
        setText(card, "selected_backend", result.selected_backend || "-");
        setText(card, "result_kind", result.result_kind || "-");
        setText(card, "elapsed_ms", Number(result.elapsed_ms || 0) ? `${result.elapsed_ms} ms` : "-");
        setText(card, "title", result.metadata?.title || "-");
        setText(card, "author", result.metadata?.author || "-");
        setText(card, "asset_count", String((result.assets || []).length));
        setText(card, "relationship_count", String((result.relationships || []).length));
        const content = card.querySelector("[data-provider-content]");
        if (content && result.metadata?.content_summary) {
            content.textContent = result.metadata.content_summary;
            content.hidden = false;
        }
        const error = card.querySelector("[data-provider-error]");
        if (error && status !== "success") {
            error.textContent = errorMessage(result.error_code);
            error.hidden = false;
        }
        const assets = card.querySelector("[data-provider-assets]");
        if (assets) {
            renderAssets(documentObject, assets, result);
        }
        renderAttempts(documentObject, card, result);
        const raw = card.querySelector("[data-provider-raw]");
        const rawOutput = card.querySelector("[data-provider-raw-output]");
        if (raw && rawOutput) {
            rawOutput.textContent = JSON.stringify(result.raw_shape || {}, null, 2);
            raw.hidden = status !== "success";
        }
        card.querySelectorAll("[data-copy-json], [data-download-json]")
            .forEach((button) => { button.disabled = false; });
    }

    async function probeRange(fetchImpl, url) {
        try {
            const response = await fetchImpl(url, {
                method: "GET",
                credentials: "omit",
                cache: "no-store",
                headers: {Range: "bytes=0-4095"},
            });
            if (!response.body || typeof response.body.getReader !== "function") {
                return {cors: "readable", status: response.status, bytes: 0};
            }
            const reader = response.body.getReader();
            let bytes = 0;
            while (bytes < 4096) {
                const item = await reader.read();
                if (item.done) { break; }
                bytes += Math.min(item.value?.byteLength || 0, 4096 - bytes);
                if (bytes >= 4096) { break; }
            }
            try { await reader.cancel(); } catch (_error) { /* expected bounded stop */ }
            return {
                cors: "readable",
                status: response.status,
                bytes,
                contentType: response.headers.get("Content-Type") || "",
                contentRange: response.headers.get("Content-Range") || "",
            };
        } catch (_error) {
            return {cors: "blocked_or_failed", status: 0, bytes: 0};
        }
    }

    function mediaElement(documentObject, descriptor) {
        if (descriptor.media_type === "image") {
            const image = documentObject.createElement("img");
            image.alt = "Provider Lab 临时图片测试";
            image.loading = "eager";
            image.decoding = "async";
            image.src = descriptor.url;
            return image;
        }
        if (descriptor.media_type === "video") {
            const video = documentObject.createElement("video");
            video.controls = true;
            video.muted = true;
            video.preload = "metadata";
            video.playsInline = true;
            video.src = descriptor.url;
            return video;
        }
        return null;
    }

    async function inspectAsset(button, options) {
        const container = button.closest(".provider-lab-asset")?.querySelector("[data-lab-inspection]");
        if (!container) { return; }
        button.disabled = true;
        container.textContent = "正在申请临时描述符并测试浏览器可用性...";
        try {
            const descriptor = await requestJson(
                options.fetch,
                options.resourceUrl,
                options.csrf,
                {
                    resource_token: button.dataset.resourceToken,
                    request_id: button.dataset.requestId,
                    provider_id: button.dataset.providerId,
                    asset_id: button.dataset.assetId,
                },
            );
            container.replaceChildren();
            if (!descriptor.inspectable || !descriptor.url) {
                container.textContent = "该资源需要 Provider 请求上下文，浏览器测试未暴露凭据。";
                return;
            }
            const status = options.document.createElement("p");
            status.textContent = "描述符已取得，正在读取媒体元数据与有限 Range。";
            const media = mediaElement(options.document, descriptor);
            container.append(status);
            if (media) { container.append(media); }
            const probe = await probeRange(options.fetch, descriptor.url);
            status.textContent = [
                `CORS: ${probe.cors}`,
                `HTTP: ${probe.status || "不可读"}`,
                `读取: ${probe.bytes || 0} B`,
                probe.contentType ? `类型: ${probe.contentType}` : "",
                probe.contentRange ? `Range: ${probe.contentRange}` : "",
            ].filter(Boolean).join(" · ");
        } catch (error) {
            container.textContent = errorMessage(error.payload?.error_code);
        } finally {
            button.disabled = false;
        }
    }

    async function copyJson(result, navigatorObject) {
        const text = JSON.stringify(result, null, 2);
        if (!navigatorObject?.clipboard?.writeText) {
            throw new Error("clipboard_unavailable");
        }
        await navigatorObject.clipboard.writeText(text);
    }

    function downloadJson(result, windowObject, documentObject) {
        const blob = new windowObject.Blob([JSON.stringify(result, null, 2)], {type: "application/json"});
        const objectUrl = windowObject.URL.createObjectURL(blob);
        const link = documentObject.createElement("a");
        link.href = objectUrl;
        link.download = jsonFilename(result);
        link.click();
        windowObject.setTimeout(() => windowObject.URL.revokeObjectURL(objectUrl), 0);
    }

    function syncVideodlControls(form) {
        const controls = form.querySelector("[data-videodl-controls]");
        if (!controls) { return null; }
        const provider = form.querySelector('input[name="providers"][value="videodl_repo"]');
        const mode = String(form.elements.videodl_mode?.value || "reviewed_auto");
        const category = String(form.elements.videodl_backend_category?.value || "dedicated");
        const selectedCommon = form.querySelectorAll(
            'input[name="videodl_common_backends"]:checked',
        ).length;
        controls.hidden = !provider?.checked || provider.disabled;
        const categoryField = form.querySelector("[data-videodl-specific-category]");
        const exactField = form.querySelector("[data-videodl-specific-backend]");
        const commonField = form.querySelector("[data-videodl-common-backends]");
        const sharingField = form.querySelector("[data-videodl-sharing-confirmation]");
        const specific = mode === "specific_backend";
        if (categoryField) { categoryField.hidden = !specific; }
        if (exactField) { exactField.hidden = !specific; }
        if (commonField) {
            commonField.hidden = !["common_only", "reviewed_auto"].includes(mode);
        }
        if (sharingField) {
            sharingField.hidden = !(
                (specific && category === "common")
                || (["common_only", "reviewed_auto"].includes(mode) && selectedCommon > 0)
            );
        }
        const exactSelect = form.elements.videodl_specific_backend;
        if (exactSelect) {
            Array.from(exactSelect.options || []).forEach((option) => {
                if (!option.value) { return; }
                const visible = option.dataset.backendCategory === category;
                option.hidden = !visible;
                option.disabled = !visible || option.dataset.backendAvailable !== "true";
            });
            const selectedOption = exactSelect.selectedOptions?.[0];
            if (selectedOption?.disabled) { exactSelect.value = ""; }
        }
        return {mode, category, selectedCommon, visible: !controls.hidden};
    }

    function createController(documentObject, overrides) {
        const options = overrides || {};
        const root = documentObject.querySelector("[data-provider-lab]");
        const form = documentObject.querySelector("[data-provider-lab-form]");
        if (!root || !form) {
            return null;
        }
        if (root.__animeImagesProviderLabController) {
            return root.__animeImagesProviderLabController;
        }
        const fetchImpl = options.fetch || globalThis.fetch.bind(globalThis);
        const windowObject = options.window || globalThis;
        const navigatorObject = options.navigator || globalThis.navigator;
        const runUrl = options.runUrl || root.dataset.runUrl;
        const resourceUrl = options.resourceUrl || root.dataset.resourceUrl;
        const maxConcurrency = Number(options.maxConcurrency || root.dataset.maxConcurrency || 3);
        const maxProviders = Number(options.maxProviders || root.dataset.maxProviders || 5);
        const videodlInventory = options.videodlInventory || readVideodlInventory(documentObject);
        const resultByProvider = new Map();
        let running = false;

        async function runOne(providerId, source, csrf, providerOptions) {
            const card = documentObject.querySelector(`[data-provider-card="${providerId}"]`);
            if (!card) { return null; }
            resetCard(card);
            setCardStatus(card, "running");
            try {
                const body = {
                    source,
                    provider_id: providerId,
                };
                if (providerId === "videodl_repo" && providerOptions) {
                    body.options = providerOptions;
                }
                const result = await requestJson(fetchImpl, runUrl, csrf, body);
                resultByProvider.set(providerId, result);
                renderResult(card, result, documentObject);
                return result;
            } catch (error) {
                const result = error.payload || {status: "failed", error_code: "PROVIDER_PROCESS_FAILED"};
                result.provider_id = providerId;
                resultByProvider.set(providerId, result);
                renderResult(card, result, documentObject);
                return result;
            }
        }

        async function submit(event) {
            event.preventDefault();
            if (running) { return; }
            const message = documentObject.querySelector("[data-provider-lab-form-message]");
            const source = String(form.elements.source?.value || "").trim();
            const providers = selectedProviderIds(
                form.querySelectorAll('input[name="providers"]'),
                maxProviders,
            );
            if (!source || source.length > 4096 || !providers.length) {
                if (message) { message.textContent = "请输入有效内容并选择 1 至 5 个可用 Provider。"; }
                return;
            }
            let controlledVideodlOptions = null;
            if (providers.includes("videodl_repo")) {
                try {
                    controlledVideodlOptions = videodlOptions(form, videodlInventory);
                } catch (error) {
                    if (message) {
                        message.textContent = error.userMessage || "videodl 受控执行选项无效。";
                    }
                    return;
                }
            }
            running = true;
            const submitButton = form.querySelector('[type="submit"]');
            if (submitButton) { submitButton.disabled = true; }
            if (message) { message.textContent = `正在运行 ${providers.length} 个独立测试...`; }
            try {
                await runWithConcurrency(
                    providers,
                    maxConcurrency,
                    (providerId) => runOne(
                        providerId,
                        source,
                        csrfToken(form),
                        providerId === "videodl_repo" ? controlledVideodlOptions : null,
                    ),
                );
                if (message) { message.textContent = "本轮测试已完成；结果不会持久化。"; }
            } finally {
                running = false;
                if (submitButton) { submitButton.disabled = false; }
            }
        }

        async function click(event) {
            const inspect = event.target.closest?.("[data-lab-inspect]");
            if (inspect) {
                event.preventDefault();
                await inspectAsset(inspect, {
                    document: documentObject,
                    fetch: fetchImpl,
                    csrf: csrfToken(form),
                    resourceUrl,
                });
                return;
            }
            const copy = event.target.closest?.("[data-copy-json]");
            const download = event.target.closest?.("[data-download-json]");
            if (!copy && !download) { return; }
            const card = event.target.closest("[data-provider-card]");
            const result = resultByProvider.get(card?.dataset.providerCard);
            if (!result) { return; }
            event.preventDefault();
            if (copy) {
                try {
                    await copyJson(result, navigatorObject);
                    copy.textContent = "已复制";
                    windowObject.setTimeout(() => { copy.textContent = "复制脱敏 JSON"; }, 1200);
                } catch (_error) {
                    copy.textContent = "复制失败";
                }
            } else {
                downloadJson(result, windowObject, documentObject);
            }
        }

        form.addEventListener("submit", submit);
        const change = () => syncVideodlControls(form);
        form.addEventListener("change", change);
        root.addEventListener("click", click);
        syncVideodlControls(form);
        const controller = {
            submit,
            runOne,
            resultByProvider,
            destroy() {
                form.removeEventListener("submit", submit);
                form.removeEventListener("change", change);
                root.removeEventListener("click", click);
                if (root.__animeImagesProviderLabController === controller) {
                    delete root.__animeImagesProviderLabController;
                }
            },
        };
        root.__animeImagesProviderLabController = controller;
        return controller;
    }

    function initializeWhenReady(documentObject, initialize) {
        if (!documentObject || typeof initialize !== "function") {
            return false;
        }
        if (documentObject.readyState === "loading") {
            documentObject.addEventListener("DOMContentLoaded", initialize, {once: true});
            return false;
        }
        const result = initialize();
        if (result === null && documentObject.readyState !== "complete") {
            documentObject.addEventListener("DOMContentLoaded", initialize, {once: true});
            return false;
        }
        return result !== null;
    }

    if (typeof document !== "undefined") {
        initializeWhenReady(document, function () {
            createController(document);
        });
    }

    return {
        ATTEMPT_OUTCOME_LABELS,
        ERROR_MESSAGES,
        STATUS_LABELS,
        attemptDisplayRows,
        createController,
        errorMessage,
        formatBytes,
        formatDuration,
        jsonFilename,
        initializeWhenReady,
        probeRange,
        requestJson,
        readVideodlInventory,
        runWithConcurrency,
        safeStatus,
        selectedProviderIds,
        syncVideodlControls,
        videodlOptions,
    };
}));
