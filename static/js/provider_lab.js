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
        queued: "QUEUED",
        cancelled: "CANCELLED",
        skipped: "NOT RUN",
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
    const ROUTE_PROVIDER_IDS = Object.freeze({yt_dlp_existing: "yt_dlp_generic",
        gallery_dl_existing: "gallery_dl_generic", parsehub_repo: "parsehub_generic", videodl_repo: "videodl_repo"});

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
                .filter((item) => Boolean(item && item.controlled_available))
                .map((item) => `${item.backend_category}:${item.backend_id}`),
        );
        const inventoryById = new Map(
            (inventory?.items || []).map((item) => [
                `${item.backend_category}:${item.backend_id}`,
                item,
            ]),
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
            const descriptor = inventoryById.get(`${backendCategory}:${exactBackend}`) || {};
            const requiresSharing = Boolean(descriptor.external_sharing_required);
            externalSharingConfirmed = requiresSharing && confirmed;
            if (requiresSharing && !externalSharingConfirmed) {
                throw videodlOptionError("使用该 Backend 前必须确认外部共享。");
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

    function createExecutionQueue(options) {
        const settings = options || {};
        const concurrency = Math.max(1, Math.min(3, Number(settings.concurrency || 3)));
        const timeoutMs = Number(settings.timeoutMs || 45000);
        const budgetMs = Number(settings.budgetMs || 180000);
        const jobs = new Map();
        const active = new Map();
        let generation = 0;
        let budgetTimer = null;
        let stopped = false;
        let context = null;
        const emit = (job) => settings.onState?.({...job}, context);
        const finishIfIdle = () => {
            if (!active.size && !Array.from(jobs.values()).some((job) => job.status === "queued")) {
                clearTimeout(budgetTimer);
                budgetTimer = null;
                settings.onIdle?.();
            }
        };
        function pump() {
            if (stopped) return;
            for (const job of jobs.values()) {
                if (active.size >= concurrency) break;
                if (job.status !== "queued") continue;
                const currentGeneration = generation;
                const runContext = context;
                const controller = new AbortController();
                active.set(job.id, controller);
                job.status = "running";
                emit(job);
                let timer;
                let timedOut = false;
                const aborted = new Promise((_resolve, reject) => {
                    controller.signal.addEventListener("abort", () => reject(new Error("cancelled")), {once: true});
                    timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
                });
                Promise.race([
                    Promise.resolve().then(() => {
                        if (generation !== currentGeneration || controller.signal.aborted) throw new Error("cancelled");
                        return settings.execute(job.id, runContext, controller.signal);
                    }),
                    aborted,
                ]).then((result) => {
                    if (generation !== currentGeneration || jobs.get(job.id) !== job) return;
                    job.result = result;
                    job.status = safeStatus(result?.status);
                    emit(job);
                }, (error) => {
                    if (generation !== currentGeneration || jobs.get(job.id) !== job) return;
                    job.status = timedOut ? "timeout" : controller.signal.aborted ? "cancelled" : "failed";
                    job.result = error.payload || {provider_id: job.id, status: job.status,
                        error_code: timedOut ? "PROVIDER_TIMEOUT" : "PROVIDER_PROCESS_FAILED"};
                    emit(job);
                }).finally(() => {
                    clearTimeout(timer);
                    if (generation !== currentGeneration) return;
                    active.delete(job.id);
                    pump();
                    finishIfIdle();
                });
            }
            finishIfIdle();
        }
        function cancel(reason) {
            stopped = true;
            clearTimeout(budgetTimer);
            budgetTimer = null;
            for (const job of jobs.values()) {
                if (job.status === "queued") {
                    job.status = reason === "budget" ? "skipped" : "cancelled";
                    job.reason = reason === "budget" ? "整体预算已用尽，未执行" : "排队任务已取消";
                    emit(job);
                }
            }
            active.forEach((controller) => controller.abort());
            finishIfIdle();
        }
        function start(ids, nextContext) {
            cancel();
            generation += 1;
            active.clear();
            jobs.clear();
            stopped = false;
            context = nextContext;
            Array.from(new Set(ids)).forEach((id) => {
                const job = {id, status: "queued", result: null};
                jobs.set(id, job);
                emit(job);
            });
            budgetTimer = setTimeout(() => cancel("budget"), budgetMs);
            pump();
        }
        function retry(id) {
            const previous = jobs.get(id);
            if (!previous || active.has(id) || previous.status === "queued") return false;
            stopped = false;
            const job = {id, status: "queued", result: null};
            jobs.set(id, job);
            emit(job);
            if (!budgetTimer) budgetTimer = setTimeout(() => cancel("budget"), budgetMs);
            pump();
            return true;
        }
        return {start, retry, cancel, jobs, get activeCount() { return active.size; },
            invalidate() { cancel(); generation += 1; active.clear(); jobs.clear(); }};
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

    async function requestJson(fetchImpl, url, token, body, options) {
        const response = await fetchImpl(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": token,
            },
            body: JSON.stringify(body),
            ...(options?.signal ? {signal: options.signal} : {}),
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
            error.status = response.status;
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
        const scopeNote = card.querySelector("[data-provider-scope-note]");
        if (scopeNote) scopeNote.textContent = "";
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
            ["大小", `${formatBytes(asset.size_bytes)}${asset.size_is_estimate === true ? "（估计）" : ""}`],
            ["质量", asset.quality_label || "未知"],
            ["Codec", asset.codec || "未知"],
            ["视频编码", asset.vcodec || "未知"],
            ["音频编码", asset.acodec === "none" ? "无音轨" : asset.acodec || "未知"],
            ["帧率", asset.fps > 0 ? `${asset.fps} fps` : "未知"],
            ["码率", asset.bitrate > 0 ? `${asset.bitrate} kbps` : "未知"],
            ["视频轨 / 音频轨", `${asset.video_tracks || "未知"} / ${asset.audio_tracks || "未知"}`],
            ["交付", asset.delivery_state || "未知"],
            ["协议", asset.protocol || "未知"],
            ["来源", asset.metadata_source === "upstream_declared" ? "上游声明" : asset.metadata_source || "未知"],
            ["Host", asset.host_class || "未知"],
            ["合并", knownBoolean(asset.requires_merge, "需要")],
            ["独立音频", knownBoolean(asset.audio_asset_present, "存在")],
            ["Header", knownBoolean(asset.headers_present, "需要")],
            ["Cookie", knownBoolean(asset.requires_cookie, "需要")],
        ];
        if (asset.work_label) values.unshift(["作品", asset.work_label]);
        values.forEach(([label, value]) => {
            const span = documentObject.createElement("span");
            const strong = documentObject.createElement("strong");
            strong.textContent = `${label} `;
            span.append(strong, documentObject.createTextNode(String(value || "-")));
            fragment.append(span);
        });
        return fragment;
    }

    function knownBoolean(value, yes) {
        return value === true ? yes : value === false ? "否" : "未知";
    }

    function qualityOrder(assets) {
        return Array.from(assets || []).map((asset, index) => ({asset, index})).sort((left, right) => {
            const pixels = (asset) => Number(asset.width || 0) * Number(asset.height || 0);
            return pixels(right.asset) - pixels(left.asset) || left.index - right.index;
        }).map(({asset}) => asset);
    }

    function renderAssets(documentObject, container, result, ordered) {
        container.replaceChildren();
        const assets = ordered || result.assets || [];
        assets.forEach((asset, index) => {
            const row = documentObject.createElement("div");
            row.className = "provider-lab-asset";
            row.dataset.assetId = String(asset.asset_id || "");
            row.hidden = index >= 8;
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
            const select = documentObject.createElement("button");
            select.type = "button";
            select.textContent = "本次使用";
            select.dataset.labSelect = "1";
            select.dataset.assetId = String(asset.asset_id || "");
            select.setAttribute("aria-pressed", "false");
            const inspection = documentObject.createElement("div");
            inspection.className = "provider-lab-inspection";
            inspection.dataset.labInspection = "1";
            inspection.textContent = "尚未请求媒体描述符。";
            row.append(meta, select, button, inspection);
            container.append(row);
        });
        if (assets.length > 8) {
            const expand = documentObject.createElement("button");
            expand.type = "button";
            expand.dataset.labExpand = "1";
            expand.textContent = `展开其余 ${assets.length - 8} 个格式`;
            container.append(expand);
        }
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

    function scopeMessage(metadata) {
        const data = metadata || {};
        const messages = [];
        if (data.truncated_count > 0) messages.push(`另有 ${data.truncated_count} 项未展开。`);
        const scope = data.unexpanded_scope;
        if (scope && (typeof scope === "string" || scope.bounded || scope.completion === "unknown")) {
            messages.push("范围受限，完整性未知；仅展示本次已取得的候选。");
        } else if (data.truncated && !data.truncated_count) {
            messages.push("存在未展开内容，完整性未知。");
        }
        return messages.join(" ");
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
        if (result.provider_id === "animeimages_current") setText(card, "selected_backend", "现有路线基线（复合结果）");
        const content = card.querySelector("[data-provider-content]");
        if (content && result.metadata?.content_summary) {
            content.textContent = result.metadata.content_summary;
            content.hidden = false;
        }
        const error = card.querySelector("[data-provider-error]");
        if (error && status !== "success") {
            error.textContent = errorMessage(result.error_code);
            if (status === "cancelled") error.textContent = "已取消；其他已完成结果保留。";
            if (status === "skipped") error.textContent = "本次未执行：整体预算已用尽。";
            error.hidden = false;
        }
        if (error && status === "success") { error.hidden = true; error.textContent = ""; }
        const assets = card.querySelector("[data-provider-assets]");
        if (assets) {
            renderAssets(documentObject, assets, result);
        }
        renderAttempts(documentObject, card, result);
        const scopeNote = card.querySelector("[data-provider-scope-note]");
        if (scopeNote) scopeNote.textContent = scopeMessage(result.metadata);
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
                `浏览器读取: ${probe.cors === "blocked_or_failed" ? "不可读（网络或来源限制）" : probe.cors}`,
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
        const text = JSON.stringify(diagnosticResult(result), null, 2);
        if (!navigatorObject?.clipboard?.writeText) {
            throw new Error("clipboard_unavailable");
        }
        await navigatorObject.clipboard.writeText(text);
    }

    function downloadJson(result, windowObject, documentObject) {
        const blob = new windowObject.Blob([JSON.stringify(diagnosticResult(result), null, 2)], {type: "application/json"});
        const objectUrl = windowObject.URL.createObjectURL(blob);
        const link = documentObject.createElement("a");
        link.href = objectUrl;
        link.download = jsonFilename(result);
        link.click();
        windowObject.setTimeout(() => windowObject.URL.revokeObjectURL(objectUrl), 0);
    }

    function diagnosticResult(result) {
        return JSON.parse(JSON.stringify(result, (key, value) => (
            /(?:url|token|cookie|authorization|headers|source_reference)/i.test(key) ? undefined : value
        )));
    }

    function routeRequestBody(source, route, draft, reset) {
        if (!route || !route.applicable || !Number.isInteger(route.revision)) throw new Error("ROUTE_NOT_APPLICABLE");
        const body = {source_url: source, expected_revision: route.revision};
        if (!reset) Object.assign(body, {
            provider_order: Array.from(draft.provider_order),
            provider_options: draft.provider_options || {}, format_preferences: draft.format_preferences || {},
        });
        return body;
    }

    function createRouteEditor(documentObject, options) {
        const settings = options || {};
        const panel = documentObject.querySelector("[data-lab-route-editor]");
        if (!panel) return null;
        const message = panel.querySelector("[data-route-status]");
        const scope = panel.querySelector("[data-route-scope]");
        const orderNode = panel.querySelector("[data-route-order]");
        const addSelect = panel.querySelector("[data-route-add]");
        const confirm = panel.querySelector("[data-route-confirm]");
        const save = panel.querySelector("[data-route-save]");
        const reset = panel.querySelector("[data-route-reset]");
        const fields = panel.querySelector("[data-route-fields]");
        let route = null;
        let source = "";
        let order = [];
        let sequence = 0;
        let busy = false;
        const canConfigure = panel.dataset.canConfigure === "true";
        const text = (value) => { if (message) message.textContent = value; };
        function field(name) { return panel.querySelector(`[name="${name}"]`); }
        function buttons() {
            if (fields) fields.disabled = busy || !canConfigure || !route?.applicable;
            if (save) save.disabled = busy || !canConfigure || !route?.applicable || !confirm?.checked || !order.length;
            if (reset) reset.disabled = busy || !canConfigure || !route?.applicable || !route.active;
        }
        function renderOrder() {
            orderNode.replaceChildren();
            order.forEach((id, index) => {
                const li = documentObject.createElement("li");
                const label = documentObject.createElement("span");
                label.textContent = `${index === 0 ? "首选" : `备选 ${index}`} · ${id}`;
                li.append(label);
                for (const [action, symbol, title] of [["up", "↑", "向前"], ["down", "↓", "向后"], ["remove", "×", "移除"]]) {
                    const button = documentObject.createElement("button");
                    button.type = "button";
                    button.dataset.routeMove = action;
                    button.dataset.providerId = id;
                    button.textContent = symbol;
                    button.title = title;
                    button.setAttribute("aria-label", `${title} ${id}`);
                    button.disabled = action === "up" && index === 0 || action === "down" && index === order.length - 1;
                    li.append(button);
                }
                orderNode.append(li);
            });
            addSelect.replaceChildren();
            const placeholder = documentObject.createElement("option");
            placeholder.value = "";
            placeholder.textContent = "添加备选上游";
            addSelect.append(placeholder);
            (route?.allowed_provider_ids || []).filter((id) => !order.includes(id)).forEach((id) => {
                const option = documentObject.createElement("option");
                option.value = id; option.textContent = id; addSelect.append(option);
            });
            buttons();
        }
        function apply(snapshot) {
            route = snapshot;
            order = Array.from(snapshot.active ? snapshot.provider_order : []);
            scope.textContent = snapshot.applicable
                ? `${snapshot.source_host} · ${snapshot.active ? "人工路线" : "默认路线，尚未人工优选"} · 版本 ${snapshot.revision}`
                : `${snapshot.source_host} · 当前来源沿用成熟路线，保存不适用`;
            for (const name of ["max_height", "preferred_video_codec", "preferred_container"]) {
                const node = field(name);
                if (node) node.value = String(snapshot.format_preferences?.[name] || "");
            }
            const mode = field("route_videodl_mode");
            if (mode) mode.value = snapshot.provider_options?.videodl_repo?.mode || "dedicated_only";
            const backend = field("route_videodl_backend");
            if (backend) backend.value = snapshot.provider_options?.videodl_repo?.backend_id || "";
            if (confirm) confirm.checked = false;
            renderOrder();
        }
        async function load() {
            const input = settings.getSource();
            if (!input) { text("请先输入来源链接。"); return; }
            const version = ++sequence;
            busy = true; buttons(); text("正在读取平台路线...");
            try {
                const url = new URL(settings.url, settings.window.location.origin);
                url.searchParams.set("source_url", input);
                const response = await settings.fetch(url.href, {credentials: "same-origin", cache: "no-store"});
                const payload = await response.json();
                if (version !== sequence) return;
                if (!response.ok || payload.ok !== true) throw new Error("ROUTE_READ_FAILED");
                source = input;
                apply(payload.route);
                text(canConfigure ? "路线已读取；本次候选选择不会自动保存。" : "无路线配置权限，仅可查看和比较。");
            } catch (_error) {
                if (version === sequence) { route = null; text("路线读取失败，请重新读取。"); }
            } finally { if (version === sequence) { busy = false; buttons(); } }
        }
        async function persist(isReset) {
            if (busy || !canConfigure || !route?.applicable || source !== settings.getSource()) return;
            if (!isReset && !confirm?.checked) return;
            if (isReset && !settings.window.confirm(`恢复 ${route.source_host} 的通用默认路线？`)) return;
            const preferences = {};
            for (const name of ["max_height", "preferred_video_codec", "preferred_container"]) {
                const value = field(name)?.value;
                if (value) preferences[name] = name === "max_height" ? Number(value) : value;
            }
            const providerOptions = {};
            if (order.includes("videodl_repo")) {
                providerOptions.videodl_repo = {mode: field("route_videodl_mode")?.value || "dedicated_only"};
                if (providerOptions.videodl_repo.mode === "specific_backend") {
                    providerOptions.videodl_repo.backend_id = field("route_videodl_backend")?.value || "";
                }
            }
            const body = routeRequestBody(source, route, {provider_order: order,
                provider_options: providerOptions, format_preferences: preferences}, isReset);
            const version = ++sequence;
            busy = true; buttons();
            try {
                const response = await settings.fetch(settings.url, {method: isReset ? "DELETE" : "POST",
                    credentials: "same-origin", cache: "no-store", headers: {"Content-Type": "application/json",
                        "X-CSRFToken": settings.csrf()}, body: JSON.stringify(body)});
                const payload = await response.json();
                if (version !== sequence) return;
                if (response.status === 409) {
                    route = null;
                    text("路线已被其他操作更新。请重新读取后核对，不会覆盖较新的版本。");
                    return;
                }
                if (!response.ok || payload.ok !== true) {
                    text(payload.error_code === "CONFIGURATION_PERMISSION_REQUIRED" ? "缺少路线配置权限。"
                        : "路线未保存。请检查上游、Backend 和格式偏好。");
                    return;
                }
                apply(payload.route);
                text(isReset ? "已恢复默认路线。" : "平台路线已保存，后续解析使用新版本。");
            } catch (_error) { if (version === sequence) text("保存状态未确认，请重新读取路线后核对。"); }
            finally { if (version === sequence) { busy = false; buttons(); } }
        }
        function selectProvider(providerId) {
            const id = ROUTE_PROVIDER_IDS[providerId];
            if (!route?.applicable || !id || !route.allowed_provider_ids.includes(id)) {
                text("该候选不适用于此来源的持久路线，或路线尚未读取。");
                return;
            }
            order = [id, ...order.filter((value) => value !== id)];
            if (confirm) confirm.checked = false;
            renderOrder(); text("已调整路线草稿；尚未保存。");
        }
        panel.addEventListener("click", (event) => {
            if (event.target.closest?.("[data-route-load]")) { void load(); return; }
            if (event.target.closest?.("[data-route-save]")) { void persist(false); return; }
            if (event.target.closest?.("[data-route-reset]")) { void persist(true); return; }
            if (event.target.closest?.("[data-route-use-current]")) { selectProvider(settings.getSelection()?.providerId); return; }
            const move = event.target.closest?.("[data-route-move]");
            if (!move || !canConfigure || !route?.applicable) return;
            const index = order.indexOf(move.dataset.providerId);
            if (index < 0) return;
            if (move.dataset.routeMove === "remove") order.splice(index, 1);
            else {
                const target = index + (move.dataset.routeMove === "up" ? -1 : 1);
                if (target >= 0 && target < order.length) [order[index], order[target]] = [order[target], order[index]];
            }
            if (confirm) confirm.checked = false;
            renderOrder();
        });
        panel.addEventListener("change", (event) => {
            if (event.target === addSelect && addSelect.value) {
                order.push(addSelect.value); if (confirm) confirm.checked = false; renderOrder();
            }
            buttons();
        });
        buttons();
        return {load, persist, selectProvider, get route() { return route; },
            invalidate() { sequence += 1; route = null; source = ""; busy = false; buttons(); text("来源已改变，请重新读取路线。"); }};
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
            const selectedOption = form.elements.videodl_specific_backend?.selectedOptions?.[0];
            const specificRequiresSharing = selectedOption?.dataset.externalSharingRequired === "true";
            sharingField.hidden = !(
                (specific && specificRequiresSharing)
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
        if (windowObject.location?.hash && form.elements.source) {
            const source = new URLSearchParams(windowObject.location.hash.slice(1)).get("source");
            if (source && source.length <= 4096) form.elements.source.value = source;
            windowObject.history?.replaceState(null, "", windowObject.location.pathname + windowObject.location.search);
        }
        const runUrl = options.runUrl || root.dataset.runUrl;
        const resourceUrl = options.resourceUrl || root.dataset.resourceUrl;
        const maxConcurrency = Number(options.maxConcurrency || root.dataset.maxConcurrency || 3);
        const maxProviders = Math.max(Number(options.maxProviders || root.dataset.maxProviders || 5),
            form.querySelectorAll('input[name="providers"]').length);
        const videodlInventory = options.videodlInventory || readVideodlInventory(documentObject);
        const resultByProvider = new Map();
        let running = false;
        let currentSource = "";
        let currentSelection = null;
        const message = documentObject.querySelector("[data-provider-lab-form-message]");
        const submitButton = form.querySelector('[type="submit"]');
        const cancelButton = form.querySelector("[data-lab-cancel]");

        function updateSelection(selection) {
            currentSelection = selection;
            const summary = root.querySelector("[data-lab-current-selection]");
            if (summary) summary.textContent = selection
                ? `本次候选：${selection.providerId} · ${selection.asset.format_label || selection.asset.quality_label || selection.asset.asset_id}。未修改平台路线。`
                : "尚未选择本次候选。平台路线未改变。";
            root.querySelectorAll("[data-lab-select]").forEach((button) => {
                const card = button.closest("[data-provider-card]");
                const selected = Boolean(selection && card?.dataset.providerCard === selection.providerId
                    && button.dataset.assetId === selection.asset.asset_id);
                button.setAttribute("aria-pressed", String(selected));
                button.textContent = selected ? "本次已选" : "本次使用";
            });
        }

        function summarize() {
            const warning = root.querySelector("[data-lab-comparison-warning]");
            if (warning) warning.textContent = comparisonWarnings(Array.from(resultByProvider.values())).join(" ");
        }

        const queue = createExecutionQueue({
            concurrency: maxConcurrency,
            timeoutMs: Number(root.dataset.providerTimeoutMs || 45000),
            budgetMs: Number(root.dataset.comparisonBudgetMs || 180000),
            execute: (providerId, context, signal) => runOne(providerId, context.source, context.csrf,
                context.providerOptions[providerId], signal),
            onState(job) {
                const card = documentObject.querySelector(`[data-provider-card="${job.id}"]`);
                if (!card) return;
                if (job.status === "queued") { resetCard(card); resultByProvider.delete(job.id); }
                setCardStatus(card, job.status);
                const retry = card.querySelector("[data-lab-retry]");
                if (retry) retry.disabled = ["running", "queued"].includes(job.status);
                if (job.result) {
                    resultByProvider.set(job.id, job.result);
                    renderResult(card, job.result, documentObject);
                    summarize();
                } else if (job.reason) {
                    const note = card.querySelector("[data-provider-error]");
                    if (note) { note.hidden = false; note.textContent = job.reason; }
                }
                if (currentSelection?.providerId === job.id && job.status === "queued") updateSelection(null);
            },
            onIdle() {
                running = false;
                if (submitButton) submitButton.disabled = false;
                if (cancelButton) cancelButton.disabled = true;
                if (message) message.textContent = "本轮执行结束；已完成结果保留，未自动选择路线。";
            },
        });
        const routeEditor = createRouteEditor(documentObject, {fetch: fetchImpl, window: windowObject,
            url: root.dataset.routeUrl || "/provider-lab/routes/", csrf: () => csrfToken(form),
            getSource: () => String(form.elements.source?.value || "").trim(),
            getSelection: () => currentSelection});

        async function runOne(providerId, source, csrf, providerOptions, signal) {
            const body = {source, provider_id: providerId};
            if (providerId === "videodl_repo" && providerOptions) body.options = providerOptions;
            const result = await requestJson(fetchImpl, runUrl, csrf, body, {signal});
            return {...result, provider_id: providerId};
        }

        async function submit(event) {
            event.preventDefault();
            if (running) { return; }
            const source = String(form.elements.source?.value || "").trim();
            const providers = selectedProviderIds(
                form.querySelectorAll('input[name="providers"]'),
                maxProviders,
            );
            if (!source || source.length > 4096 || !providers.length) {
                if (message) { message.textContent = `请输入有效内容并选择 1 至 ${maxProviders} 个可用 Provider。`; }
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
            if (submitButton) { submitButton.disabled = true; }
            if (cancelButton) cancelButton.disabled = false;
            if (message) { message.textContent = `正在运行 ${providers.length} 个独立测试...`; }
            currentSource = source;
            resultByProvider.clear();
            updateSelection(null);
            root.querySelectorAll("[data-provider-card]").forEach((card) => {
                resetCard(card);
                if (!providers.includes(card.dataset.providerCard)) setCardStatus(card, "skipped");
            });
            queue.start(providers, {source, csrf: csrfToken(form), providerOptions: {videodl_repo: controlledVideodlOptions}});
            void routeEditor?.load();
            running = true;
            if (submitButton) submitButton.disabled = true;
            if (cancelButton) cancelButton.disabled = false;
        }

        async function click(event) {
            const useCurrent = event.target.closest?.("[data-lab-current-inspect]");
            if (useCurrent && currentSelection) {
                const card = documentObject.querySelector(`[data-provider-card="${currentSelection.providerId}"]`);
                const button = Array.from(card?.querySelectorAll("[data-lab-inspect]") || [])
                    .find((node) => node.dataset.assetId === currentSelection.asset.asset_id);
                if (button) {
                    button.closest("[data-asset-id]").hidden = false;
                    button.scrollIntoView?.({block: "nearest"});
                    await inspectAsset(button, {document: documentObject, fetch: fetchImpl, csrf: csrfToken(form), resourceUrl});
                }
                return;
            }
            const cancel = event.target.closest?.("[data-lab-cancel]");
            if (cancel) { event.preventDefault(); queue.cancel(); return; }
            const retry = event.target.closest?.("[data-lab-retry]");
            if (retry) {
                event.preventDefault();
                if (queue.retry(retry.closest("[data-provider-card]")?.dataset.providerCard)) {
                    running = true;
                    if (submitButton) submitButton.disabled = true;
                    if (cancelButton) cancelButton.disabled = false;
                }
                return;
            }
            const expand = event.target.closest?.("[data-lab-expand]");
            if (expand) {
                expand.closest("[data-provider-assets]").querySelectorAll("[data-asset-id]")
                    .forEach((row) => { row.hidden = false; });
                expand.remove();
                return;
            }
            const select = event.target.closest?.("[data-lab-select]");
            if (select) {
                const providerId = select.closest("[data-provider-card]")?.dataset.providerCard;
                const result = resultByProvider.get(providerId);
                const asset = result?.assets?.find((item) => item.asset_id === select.dataset.assetId);
                if (asset) updateSelection({providerId, requestId: result.request_id, asset});
                return;
            }
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
        const input = () => {
            if (String(form.elements.source?.value || "").trim() !== currentSource) {
                queue.invalidate();
                routeEditor?.invalidate();
                resultByProvider.clear();
                currentSource = "";
                updateSelection(null);
                root.querySelectorAll("[data-provider-card]").forEach(resetCard);
                running = false;
                if (submitButton) submitButton.disabled = false;
                if (cancelButton) cancelButton.disabled = true;
            }
        };
        form.addEventListener("change", change);
        form.elements.source?.addEventListener("input", input);
        root.addEventListener("click", click);
        const sortFormats = (event) => {
            if (!event.target.matches?.("[data-lab-format-order]")) return;
            const card = event.target.closest("[data-provider-card]");
            const result = resultByProvider.get(card?.dataset.providerCard);
            if (!result) return;
            renderAssets(documentObject, card.querySelector("[data-provider-assets]"), result,
                event.target.value === "resolution" ? qualityOrder(result.assets) : result.assets);
            updateSelection(currentSelection);
        };
        root.addEventListener("change", sortFormats);
        syncVideodlControls(form);
        const controller = {
            submit,
            runOne,
            resultByProvider,
            queue,
            routeEditor,
            get currentSelection() { return currentSelection; },
            destroy() {
                queue.invalidate();
                routeEditor?.invalidate();
                form.removeEventListener("submit", submit);
                form.removeEventListener("change", change);
                form.elements.source?.removeEventListener("input", input);
                root.removeEventListener("click", click);
                root.removeEventListener("change", sortFormats);
                if (root.__animeImagesProviderLabController === controller) {
                    delete root.__animeImagesProviderLabController;
                }
            },
        };
        root.__animeImagesProviderLabController = controller;
        return controller;
    }

    function comparisonWarnings(results) {
        const successful = results.filter((result) => result?.status === "success");
        if (successful.length < 2) return [];
        const warnings = [];
        const ids = new Set(successful.map((result) => result.metadata?.content_id).filter(Boolean));
        if (ids.size > 1) warnings.push("作品标识不同，不能直接视为同一作品的画质对比。");
        const durations = successful.map((result) => Math.max(0, ...(result.assets || []).map((asset) =>
            Number(asset.duration_seconds || asset.duration || 0)))).filter((value) => value > 0);
        if (durations.length > 1 && Math.max(...durations) - Math.min(...durations) > Math.max(5, Math.min(...durations) * .1)) {
            warnings.push("时长明显不同，请核对内容范围。");
        }
        if (new Set(successful.map((result) => (result.assets || []).length)).size > 1) {
            warnings.push("候选数量不同，可能包含分离轨、多个格式或缺项。");
        }
        return warnings;
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
        createExecutionQueue,
        createRouteEditor,
        routeRequestBody,
        diagnosticResult,
        comparisonWarnings,
        knownBoolean,
        qualityOrder,
        metaFragment,
        scopeMessage,
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
