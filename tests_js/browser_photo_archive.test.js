"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const photo = require("../static/js/browser_photo_archive.js");


const SIGNATURES = {
    jpeg: Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, 0x01]),
    png: Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01]),
    gif: Uint8Array.from(Buffer.from("GIF89a-data")),
    webp: Uint8Array.from(Buffer.from("RIFF\u0004\u0000\u0000\u0000WEBPdata", "binary")),
    avif: Uint8Array.from([
        0x00, 0x00, 0x00, 0x18,
        0x66, 0x74, 0x79, 0x70,
        0x61, 0x76, 0x69, 0x66,
        0x00, 0x00, 0x00, 0x00,
        0x61, 0x76, 0x69, 0x66,
        0x6d, 0x69, 0x66, 0x31,
    ]),
    heic: Uint8Array.from([
        0x00, 0x00, 0x00, 0x18,
        0x66, 0x74, 0x79, 0x70,
        0x68, 0x65, 0x69, 0x63,
        0x00, 0x00, 0x00, 0x00,
        0x68, 0x65, 0x69, 0x63,
        0x6d, 0x69, 0x66, 0x31,
    ]),
};
const MP4_BYTES = Uint8Array.from([
    0x00, 0x00, 0x00, 0x18,
    0x66, 0x74, 0x79, 0x70,
    0x69, 0x73, 0x6f, 0x6d,
    0x00, 0x00, 0x00, 0x00,
    0x69, 0x73, 0x6f, 0x6d,
    0x69, 0x73, 0x6f, 0x32,
    0x6d, 0x64, 0x61, 0x74,
    0x01, 0x02, 0x03, 0x04,
]);


function fakeResponse(chunks, options) {
    const settings = options || {};
    let index = 0;
    let cancelled = false;
    const values = chunks.map((chunk) => chunk instanceof Uint8Array ? chunk : Uint8Array.from(chunk));
    return {
        ok: settings.ok !== false,
        status: settings.status || 200,
        type: settings.type || "cors",
        url: settings.url || "https://ci.xiaohongshu.com/resource",
        headers: {
            get(name) {
                const normalized = String(name).toLowerCase();
                if (normalized === "content-type") {
                    return settings.contentType || "application/octet-stream";
                }
                if (normalized === "content-length") {
                    return settings.contentLength === undefined
                        ? String(values.reduce((sum, value) => sum + value.byteLength, 0))
                        : String(settings.contentLength || "");
                }
                return "";
            },
        },
        body: {
            getReader() {
                return {
                    async read() {
                        if (cancelled || index >= values.length) {
                            return {done: true, value: undefined};
                        }
                        const value = values[index];
                        index += 1;
                        return {done: false, value};
                    },
                    async cancel() {
                        cancelled = true;
                    },
                };
            },
            async cancel() {
                cancelled = true;
            },
        },
    };
}


async function parseStoreZip(blob) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const decoder = new TextDecoder();
    const entries = [];
    let offset = 0;
    while (offset + 4 <= bytes.length && view.getUint32(offset, true) === 0x04034b50) {
        const flags = view.getUint16(offset + 6, true);
        const method = view.getUint16(offset + 8, true);
        const expectedCrc = view.getUint32(offset + 14, true);
        const size = view.getUint32(offset + 18, true);
        const nameLength = view.getUint16(offset + 26, true);
        const extraLength = view.getUint16(offset + 28, true);
        const nameStart = offset + 30;
        const dataStart = nameStart + nameLength + extraLength;
        const data = bytes.slice(dataStart, dataStart + size);
        entries.push({
            name: decoder.decode(bytes.slice(nameStart, nameStart + nameLength)),
            flags,
            method,
            expectedCrc,
            data,
        });
        offset = dataStart + size;
    }
    return entries;
}


test("image format detection uses magic for JPEG PNG GIF WebP AVIF and HEIC", () => {
    for (const [format, bytes] of Object.entries(SIGNATURES)) {
        const detected = photo.detectImageFormat(bytes, {contentType: "application/octet-stream"});
        assert.equal(detected.format, format);
        assert.equal(detected.confidence, "magic");
    }
});


test("magic overrides conflicting MIME while unknown and HTML remain failures", () => {
    const mismatch = photo.detectImageFormat(SIGNATURES.png, {contentType: "image/jpeg"});
    assert.equal(mismatch.extension, "png");
    assert.equal(mismatch.metadataMismatch, true);
    assert.equal(
        photo.detectImageFormat(Uint8Array.from([1, 2, 3]), {
            contentType: "application/octet-stream",
            url: "https://ci.xiaohongshu.com/resource",
        }).errorCode,
        "IMAGE_FORMAT_UNDETERMINED",
    );
    assert.equal(
        photo.detectImageFormat(Buffer.from("<!doctype html>"), {contentType: "image/jpeg"}).errorCode,
        "IMAGE_CONTENT_INVALID",
    );
});


test("specific trusted MIME and safe URL suffix are lower-confidence fallbacks", () => {
    assert.equal(
        photo.detectImageFormat(Uint8Array.from([1, 2]), {contentType: "image/webp"}).confidence,
        "content_type",
    );
    assert.equal(
        photo.detectImageFormat(Uint8Array.from([1, 2]), {
            contentType: "application/octet-stream",
            url: "https://site.test/image.avif?variant=controlled",
        }).extension,
        "avif",
    );
});


test("pure original images use Browser Photo while mixed or server-required selections do not", () => {
    const images = [
        {type: "image", url: "https://ci.xiaohongshu.com/one"},
        {type: "image", url: "https://ci.xiaohongshu.com/two"},
    ];
    assert.equal(photo.supportsSelection(images, "original"), true);
    assert.equal(photo.supportsSelection(images, "jpeg"), false);
    assert.equal(photo.supportsSelection([...images, {type: "video", url: "https://video.test/a"}]), false);
    assert.equal(photo.supportsSelection([{...images[0], requires_cookie: true}]), false);
    assert.equal(photo.supportsSelection([{...images[0], requires_merge: true}]), false);
});


test("Single pure-image routing has no silent server fallback when Browser Photo is unavailable", () => {
    const source = fs.readFileSync(
        path.join(__dirname, "../static/js/xhs_download.js"),
        "utf8",
    );
    assert.match(source, /selectedSupportsBrowserPhoto\(selected, payload\.save_format\)/);
    assert.match(source, /if \(!browserPhotoApi\)/);
    assert.match(source, /浏览器图片下载组件不可用，可明确选择/);
    assert.match(source, /forceServerPhotoArchive/);
    assert.doesNotMatch(source, /catch[\s\S]{0,240}(?:downloadWithTask|postDownloadPayload)/);
});


test("filename stems are safe and never invent an image extension", () => {
    assert.equal(photo.filenameStem("作品:标题?.jpg", "image"), "作品_标题_");
    assert.equal(photo.filenameStem("suffixless", "image"), "suffixless");
    assert.equal(photo.filenameWithSuffix("a".repeat(200), "_01.png").endsWith("_01.png"), true);
    assert.equal(photo.filenameWithSuffix("a".repeat(200), "_images.zip").endsWith("_images.zip"), true);
});


test("CRC32 and STORE ZIP preserve bytes, UTF-8 names, order and method", async () => {
    assert.equal(photo.crc32(Buffer.from("123456789")), 0xcbf43926);
    const zip = photo.createStoreZip([
        {filename: "苍蓝ya_01.png", chunks: [SIGNATURES.png]},
        {filename: "苍蓝ya_02.jpg", chunks: [SIGNATURES.jpeg]},
    ]);
    const entries = await parseStoreZip(zip);
    assert.deepEqual(entries.map((entry) => entry.name), ["苍蓝ya_01.png", "苍蓝ya_02.jpg"]);
    assert.deepEqual(entries.map((entry) => entry.method), [0, 0]);
    assert.equal(entries.every((entry) => (entry.flags & 0x0800) !== 0), true);
    assert.deepEqual(entries[0].data, SIGNATURES.png);
    assert.equal(entries[0].expectedCrc, photo.crc32(SIGNATURES.png));
});


test("single image download assigns the magic-derived extension and preserves exact bytes", async () => {
    let saved = null;
    const result = await photo.downloadImage({
        type: "image",
        url: "https://ci.xiaohongshu.com/suffixless",
        filenameStem: "作品_01",
    }, {
        fetchImpl: async () => fakeResponse([SIGNATURES.png]),
        async handoff(blob, filename) {
            saved = {blob, filename};
        },
    });
    assert.equal(result.filename, "作品_01.png");
    assert.equal(saved.filename, "作品_01.png");
    assert.deepEqual(new Uint8Array(await saved.blob.arrayBuffer()), SIGNATURES.png);
});


test("XHS and ParseHub images require magic instead of trusting MIME or suffix", async () => {
    const baseItem = {
        type: "image",
        url: "https://sns-img-hw.xhscdn.com/resource.jpg",
        source_platform: "xiaohongshu",
        provider: "parsehub_xhs_images",
    };
    await assert.rejects(
        () => photo.downloadImage(baseItem, {
            fetchImpl: async () => fakeResponse([Uint8Array.from([1, 2, 3])], {
                contentType: "image/jpeg",
            }),
            handoff: async () => assert.fail("handoff must not run"),
        }),
        (error) => error.errorCode === "IMAGE_FORMAT_UNDETERMINED",
    );

    let saved = null;
    const result = await photo.downloadImage(baseItem, {
        fetchImpl: async () => fakeResponse([SIGNATURES.heic], {
            contentType: "image/jpeg",
        }),
        async handoff(blob, filename) {
            saved = {blob, filename};
        },
    });
    assert.equal(result.format, "heic");
    assert.equal(saved.filename.endsWith(".heic"), true);
    assert.deepEqual(new Uint8Array(await saved.blob.arrayBuffer()), SIGNATURES.heic);
});


test("mixed Browser archive keeps input order and reports unknown bytes without inventing jpg", async () => {
    const bodies = [SIGNATURES.png, Uint8Array.from([1, 2, 3]), SIGNATURES.webp];
    let saved = null;
    const result = await photo.createArchive(bodies.map((_body, index) => ({
        type: "image",
        url: `https://ci.xiaohongshu.com/item-${index}`,
    })), {
        baseFilename: "图集",
        fetchImpl: async (url) => fakeResponse([bodies[Number(url.split("-").pop())]], {
            contentLength: "",
        }),
        async handoff(blob, filename) {
            saved = {blob, filename};
        },
    });
    const entries = await parseStoreZip(saved.blob);
    assert.equal(result.successCount, 2);
    assert.equal(result.failureCount, 1);
    assert.deepEqual(entries.map((entry) => entry.name), ["图集_01.png", "图集_03.webp", "failed.txt"]);
    assert.match(new TextDecoder().decode(entries[2].data), /图片 02: IMAGE_FORMAT_UNDETERMINED/);
    assert.equal(entries.some((entry) => entry.name.endsWith(".jpg")), false);
});


test("Browser archive limits active CDN fetches to configured concurrency", async () => {
    let active = 0;
    let maximum = 0;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const fetchImpl = async () => {
        active += 1;
        maximum = Math.max(maximum, active);
        await gate;
        active -= 1;
        return fakeResponse([SIGNATURES.jpeg]);
    };
    const promise = photo.createArchive(Array.from({length: 10}, (_value, index) => ({
        type: "image",
        url: `https://ci.xiaohongshu.com/${index}`,
    })), {
        fetchImpl,
        handoff: async () => {},
    });
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(maximum, 3);
    release();
    await promise;
});


test("pre-start CORS failure is terminal and never invokes a server or handoff fallback", async () => {
    let handoffCalls = 0;
    await assert.rejects(
        () => photo.createArchive([
            {type: "image", url: "https://ci.xiaohongshu.com/one"},
            {type: "image", url: "https://ci.xiaohongshu.com/two"},
        ], {
            fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
            handoff: async () => { handoffCalls += 1; },
        }),
        (error) => error.errorCode === "BROWSER_PHOTO_CORS_UNAVAILABLE",
    );
    assert.equal(handoffCalls, 0);
});


test("aggregate and per-item limits abort before a ZIP is handed off", async () => {
    await assert.rejects(
        () => photo.createArchive([
            {type: "image", url: "https://ci.xiaohongshu.com/one"},
            {type: "image", url: "https://ci.xiaohongshu.com/two"},
        ], {
            itemMaxBytes: 32,
            archiveMaxBytes: SIGNATURES.png.length + SIGNATURES.jpeg.length - 1,
            fetchImpl: async (url) => fakeResponse([
                url.endsWith("one") ? SIGNATURES.png : SIGNATURES.jpeg,
            ], {contentLength: ""}),
            handoff: async () => assert.fail("handoff must not run"),
        }),
        (error) => error.errorCode === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE",
    );
});


test("known aggregate Content-Length stops before image bodies are read", async () => {
    let readCalls = 0;
    let handoffCalls = 0;
    await assert.rejects(
        () => photo.createArchive([
            {type: "image", url: "https://ci.xiaohongshu.com/one"},
            {type: "image", url: "https://ci.xiaohongshu.com/two"},
        ], {
            archiveMaxBytes: 10,
            itemMaxBytes: 10,
            concurrency: 1,
            fetchImpl: async () => {
                const response = fakeResponse([SIGNATURES.png], {contentLength: 6});
                const originalReader = response.body.getReader;
                response.body.getReader = function () {
                    const reader = originalReader.call(response.body);
                    const originalRead = reader.read;
                    reader.read = async function () {
                        readCalls += 1;
                        return originalRead.call(reader);
                    };
                    return reader;
                };
                return response;
            },
            handoff: async () => { handoffCalls += 1; },
        }),
        (error) => error.errorCode === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE",
    );
    assert.equal(readCalls, 2);
    assert.equal(handoffCalls, 0);
});


test("external cancellation stops archive creation without a handoff", async () => {
    const controller = new AbortController();
    let handoffCalls = 0;
    const promise = photo.createArchive([
        {type: "image", url: "https://ci.xiaohongshu.com/one"},
        {type: "image", url: "https://ci.xiaohongshu.com/two"},
    ], {
        signal: controller.signal,
        fetchImpl: (_url, options) => new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
        handoff: async () => { handoffCalls += 1; },
    });
    controller.abort();
    await assert.rejects(promise, (error) => error.errorCode === "BROWSER_PHOTO_CANCELLED");
    assert.equal(handoffCalls, 0);
});


test("a stalled image read hits the bounded idle timeout without a handoff", async () => {
    let handoffCalls = 0;
    await assert.rejects(
        () => photo.downloadImage({
            type: "image",
            url: "https://ci.xiaohongshu.com/stalled",
        }, {
            idleTimeoutMs: 5,
            fetchImpl: async () => ({
                ok: true,
                status: 200,
                type: "cors",
                url: "https://ci.xiaohongshu.com/stalled",
                headers: {get() { return ""; }},
                body: {
                    getReader() {
                        return {
                            read() { return new Promise(() => {}); },
                            async cancel() {},
                        };
                    },
                },
            }),
            handoff: async () => { handoffCalls += 1; },
        }),
        (error) => error.errorCode === "BROWSER_PHOTO_FETCH_TIMEOUT",
    );
    assert.equal(handoffCalls, 0);
});


test("transfer meter exposes real bytes, rolling speed and ETA only with known totals", () => {
    let current = 0;
    const known = photo.createTransferMeter({totalItems: 2, now: () => current});
    known.setItemTotal(0, 1000);
    known.setItemTotal(1, 1000);
    known.addBytes(500);
    current = 2000;
    known.addBytes(500);
    const snapshot = known.snapshot("fetching");
    assert.equal(snapshot.receivedBytes, 1000);
    assert.equal(snapshot.totalBytes, 2000);
    assert.equal(snapshot.percent, 50);
    assert.equal(snapshot.bytesPerSecond, 500);
    assert.equal(snapshot.etaSeconds, 2);

    const unknown = photo.createTransferMeter({totalItems: 2});
    unknown.setItemTotal(0, 1000);
    unknown.addBytes(500);
    const indeterminate = unknown.snapshot("fetching");
    assert.equal(indeterminate.totalKnown, false);
    assert.equal(indeterminate.percent, null);
    assert.equal(indeterminate.etaSeconds, null);
});


test("LivePhoto pair ZIP preserves original HEIC and MP4 bytes in exact order", async () => {
    let saved = null;
    const progressStages = [];
    const pair = {
        archive_filename: "作品_01_livephoto.zip",
        still: {
            type: "image",
            url: "https://sns-img-hw.xhscdn.com/still",
            filename: "作品_01_still.heic",
            expected_size_bytes: SIGNATURES.heic.byteLength,
            source_platform: "xiaohongshu",
        },
        motion: {
            type: "video",
            url: "https://sns-video-v7.xhscdn.com/motion.mp4",
            filename: "作品_01_motion.mp4",
            format_class: "mp4",
            expected_size_bytes: MP4_BYTES.byteLength,
        },
    };
    const result = await photo.createLivePhotoPairArchive(pair, {
        fetchImpl: async (url) => url.includes("still")
            ? fakeResponse([SIGNATURES.heic], {contentType: "image/jpeg"})
            : fakeResponse([MP4_BYTES], {contentType: "video/mp4"}),
        onProgress(snapshot) { progressStages.push(snapshot.stage); },
        async handoff(blob, filename) { saved = {blob, filename}; },
    });
    const entries = await parseStoreZip(saved.blob);

    assert.equal(result.entryCount, 2);
    assert.equal(saved.filename, "作品_01_livephoto.zip");
    assert.deepEqual(
        entries.map((entry) => entry.name),
        ["作品_01_still.heic", "作品_01_motion.mp4"],
    );
    assert.deepEqual(entries[0].data, SIGNATURES.heic);
    assert.deepEqual(entries[1].data, MP4_BYTES);
    assert.equal(entries[0].expectedCrc, photo.crc32(SIGNATURES.heic));
    assert.equal(entries[1].expectedCrc, photo.crc32(MP4_BYTES));
    assert.equal(entries.every((entry) => entry.method === 0), true);
    assert.equal(entries.some((entry) => entry.name === "failed.txt"), false);
    assert.equal(progressStages.includes("fetching_motion"), true);
    assert.equal(progressStages.at(-1), "completed");
});


test("LivePhoto pair ZIP preserves PNG alpha bytes and magic-derived extension", async () => {
    let saved = null;
    await photo.createLivePhotoPairArchive({
        archive_filename: "透明素材_livephoto.zip",
        still: {
            type: "image",
            url: "https://ci.xiaohongshu.com/alpha",
            filename: "透明素材_still.jpg",
            source_platform: "xiaohongshu",
        },
        motion: {
            type: "video",
            url: "https://sns-bak-v3.xhscdn.com/motion.mp4",
            filename: "透明素材_motion.mp4",
            format_class: "mp4",
        },
    }, {
        fetchImpl: async (url) => url.includes("alpha")
            ? fakeResponse([SIGNATURES.png], {contentType: "image/jpeg"})
            : fakeResponse([MP4_BYTES], {contentType: "video/mp4"}),
        async handoff(blob) { saved = blob; },
    });
    const entries = await parseStoreZip(saved);
    assert.equal(entries[0].name, "透明素材_still.png");
    assert.deepEqual(entries[0].data, SIGNATURES.png);
});


test("LivePhoto pair rejects invalid motion or still without handing off a partial ZIP", async (t) => {
    for (const failure of ["still", "motion"]) {
        await t.test(failure, async () => {
            let handoffCalls = 0;
            await assert.rejects(
                () => photo.createLivePhotoPairArchive({
                    archive_filename: "fixture_livephoto.zip",
                    still: {
                        type: "image",
                        url: "https://ci.xiaohongshu.com/still",
                        filename: "fixture_still.jpg",
                        source_platform: "xiaohongshu",
                    },
                    motion: {
                        type: "video",
                        url: "https://sns-video-v7.xhscdn.com/motion.mp4",
                        filename: "fixture_motion.mp4",
                        format_class: "mp4",
                    },
                }, {
                    fetchImpl: async (url) => {
                        if (url.includes("still")) {
                            return fakeResponse([
                                failure === "still" ? Uint8Array.from([1, 2, 3]) : SIGNATURES.jpeg,
                            ]);
                        }
                        return fakeResponse([
                            failure === "motion" ? Uint8Array.from([1, 2, 3]) : MP4_BYTES,
                        ], {contentType: "video/mp4"});
                    },
                    async handoff() { handoffCalls += 1; },
                }),
                (error) => failure === "still"
                    ? error.errorCode === "BROWSER_LIVEPHOTO_STILL_FETCH_FAILED"
                    : error.errorCode === "BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED",
            );
            assert.equal(handoffCalls, 0);
        });
    }
});


test("LivePhoto pair enforces aggregate size and expected byte equality", async (t) => {
    const pair = {
        archive_filename: "fixture_livephoto.zip",
        still: {
            type: "image",
            url: "https://ci.xiaohongshu.com/still",
            filename: "fixture_still.jpg",
            source_platform: "xiaohongshu",
        },
        motion: {
            type: "video",
            url: "https://sns-video-v7.xhscdn.com/motion.mp4",
            filename: "fixture_motion.mp4",
            format_class: "mp4",
        },
    };
    const fetchImpl = async (url) => url.includes("still")
        ? fakeResponse([SIGNATURES.jpeg])
        : fakeResponse([MP4_BYTES], {contentType: "video/mp4"});

    await t.test("aggregate limit", async () => {
        await assert.rejects(
            () => photo.createLivePhotoPairArchive(pair, {
                archiveMaxBytes: SIGNATURES.jpeg.byteLength + MP4_BYTES.byteLength - 1,
                fetchImpl,
                handoff: async () => assert.fail("handoff must not run"),
            }),
            (error) => error.errorCode === "BROWSER_LIVEPHOTO_PAIR_TOO_LARGE",
        );
    });
    await t.test("expected motion size", async () => {
        await assert.rejects(
            () => photo.createLivePhotoPairArchive({
                ...pair,
                motion: {...pair.motion, expected_size_bytes: MP4_BYTES.byteLength + 1},
            }, {
                fetchImpl,
                handoff: async () => assert.fail("handoff must not run"),
            }),
            (error) => error.errorCode === "BROWSER_LIVEPHOTO_SIZE_MISMATCH",
        );
    });
});


test("LivePhoto pair cancellation aborts both stages without browser handoff", async () => {
    const controller = new AbortController();
    let handoffCalls = 0;
    const promise = photo.createLivePhotoPairArchive({
        archive_filename: "fixture_livephoto.zip",
        still: {
            type: "image",
            url: "https://ci.xiaohongshu.com/still",
            filename: "fixture_still.jpg",
            source_platform: "xiaohongshu",
        },
        motion: {
            type: "video",
            url: "https://sns-video-v7.xhscdn.com/motion.mp4",
            filename: "fixture_motion.mp4",
            format_class: "mp4",
        },
    }, {
        signal: controller.signal,
        fetchImpl: (_url, options) => new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
        async handoff() { handoffCalls += 1; },
    });
    controller.abort();
    await assert.rejects(promise, (error) => error.errorCode === "BROWSER_PHOTO_CANCELLED");
    assert.equal(handoffCalls, 0);
});
