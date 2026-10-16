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
        unknown: "UNKNOWN",
        sent: "SENT / UNKNOWN",
        planned: "PLANNED",
        reserved: "RESERVED / UNKNOWN",
        in_flight: "IN FLIGHT / UNKNOWN",
        not_executed: "NOT RUN",
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
        PROVIDER_AUTH_FAILED: "上游认证失败，请检查该通道的账号配置。",
        PROVIDER_UPSTREAM_FAILED: "上游接口请求或响应失败，尚未取得有效媒体。",
        PROVIDER_EXECUTOR_FAILED: "上游辅助程序或请求执行失败，具体远端原因未确认。",
        PROVIDER_RESULT_EMPTY: "Provider 未返回可比较的资源。",
        PROVIDER_LICENSE_RESTRICTED: "该 Provider 仅限显式启用的非商业本地研究。",
        PROVIDER_RATE_LIMITED: "请求过于频繁，请稍后重试。",
        PROVIDER_ADMISSION_DEFERRED: "天天解析正在排队，或前次调用结果尚未确认；本次未发起解析，不会自动重试。",
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

    function readProviderDescriptors(documentObject) {
        try {
            const rows = JSON.parse(documentObject.querySelector("#provider-lab-config")?.textContent || "[]");
            return Array.isArray(rows) ? rows : [];
        } catch (_error) { return []; }
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

    function errorMessage(code, detail) {
        if (code === "PROVIDER_RESULT_EMPTY" && detail === "SERVICE_EMPTY_RESULT") {
            return "服务返回的结果没有可用媒体；不是下载失败。";
        }
        if (code === "PROVIDER_UPSTREAM_FAILED") {
            const business = /^SERVICE_BUSINESS_CODE_(-?\d{1,3})$/.exec(detail || "");
            if (business) return `服务业务响应未通过，业务码 ${business[1]}；该值不是 HTTP 状态，具体含义待核对。`;
            if (detail === "SERVICE_BUSINESS_CODE_UNKNOWN") return "服务业务响应未通过，未提供可安全识别的业务码。";
            const jsonFailure = /^SERVICE_JSON_(AUTH|PARSE|UNKNOWN)_HTTP_([1-5]\d{2}|UNKNOWN)_TYPE_(JSON|HTML|TEXT|OTHER|MISSING)$/.exec(detail || "");
            if (jsonFailure) {
                const stages = {AUTH: "上游授权", PARSE: "上游解析", UNKNOWN: "未知"};
                const types = {JSON: "JSON", HTML: "HTML", TEXT: "文本", OTHER: "其他", MISSING: "未提供"};
                return `${stages[jsonFailure[1]]}阶段响应不是有效 JSON；HTTP ${jsonFailure[2] === "UNKNOWN" ? "未知" : jsonFailure[2]}，Content-Type 类别：${types[jsonFailure[3]]}。`;
            }
            if (/^SERVICE_HTTP_[45]\d\d$/.test(detail || "")) {
                return `服务接口返回 HTTP ${detail.slice(-3)}；这不是本地管理员权限错误。`;
            }
            const serviceDetails = {
                SERVICE_NETWORK_ERROR: "服务请求网络失败，尚不能确定是连接、DNS 或其他网络原因。",
                SERVICE_TLS_ERROR: "服务请求 TLS 校验失败；未关闭证书校验。",
                SERVICE_INVALID_JSON: "服务响应不是有效 JSON。",
                SERVICE_RESPONSE_REJECTED: "服务未返回可用结果，具体业务原因未确认。",
                SERVICE_AUTH_REJECTED: "辅助程序的上游授权阶段未通过，不代表本地管理员登录失败。",
                SERVICE_PARSE_REJECTED: "辅助程序的上游解析阶段未返回可用结果。",
                SERVICE_REDIRECT_REJECTED: "服务重定向被现有安全规则拒绝。",
                SERVICE_RESPONSE_TOO_LARGE: "服务响应超过读取上限。",
                SERVICE_HELPER_FAILED: "辅助程序已运行但失败，具体原因未知；不能据此判定 Node 缺失。",
            };
            if (Object.prototype.hasOwnProperty.call(serviceDetails, detail)) return serviceDetails[detail];
        }
        if (code === "PROVIDER_POLICY_REJECTED" && detail === "UPSTREAM_VERSION_UNSUPPORTED") {
            return "当前上游运行版本尚未通过本应用兼容验证；不是账号未配置或作品不支持。";
        }
        if (code === "PROVIDER_SCHEMA_INVALID" && detail === "UPSTREAM_VERSION_CHANGED") {
            return "上游运行版本在本次请求中发生变化，请核对运行环境；本次未自动重试。";
        }
        if (code === "PROVIDER_POLICY_REJECTED" && detail === "NESTED_AGGREGATOR_TLS_BYPASS_BLOCKED") {
            return "该固定版本 Backend 使用嵌套聚合服务并绕过 TLS 校验，已被安全策略禁用；不是管理员权限不足。";
        }
        if (code === "PROVIDER_TIMEOUT" && detail === "MATCH_DEADLINE") {
            return "上游匹配阶段超时，尚未进入媒体解析；不是管理员权限不足。";
        }
        if (code === "PROVIDER_TIMEOUT" && detail === "PARSE_DEADLINE") {
            return "上游解析阶段超时；可能涉及网络或源站响应，不能仅凭超时确定原因。";
        }
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
        const receipt = card.querySelector("[data-history-receipt]");
        if (receipt) receipt.textContent = "";
        card.querySelectorAll("[data-copy-json], [data-download-json]")
            .forEach((button) => { button.disabled = true; });
    }

    function metaFragment(documentObject, asset) {
        const fragment = documentObject.createDocumentFragment();
        const values = [
            ["类型", asset.media_type],
            ["角色", asset.role],
            ["格式", asset.declared_extension || "未知"],
            ["尺寸", asset.width || asset.height ? `${asset.width || "未知"}×${asset.height || "未知"}` : "未知"],
            ["时长", formatDuration(asset.duration_seconds)],
            ["大小", `${formatBytes(asset.size_bytes)}${asset.size_is_estimate === true ? "（估计）" : ""}`],
            ["质量", asset.quality_label || "未知"],
            ["Codec", asset.codec || "未知"],
            ["视频编码", asset.vcodec || "未知"],
            ["音频编码", asset.acodec === "none" ? "无音轨" : asset.acodec || "未知"],
            ["帧率", asset.fps > 0 ? `${asset.fps} fps` : "未知"],
            ["码率", asset.bitrate > 0 ? `${asset.bitrate} kbps` : "未知"],
            ["语言", asset.language || "未知"],
            ["内嵌视频轨 / 音频轨", `${trackLabel(asset.video_tracks)} / ${trackLabel(asset.audio_tracks)}`],
            ["交付", asset.delivery_state || "未知"],
            ["协议", asset.protocol || "未知"],
            ["来源", asset.metadata_source === "upstream_declared" ? "上游声明" : asset.metadata_source || "未知"],
            ["Host", asset.host_class || "未知"],
            ["合并", knownBoolean(asset.requires_merge, "需要")],
            ["独立音频资源", knownBoolean(asset.audio_asset_present, "存在")],
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

    function trackLabel(value) {
        return value === "present" ? "存在" : value === "absent" ? "无" : "未知";
    }

    function qualityOrder(assets) {
        const source = Array.from(assets || []);
        const videos = source.filter((asset) => asset.media_type === "video").map((asset, index) => ({asset, index})).sort((left, right) => {
            const known = (value) => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : -1;
            return known(right.asset.height) - known(left.asset.height)
                || known(right.asset.width) - known(left.asset.width) || left.index - right.index;
        }).map(({asset}) => asset);
        let index = 0;
        return source.map((asset) => asset.media_type === "video" ? videos[index++] : asset);
    }

    function historyCandidateId(executionId, ordinal) {
        return `history:${executionId}:${ordinal}`;
    }

    function renderAssets(documentObject, container, result, ordered) {
        container.replaceChildren();
        if (result.history_summary && !Array.isArray(result.assets)) {
            const note = documentObject.createElement("p");
            note.textContent = "旧记录未保留候选明细；候选数量未知，不等于零。历史摘要可查看；资源访问需要重新解析。";
            container.append(note); return;
        }
        const assets = ordered || result.assets || [];
        // View-only identities follow original ordinals, never upstream IDs or sorted positions.
        const historyIds = result.history_summary && result.history_execution_id !== undefined
            ? new Map((result.assets || []).map((asset, ordinal) => [asset, historyCandidateId(result.history_execution_id, ordinal)])) : null;
        assets.forEach((asset, index) => {
            const row = documentObject.createElement("div");
            row.className = "provider-lab-asset";
            row.dataset.assetId = String(asset.asset_id || "");
            row.hidden = index >= 64;
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
            button.disabled = result.history_summary === true || !asset.resource_token;
            if (result.history_summary) button.dataset.resourceToken = "";
            const select = documentObject.createElement("button");
            select.type = "button";
            select.textContent = "本次使用";
            select.dataset.labSelect = "1";
            select.dataset.assetId = String(asset.asset_id || "");
            if (historyIds) {
                row.dataset.historySelectionId = historyIds.get(asset);
                select.dataset.historySelectionId = historyIds.get(asset);
            }
            select.setAttribute("aria-pressed", "false");
            const inspection = documentObject.createElement("div");
            inspection.className = "provider-lab-inspection";
            inspection.dataset.labInspection = "1";
            inspection.textContent = result.history_summary ? "历史摘要可查看；资源访问需要重新解析。" : "尚未请求媒体描述符。";
            row.append(meta, select, button, inspection);
            container.append(row);
        });
        const counts = documentObject.createElement("p");
        counts.dataset.labDisplayCounts = "1";
        const metadata = result.metadata || {};
        counts.dataset.returnedCount = metadata.returned_count ?? "未知";
        counts.dataset.retainedCount = metadata.retained_count ?? assets.length;
        counts.dataset.unexpandedCount = metadata.unexpanded_count ?? "未知";
        container.append(counts);
        if (assets.length > 64) {
            const expand = documentObject.createElement("button");
            expand.type = "button";
            expand.dataset.labExpand = "1";
            expand.textContent = "再显示 64 项";
            container.append(expand);
        }
        updateAssetCounts(container);
    }

    function updateAssetCounts(container) {
        const rows = Array.from(container.querySelectorAll(".provider-lab-asset"));
        const shown = rows.filter((row) => !row.hidden).length;
        const counts = container.querySelector("[data-lab-display-counts]");
        if (counts) counts.textContent = `返回 ${counts.dataset.returnedCount} · 保留 ${counts.dataset.retainedCount} · 已显示 ${shown} · 待展开 ${rows.length - shown} · 上游未展开 ${counts.dataset.unexpandedCount}`;
        const expand = container.querySelector("[data-lab-expand]");
        if (expand) { expand.hidden = shown >= rows.length; expand.textContent = `再显示 ${Math.min(64, rows.length - shown)} 项`; }
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
        const queueWait = result.queue_wait_ms ?? result.metadata?.queue_wait_ms;
        const execution = result.execution_ms ?? result.metadata?.execution_ms;
        if (Number.isFinite(queueWait) && Number.isFinite(execution)) {
            setText(card, "elapsed_ms", `排队 ${queueWait} ms；执行 ${execution} ms`);
        }
        setText(card, "title", result.metadata?.title || "-");
        setText(card, "author", result.metadata?.author || "-");
        setText(card, "asset_count", result.history_summary && !Array.isArray(result.assets) ? "未知（未保留候选）" : String((result.assets || []).length));
        setText(card, "relationship_count", String((result.relationships || []).length));
        if (result.provider_id === "animeimages_current") setText(card, "selected_backend", "现有路线基线（复合结果）");
        const content = card.querySelector("[data-provider-content]");
        if (content && result.metadata?.content_summary) {
            content.textContent = result.metadata.content_summary;
            content.hidden = false;
        }
        const error = card.querySelector("[data-provider-error]");
        if (error && status !== "success") {
            error.textContent = errorMessage(result.error_code, result.error_detail);
            if (status === "cancelled") error.textContent = "已取消；其他已完成结果保留。";
            if (status === "skipped") error.textContent = result.not_run_reason || "本次未执行：整体预算已用尽。";
            if (["unknown", "sent", "reserved", "in_flight", "queued", "running", "waiting"].includes(status)) error.textContent = `原执行状态：${STATUS_LABELS[status]}；请先核对原调用账本，不会自动重发。`;
            if (["planned", "not_executed"].includes(status)) error.textContent = "原记录：未执行。";
            error.hidden = false;
        }
        if (error && status === "success") { error.hidden = true; error.textContent = ""; }
        let receipt = card.querySelector("[data-history-receipt]");
        if (result.history && !receipt) {
            receipt = documentObject.createElement("p"); receipt.dataset.historyReceipt = "1"; card.append(receipt);
        }
        if (receipt) receipt.textContent = !result.history ? "" : result.history.saved === true
            ? `评估历史已保存（${result.history.id}）。`
            : `解析结果与历史保存独立：历史保存失败或未确认（${result.history.error_code || "未知"}）；不会自动重跑。`;
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
        const anchor = documentObject.createElement("span");
        panel.parentNode.insertBefore(anchor, panel);
        const labels = {yt_dlp_generic: "yt-dlp", gallery_dl_generic: "gallery-dl", parsehub_generic: "ParseHub",
            videodl_repo: "videodl", tiantianjiexi_generic: "天天解析", kukutool_generic: "Kukutool"};
        const providerName = (id) => settings.descriptors?.find((item) =>
            (item.execution_provider_id || ROUTE_PROVIDER_IDS[item.provider_id]) === id)?.label || labels[id] || id;
        const routeNames = (ids) => (ids || []).map(providerName).join(" → ");
        let route = null;
        let source = "";
        let order = [];
        let sequence = 0;
        let busy = false;
        let reference = null;
        const canConfigure = panel.dataset.canConfigure === "true";
        const text = (value) => { if (message) message.textContent = value; };
        function field(name) { return panel.querySelector(`[name="${name}"]`); }
        function buttons() {
            if (fields) fields.disabled = busy || !canConfigure || !route?.applicable;
            if (save) save.disabled = busy || !canConfigure || !route?.applicable || !confirm?.checked || !order.length;
            if (reset) reset.disabled = busy || !canConfigure || !route?.applicable || !route.active;
        }
        function restore() { anchor.parentNode?.insertBefore(panel, anchor.nextSibling); }
        function showCurrent() {
            const current = panel.querySelector("[data-route-current]");
            if (current) current.textContent = !route ? "当前路线：尚未读取" : route.applicable
                ? `${route.active ? "当前人工路线" : "当前默认路线"}：${routeNames(route.effective_provider_order || route.provider_order) || "未配置"} · revision ${route.revision}`
                : "当前沿用成熟平台路线，不适用通用路线编辑。";
            settings.onChange?.();
        }
        function draftSummary() {
            const node = panel.querySelector("[data-route-confirmation-summary]");
            if (!node || !route) return;
            const current = routeNames(route.effective_provider_order || route.provider_order) || "未配置";
            const preferences = ["max_height", "preferred_video_codec", "preferred_container"]
                .filter((name) => field(name)?.value).map((name) => `${name}=${field(name).value}`).join("，") || "未指定";
            const execution = order.includes("videodl_repo")
                ? `${field("route_videodl_mode")?.value || "未知，请显式设置"} ${field("route_videodl_backend")?.value || ""}` : "该上游标准模式";
            node.textContent = `来源站点：${route.source_host}；revision ${route.revision}；当前：${current}；拟固定：${order[0] ? providerName(order[0]) : "未选择"}；执行选项：${execution}；格式偏好：${preferences}；明确备选：${routeNames(order.slice(1)) || "无，首选失败即停止"}。`;
        }
        function renderOrder() {
            orderNode.replaceChildren();
            order.forEach((id, index) => {
                const li = documentObject.createElement("li");
                const label = documentObject.createElement("span");
                label.textContent = `${index === 0 ? "首选" : `备选 ${index}`} · ${providerName(id)}`;
                li.append(label);
                for (const [action, symbol, title] of [["up", "↑", "向前"], ["down", "↓", "向后"], ["remove", "×", "移除"]]) {
                    const button = documentObject.createElement("button");
                    button.type = "button";
                    button.dataset.routeMove = action;
                    button.dataset.providerId = id;
                    button.textContent = symbol;
                    button.title = title;
                    button.setAttribute("aria-label", `${title} ${providerName(id)}`);
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
                option.value = id; option.textContent = providerName(id); addSelect.append(option);
            });
            const videodl = panel.querySelector("[data-route-videodl]");
            if (videodl) videodl.hidden = !order.includes("videodl_repo");
            showCurrent();
            buttons();
            draftSummary();
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
        async function load({preserveDraft = false} = {}) {
            const input = settings.getSource();
            if (!input) { text("请先输入来源链接。"); return; }
            if (preserveDraft && route && source) {
                try {
                    if (new URL(input).hostname.toLowerCase().replace(/\.$/, "") === route.source_host) {
                        source = input;
                        return;
                    }
                } catch (_error) { /* The route endpoint remains the source validator. */ }
            }
            const version = ++sequence;
            route = null; source = ""; order = []; reference = null;
            const referenceNode = panel.querySelector("[data-route-reference]");
            if (referenceNode) referenceNode.textContent = "";
            renderOrder();
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
                if (!isReset && !field("route_videodl_mode")?.value) { text("历史 videodl 选项未知或不支持，请显式设置执行模式。"); return; }
                providerOptions.videodl_repo = {mode: field("route_videodl_mode")?.value || "dedicated_only"};
                if (providerOptions.videodl_repo.mode === "specific_backend") {
                    providerOptions.videodl_repo.backend_id = field("route_videodl_backend")?.value || "";
                }
            }
            const body = routeRequestBody(source, route, {provider_order: order,
                provider_options: providerOptions, format_preferences: preferences}, isReset);
            if (!isReset && reference) body.reference_execution_id = reference.id;
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
                        : payload.error_code === "HISTORY_PROVIDER_UNAVAILABLE" ? "拟保存的上游当前已禁用或未配置，请重新核对当前能力。路线未保存。"
                        : payload.error_code === "INVALID_HISTORY_REFERENCE" ? "历史参考记录与当前来源不匹配或已不可用，请重新选择对应来源的历史记录。路线未保存。"
                        : "路线未保存。请检查上游、Backend 和格式偏好。");
                    return;
                }
                apply(payload.route);
                text(isReset ? "已恢复默认路线。" : "平台路线已保存，后续解析使用新版本。");
            } catch (_error) { if (version === sequence) text("保存状态未确认，请重新读取路线后核对。"); }
            finally { if (version === sequence) { busy = false; buttons(); } }
        }
        function selectProvider(providerId, evidence, position) {
            const descriptor = settings.descriptors?.find((item) => item.provider_id === providerId);
            evidence = evidence || settings.getSelection?.()?.historyExecution;
            if (evidence && (!descriptor || descriptor.available !== true || descriptor.route_selectable !== true)) {
                text("当前 Provider 配置不允许保存路线；历史结果仍可查看。"); return;
            }
            const id = descriptor?.execution_provider_id || ROUTE_PROVIDER_IDS[providerId];
            if (providerId === "animeimages_current" || descriptor?.baseline || descriptor?.route_selectable === false) {
                text(descriptor?.route_unavailable_reason || "当前路线基线不是独立上游，不能保存为固定路线。");
                return;
            }
            if (busy || !canConfigure || !route?.applicable || source !== settings.getSource()
                    || !id || !route.allowed_provider_ids.includes(id)) {
                text("该候选不适用于此来源的持久路线，或路线尚未读取。");
                return;
            }
            const placement = position || (evidence ? "primary" : "replace");
            if (placement === "fallback" && !order.length) {
                text("请先设置首选上游，再加入备选；尚未保存。"); return;
            }
            const alreadyIncluded = order.includes(id);
            if (placement === "fallback") order = Array.from(new Set([...order, id]));
            else order = placement === "primary" ? [id, ...order.filter((item) => item !== id)] : [id];
            reference = evidence || null;
            const referenceNode = panel.querySelector("[data-route-reference]");
            if (referenceNode) referenceNode.textContent = reference ? historyEvidence(reference) : "即时选择；无历史引用。";
            if (reference && id === "videodl_repo" && !alreadyIncluded) {
                const recorded = reference.options?.videodl_repo || reference.options || {};
                const mode = field("route_videodl_mode");
                const backend = field("route_videodl_backend");
                if (mode) mode.value = recorded.mode === "dedicated_only" ? "dedicated_only" : "";
                if (backend) backend.value = "";
                if (recorded.mode === "specific_backend" && recorded.backend_category === "dedicated") {
                    const backendId = recorded.backend_id || (recorded.backend_ids?.length === 1 ? recorded.backend_ids[0] : "");
                    if (backend && Array.from(backend.options || []).some((item) => item.value === backendId && !item.disabled)) {
                        mode.value = "specific_backend"; backend.value = backendId;
                    }
                }
            }
            if (confirm) confirm.checked = false;
            renderOrder(); text(reference && id === "videodl_repo" && !field("route_videodl_mode")?.value
                ? "历史 videodl 选项未知或当前不支持，请显式设置；尚未保存。" : "已调整路线草稿；尚未保存。");
        }
        panel.addEventListener("click", (event) => {
            if (event.target.closest?.("[data-route-cancel]")) {
                reference = null;
                const node = panel.querySelector("[data-route-reference]"); if (node) node.textContent = "";
                if (route) apply(route); text("已取消草稿，平台路线未改变。"); return;
            }
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
                if (canConfigure && !busy && route?.applicable && route.allowed_provider_ids.includes(addSelect.value)) {
                    order = Array.from(new Set([...order, addSelect.value]));
                }
                if (confirm) confirm.checked = false; renderOrder();
            }
            if (event.target !== confirm && confirm) confirm.checked = false;
            draftSummary();
            buttons();
        });
        buttons();
        return {load, persist, selectProvider, restore, mount(container) { container.append(panel); }, get route() { return route; },
            get currentSummary() { return panel.querySelector("[data-route-current]")?.textContent || ""; },
            invalidate() { sequence += 1; route = null; source = ""; reference = null; order = []; busy = false;
                const node = panel.querySelector("[data-route-reference]"); if (node) node.textContent = "";
                scope.textContent = "尚未读取来源范围";
                const summary = panel.querySelector("[data-route-confirmation-summary]");
                if (summary) summary.textContent = "请先读取来源站点并选择固定上游。";
                renderOrder(); text("来源已改变，请重新读取路线。"); }};
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
        const historicalProvider = form.dataset?.historyRetestProvider;
        controls.hidden = historicalProvider ? historicalProvider !== "videodl_repo" : !provider?.checked || provider.disabled;
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

    function historyVersionSummary(value) {
        const short = (item) => {
            if (typeof item !== "string" || !item.trim()) return "未知";
            return /^[a-f0-9]{12,64}$/i.test(item) ? item.slice(0, 8)
                : item.length > 32 ? `${item.slice(0, 32)}…` : item;
        };
        if (typeof value === "string") return short(value);
        const version = value || {};
        const app = short(version.animeimages_head ?? version.animeimages ?? version.app ?? version.head);
        const scout = short(version.scout_head ?? version.scout_revision?.head);
        const parts = [`App ${app}${version.dirty === true || version.tracked_changes === true ? "（有未提交改动）" : ""}`,
            `Scout ${scout}${version.scout_revision?.dirty === true ? "（有未提交改动）" : ""}`];
        if (version.scout_code) parts.push(`Scout 代码指纹 ${short(version.scout_code)}`);
        return parts.join(" · ");
    }

    function historyTime(value) {
        if (!value) return "未知";
        if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value}（未记录时刻）`;
        if (!/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return "未知（原时间缺少时区）";
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) return "未知";
        const parts = new Intl.DateTimeFormat("en-GB", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).formatToParts(date);
        const p = Object.fromEntries(parts.map(({type, value}) => [type, value]));
        return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}（北京时间）`;
    }

    function historyOrigin(value) {
        return value === "scout" ? "Scout · 自动评估" : value === "lab" ? "Provider Lab · 人工测试/复测" : "来源未记录";
    }

    function historyRelation(execution) {
        const parent = String(execution.replay_of || "");
        if (!parent) return `记录 #${execution.id}`;
        if (parent.endsWith(":planned")) return `计划关联 · 记录 #${execution.id}`;
        if ((execution.origin || execution.batch?.origin) === "lab" && /^\d+$/.test(parent)) {
            return `单项复测 · 原记录 #${parent} → 当前 #${execution.id}`;
        }
        return `历史执行关联 · 记录 #${execution.id}`;
    }

    function historyTimestamp(value) {
        if (typeof value !== "string" || !/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
        const time = Date.parse(value);
        return Number.isFinite(time) ? time : null;
    }

    function historyEvidence(execution) {
        const batch = execution.batch || {};
        return `参考批次：${batch.label || batch.batch_key || "未知"}；建档时间：${historyTime(batch.created_at)}；执行时间：${historyTime(execution.started_at)} → ${historyTime(execution.finished_at)}；版本：${historyVersionSummary(execution.version ?? batch.metadata?.version)}；来源：${historyOrigin(execution.origin || batch.origin)}；原状态：${execution.state || "未知"}`;
    }

    function historyRetestReason(execution, descriptor) {
        if (execution.result?.error_detail === "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"
                || execution.result?.client_status === "OUTCOME_UNKNOWN") {
            return "前次服务调用结果未知，尚不能确认是否已执行；请先核对原调用账本与服务状态，不能直接复测或重发。";
        }
        if (!execution.source_url) return "缺少原始来源链接，不能复测或保存路线。";
        if (typeof execution.state !== "string" || !["returned", "success", "failed", "timeout", "unsupported",
            "not_executed", "planned", "skipped", "cancelled", "canceled", "not_configured", "not_authorized"]
            .includes(execution.state.trim().toLowerCase())) return "原调用结果未确认，请先核对账本；不能重发。";
        if (execution.can_retest !== true) return "该记录当前不可复测。";
        if (!descriptor || descriptor.available !== true) return "Provider 已移除、禁用或当前不可用。";
        return "";
    }

    function createHistoryWorkspace(documentObject, settings) {
        const root = settings.root;
        const panel = root.querySelector("[data-lab-history]");
        if (!panel) return null;
        const batchesNode = panel.querySelector("[data-history-batches]");
        const executionsNode = panel.querySelector("[data-history-executions]");
        const status = panel.querySelector("[data-history-status]");
        const host = panel.querySelector("[data-history-host]");
        const order = panel.querySelector("[data-history-order]");
        const stateFilter = panel.querySelector("[data-history-state]");
        let batches = [], executions = [], batch = null, selected = null;
        let inspected = null, pending = null, page = 0;
        const pageSize = 10;
        const providers = {parsehub_repo: "ParseHub", videodl_repo: "videodl", yt_dlp_existing: "yt-dlp", gallery_dl_existing: "gallery-dl", tiantian_existing: "天天解析", kukutool_existing: "Kukutool"};
        const sampleIdentity = (e) => `${e.source_host || ""}:${e.sample_key || e.source_url || e.id}`;
        const outcome = (e) => ["not_executed", "planned", "skipped"].includes(e.state) ? "not_executed" : e.result?.status || e.state || "unknown";
        const outcomeLabel = (e) => ({success: "成功", failed: "失败", timeout: "超时", unsupported: "不支持", not_executed: "未执行", unknown: "未知", queued: "排队中", running: "执行中"}[outcome(e)] || "待核对");
        function cancelPending() { pending = null; settings.configureRetest?.(null); }
        function clearSelection(preserveRoute = false) {
            selected = null; selectedAsset = null;
            root.setAttribute("data-history-selection", "closed");
            settings.onSelection(null);
            if (!preserveRoute) settings.routeEditor?.invalidate();
        }
        function refreshCurrentRoute() {
            const summary = panel.querySelector("[data-history-current-route]");
            if (!summary) return;
            const execution = executions.find((item) => String(item.id) === String(inspected));
            const route = settings.routeEditor?.route;
            let matches = false;
            try { matches = Boolean(route && new URL(execution?.source_url).hostname.replace(/\.$/, "") === route.source_host); }
            catch (_error) { /* Missing sources cannot identify a route. */ }
            summary.textContent = matches
                ? `${route.source_host} · ${settings.routeEditor.currentSummary}`
                : "当前路线：尚未选择站点";
        }
        let sequence = 0, listSequence = 0, active = false, busy = false, selectedAsset = null;
        const attempted = new Set();
        const node = (tag, text, attribute, value = "") => {
            const element = documentObject.createElement(tag);
            if (text !== undefined) element.textContent = String(text);
            if (attribute) element.setAttribute(attribute, value);
            if (tag === "button") element.type = "button";
            return element;
        };
        function metadataDetails(label, value) {
            const details = node("details");
            details.className = "provider-lab-history-metadata";
            details.append(node("summary", label), node("pre", JSON.stringify(value ?? "未知", null, 2)));
            return details;
        }
        async function read(params) {
            const url = new URL(settings.url, settings.window.location.origin);
            if (params) url.searchParams.set("batch_id", params);
            const response = await settings.fetch(url.href, {credentials: "same-origin", cache: "no-store"});
            const payload = await response.json();
            if (!response.ok || payload.ok !== true) throw new Error("HISTORY_READ_FAILED");
            return payload;
        }
        async function loadBatches() {
            const version = ++listSequence;
            status.textContent = "正在读取历史批次...";
            try {
                const payload = await read();
                if (version !== listSequence) return;
                batches = Array.isArray(payload.batches) ? payload.batches : [];
                batchesNode.replaceChildren();
                for (const item of batches) {
                    const button = node("button", undefined, "data-history-batch", item.id);
                    button.setAttribute("aria-pressed", String(String(batch?.id) === String(item.id)));
                    button.append(node("strong", item.label || item.batch_key || item.id),
                        node("span", historyOrigin(item.origin), "data-history-origin", item.origin || "unknown"),
                        node("span", `建档时间：${historyTime(item.created_at)}`),
                        node("span", `样本 ${item.sample_count ?? "未知"} · 记录 ${item.execution_count ?? "未知"}`),
                        node("span", [["success", "成功"], ["unsupported", "不支持"], ["failed", "失败"], ["not_executed", "未执行"]].map(([key, label]) => `${label} ${item.counts?.[key] ?? "未知"}`).join(" · ")));
                    const entry = node("div"); entry.className = "provider-lab-history-batch";
                    const info = metadataDetails("批次详情与原始元数据", item.metadata);
                    info.append(node("span", `同步状态：${item.sync_status === "stored" ? "已入库" : "未知"}`),
                        node("p", `目标 ${item.target_count ?? "未知"} · 计划 ${item.counts?.planned ?? "未知"} · 已执行 ${item.counts?.executed ?? "未知"} · 未知 ${item.counts?.unknown ?? "未知"}`),
                        node("p", historyVersionSummary(item.metadata?.version ?? item.metadata)));
                    entry.append(button, info);
                    batchesNode.append(entry);
                }
                status.textContent = batches.length ? `${batches.length} 个批次；最新记录不代表已证明最佳。` : "暂无历史批次。";
            } catch (_error) { if (version === listSequence) status.textContent = "历史读取失败，可刷新重试；未发起解析。"; }
        }
        async function loadBatch(id) {
            const version = ++sequence;
            settings.routeEditor?.restore?.();
            cancelPending(); inspected = null; page = 0;
            clearSelection();
            batch = null; executions = []; executionsNode.replaceChildren();
            status.textContent = "正在读取批次执行记录...";
            try {
                const payload = await read(id);
                if (version !== sequence) return;
                batch = payload.batch;
                batchesNode.querySelectorAll("[data-history-batch]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.historyBatch === String(batch.id))));
                executions = (Array.isArray(payload.executions) ? payload.executions : []).map((item) => ({...item, batch}));
                panel.querySelector("[data-history-heading]").textContent = batch.label || batch.batch_key || "批次";
                host.replaceChildren(node("option", "全部站点", "value", "")); host.value = "";
                Array.from(new Set(executions.map((item) => item.source_host || "未知站点"))).sort().forEach((value) => host.append(node("option", value, "value", value)));
                render(); status.textContent = `${executions.length} 条执行/计划记录；仅查看已保存摘要。`;
            } catch (_error) { if (version === sequence) status.textContent = "批次读取失败，请重新选择；未发起解析。"; }
        }
        function render() {
            settings.configureRetest?.(null);
            settings.routeEditor?.restore?.();
            executionsNode.replaceChildren();
            const rows = executions.filter((item) => !host.value || (item.source_host || "未知站点") === host.value).slice();
            rows.sort((a, b) => order.value === "provider" ? String(a.provider_id).localeCompare(String(b.provider_id))
                : ((historyTimestamp(a.started_at) ?? -Infinity) - (historyTimestamp(b.started_at) ?? -Infinity)) * (order.value === "oldest" ? 1 : -1));
            const groups = new Map();
            rows.forEach((e) => { const key = sampleIdentity(e); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(e); });
            const visibleGroups = Array.from(groups.values()).filter((items) => !stateFilter?.value || items.some((e) =>
                stateFilter.value === "failed" ? ["failed", "timeout"].includes(outcome(e)) : outcome(e) === stateFilter.value));
            page = Math.min(page, Math.max(0, Math.ceil(visibleGroups.length / pageSize) - 1));
            const columns = Array.from(new Set([...Object.keys(providers), ...rows.map((e) => e.provider_id).filter((id) => id && id !== "animeimages_current")]));
            const wrap = node("div"); wrap.className = "provider-lab-history-matrix";
            wrap.setAttribute("tabindex", "0"); wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "作品与上游比较");
            const table = node("table"), thead = node("thead"), tr = node("tr");
            tr.append(node("th", "站点 / 作品"));
            columns.forEach((id) => tr.append(node("th", providers[id] || id)));
            thead.append(tr); table.append(thead);
            const tbody = node("tbody");
            for (const items of visibleGroups.slice(page * pageSize, (page + 1) * pageSize)) {
                const first = items[0], row = node("tr"), label = node("th");
                label.setAttribute("scope", "row");
                label.append(node("strong", first.source_host || "来源站点未保留"), node("p", first.title || "未记录作品标题"));
                if (!first.source_url) label.append(node("small", "来源链接未保留"));
                row.append(label);
                columns.forEach((id) => {
                    const cell = node("td");
                    const observations = items.filter((e) => e.provider_id === id).sort((a, b) =>
                        ((historyTimestamp(b.started_at) ?? -Infinity) - (historyTimestamp(a.started_at) ?? -Infinity)) || Number(b.id) - Number(a.id));
                    if (!observations.length) cell.append(node("span", "未纳入"));
                    else {
                        const e = observations[0], button = node("button", undefined, "data-history-inspect", e.id);
                        button.className = "provider-lab-history-cell";
                        button.setAttribute("data-state", outcome(e));
                        button.setAttribute("aria-pressed", String(observations.some((x) => String(x.id) === String(inspected))));
                        button.append(node("strong", outcomeLabel(e)));
                        if (Array.isArray(e.result?.assets)) button.append(node("span", `${e.result.assets.length} 候选`));
                        if (observations.length > 1) button.append(node("small", `${observations.length} 次记录 · ${observations.every((x) => historyTimestamp(x.started_at) !== null) ? "最新" : "时间不全"}`));
                        cell.append(button);
                    }
                    row.append(cell);
                });
                tbody.append(row);
            }
            table.append(tbody); wrap.append(node("p", "", "data-history-current-route"), table); executionsNode.append(wrap);
            refreshCurrentRoute();
            const paging = node("div"); paging.className = "provider-lab-history-pagination";
            const prev = node("button", "上一页", "data-history-page", "prev"), next = node("button", "下一页", "data-history-page", "next");
            prev.disabled = page === 0; next.disabled = (page + 1) * pageSize >= visibleGroups.length;
            paging.append(prev, node("span", `${visibleGroups.length} 条作品 · 第 ${page + 1} / ${Math.max(1, Math.ceil(visibleGroups.length / pageSize))} 页`), next);
            executionsNode.append(paging);
            const baselines = rows.filter((e) => e.provider_id === "animeimages_current");
            if (baselines.length) {
                const baseline = node("details"); baseline.append(node("summary", `复合路线基线（${baselines.length} 条，非独立上游）`));
                baselines.forEach((e) => baseline.append(node("button", `${e.source_host || "来源未保留"} · ${outcomeLabel(e)}`, "data-history-inspect", e.id)));
                executionsNode.append(baseline);
            }
            const samples = new Map();
            rows.filter((execution) => String(execution.id) === String(inspected)).forEach((execution) => {
                const key = `${execution.source_host || ""}:${execution.sample_key || execution.source_url || execution.id}`;
                let sample = samples.get(key);
                if (!sample) {
                    sample = node("section", undefined, "data-history-sample", key);
                    sample.className = "provider-lab-history-sample provider-lab-history-detail";
                    sample.append(node("h3", `${execution.source_host || "未知站点"} · ${execution.title || "未记录作品标题"}`), node("p", execution.source_url || "原始来源缺失"));
                    samples.set(key, sample); executionsNode.append(sample);
                }
                const card = node("article", undefined, "data-history-execution", execution.id);
                card.className = "provider-lab-card";
                const descriptor = settings.descriptors.find((item) => item.provider_id === execution.provider_id);
                card.append(node("h4", providers[execution.provider_id] || descriptor?.label || execution.provider_id || "未知 Provider"),
                    node("span", historyOrigin(execution.origin || batch?.origin), "data-history-origin", execution.origin || batch?.origin || "unknown"),
                    node("p", `执行时间：${historyTime(execution.started_at)}`),
                    node("p", historyRelation(execution)),
                    node("span", "", "data-provider-status-label"), node("p", "", "data-provider-error"));
                const details = node("details");
                const observations = rows.filter((e) => sampleIdentity(e) === sampleIdentity(execution) && e.provider_id === execution.provider_id);
                if (observations.length > 1) {
                    const chooser = node("select", undefined, "data-history-observation"); chooser.setAttribute("aria-label", "执行记录");
                    observations.forEach((e) => chooser.append(node("option", `#${e.id} · ${historyTime(e.started_at)} · ${outcomeLabel(e)}`, "value", e.id)));
                    chooser.value = String(execution.id); card.append(chooser);
                }
                details.append(node("summary", `候选与执行详情（${Array.isArray(execution.result?.assets) ? `${execution.result.assets.length} 项` : "候选未知 / 未保留"}）`));
                details.append(metadataDetails("完整执行版本", execution.version));
                details.append(node("p", historyEvidence(execution)), node("p", `当前配置：${descriptor ? descriptor.available ? "可用" : "不可用" : "已移除或未知"} · 当前版本 ${descriptor?.provider_version || "未知"} · 原 Provider 版本 ${execution.result?.provider_version || "未知"}`));
                details.append(node("p", `来源记录：${execution.source_status || "未知"} / ${execution.source_state || "未知"}；复跑关系：${execution.replay_of || "无 / 未知"}；原执行选项：${JSON.stringify(execution.options ?? "未知")}`));
                details.append(node("p", `底层关系：${JSON.stringify(execution.result?.relationships ?? "未知")}；限制：${JSON.stringify(execution.result?.limitations ?? "未知")}；警告：${JSON.stringify(execution.result?.warnings ?? [])}`));
                details.append(node("p", `原候选统计：${execution.result?.candidate_count ?? "未知"}；保留信息：${JSON.stringify(execution.result?.retention ?? execution.result?.upstream_retention ?? "未知")}；缓存：${JSON.stringify(execution.result?.cache_status ?? execution.result?.cache_hit ?? "未知")}`));
                for (const name of ["claimed_platform", "selected_backend", "result_kind", "elapsed_ms", "title", "author", "asset_count", "relationship_count"]) {
                    const line = node("p"); line.append(node("span", `${name}：`), node("span", "", "data-field", name)); details.append(line);
                }
                details.append(node("p", "", "data-provider-content"), node("div", "", "data-provider-assets"), node("p", "", "data-provider-scope-note"));
                const attempts = node("section", undefined, "data-provider-attempts");
                attempts.className = "provider-lab-attempt-table-wrap";
                const table = node("table"), head = node("thead"), header = node("tr");
                ["#", "Backend", "类别", "结果", "耗时", "候选", "选择"].forEach((label) => header.append(node("th", label)));
                head.append(header); table.append(head, node("tbody", undefined, "data-provider-attempt-rows"));
                attempts.append(table); details.append(attempts);
                const sort = node("select", undefined, "data-history-format");
                sort.setAttribute("aria-label", "历史候选排序");
                sort.append(node("option", "上游原顺序", "value", "source"), node("option", "分辨率降序", "value", "resolution"));
                details.append(sort);
                card.append(details);
                renderResult(card, summary(execution), documentObject);
                const route = node("button", "设为首选", "data-history-route", "primary");
                route.disabled = !execution.source_url || !descriptor?.available || descriptor.route_selectable !== true;
                const fallback = node("button", "加入备选", "data-history-route", "fallback");
                fallback.disabled = route.disabled;
                const retest = node("button", "复测此作品 / 此 Provider", "data-history-retest");
                const reason = historyRetestReason(execution, descriptor);
                retest.disabled = busy || attempted.has(execution.id) || Boolean(reason);
                const actions = node("div");
                actions.className = "provider-lab-card-actions";
                actions.append(route, fallback, retest);
                card.append(actions, node("p", reason || "复测使用当前执行选项与本次外部分享授权。"));
                if (selected?.id === execution.id) card.classList.add("is-selected");
                sample.append(card);
                if (execution.source_url) settings.routeEditor?.mount?.(sample);
                if (String(pending?.id) === String(execution.id)) {
                    const confirmation = node("section", undefined, "data-history-confirmation");
                    confirmation.append(node("h4", "确认单项复测"), node("p", `${execution.source_host || "来源"} · ${providers[execution.provider_id] || execution.provider_id} · 仅复测这一项`), node("p", execution.source_url));
                    card.append(confirmation);
                    settings.configureRetest?.(execution, confirmation);
                    confirmation.append(node("button", "取消", "data-history-cancel"), node("button", "确认复测这一项", "data-history-confirm", execution.id));
                }
            });
            markSelection();
        }
        function summary(execution) {
            return {...execution.result, provider_id: execution.provider_id,
                status: Object.prototype.hasOwnProperty.call(STATUS_LABELS, execution.state)
                    ? execution.state : execution.result?.status || "unknown", history_summary: true,
                history_execution_id: execution.id};
        }
        async function select(execution, asset, draft) {
            selected = execution;
            selectedAsset = asset;
            root.setAttribute("data-history-selection", "open");
            settings.onSelection({providerId: execution.provider_id, asset, historyExecution: execution});
            markSelection();
            if (!execution.source_url) return;
            await settings.routeEditor?.load({preserveDraft: true});
            refreshCurrentRoute();
            if (selected !== execution || !active) return;
            if (draft) settings.routeEditor?.selectProvider(execution.provider_id, execution, draft === true ? "primary" : draft);
            if (draft) root.querySelector("[data-lab-route-editor]")?.scrollIntoView?.({block: "start", behavior: "smooth"});
        }
        function markSelection() {
            panel.querySelectorAll("[data-lab-select]").forEach((button) => {
                const chosen = Boolean(selectedAsset) && button.closest("[data-history-execution]")?.dataset.historyExecution === String(selected?.id)
                    && button.dataset.historySelectionId === historyCandidateId(selected.id, selected.result.assets.indexOf(selectedAsset));
                button.setAttribute("aria-pressed", String(chosen));
                button.textContent = chosen ? "本次已选" : "本次使用";
            });
        }
        async function click(event) {
            const target = event.target;
            if (target.closest?.("[data-history-cancel]")) { cancelPending(); render(); return; }
            const inspection = target.closest?.("[data-history-inspect]");
            if (inspection) {
                cancelPending(); clearSelection(true); inspected = inspection.dataset.historyInspect; render();
                const execution = executions.find((item) => String(item.id) === String(inspected));
                if (execution?.source_url) {
                    root.setAttribute("data-history-selection", "open");
                    await settings.routeEditor?.load({preserveDraft: true});
                    refreshCurrentRoute();
                } else settings.routeEditor?.invalidate();
                executionsNode.querySelector(".provider-lab-history-detail")?.scrollIntoView?.({block: "nearest"});
                return;
            }
            const pagination = target.closest?.("[data-history-page]");
            if (pagination && !pagination.disabled) { cancelPending(); clearSelection(); inspected = null; page += pagination.dataset.historyPage === "next" ? 1 : -1; render(); return; }
            if (target.closest?.("[data-history-refresh]")) return loadBatches();
            const batchButton = target.closest?.("[data-history-batch]");
            if (batchButton) return loadBatch(batchButton.dataset.historyBatch);
            if (target.closest?.("[data-lab-expand]")) {
                const container = target.closest("[data-provider-assets]");
                Array.from(container.querySelectorAll(".provider-lab-asset")).filter((row) => row.hidden).slice(0, 64).forEach((row) => { row.hidden = false; });
                updateAssetCounts(container); return;
            }
            const card = target.closest?.("[data-history-execution]");
            const execution = executions.find((item) => String(item.id) === card?.dataset.historyExecution);
            if (!execution) return;
            if (target.closest?.("[data-lab-inspect]")) return;
            const choose = target.closest?.("[data-lab-select]");
            if (choose) {
                const asset = execution.result?.assets?.find((_asset, ordinal) =>
                    historyCandidateId(execution.id, ordinal) === choose.dataset.historySelectionId);
                if (asset) return select(execution, asset, false);
                return;
            }
            const routeAction = target.closest?.("[data-history-route]");
            if (routeAction) {
                if (routeAction.disabled) return;
                cancelPending(); render(); return select(execution, null, routeAction.dataset.historyRoute || "primary");
            }
            const confirming = target.closest?.("[data-history-confirm]");
            if ((!target.closest?.("[data-history-retest]") && !confirming) || busy || attempted.has(execution.id)) return;
            const reason = historyRetestReason(execution, settings.descriptors.find((item) => item.provider_id === execution.provider_id));
            if (reason) { status.textContent = reason; return; }
            if (!confirming) { pending = execution; render(); return; }
            if (pending !== execution || String(inspected) !== String(execution.id) || confirming.dataset.historyConfirm !== String(execution.id)) return;
            cancelPending();
            busy = true; attempted.add(execution.id); render();
            status.textContent = "正在执行指定项；原记录保持不变。";
            try {
                const result = await settings.retest(execution);
                if (result.executed === false) { attempted.delete(execution.id); status.textContent = result.not_run_reason || "未执行，请核对当前授权与选项。"; }
                else {
                    status.textContent = `复测结果：${result.status || "未知"}；${result.history?.saved === true ? `历史已保存（${result.history.id}）` : `历史未确认保存：${result.history?.error_code || "未知"}；不会自动重跑`}`;
                    const output = node("article"); output.className = "provider-lab-card";
                    output.append(node("h3", "本次指定项复测"), node("p", "", "data-provider-error"), node("span", "", "data-provider-status-label"), node("div", "", "data-provider-assets"));
                    renderResult(output, {...result, history_summary: true}, documentObject);
                    output.querySelectorAll("[data-lab-select]").forEach((button) => { button.disabled = true; button.title = "从保存后的历史记录选择路线依据"; });
                    panel.append(output);
                }
            } catch (_error) { status.textContent = "复测请求结果未确认；请核对历史与调用账本，不自动重发。"; }
            finally { busy = false; render(); }
        }
        async function change(event) {
            if (event.target === host || event.target === order || event.target === stateFilter) { cancelPending(); clearSelection(); inspected = null; page = 0; render(); return; }
            if (event.target.matches?.("[data-history-observation]")) {
                cancelPending(); clearSelection(true); inspected = event.target.value; render();
                const execution = executions.find((item) => String(item.id) === String(inspected));
                if (execution?.source_url) {
                    root.setAttribute("data-history-selection", "open");
                    await settings.routeEditor?.load({preserveDraft: true});
                    refreshCurrentRoute();
                } else settings.routeEditor?.invalidate();
                return;
            }
            if (!event.target.matches?.("[data-history-format]")) return;
            const card = event.target.closest("[data-history-execution]");
            const execution = executions.find((item) => String(item.id) === card.dataset.historyExecution);
            renderAssets(documentObject, card.querySelector("[data-provider-assets]"), summary(execution),
                event.target.value === "resolution" ? qualityOrder(execution.result?.assets) : execution.result?.assets);
            markSelection();
        }
        panel.addEventListener("click", click); panel.addEventListener("change", change);
        return {loadBatches, loadBatch, refreshCurrentRoute, get source() { return selected?.source_url || executions.find((item) => String(item.id) === String(inspected))?.source_url || ""; }, get active() { return active; }, get busy() { return busy; },
            activate(value) { cancelPending(); settings.routeEditor?.restore?.(); active = value; panel.hidden = !value; clearSelection(); markSelection(); if (value) void loadBatches(); },
            destroy() { sequence += 1; listSequence += 1; settings.routeEditor?.restore?.(); panel.removeEventListener("click", click); panel.removeEventListener("change", change); }};
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
        const descriptors = options.descriptors || readProviderDescriptors(documentObject);
        const descriptorById = new Map(descriptors.map((item) => [item.provider_id, item]));
        const executedProviders = new Set();
        const requestedProviders = new Set();
        let includedProviders = [];
        const resultByProvider = new Map();
        let running = false;
        let currentSource = "";
        let currentSelection = null;
        let historyWorkspace = null;
        const message = documentObject.querySelector("[data-provider-lab-form-message]");
        const submitButton = form.querySelector('[data-lab-all]') || form.querySelector('[type="submit"]');
        const cancelButton = form.querySelector("[data-lab-cancel]");

        function updateSelection(selection) {
            currentSelection = selection;
            const summary = root.querySelector("[data-lab-current-selection]");
            if (summary) summary.textContent = selection
                ? `本次候选：${selection.providerId} · ${selection.asset?.format_label || selection.asset?.quality_label || selection.asset?.asset_id || "历史执行"}。未修改平台路线。`
                : "尚未选择本次候选。平台路线未改变。";
            root.querySelectorAll("[data-lab-select]").forEach((button) => {
                const card = button.closest("[data-provider-card]");
                const selected = Boolean(selection && card?.dataset.providerCard === selection.providerId
                    && button.dataset.assetId === selection.asset?.asset_id);
                button.setAttribute("aria-pressed", String(selected));
                button.textContent = selected ? "本次已选" : "本次使用";
            });
        }

        function summarize() {
            const warning = root.querySelector("[data-lab-comparison-warning]");
            if (warning) warning.textContent = comparisonWarnings(Array.from(resultByProvider.values())).join(" ");
        }

        function runStats() {
            const stats = {included: 0, executed: 0, success: 0, unsupported: 0, failed: 0, timeout: 0, notrun: 0};
            for (const id of includedProviders) {
                if (descriptorById.get(id)?.baseline || id === "animeimages_current") continue;
                stats.included += 1;
                const job = queue.jobs.get(id);
                const notrun = job?.result?.executed === false || ["skipped", "not_configured"].includes(job?.status);
                const executed = !notrun && (job?.result?.executed === true || executedProviders.has(id) && job?.result?.executed !== false);
                if (executed) stats.executed += 1;
                if (job?.result?.executed === false) stats.notrun += 1;
                else if (["success", "unsupported", "failed", "timeout"].includes(job?.status)) stats[job.status] += 1;
                else if (["skipped", "not_configured"].includes(job?.status) || job?.status === "cancelled" && !requestedProviders.has(id)) stats.notrun += 1;
            }
            return stats;
        }
        function showRunStats() {
            const node = root.querySelector("[data-lab-run-stats]");
            if (!node) return;
            const s = runStats();
            node.textContent = `纳入 ${s.included} · 实际执行 ${s.executed} · 成功 ${s.success} · 不支持 ${s.unsupported} · 失败 ${s.failed} · 超时 ${s.timeout} · 未执行 ${s.notrun}`;
            const uncertain = includedProviders.filter((id) => queue.jobs.get(id)?.status === "cancelled" && requestedProviders.has(id) && !executedProviders.has(id)).length;
            if (uncertain) node.textContent += ` · 已取消，执行状态未确认 ${uncertain}`;
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
                showRunStats();
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
            getSource: () => historyWorkspace?.active ? historyWorkspace.source : String(form.elements.source?.value || "").trim(),
            getSelection: () => currentSelection, descriptors, onChange: () => historyWorkspace?.refreshCurrentRoute()});
        const inputBand = root.querySelector(".provider-lab-input-band");
        const inputAnchor = inputBand ? documentObject.createElement("div") : null;
        if (inputBand) inputBand.parentNode.insertBefore(inputAnchor, inputBand);
        let savedAdvancedOpen = false;
        function configureHistoryRetest(execution, container) {
            const wasOpen = root.dataset.historyConfig === "open";
            root.setAttribute("data-history-config", execution ? "open" : "closed");
            form.setAttribute("data-history-retest-provider", execution?.provider_id || "");
            if (!inputBand) return;
            const advanced = form.querySelector(".provider-lab-advanced");
            if (!execution) {
                inputAnchor.parentNode.insertBefore(inputBand, inputAnchor.nextSibling);
                if (wasOpen && advanced) advanced.open = savedAdvancedOpen;
                form.querySelectorAll('input[name="provider_authorizations"]').forEach((input) => { if (input.closest("label")) input.closest("label").hidden = false; });
                syncVideodlControls(form, videodlInventory);
                return;
            }
            if (advanced) { savedAdvancedOpen = advanced.open; advanced.open = true; }
            form.querySelectorAll('input[name="provider_authorizations"]').forEach((input) => { if (input.closest("label")) input.closest("label").hidden = input.value !== execution.provider_id; });
            const controls = form.querySelector("[data-videodl-controls]");
            if (controls) controls.hidden = execution.provider_id !== "videodl_repo";
            container.append(inputBand);
        }
        historyWorkspace = createHistoryWorkspace(documentObject, {root, descriptors, routeEditor,
            fetch: fetchImpl, window: windowObject, url: root.dataset.historyUrl || "/admin/provider-lab/history/",
            onSelection: updateSelection,
            configureRetest: configureHistoryRetest,
            retest: (execution) => {
                if (running) return Promise.resolve({executed: false, not_run_reason: "即时任务仍在执行，请结束后复测。"});
                return runOne(execution.provider_id, execution.source_url, csrfToken(form), null, undefined, execution.id);
            }});

        async function runOne(providerId, source, csrf, providerOptions, signal, historyExecutionId) {
            const descriptor = descriptorById.get(providerId);
            const authorized = Boolean(form.querySelector(`input[name="provider_authorizations"][value="${providerId}"]`)?.checked);
            let reason = descriptor?.available === false ? descriptor.unexecuted_reason === "PROVIDER_DISABLED"
                ? "该上游已关闭。" : descriptor.not_configured_hint || "当前通道未配置或缺少运行依赖。" : "";
            const requirements = Array.isArray(descriptor?.requires_confirmation) ? descriptor.requires_confirmation : [];
            if (!reason && requirements.length && !authorized) reason = "尚未确认本次任务的外部服务与来源分享授权。";
            if (!reason && providerId === "videodl_repo") {
                try { providerOptions = videodlOptions(form, videodlInventory); }
                catch (error) { reason = error.userMessage || "受控执行选项未获授权。"; }
            }
            if (reason) return {provider_id: providerId, status: "skipped", executed: false, not_run_reason: reason, assets: []};
            const body = {source, provider_id: providerId};
            if (historyExecutionId !== undefined) body.history_execution_id = historyExecutionId;
            if (requirements.length) body.confirmations = Object.fromEntries(requirements.map((name) => [name, authorized]));
            if (providerId === "videodl_repo" && providerOptions) body.options = providerOptions;
            requestedProviders.add(providerId);
            const result = await requestJson(fetchImpl, runUrl, csrf, body, {signal});
            if (result.executed === true || !descriptor && result.executed !== false) executedProviders.add(providerId);
            if (result.executed === false) return {...result, provider_id: providerId, status: "skipped",
                not_run_reason: result.unexecuted_reason === "PROVIDER_DISABLED" ? "该上游已关闭。"
                    : result.unexecuted_reason === "CONFIRMATION_REQUIRED" ? "缺少本次调用授权。" : "当前配置或运行条件不允许执行。"};
            return {...result, provider_id: providerId};
        }

        async function submit(event) {
            event.preventDefault();
            if (historyWorkspace?.active) return;
            if (historyWorkspace?.busy) { if (message) message.textContent = "指定项复测仍在执行，请等待结果。"; return; }
            if (running) { return; }
            const source = String(form.elements.source?.value || "").trim();
            const providers = descriptors.length && !event.submitter?.hasAttribute?.("data-lab-selected")
                ? descriptors.filter((item) => !item.baseline && item.provider_id !== "animeimages_current").map((item) => item.provider_id)
                : selectedProviderIds(
                form.querySelectorAll('input[name="providers"]'),
                maxProviders,
            );
            if (!source || source.length > 4096 || !providers.length) {
                if (message) { message.textContent = `请输入有效内容并选择 1 至 ${maxProviders} 个可用 Provider。`; }
                return;
            }
            running = true;
            if (submitButton) { submitButton.disabled = true; }
            if (cancelButton) cancelButton.disabled = false;
            if (message) { message.textContent = `正在运行 ${providers.length} 个独立测试...`; }
            currentSource = source;
            includedProviders = Array.from(new Set(providers));
            executedProviders.clear();
            requestedProviders.clear();
            resultByProvider.clear();
            updateSelection(null);
            root.querySelectorAll("[data-provider-card]").forEach((card) => {
                resetCard(card);
                if (!providers.includes(card.dataset.providerCard)) setCardStatus(card, "skipped");
            });
            queue.start(includedProviders, {source, csrf: csrfToken(form), providerOptions: {}});
            void routeEditor?.load();
            running = true;
            if (submitButton) submitButton.disabled = true;
            if (cancelButton) cancelButton.disabled = false;
        }

        async function click(event) {
            const tab = event.target.closest?.("[data-lab-tab]");
            if (tab) {
                const historical = tab.dataset.labTab === "history";
                root.dataset.labView = historical ? "history" : "instant";
                root.querySelectorAll("[data-lab-tab]").forEach((button) => button.setAttribute("aria-pressed", String(button === tab)));
                historyWorkspace?.activate(historical); return;
            }
            if (event.target.closest?.("[data-lab-history]")) return;
            const useCurrent = event.target.closest?.("[data-lab-current-inspect]");
            if (useCurrent && currentSelection?.historyExecution) return;
            if (useCurrent && currentSelection) {
                const card = documentObject.querySelector(`[data-provider-card="${currentSelection.providerId}"]`);
                const button = Array.from(card?.querySelectorAll("[data-lab-inspect]") || [])
                    .find((node) => node.dataset.assetId === currentSelection.asset.asset_id);
                if (button) {
                    button.closest("[data-asset-id]").hidden = false;
                    updateAssetCounts(card.querySelector("[data-provider-assets]"));
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
                if (historyWorkspace?.busy) return;
                if (queue.retry(retry.closest("[data-provider-card]")?.dataset.providerCard)) {
                    running = true;
                    if (submitButton) submitButton.disabled = true;
                    if (cancelButton) cancelButton.disabled = false;
                }
                return;
            }
            const expand = event.target.closest?.("[data-lab-expand]");
            if (expand) {
                const container = expand.closest("[data-provider-assets]");
                Array.from(container.querySelectorAll(".provider-lab-asset")).filter((row) => row.hidden)
                    .slice(0, 64).forEach((row) => { row.hidden = false; });
                updateAssetCounts(container);
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
                includedProviders = [];
                executedProviders.clear();
                requestedProviders.clear();
                showRunStats();
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
            if (event.target.closest?.("[data-lab-history]")) return;
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
            historyWorkspace,
            get currentSelection() { return currentSelection; },
            get runStats() { return runStats(); },
            destroy() {
                historyWorkspace?.destroy();
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
            return createController(document);
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
        createHistoryWorkspace,
        historyRetestReason,
        historyEvidence,
        historyTime,
        historyOrigin,
        historyRelation,
        historyTimestamp,
        routeRequestBody,
        diagnosticResult,
        comparisonWarnings,
        knownBoolean,
        qualityOrder,
        metaFragment,
        scopeMessage,
        trackLabel,
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
