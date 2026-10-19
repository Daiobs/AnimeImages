"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const picker = require("../static/js/ytdlp_result_picker.js");
const ordinary = require("../static/js/ordinary_media_download.js");

const win = {location: {href: "http://localhost:8000/results/", origin: "http://localhost:8000"}};
const documentObject = {querySelector: () => ({value: "csrf-fixture"}), cookie: ""};
const root = {dataset: {selectionUrl: "/media/ytdlp-selected-format/"}};
const SERVICE_SCHEMA = "service_selected_format_v1";
const SERVICE_PROVIDERS = ["tiantianjiexi_generic", "kukutool_generic"];
const LEGACY_SERVICE_PROVIDERS = [...SERVICE_PROVIDERS, "tiantianjiexi", "kukutool"];
function serviceSelection(provider, id = "service720", kind = "video") {
    return {...selected(id, kind), provider, schema: SERVICE_SCHEMA, formatId: id};
}
function serviceItem(provider, id = "service720", kind = "video", extra = {}) {
    return item(id, kind, {provider, selection_schema: SERVICE_SCHEMA,
        audio_tracks: "unknown", video_tracks: "unknown", ...extra});
}
function selected(id = "fmt720", kind = "video") { return {id, kind, token: `signed-${id}`}; }
function item(id = "fmt720", type = "video", extra = {}) {
    return {selection_schema: picker.SCHEMA, candidate_id: id, selected_format_id: id,
        selection_expires_at: Date.now() + 60000,
        type, provider: "yt_dlp_generic", source_platform: "upstream", asset_id: `upstream:selection:${id}`,
        url: `https://cdn.example.org/${id}.${type === "audio" ? "m4a" : "mp4"}`,
        filename: `${id}.${type === "audio" ? "m4a" : "mp4"}`,
        browser_upstream_bound: true, browser_direct_fetch_safe: true, requires_merge: false,
        requires_cookie: false, audio_tracks: "present", video_tracks: type === "audio" ? "absent" : "present",
        ...extra};
}
function response(content, source, type = "video/mp4", overrides = {}) {
    const bytes = new Blob([content], {type});
    return {ok: true, type: "cors", status: 200, url: source, redirected: false,
        headers: {get(name) { return ({"content-type": type, "content-length": String(bytes.size)})[name.toLowerCase()] || null; }},
        body: bytes.stream(), blob: async () => bytes, ...overrides};
}

for (const provider of LEGACY_SERVICE_PROVIDERS) {
    test(`${provider} legacy delivery is exact-candidate video only and never an ordinary fetch grant`, async () => {
        const selection = {...serviceSelection(provider), canLegacy: true, legacyAssetId: "third_party:source:video:1"};
        const media = serviceItem(provider, "service720", "video", {
            selection_delivery: "legacy_manifest", browser_upstream_bound: false, browser_direct_fetch_safe: false,
            asset_id: "third_party:source:video:1", provider_resource_id: "source", provider_cache_key: "cache-source",
            provider_manifest_token: "signed-legacy", source_platform: "third_party", url: "http://cdn.example/video720.mp4",
            size_bytes: 1024,
        });
        assert.equal(picker.validateItem(media, selection, "legacy_manifest"), media);
        const emptyFormatSelection = {...selection, formatId: ""};
        const emptyFormatItem = {...media, selected_format_id: ""};
        assert.equal(picker.validateItem(emptyFormatItem, emptyFormatSelection, "legacy_manifest"), emptyFormatItem);
        for (const change of [{selected_format_id: "other"}, {selected_format_id: undefined}, {selected_format_id: null},
            {candidate_id: "other"}, {asset_id: "other"}]) {
            assert.throws(() => picker.validateItem({...emptyFormatItem, ...change}, emptyFormatSelection, "legacy_manifest"));
        }
        assert.equal(ordinary.isOrdinaryMediaItem(media), false);
        assert.equal(picker.validateLinkItem(media, selection).url, media.url);
        const changes = [{candidate_id: "other"}, {selected_format_id: "other"}, {type: "audio"},
            ...LEGACY_SERVICE_PROVIDERS.filter(value => value !== provider).map(provider => ({provider})),
            {selection_schema: picker.SCHEMA},
            {selection_delivery: "direct"}, {selection_delivery: undefined}, {browser_upstream_bound: true},
            {browser_direct_fetch_safe: true}, {requires_merge: true}, {requires_cookie: true},
            {asset_id: "upstream:selection:service720"}, {url: "javascript:alert(1)"},
            {url: "https://user:pass@cdn.example/video.mp4"}, {url: "https://cdn.example/video.mp4#x"},
            {filename: ""}, {filename: "video\n.mp4"}];
        for (const field of ["asset_id", "provider_resource_id", "provider_cache_key", "provider_manifest_token", "source_platform"]) {
            changes.push({[field]: undefined}, {[field]: " "}, {[field]: "bad\nvalue"});
        }
        for (const change of changes) assert.throws(() => picker.validateItem({...media, ...change}, selection, "legacy_manifest"), JSON.stringify(change));
        assert.throws(() => picker.validateItem(media, selection));
        for (const change of [{canLegacy: false}, {legacyAssetId: "other"}, {legacyAssetId: ""}, {formatId: undefined}]) {
            assert.throws(() => picker.validateItem(media, {...selection, ...change}, "legacy_manifest"));
        }
        assert.throws(() => picker.validateItem({...media, provider: "yt_dlp_generic", selection_schema: picker.SCHEMA},
            {...selection, provider: "yt_dlp_generic", schema: picker.SCHEMA}, "legacy_manifest"));
        assert.throws(() => picker.validateItem({...media, type: "audio"}, {...selection, kind: "audio"}, "legacy_manifest"));
        for (const unsupported of ["other", "videodl_repo", "douyin", "tiantianjiexi_custom", "kukutool_custom"]) {
            assert.throws(() => picker.validateItem({...media, provider: unsupported},
                {...selection, provider: unsupported}, "legacy_manifest"));
        }
        let calls = 0;
        const environment = {documentObject, windowObject: win, fetchImpl: async (_url, options) => {
            calls += 1;
            assert.deepEqual(JSON.parse(options.body), {candidate_id: selection.id, resource_token: selection.token,
                action: ["legacy", "preview", "download"][calls - 1]});
            return {ok: true, json: async () => ({status: "success", item: media})};
        }};
        assert.equal((await picker.descriptor(root, selection, "legacy", environment)).url, media.url);
        await assert.rejects(picker.descriptor(root, selection, "preview", environment));
        await assert.rejects(picker.descriptor(root, selection, "download", environment));
        assert.equal(calls, 3);
    });
}

test("selection snapshots independent legacy capability and same-origin endpoints reject invalid destinations", () => {
    const radio = {value: "v", dataset: {token: "signed-v", kind: "video", canLegacy: "true", legacyAssetId: "old-v"}};
    const selection = picker.selected({dataset: {}, querySelector: () => radio});
    radio.dataset.canLegacy = "false";
    radio.dataset.legacyAssetId = "other";
    assert.equal(selection.canLegacy, true);
    assert.equal(selection.legacyAssetId, "old-v");
    for (const url of [undefined, "", " ", "https://evil.example/download", "//evil.example/status",
        "http://user:pass@localhost:8000/download", "/download#fragment", "javascript:alert(1)"]) {
        assert.throws(() => picker.sameOriginPath(url, win));
    }
    assert.equal(picker.sameOriginPath("/download?token=exact", win), "http://localhost:8000/download?token=exact");
});

for (const provider of ["tiantianjiexi", "kukutool"]) {
    test(`${provider} direct nondefault selection keeps frozen candidate/token and exact signed asset identity`, async () => {
        const assetId = `douyin:7123456789012345678:video:${provider}:format:0123456789abcdef01234567`;
        const radio = {value: "low", dataset: {token: "signed-low-candidate-token", kind: "video", formatId: "",
            canDownload: "true", canPreview: "true", canLegacy: "true", legacyAssetId: assetId}};
        const serviceRoot = {dataset: {selectionUrl: "/media/service-selected-format/", provider,
            selectionSchema: SERVICE_SCHEMA}, querySelector: () => radio};
        const selection = picker.selected(serviceRoot);
        const media = serviceItem(provider, "low", "video", {selected_format_id: "", asset_id: assetId,
            source_platform: "douyin", provider_resource_id: "7123456789012345678",
            provider_cache_key: `douyin:${provider}:validated:7123456789012345678`,
            provider_manifest_token: "signed-low-manifest:timestamp:signature",
            url: "https://cdn.example.org/nondefault.mp4?signature=low-exact", filename: "nondefault.mp4"});
        const before = JSON.stringify(media);
        radio.value = "high"; radio.dataset.token = "signed-high-token"; radio.dataset.legacyAssetId = "other-asset";
        serviceRoot.dataset.provider = `${provider}_generic`;
        const calls = [], saves = [];
        const environment = {documentObject, windowObject: win, fetchImpl: async (_url, options) => {
            calls.push(JSON.parse(options.body));
            return {ok: true, json: async () => ({status: "success", item: media})};
        }};
        for (const action of ["download", "preview"]) {
            assert.equal(await picker.descriptor(serviceRoot, selection, action, environment), media);
        }
        await ordinary.download(media, {fetchImpl: async url => {
            assert.equal(url, media.url); return response("nondefault-bytes", url);
        }, handoffBlob: async (blob, filename) => saves.push([await blob.text(), filename])});
        assert.deepEqual(calls, ["download", "preview"].map(action => ({candidate_id: "low",
            resource_token: "signed-low-candidate-token", action})));
        assert.deepEqual(saves, [["nondefault-bytes", "nondefault.mp4"]]);
        assert.equal(JSON.stringify(media), before);
        assert.equal(selection.legacyAssetId, assetId);
        assert.equal(selection.provider, provider);
        const changes = [{candidate_id: "high"}, {selected_format_id: "high"}, {provider: `${provider}_generic`},
            {selection_schema: picker.SCHEMA}, {asset_id: `douyin:7123456789012345678:video:${provider}`},
            {source_platform: "upstream"}, {provider_resource_id: "8123456789012345678"},
            {provider_cache_key: "other-cache"}, {provider_manifest_token: ""}, {provider_manifest_token: "bad\ntoken"},
            {selection_delivery: "legacy_manifest"}, {url: "http://cdn.example.org/nondefault.mp4"}];
        for (const change of changes) assert.throws(() => picker.validateItem({...media, ...change}, selection));
        for (const change of [{legacyAssetId: ""}, {legacyAssetId: "other-asset"}]) {
            assert.throws(() => picker.validateItem(media, {...selection, ...change}));
        }
    });
}

for (const provider of SERVICE_PROVIDERS) {
    test(`${provider} selected download and link descriptors preserve provider and resource identity`, async () => {
        const serviceRoot = {dataset: {selectionUrl: "/media/service-selected-format/",
            provider, selectionSchema: SERVICE_SCHEMA}};
        const calls = [], saved = [];
        const environment = {documentObject, windowObject: win, fetchImpl: async (url, options) => {
            const body = JSON.parse(options.body); calls.push(body);
            assert.equal(url, "http://localhost:8000/media/service-selected-format/");
            assert.equal(options.credentials, "same-origin");
            assert.equal(options.headers["X-CSRFToken"], "csrf-fixture");
            return {ok: true, json: async () => ({status: "success", item: serviceItem(provider, body.candidate_id)})};
        }};
        for (const id of ["service1080", "service720"]) {
            const selection = serviceSelection(provider, id);
            const media = await picker.descriptor(serviceRoot, selection, "download", environment);
            const link = await picker.resolveLink(serviceRoot, selection, environment);
            assert.equal(link.url, media.url);
            assert.equal(link.filename, media.filename);
            assert.equal(media.provider, provider);
            assert.equal(media.audio_tracks, "unknown", "UI must not fabricate optional track facts");
            await ordinary.download(media, {filename: media.filename,
                fetchImpl: async url => { assert.equal(url, link.url); return response(id, url); },
                handoffBlob: async (blob, filename) => saved.push([await blob.text(), filename]),
            });
        }
        assert.deepEqual(calls, ["service1080", "service720"].flatMap(id => ["download", "link"].map(action =>
            ({candidate_id: id, resource_token: `signed-${id}`, action}))));
        assert.deepEqual(saved, [["service1080", "service1080.mp4"], ["service720", "service720.mp4"]]);
    });

    test(`${provider} rejects swapped provider/schema/token identity without media fallback`, async () => {
        const selection = serviceSelection(provider);
        for (const change of [{provider: "yt_dlp_generic"}, {provider: SERVICE_PROVIDERS.find(p => p !== provider)},
            {provider: "videodl_repo"}, {selection_schema: picker.SCHEMA}, {selection_schema: ""},
            {candidate_id: "other"}, {selected_format_id: "other"}]) {
            for (const action of ["download", "preview", "link"]) {
                let calls = 0;
                await assert.rejects(picker.descriptor(root, selection, action, {documentObject, windowObject: win,
                    fetchImpl: async () => { calls += 1; return {ok: true, json: async () => ({status: "success",
                        item: serviceItem(provider, "service720", "video", change)})}; }}));
                assert.equal(calls, 1);
            }
        }
        for (const change of [{selection_schema: picker.SCHEMA}, {selection_schema: ""},
            {candidate_id: ""}, {browser_upstream_bound: false}, {source_platform: ""},
            {requires_cookie: true}, {requires_merge: true}, {browser_direct_fetch_safe: false}]) {
            assert.equal(ordinary.isOrdinaryMediaItem(serviceItem(provider, "service720", "video", change)), false);
        }
    });

    test(`${provider} audio authorization retains unknown track facts and rejects HTML`, async () => {
        const media = serviceItem(provider, "voice", "audio", {service_audio_file: true});
        const before = JSON.stringify(media);
        assert.equal(ordinary.isOrdinaryMediaItem(media), true);
        assert.equal(ordinary.isOrdinaryMediaItem({...media, service_audio_file: false}), false);
        let handoffs = 0;
        await ordinary.download(media, {filename: media.filename,
            fetchImpl: async url => response("service-audio", url, "audio/mp4"),
            handoffBlob: async (blob, filename) => {
                handoffs += 1; assert.equal(await blob.text(), "service-audio"); assert.equal(filename, "voice.m4a");
            }});
        assert.equal(handoffs, 1);
        assert.equal(JSON.stringify(media), before);
        for (const mime of ["text/html", "video/mp4", "application/vnd.apple.mpegurl"]) {
            await assert.rejects(ordinary.download(media, {filename: media.filename,
                fetchImpl: async url => response("not audio", url, mime),
                handoffBlob: () => assert.fail("invalid MIME cannot save"),
            }), error => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT");
        }
    });

    test(`${provider} HTTP and HLS remain link-only and video HTML never reaches save`, async () => {
        const selection = serviceSelection(provider);
        for (const url of ["http://cdn.example.org/video.mp4", "https://cdn.example.org/master.m3u8"]) {
            const media = serviceItem(provider, "service720", "video", {url, browser_direct_fetch_safe: false,
                filename: url.endsWith("m3u8") ? "master.m3u8" : "video.mp4"});
            assert.equal(picker.validateLinkItem(media, selection).url, url);
            assert.throws(() => picker.validateItem(media, selection));
            assert.equal(ordinary.isOrdinaryMediaItem(media), false);
        }
        const media = serviceItem(provider);
        for (const mime of ["text/html", "application/vnd.apple.mpegurl"]) {
            await assert.rejects(ordinary.download(media, {filename: media.filename,
                fetchImpl: async url => response("not video", url, mime),
                handoffBlob: () => assert.fail("no HTML or manifest handoff"),
            }), error => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT");
        }
    });
}

test("selection captures one immutable candidate identity and its exact token", () => {
    const radio = {value: "b", dataset: {token: "signed-b", kind: "video", canDownload: "true", canPreview: "false", canCheck: "true", label: "720p"}};
    const result = picker.selected({querySelector: () => radio});
    radio.value = "c"; radio.dataset.token = "signed-c";
    assert.equal(result.id, "b"); assert.equal(result.token, "signed-b");
    assert.equal(result.canDownload, true); assert.equal(result.canPreview, false);
    assert.equal(result.canCheck, true);
    assert.equal(Object.isFrozen(result), true);
});

test("check descriptor posts only the selected identity and returns no download grant", async () => {
    const selection = {...selected(), formatId: "format-720"};
    const payload = {status: "success", candidate: {candidate_id: selection.id, format_id: selection.formatId,
        can_download: true, can_preview: true, can_check: false},
        check: {reason: "complete", requests: 2, bytes_read: 4096, complete_moov: true, video: "present", audio: "present"}};
    const controller = new AbortController(); let calls = 0;
    const result = await picker.descriptor(root, selection, "check", {documentObject, windowObject: win,
        fetchImpl: async (_url, options) => {
            calls += 1;
            assert.deepEqual(JSON.parse(options.body), {candidate_id: selection.id, resource_token: selection.token, action: "check"});
            assert.equal(options.signal, controller.signal);
            return {ok: true, json: async () => payload};
        }}, controller.signal);
    assert.equal(calls, 1);
    assert.deepEqual(result, {candidate: payload.candidate, check: payload.check});
    assert.equal(result.url, undefined);
});

test("check rejects substituted identities, malformed flags and all error statuses without retry", async () => {
    const selection = {...selected(), formatId: "format-720"};
    const candidate = {candidate_id: selection.id, format_id: selection.formatId,
        can_download: true, can_preview: true, can_check: false};
    const payloads = [null, {status: "success"}, ...[
        {candidate_id: "other"}, {format_id: "other"}, {format_id: undefined},
        {can_download: "true"}, {can_preview: undefined}, {can_check: "false"},
    ].map(change => ({status: "success", candidate: {...candidate, ...change}, check: {reason: "complete"}})),
    {status: "success", candidate, check: {}}, {status: "success", candidate}];
    for (const payload of payloads) {
        await assert.rejects(picker.descriptor(root, selection, "check", {documentObject, windowObject: win,
            fetchImpl: async () => ({ok: true, json: async () => payload})}));
    }
    for (const status of [403, 409, 429, 503]) {
        let calls = 0;
        await assert.rejects(picker.descriptor(root, selection, "check", {documentObject, windowObject: win,
            fetchImpl: async () => { calls += 1; return {ok: false, status,
                json: async () => ({status: "error", message: `check-error-${status}`})}; }}),
        {message: `check-error-${status}`});
        assert.equal(calls, 1);
    }
});

test("selected descriptor posts candidate identity, never a client URL or format expression", async () => {
    const calls = [];
    const env = {windowObject: win, documentObject, fetchImpl: async (url, options) => {
        const body = JSON.parse(options.body); calls.push({url, options, body});
        return {ok: true, json: async () => ({status: "success", item: item(body.candidate_id)})};
    }};
    const a = await picker.descriptor(root, selected("1080"), "download", env);
    const b = await picker.descriptor(root, selected("720"), "download", env);
    assert.match(a.url, /1080\.mp4$/); assert.match(b.url, /720\.mp4$/);
    assert.deepEqual(Object.keys(calls[1].body).sort(), ["action", "candidate_id", "resource_token"]);
    assert.equal(calls[1].options.credentials, "same-origin");
    assert.equal(calls[1].options.headers["X-CSRFToken"], "csrf-fixture");
});

test("descriptor endpoints are same-origin even if DOM data is modified", async () => {
    for (const url of ["https://elsewhere.example/api", "//elsewhere.example/api", "javascript:alert(1)"]) {
        assert.throws(() => picker.sameOriginPath(url, win));
    }
    assert.equal(picker.sameOriginPath("/media/ytdlp-selected-format/", win), "http://localhost:8000/media/ytdlp-selected-format/");
});

test("returned identity, type, flags and URL cannot silently substitute another format", () => {
    for (const change of [{candidate_id: "other"}, {type: "audio"}, {provider: "videodl_repo"},
        {requires_merge: true}, {requires_cookie: true}, {browser_upstream_bound: false},
        {url: "http://cdn.example.org/video.mp4"}, {url: "https://user:pass@cdn.example.org/video.mp4"},
        {url: "https://cdn.example.org:444/video.mp4"}, {url: "https://cdn.example.org/v.mp4#fragment"},
        {url: "https://cdn.example.org/\\file"}, {url: "data:text/html,anything"}]) {
        assert.throws(() => picker.validateItem(item("fmt720", "video", change), selected()));
    }
    assert.equal(picker.validateItem(item(), selected()).candidate_id, "fmt720");
});

test("download and preview descriptors must match the frozen format ID as well as candidate ID", async () => {
    const selection = {...selected(), formatId: "format-720"};
    for (const action of ["download", "preview"]) {
        for (const formatId of ["other-format", undefined]) {
            await assert.rejects(picker.descriptor(root, selection, action, {documentObject, windowObject: win,
                fetchImpl: async () => ({ok: true, json: async () => ({status: "success",
                    item: item("fmt720", "video", {selected_format_id: formatId})})})}));
        }
        const result = await picker.descriptor(root, selection, action, {documentObject, windowObject: win,
            fetchImpl: async () => ({ok: true, json: async () => ({status: "success",
                item: item("fmt720", "video", {selected_format_id: "format-720"})})})});
        assert.equal(result.selected_format_id, "format-720");
    }
});

test("expired, denied and malformed descriptors stop without a media fetch", async () => {
    for (const result of [{ok: false, json: async () => ({status: "error", message: "已过期"})},
        {ok: true, json: async () => ({status: "success", item: item("other")})},
        {ok: false, json: async () => { throw new Error("bad json"); }}]) {
        let calls = 0;
        await assert.rejects(picker.descriptor(root, selected(), "download", {
            windowObject: win, documentObject, fetchImpl: async () => { calls += 1; return result; },
        }));
        assert.equal(calls, 1);
    }
});

test("descriptor propagates the operation abort signal and does not auto retry", async () => {
    const controller = new AbortController(); controller.abort();
    let count = 0;
    await assert.rejects(picker.descriptor(root, selected(), "download", {documentObject, windowObject: win,
        fetchImpl: async (_url, options) => {
            count += 1; assert.equal(options.signal, controller.signal);
            throw new DOMException("Aborted", "AbortError");
        }}, controller.signal), {name: "AbortError"});
    assert.equal(count, 1);
});

test("bound audio support does not open arbitrary image/audio or merge/cookie items", () => {
    assert.equal(ordinary.isOrdinaryMediaItem(item("voice", "audio")), true);
    for (const change of [{selection_schema: ""}, {source_platform: ""}, {provider: "gallery_dl_generic"},
        {audio_tracks: "unknown"}, {video_tracks: "present"}, {browser_upstream_bound: false},
        {requires_cookie: true}, {requires_merge: true}, {type: "image"}]) {
        assert.equal(ordinary.isOrdinaryMediaItem(item("voice", "audio", change)), false);
    }
});

test("selected audio retains original M4A bytes and filename via existing browser transport", async () => {
    const audio = item("voice", "audio"); let handoff = null; let requested = null;
    const result = await ordinary.download(audio, {
        filename: audio.filename,
        fetchImpl: async (url, options) => {
            requested = {url, options}; return response("exact-audio-bytes", audio.url, "audio/mp4");
        },
        handoffBlob: async (blob, filename) => { handoff = {text: await blob.text(), filename}; },
    });
    assert.equal(requested.url, audio.url); assert.equal(requested.options.credentials, "omit");
    assert.equal(requested.options.redirect, "error"); assert.equal(requested.options.mode, "cors");
    assert.equal(requested.options.referrerPolicy, "no-referrer");
    assert.deepEqual(handoff, {text: "exact-audio-bytes", filename: "voice.m4a"});
    assert.equal(result.transport, "browser_blob"); assert.equal(result.receivedBytes, 17);
});

test("switching two selected video descriptors uses the selected URL and no server fallback", async () => {
    const seen = [];
    for (const id of ["1080", "720"]) {
        const video = item(id);
        const saved = await ordinary.download(video, {filename: video.filename,
            fetchImpl: async (url) => { seen.push(url); return response(id, url); },
            handoffBlob: async (blob, filename) => { assert.equal(await blob.text(), id); assert.equal(filename, id + ".mp4"); },
            streamingClient: {capabilityAvailable() { assert.fail("bound selection must not switch to server/other transport"); }},
        });
        assert.equal(saved.receivedBytes, id.length);
    }
    assert.deepEqual(seen, ["https://cdn.example.org/1080.mp4", "https://cdn.example.org/720.mp4"]);
});

test("audio/video MIME guards do not accept HTML or swap a selected video for audio", async () => {
    assert.equal(ordinary.contentTypeAllowsSelectedAudio("audio/mp4; codecs=mp4a"), true);
    assert.equal(ordinary.contentTypeAllowsSelectedAudio("text/html"), false);
    assert.equal(ordinary.contentTypeAllowsOrdinaryVideo("audio/mp4"), false);
    for (const [kind, type] of [["audio", "text/html"], ["audio", "video/mp4"], ["video", "audio/mp4"]]) {
        const media = item("bad", kind);
        await assert.rejects(ordinary.download(media, {filename: media.filename,
            fetchImpl: async () => response("bytes", media.url, type),
            handoffBlob: () => assert.fail("no invalid handoff"),
        }), error => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT");
    }
});

test("size budget and redirects keep existing bounded-download protections", async () => {
    const media = item(); let requested = 0;
    await assert.rejects(ordinary.download({...media, size_bytes: 50}, {maxBytes: 10,
        fetchImpl: () => { requested += 1; },
    }), error => error.errorCode === "BROWSER_BLOB_TOO_LARGE");
    assert.equal(requested, 0);
    for (const override of [{redirected: true}, {type: "opaque"}, {url: "https://different.example/x.mp4"}]) {
        await assert.rejects(ordinary.download(media, {
            fetchImpl: async () => response("x", media.url, "video/mp4", override),
            handoffBlob: () => assert.fail("no redirected handoff"),
        }));
    }
});

test("cover descriptor stays a URL rather than a download grant", async () => {
    const source = await picker.descriptor(root, selected(), "cover", {documentObject, windowObject: win,
        fetchImpl: async () => ({ok: true, json: async () => ({status: "success", url: "https://cdn.example.org/cover.jpg"})}),
    });
    assert.equal(source, "https://cdn.example.org/cover.jpg");
});

test("link resolution accepts HTTP/HTTPS and manifest filenames without download flags", async () => {
    for (const [protocol, extension] of [["http", "m3u8"], ["https", "mpd"]]) {
        const selection = {...selected(), formatId: "format-1", canDownload: false};
        const link = {selection_schema: picker.SCHEMA, candidate_id: selection.id, selected_format_id: "format-1",
            provider: "yt_dlp_generic", type: "video", filename: `source.${extension}`,
            url: `${protocol}://cdn.example.org/source.${extension}?signed=exact%2Fbytes`};
        let calls = 0;
        const result = await picker.resolveLink(root, selection, {documentObject, windowObject: win,
            fetchImpl: async (url, options) => {
                calls += 1;
                assert.equal(url, "http://localhost:8000/media/ytdlp-selected-format/");
                assert.deepEqual(JSON.parse(options.body), {candidate_id: selection.id, resource_token: selection.token, action: "link"});
                return {ok: true, json: async () => ({status: "success", item: link})};
            }});
        assert.equal(calls, 1);
        assert.equal(result.filename, `source.${extension}`);
        assert.equal(result.url, link.url);
        assert.equal(Object.isFrozen(result), true);
    }
});

test("link descriptors reject unsafe URL syntax, mismatched identities and malformed responses", async () => {
    for (const change of [{url: "javascript:alert(1)"}, {url: "data:text/html,evil"},
        {url: "file:///etc/passwd"}, {url: "https://user:pass@cdn.example.org/x"},
        {url: "https://cdn.example.org:8080/x"}, {url: "https://cdn.example.org/x\nheader"},
        {url: "https://cdn.example.org/\\evil"}, {url: "//cdn.example.org/x"},
        {candidate_id: "other"}, {selected_format_id: "other"}, {provider: "other"},
        {selection_schema: "other"}, {type: "audio"}, {filename: "bad\nout=evil"}, {filename: ""}]) {
        await assert.rejects(picker.resolveLink(root, {...selected(), formatId: "fmt720"}, {
            documentObject, windowObject: win, fetchImpl: async () => ({ok: true,
                json: async () => ({status: "success", item: item("fmt720", "video", change)})}),
        }));
    }
    for (const payload of [null, {}, {status: "success"}]) {
        await assert.rejects(picker.resolveLink(root, selected(), {documentObject, windowObject: win,
            fetchImpl: async () => ({ok: true, json: async () => payload})}));
    }
});

test("expired or denied link authorization reports the server reason without fallback", async () => {
    for (const message of ["授权已过期，请重新解析。", "当前访问受限。"] ) {
        let calls = 0;
        await assert.rejects(picker.resolveLink(root, selected(), {documentObject, windowObject: win,
            fetchImpl: async () => { calls += 1; return {ok: false, status: 403,
                json: async () => ({status: "error", message})}; }}), {message});
        assert.equal(calls, 1);
    }
});

test("selected WebM audio preserves bytes and suffix, with M4A/MP3 unchanged", async () => {
    for (const [extension, mime] of [["webm", "audio/webm"], ["weba", "audio/webm"],
        ["webm", "application/octet-stream"], ["m4a", "audio/mp4"], ["mp3", "audio/mpeg"]]) {
        const media = item("voice", "audio", {filename: `voice.${extension}`,
            url: `https://cdn.example.org/voice.${extension}`, audio_tracks: "present", video_tracks: "absent"});
        assert.equal(ordinary.isOrdinaryMediaItem(media), true);
        assert.equal(ordinary.contentTypeAllowsSelectedAudio(mime, media), true);
        let saved;
        await ordinary.download(media, {
            fetchImpl: async url => response("exact-original-audio-bytes", url, mime),
            handoffBlob: async (blob, filename) => { saved = {bytes: await blob.text(), filename}; },
        });
        assert.deepEqual(saved, {bytes: "exact-original-audio-bytes", filename: `voice.${extension}`});
    }
});

test("valid synthetic Opus WebM reaches the save handoff byte-for-byte without transcoding", async () => {
    const bytes = require('node:fs').readFileSync(require('node:path').join(__dirname, 'fixtures/opus-tone.webm'));
    const media = item('opus-tone', 'audio', {filename: 'opus-tone.webm',
        url: 'https://cdn.example.org/opus-tone.webm'});
    let saved;
    await ordinary.download(media, {
        fetchImpl: async url => response(bytes, url, 'audio/webm'),
        handoffBlob: async (blob, filename) => { saved = {bytes: Buffer.from(await blob.arrayBuffer()), filename}; },
    });
    assert.deepEqual(saved, {bytes, filename: 'opus-tone.webm'});
});

test("selected WebM audio rejects video, HTML, wrong containers and unknown tracks", async () => {
    const media = item("voice", "audio", {filename: "voice.webm", url: "https://cdn.example.org/voice.webm",
        audio_tracks: "present", video_tracks: "absent"});
    for (const mime of ["video/webm", "text/html", "audio/mpeg"]) {
        assert.equal(ordinary.contentTypeAllowsSelectedAudio(mime, media), false);
        await assert.rejects(ordinary.download(media, {
            fetchImpl: async url => response("wrong-container", url, mime),
            handoffBlob: () => assert.fail("invalid container must not be saved"),
        }), error => error.errorCode === "BROWSER_DOWNLOAD_UNSUPPORTED_CONTENT");
    }
    for (const tracks of [{audio_tracks: "unknown"}, {video_tracks: "unknown"}]) {
        const unknown = {...media, ...tracks};
        assert.equal(ordinary.isOrdinaryMediaItem(unknown), false);
        await assert.rejects(ordinary.download(unknown, {
            fetchImpl: () => assert.fail("unknown tracks must not be fetched"),
            handoffBlob: () => assert.fail("unknown tracks must not be saved"),
        }));
    }
});
