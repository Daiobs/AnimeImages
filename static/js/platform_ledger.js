(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.AnimeImagesPlatformLedger = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";
    const PROVIDERS = {parsehub_repo: "ParseHub", videodl_repo: "videodl", yt_dlp_existing: "yt-dlp",
        gallery_dl_existing: "gallery-dl", tiantian_existing: "天天解析", kukutool_existing: "Kukutool",
        animeimages_current: "复合路线基线"};
    const EXECUTORS = {parsehub_generic: "ParseHub", yt_dlp_generic: "yt-dlp", gallery_dl_generic: "gallery-dl",
        tiantianjiexi_generic: "天天解析", kukutool_generic: "Kukutool", videodl_repo: "videodl"};
    const TYPES = {image: "图片", gallery: "图集", article: "正文媒体", multimedia: "混合媒体", livephoto: "实况", video: "视频", audio: "音频", unknown: "未知"};
    const EVIDENCE = {confirmed: "已确认目标媒体", unconfirmed: "未确认媒体", declared: "仅声明", none: "尚无有效媒体",
        confirmed_media: "已确认目标媒体", unconfirmed_media: "未确认媒体", declared_only: "仅声明", no_valid_media: "尚无有效媒体"};
    const HINTS = {unknown_source: "来源未保留，仅供追溯，不形成平台路线建议", no_records: "已配置 / 暂无记录", content_conflict: "不同内容的候选有差异；保存将作用于整个 host",
        reference_only: "有媒体参考，暂不可固定", comparable: "多家有正向证据，需核对同作品与质量字段",
        unique_positive: "唯一已验证候选，非最佳画质结论", unconfirmed_media: "有媒体返回，目标身份待核对", no_positive_evidence: "尚无已确认目标媒体"};
    const ADOPT = {unknown_source: "来源未知，无法进入路线草稿", protected: "沿用成熟路线，不适用通用路线保存", existing_route: "沿用既有人工路线", draft_candidate: "可进入草稿，保存前核对当前配置",
        reference_only: "仅供参考，目标身份或执行选项需核对；Common / 未知选项暂不可固定", no_evidence: "暂无正向媒体参考"};
    const STATES = {success: "成功", failed: "失败", timeout: "超时", unsupported: "不支持", not_executed: "未执行",
        skipped: "未执行", planned: "未执行", running: "执行中", unknown: "未知", native_observed: "原生观察",
        partial: "失败含部分媒体", offline: "离线回放", returned: "已返回"};
    function routeText(route) {
        if (!route) return "路线未读取";
        if (route.applicable === false) return route.reason === "unknown_source" ? "来源未知，无法确定路线"
            : route.reason === "source_blocked" ? "当前来源不适用通用路线" : "沿用成熟路线";
        const ids = route.active ? route.provider_order : route.effective_provider_order;
        return `${route.active ? "人工" : "默认"} · ${(ids || []).map((id) => EXECUTORS[id] || id).join(" → ") || "未记录"}`;
    }
    function groupWorks(rows) {
        const groups = new Map();
        for (const row of rows) {
            const key = row.source_url || row.work_key || `${row.batch?.id}:${row.sample_key || row.id}`;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(row);
        }
        return [...groups.values()];
    }
    function groupObservations(rows) {
        const groups = new Map();
        for (const row of rows) {
            const key = `${row.provider_id}:${row.application_record_id || row.id}`;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(row);
        }
        return [...groups.values()];
    }
    function createWorkspace(doc, settings) {
        const root = settings.root, panel = root.querySelector("[data-platform-ledger]");
        if (!panel) return null;
        const overview = panel.querySelector("[data-ledger-overview]"), detail = panel.querySelector("[data-ledger-detail]");
        const filters = panel.querySelector("[data-ledger-filters]"), status = panel.querySelector("[data-ledger-status]");
        const tableNode = panel.querySelector("[data-ledger-table]"), paging = panel.querySelector("[data-ledger-pagination]");
        let active = false, page = 1, host = "", detailPage = 1, workId = null, sequence = 0, recordSequence = 0;
        let data = null, detailData = null, selectedRecord = null, loadedAssets = 64;
        const state = Object.fromEntries(["q", "content_type", "provider_id", "evidence", "route_state", "sort"].map((key) => [key, ""]));
        state.sort = "latest";
        const n = (tag, text, attr, value = "") => {
            const el = doc.createElement(tag);
            if (text !== undefined) el.textContent = String(text);
            if (attr) el.setAttribute(attr, String(value));
            if (tag === "button") el.type = "button";
            return el;
        };
        const time = (value) => settings.lab.historyTime(value);
        const providerName = (id) => PROVIDERS[id] || EXECUTORS[id] || id || "未知上游";
        const providerSnapshot = (id) => {
            const d = settings.descriptors.find((item) => item.provider_id === id || item.execution_provider_id === id);
            return d ? d.available ? "页面加载时可用" : d.unexecuted_reason === "PROVIDER_DISABLED" ? "页面加载时已关闭" : "页面加载时未配置/不可用" : "当前配置未检查";
        };
        async function read(params) {
            const url = new URL(settings.url, settings.window.location.origin);
            for (const [key, value] of Object.entries(params)) if (value !== "" && value !== null) url.searchParams.set(key, String(value));
            const response = await settings.fetch(url.href, {credentials: "same-origin", cache: "no-store"});
            const payload = await response.json();
            if (!response.ok || payload.ok !== true) throw new Error(payload.error_code || "LEDGER_READ_FAILED");
            return payload;
        }
        function paginate(container, payload, attribute) {
            container.replaceChildren(); container.className = "platform-ledger-pagination";
            const current = payload.page || 1, pages = payload.pages || Math.max(1, Math.ceil(payload.total / (payload.page_size || 20)));
            const prev = n("button", "←", attribute, current - 1), next = n("button", "→", attribute, current + 1);
            prev.setAttribute("aria-label", "上一页"); next.setAttribute("aria-label", "下一页");
            prev.title = "上一页"; next.title = "下一页"; prev.disabled = current <= 1; next.disabled = current >= pages;
            container.append(prev, n("span", `第 ${current} / ${pages} 页 · 共 ${payload.total} 项`), next);
        }
        function providerSummary(p, compact = false) {
            const block = n("div"); block.className = "platform-ledger-provider";
            const short = {confirmed: "有正向证据", confirmed_media: "有正向证据", unconfirmed: "媒体待确认", unconfirmed_media: "媒体待确认", none: "暂无媒体", no_valid_media: "暂无媒体"};
            const evidence = n("span", (compact && short[p.evidence]) || EVIDENCE[p.evidence] || p.evidence || "尚无有效媒体");
            evidence.className = ["confirmed", "confirmed_media"].includes(p.evidence) ? "ledger-positive" : "platform-ledger-muted";
            block.append(n("strong", providerName(p.provider_id)), evidence);
            const latest = n("small", `最近：${STATES[p.latest_status] || p.latest_status || "未知"}`);
            latest.title = p.latest_error || ""; block.append(latest);
            return block;
        }
        function renderTable() {
            tableNode.replaceChildren();
            const table = n("table"), thead = n("thead"), header = n("tr"), tbody = n("tbody");
            table.className = "platform-ledger-table";
            const headings = ["平台 / hostname", "内容", "上游概况", "当前路线", "选路提示", "最近观察", "操作"];
            headings.forEach((label) => header.append(n("th", label))); thead.append(header); table.append(thead);
            for (const item of data.platforms || []) {
                const row = n("tr");
                const values = headings.map((label) => n("td", undefined, "data-label", label));
                values[0].append(n("strong", item.display_name || item.source_host));
                if (item.display_name && item.display_name !== item.source_host) values[0].append(n("small", item.source_host));
                values[0].append(n("small", `${item.observation_count ?? 0} 次独立观察 · ${item.record_count ?? 0} 条记录`));
                values[1].textContent = (item.content_types || []).map((type) => TYPES[type] || type).join(" / ") || "未知";
                for (const p of item.providers || []) values[2].append(providerSummary(p, true));
                if (!(item.providers || []).length) values[2].textContent = "暂无执行记录";
                values[3].textContent = routeText(item.current_route);
                values[4].textContent = HINTS[item.route_hint] || item.route_hint || "尚无可比较的目标媒体证据";
                values[5].textContent = time(item.latest_at);
                values[6].className = "platform-ledger-actions";
                values[6].append(n("button", "查看对比", "data-ledger-open", item.source_host),
                    n("button", "补充测试", "data-ledger-test", item.source_host), n("button", "编辑路线", "data-ledger-route", item.source_host));
                row.append(...values); tbody.append(row);
            }
            table.append(tbody); tableNode.append(table);
            if (!(data.platforms || []).length) tableNode.append(n("p", "没有符合当前筛选的来源。"));
            status.textContent = `已收录 ${data.total} 个匹配来源 · 本页 ${(data.platforms || []).length} 个`;
            const scope = panel.querySelector("[data-ledger-scope]"); scope.replaceChildren();
            scope.append(n("span", `本应用持久记录与实际路线；${data.record_count ?? "未知"} 条记录，${data.unique_observation_count ?? "未知"} 次去重观察。未导入归档不在本表。 `));
            if (data.unknown_source_count) scope.append(n("button", `来源未保留：${data.unknown_source_count} 条`, "data-ledger-open", "unknown"));
            paginate(paging, data, "data-ledger-page");
        }
        async function loadTable() {
            const version = ++sequence;
            status.textContent = "正在读取全范围摘要…";
            try {
                const payload = await read({view: "platforms", ...state, page, page_size: 20});
                if (version !== sequence || !active) return;
                data = payload; page = payload.page; renderTable();
            } catch (error) { if (version === sequence) status.textContent = `读取失败，未将其计为零平台：${error.message}`; }
        }
        function currentRoute() {
            const node = detail.querySelector("[data-ledger-current-route]");
            if (node) node.textContent = routeText(settings.routeEditor.route || detailData?.platform?.current_route);
        }
        function renderDetail() {
            settings.routeEditor.restore(); detail.replaceChildren();
            const back = n("button", "← 平台总表", "data-ledger-back");
            const heading = n("div"); heading.className = "platform-ledger-detail-heading";
            heading.append(back, n("h2", host === "unknown" ? "来源未保留的记录" : host));
            if (host !== "unknown") heading.append(n("button", "补充测试", "data-ledger-test", host), n("button", "编辑路线", "data-ledger-route", host));
            detail.append(heading, n("p", "", "data-ledger-current-route"));
            detail.append(n("p", host === "unknown" ? "来源未保留，不推断平台或路线范围。" : "路线作用于精确 hostname 的全部内容；以下内容分组仅用于比较。不同作品不能据此排名画质。"));
            const p = detailData.platform;
            if (p) {
                detail.append(n("p", HINTS[p.route_hint] || p.route_hint || "尚无路线参考"));
                const evidence = n("div"); evidence.className = "platform-ledger-detail-providers";
                for (const provider of p.providers || []) {
                    const block = providerSummary(provider);
                    block.append(n("small", `最近尝试 ${time(provider.latest_at)} · 最近成功 ${time(provider.last_success_at)}`),
                        n("small", providerSnapshot(provider.provider_id)), n("small", ADOPT[provider.adoptability] || provider.adoptability || "当前可采用性未检查"));
                    if (provider.latest_error) block.append(n("small", `最近错误：${provider.latest_error}`));
                    if (provider.undated_count) block.append(n("small", `${provider.undated_count} 次观察时间未知，不据此判断最新`));
                    if (provider.partial_media_count) block.append(n("small", `${provider.partial_media_count} 次最终失败含部分媒体`));
                    evidence.append(block);
                }
                detail.append(evidence);
            }
            if (workId) detail.append(n("button", "返回该平台全部作品", "data-ledger-all-works"));
            detail.append(n("p", `跨批次共 ${detailData.total} 条记录 · 当前加载 ${(detailData.executions || []).length} 条；关联副本保留来源，统计只计一次。`));
            for (const rows of groupWorks(detailData.executions || [])) {
                const sample = n("section"); sample.className = "platform-ledger-work";
                sample.append(n("h3", rows.find((e) => e.title)?.title || "作品标题未记录"));
                const types = [...new Set(rows.flatMap((e) => e.content_types || []))];
                sample.append(n("p", `${types.map((x) => TYPES[x] || x).join(" / ") || "类型未知"} · ${rows[0].source_host || "来源未保留"}`));
                if ((rows[0].source_url || rows[0].can_compare_work) && !workId) sample.append(n("button", "仅比较这条作品（全部批次）", "data-ledger-work", rows[0].id));
                const list = n("div"); list.className = "platform-ledger-observations";
                groupObservations(rows).forEach((copies) => {
                    const e = copies.find((item) => !item.application_record_id) || copies[0];
                    const cell = n("div"); cell.className = "platform-ledger-observation";
                    const button = n("button", undefined, "data-ledger-inspect", e.id);
                    const outcome = e.latest_status || e.observation_status || e.result?.status || e.state || "unknown";
                    button.append(n("strong", providerName(e.provider_id)), n("span", STATES[outcome] || outcome),
                        n("span", e.asset_count == null && e.result?.candidate_count == null ? "候选数量未知" : `${e.asset_count ?? e.result.candidate_count} 个已保留候选`), n("small", time(e.started_at || e.finished_at)),
                        n("small", `${e.origin === "scout" ? "Scout" : e.origin === "lab" ? "Provider Lab" : e.origin || "来源未知"} · ${e.batch?.label || e.batch?.batch_key || "批次未知"}`));
                    if (e.evidence) button.append(n("small", EVIDENCE[e.evidence] || e.evidence));
                    if (e.application_record_id) button.append(n("small", `关联调用 #${e.application_record_id}，不重复计数`));
                    if (copies.length > 1) button.append(n("small", `另有 ${copies.length - 1} 条关联副本，不重复计数`));
                    if (e.evidence_kind) button.append(n("small", {product_execution: "产品执行记录", native_capture_replay: "原生观察 / 离线转换", offline_replay: "离线回放"}[e.evidence_kind] || e.evidence_kind));
                    if (e.result?.error_detail || e.result?.error_code) button.append(n("small", e.result.error_detail || e.result.error_code));
                    cell.append(button);
                    if (copies.length > 1) {
                        const links = n("details"); links.append(n("summary", "关联来源记录"));
                        copies.filter((copy) => copy !== e).forEach((copy) => links.append(n("button", `${copy.origin === "scout" ? "Scout" : "Provider Lab"} · #${copy.id}`, "data-ledger-inspect", copy.id)));
                        cell.append(links);
                    }
                    list.append(cell);
                });
                sample.append(list); detail.append(sample);
            }
            const nav = n("nav"); paginate(nav, detailData, "data-ledger-detail-page"); detail.append(nav);
            detail.append(n("section", undefined, "data-ledger-record"), n("section", undefined, "data-ledger-route-slot"));
            currentRoute();
        }
        async function loadDetail({edit = false} = {}) {
            const version = ++sequence; recordSequence += 1; selectedRecord = null;
            overview.hidden = true; detail.hidden = false;
            settings.routeEditor.restore(); detail.replaceChildren(n("p", "正在读取跨批次记录…"));
            try {
                const payload = await read({view: "platform", source_host: host, page: detailPage, page_size: 20, work_id: workId});
                if (version !== sequence || !active) return;
                detailData = {...payload, ...payload.paging}; detailPage = detailData.page; renderDetail();
                if (edit) await showRoute();
            } catch (error) { if (version === sequence) detail.replaceChildren(n("button", "← 平台总表", "data-ledger-back"), n("p", `详情读取失败：${error.message}`)); }
        }
        async function open(nextHost, edit = false) {
            if (host !== nextHost) { settings.routeEditor.invalidate(); detailPage = 1; workId = null; }
            host = nextHost; await loadDetail({edit});
        }
        async function showRoute(evidence, position) {
            if (!host || host === "unknown") return;
            const version = sequence, target = host;
            settings.routeEditor.mount(detail.querySelector("[data-ledger-route-slot]"));
            await settings.routeEditor.load({preserveDraft: true});
            if (version !== sequence || target !== host || !active) return;
            if (evidence) settings.routeEditor.selectProvider(evidence.provider_id, evidence, position);
            currentRoute();
            detail.querySelector("[data-ledger-route-slot]")?.scrollIntoView?.({block: "nearest"});
        }
        function renderRecord() {
            const container = detail.querySelector("[data-ledger-record]"); if (!container || !selectedRecord) return;
            const e = selectedRecord, result = e.result || {};
            container.replaceChildren(); container.className = "platform-ledger-record";
            container.append(n("h3", `${providerName(e.provider_id)} · 记录 #${e.id}`), n("p", settings.lab.historyEvidence(e)),
                n("p", `实际执行：${time(e.started_at)} 至 ${time(e.finished_at)}`), n("p", settings.lab.historyRelation(e)),
                n("p", `状态：${STATES[result.status] || result.status || e.state || "未知"} · 错误：${result.error_detail || result.error_code || "无记录"}`));
            const metadata = n("details"); metadata.append(n("summary", "版本、Backend、完整性与限制"), n("pre", JSON.stringify({version: e.version,
                provider_version: result.provider_version, backend: result.selected_backend || result.backend_id, options: e.options,
                source: e.origin, batch: e.batch, metadata: result.metadata, retention: result.retention || result.upstream_retention,
                relationships: result.relationships, limitations: result.limitations, cache: result.cache_status ?? result.cache_hit}, null, 2)));
            container.append(metadata, n("p", "候选仅供比较，无资源授权；分辨率与格式是记录值，未据此验证实际画质或下载。"));
            const descriptor = settings.descriptors.find((x) => x.provider_id === e.provider_id);
            const actions = n("div"); actions.className = "platform-ledger-actions";
            for (const [value, label] of [["primary", "设为首选"], ["fallback", "加入备选"]]) {
                const button = n("button", label, "data-ledger-draft", value);
                button.disabled = !e.source_host || !descriptor?.available || descriptor.route_selectable !== true || e.provider_id === "animeimages_current";
                actions.append(button);
            }
            container.append(actions);
            const assets = Array.isArray(result.assets) ? result.assets : null;
            if (!assets) { container.append(n("p", "候选明细未保留，不等于没有媒体。")); return; }
            const sort = n("select", undefined, "data-ledger-format-order"); sort.setAttribute("aria-label", "候选顺序");
            sort.append(n("option", "上游原顺序", "value", "source"), n("option", "已加载视频分辨率降序", "value", "resolution"));
            sort.value = e._displayOrder || "source"; container.append(sort);
            const list = n("div"); list.className = "platform-ledger-assets";
            const ordered = sort.value === "resolution" ? settings.lab.qualityOrder(assets) : assets;
            ordered.slice(0, loadedAssets).forEach((asset) => {
                const row = n("div"); row.className = "provider-lab-asset";
                row.append(settings.lab.metaFragment(doc, asset)); list.append(row);
            });
            container.append(n("p", `已显示 ${Math.min(loadedAssets, assets.length)} / 已加载 ${assets.length} / 已保留 ${e.asset_count ?? (Array.isArray(result.assets) ? assets.length : "未知")} 项；上游返回总量 ${result.metadata?.returned_count ?? result.candidate_count ?? "未知"}`), list);
            if (loadedAssets < assets.length || e.asset_paging?.page < e.asset_paging?.pages) container.append(n("button", "再显示候选", "data-ledger-expand"));
        }
        async function inspect(id) {
            const version = ++recordSequence, target = host;
            const container = detail.querySelector("[data-ledger-record]"); container.replaceChildren(n("p", "正在读取候选明细…"));
            try {
                const payload = await read({execution_id: id});
                if (version !== recordSequence || target !== host || !active) return;
                selectedRecord = payload.execution; loadedAssets = 64; renderRecord();
                container.scrollIntoView?.({block: "nearest"});
            } catch (error) { if (version === recordSequence) container.replaceChildren(n("p", `明细读取失败：${error.message}`)); }
        }
        async function expandAssets() {
            const e = selectedRecord;
            if (!e) return;
            if (loadedAssets < (e.result.assets || []).length) { loadedAssets += 64; renderRecord(); return; }
            if (!e.asset_paging || e.asset_paging.page >= e.asset_paging.pages || e._loading) return;
            e._loading = true;
            const version = recordSequence;
            try {
                const payload = await read({execution_id: e.id, asset_page: e.asset_paging.page + 1, asset_page_size: e.asset_paging.page_size});
                if (version !== recordSequence || selectedRecord !== e || !active) return;
                e.result.assets.push(...(payload.execution.result.assets || [])); e.asset_paging = payload.execution.asset_paging;
                loadedAssets += 64; renderRecord();
            } catch (_error) {
                if (selectedRecord === e) detail.querySelector("[data-ledger-record]").append(n("p", "候选下一页读取失败，原有候选保留；未重新解析。"));
            } finally { e._loading = false; }
        }
        async function supplementary(target) {
            let source = target === host ? detailData?.platform?.safe_source_url : data?.platforms?.find((p) => p.source_host === target)?.safe_source_url;
            if (!source && target !== "unknown") {
                const version = sequence;
                try {
                    const payload = await read({view: "platform", source_host: target, page: 1, page_size: 1});
                    if (version !== sequence || !active) return;
                    source = payload.platform?.safe_source_url;
                } catch (_error) {
                    if (version !== sequence || !active) return;
                    status.textContent = "未取得安全作品引用；可在即时测试中输入公开作品。";
                }
            }
            settings.openInstant(source || "", target);
        }
        async function click(event) {
            const target = event.target;
            if (target.closest?.("[data-lab-route-editor]")) return;
            if (target.closest?.("[data-ledger-refresh]")) return loadTable();
            if (target.closest?.("[data-ledger-back]")) {
                sequence += 1; recordSequence += 1; settings.routeEditor.invalidate(); settings.routeEditor.restore();
                host = ""; detail.hidden = true; overview.hidden = false; return loadTable();
            }
            const openButton = target.closest?.("[data-ledger-open]"); if (openButton) return open(openButton.dataset.ledgerOpen);
            const route = target.closest?.("[data-ledger-route]"); if (route) return host === route.dataset.ledgerRoute ? showRoute() : open(route.dataset.ledgerRoute, true);
            const supplement = target.closest?.("[data-ledger-test]"); if (supplement) return supplementary(supplement.dataset.ledgerTest);
            const pageButton = target.closest?.("[data-ledger-page]"); if (pageButton && !pageButton.disabled) { page = Number(pageButton.dataset.ledgerPage); return loadTable(); }
            const detailPaging = target.closest?.("[data-ledger-detail-page]"); if (detailPaging && !detailPaging.disabled) { detailPage = Number(detailPaging.dataset.ledgerDetailPage); return loadDetail(); }
            const work = target.closest?.("[data-ledger-work]"); if (work) { workId = Number(work.dataset.ledgerWork); detailPage = 1; return loadDetail(); }
            if (target.closest?.("[data-ledger-all-works]")) { workId = null; detailPage = 1; return loadDetail(); }
            const record = target.closest?.("[data-ledger-inspect]"); if (record) return inspect(record.dataset.ledgerInspect);
            const draft = target.closest?.("[data-ledger-draft]"); if (draft && !draft.disabled && selectedRecord) return showRoute(selectedRecord, draft.dataset.ledgerDraft);
            if (target.closest?.("[data-ledger-expand]")) return expandAssets();
        }
        async function filter(event) {
            event.preventDefault?.();
            for (const key of Object.keys(state)) state[key] = filters.querySelector(`[name="${key}"]`)?.value || "";
            page = 1; await loadTable();
        }
        function change(event) {
            if (event.target.matches?.("[data-ledger-format-order]") && selectedRecord) { selectedRecord._displayOrder = event.target.value; renderRecord(); }
        }
        panel.addEventListener("click", click); panel.addEventListener("change", change); filters.addEventListener("submit", filter);
        return {open, inspect, refresh: () => active ? host ? loadDetail() : loadTable() : Promise.resolve(), currentRoute,
            get active() { return active; }, get host() { return host && host !== "unknown" ? host : ""; },
            activate(value) {
                sequence += 1; recordSequence += 1; active = value; panel.hidden = !value;
                if (value) return host ? loadDetail() : loadTable();
                settings.routeEditor.restore();
            },
            destroy() { sequence += 1; recordSequence += 1; panel.removeEventListener("click", click); panel.removeEventListener("change", change); filters.removeEventListener("submit", filter); }};
    }
    return {createWorkspace, groupWorks, groupObservations, routeText};
}));
