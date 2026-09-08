(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesBrowserPhoto = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    const ITEM_MAX_BYTES = 50 * 1024 * 1024;
    const ARCHIVE_MAX_BYTES = 128 * 1024 * 1024;
    const LIVEPHOTO_MOTION_MAX_BYTES = 1024 * 1024 * 1024;
    const FETCH_CONCURRENCY = 3;
    const FETCH_IDLE_TIMEOUT_MS = 30000;
    const SPEED_WINDOW_MS = 2500;
    const MIN_ETA_SAMPLE_MS = 1500;
    const FORMAT_DETAILS = Object.freeze({
        jpeg: Object.freeze({format: "jpeg", extension: "jpg", mimeType: "image/jpeg"}),
        png: Object.freeze({format: "png", extension: "png", mimeType: "image/png"}),
        gif: Object.freeze({format: "gif", extension: "gif", mimeType: "image/gif"}),
        webp: Object.freeze({format: "webp", extension: "webp", mimeType: "image/webp"}),
        avif: Object.freeze({format: "avif", extension: "avif", mimeType: "image/avif"}),
        heic: Object.freeze({format: "heic", extension: "heic", mimeType: "image/heic"}),
    });
    const CONTENT_TYPE_FORMATS = Object.freeze({
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
        "image/heic": "heic",
        "image/heif": "heic",
    });
    const URL_EXTENSION_FORMATS = Object.freeze({
        jpeg: "jpeg",
        jpg: "jpeg",
        png: "png",
        gif: "gif",
        webp: "webp",
        avif: "avif",
        heic: "heic",
        heif: "heic",
    });
    const textEncoder = new TextEncoder();
    let crcTable = null;

    function positiveInteger(value) {
        const parsed = Number.parseInt(value || "0", 10);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }

    function normalizeContentType(value) {
        return String(value || "").split(";", 1)[0].trim().toLowerCase();
    }

    function isSafeXhsImageUrl(value) {
        try {
            const parsed = new URL(String(value || ""));
            const hostname = parsed.hostname.toLowerCase();
            const pathParts = parsed.pathname.split("/").filter(Boolean);
            const resourceKey = String(pathParts[pathParts.length - 1] || "").toLowerCase();
            const suffixAllowed = (
                hostname === "xhscdn.com"
                || hostname.endsWith(".xhscdn.com")
                || hostname === "xiaohongshu.com"
                || hostname.endsWith(".xiaohongshu.com")
            );
            const hasKnownResourcePath = pathParts.some(
                (part, index) => ["notes_pre_post", "spectrum"].includes(part)
                    && index + 1 < pathParts.length,
            );
            const candidateClassAllowed = (
                hostname === "ci.xiaohongshu.com"
                || hostname.startsWith("sns-webpic")
                || hostname.startsWith("sns-img")
                || hasKnownResourcePath
            );
            return Boolean(
                parsed.protocol === "https:"
                && !parsed.username
                && !parsed.password
                && !parsed.port
                && suffixAllowed
                && pathParts.length
                && !["", ".", "..", "null", "none", "undefined", "notes_pre_post", "spectrum"]
                    .includes(resourceKey)
                && !/(?:^|[/_.-])(?:avatar|favicon|logo|icon|sprite|emoji|qrcode)(?:[/_.-]|$)/i
                    .test(parsed.pathname)
                && !/\.(?:js|css|zip)$/i.test(parsed.pathname)
                && candidateClassAllowed
            );
        } catch (error) {
            return false;
        }
    }

    function bytesView(value) {
        if (value instanceof Uint8Array) {
            return value;
        }
        if (value instanceof ArrayBuffer) {
            return new Uint8Array(value);
        }
        if (ArrayBuffer.isView(value)) {
            return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
        }
        return new Uint8Array(value || []);
    }

    function asciiPrefix(value, limit) {
        const bytes = bytesView(value).subarray(0, limit || 512);
        let text = "";
        for (const byte of bytes) {
            text += String.fromCharCode(byte);
        }
        return text.replace(/^\s+/, "").toLowerCase();
    }

    function prefixLooksInvalid(value) {
        const prefix = asciiPrefix(value, 512);
        return [
            "<!doctype html",
            "<html",
            "<head",
            "<body",
            "<?xml",
            "<svg",
            "{",
            "[",
        ].some((marker) => prefix.startsWith(marker));
    }

    function detectImageMagic(value) {
        const bytes = bytesView(value);
        if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
            return "jpeg";
        }
        if (
            bytes.length >= 8
            && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
                .every((byte, index) => bytes[index] === byte)
        ) {
            return "png";
        }
        const six = asciiPrefix(bytes, 6);
        if (six === "gif87a" || six === "gif89a") {
            return "gif";
        }
        if (
            bytes.length >= 12
            && asciiPrefix(bytes.subarray(0, 4), 4) === "riff"
            && asciiPrefix(bytes.subarray(8, 12), 4) === "webp"
        ) {
            return "webp";
        }
        if (bytes.length >= 16 && asciiPrefix(bytes.subarray(4, 8), 4) === "ftyp") {
            const boxSize = (
                ((bytes[0] << 24) >>> 0)
                + (bytes[1] << 16)
                + (bytes[2] << 8)
                + bytes[3]
            ) >>> 0;
            const boxEnd = Math.min(bytes.length, boxSize);
            const heicBrands = new Set([
                "heic", "heix", "hevc", "hevx", "heim", "heis", "heif", "mif1", "msf1",
            ]);
            for (let index = 8; index + 4 <= boxEnd; index += index === 8 ? 8 : 4) {
                const brand = asciiPrefix(bytes.subarray(index, index + 4), 4);
                if (brand === "avif" || brand === "avis") {
                    return "avif";
                }
                if (heicBrands.has(brand)) {
                    return "heic";
                }
            }
        }
        return "";
    }

    function detectMp4Magic(value) {
        const bytes = bytesView(value);
        if (bytes.length < 12 || asciiPrefix(bytes.subarray(4, 8), 4) !== "ftyp") {
            return false;
        }
        const acceptedBrands = new Set([
            "avc1", "dash", "hev1", "hvc1", "iso2", "iso3", "iso4", "iso5",
            "iso6", "isom", "m4v ", "mp41", "mp42", "msnv",
        ]);
        const boxSize = (
            ((bytes[0] << 24) >>> 0)
            + (bytes[1] << 16)
            + (bytes[2] << 8)
            + bytes[3]
        ) >>> 0;
        const boxEnd = Math.min(bytes.length, Math.max(12, boxSize));
        for (let index = 8; index + 4 <= boxEnd; index += index === 8 ? 8 : 4) {
            if (acceptedBrands.has(asciiPrefix(bytes.subarray(index, index + 4), 4))) {
                return true;
            }
        }
        return acceptedBrands.has(asciiPrefix(bytes.subarray(8, 12), 4));
    }

    function formatFromUrl(url) {
        try {
            const path = new URL(String(url || ""), "https://local.invalid").pathname;
            const match = path.toLowerCase().match(/\.([a-z0-9]+)$/);
            return match ? URL_EXTENSION_FORMATS[match[1]] || "" : "";
        } catch (error) {
            return "";
        }
    }

    function formatResult(format, confidence, metadataMismatch) {
        return Object.freeze({
            ...FORMAT_DETAILS[format],
            confidence,
            metadataMismatch: Boolean(metadataMismatch),
            errorCode: "",
        });
    }

    function formatError(errorCode) {
        return Object.freeze({
            format: "",
            extension: "",
            mimeType: "",
            confidence: "",
            metadataMismatch: false,
            errorCode,
        });
    }

    function detectImageFormat(value, options) {
        const settings = options && typeof options === "object" ? options : {};
        const bytes = bytesView(value);
        const contentType = normalizeContentType(settings.contentType);
        const declaredFormat = CONTENT_TYPE_FORMATS[contentType] || "";
        const magicFormat = detectImageMagic(bytes);
        if (magicFormat) {
            return formatResult(
                magicFormat,
                "magic",
                Boolean(declaredFormat && declaredFormat !== magicFormat),
            );
        }
        if (prefixLooksInvalid(bytes)) {
            return formatError("IMAGE_CONTENT_INVALID");
        }
        if (declaredFormat) {
            return formatResult(declaredFormat, "content_type", false);
        }
        if (
            contentType.startsWith("text/")
            || ["application/json", "application/xml", "image/svg+xml"].includes(contentType)
        ) {
            return formatError("IMAGE_CONTENT_INVALID");
        }
        const urlFormat = formatFromUrl(settings.url);
        return urlFormat
            ? formatResult(urlFormat, "url_suffix", false)
            : formatError("IMAGE_FORMAT_UNDETERMINED");
    }

    function sanitizeFilename(value, fallback) {
        const sanitized = String(value || fallback || "image")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/[\u0000-\u001f\u007f]+/g, "")
            .replace(/\s+/g, " ")
            .trim()
            .replace(/^[. ]+|[. ]+$/g, "")
            .slice(0, 160);
        return sanitized || fallback || "image";
    }

    function filenameStem(value, fallback) {
        const sanitized = sanitizeFilename(value, fallback || "image");
        return sanitized.replace(/\.(?:jpe?g|png|gif|webp|avif|heic|heif)$/i, "") || "image";
    }

    function filenameWithSuffix(stem, suffix) {
        const safeSuffix = String(suffix || "");
        const maxStemLength = Math.max(1, 160 - safeSuffix.length);
        const safeStem = filenameStem(stem, "image").slice(0, maxStemLength);
        return sanitizeFilename(`${safeStem}${safeSuffix}`, `image${safeSuffix}`);
    }

    function sanitizeZipEntryPath(value, fallback) {
        const segments = String(value || "")
            .replace(/\\/g, "/")
            .split("/")
            .filter((segment) => segment && segment !== "." && segment !== "..")
            .map((segment) => sanitizeFilename(segment, "item"));
        return segments.join("/") || sanitizeFilename(fallback, "item");
    }

    function formatBytes(value) {
        const bytes = Math.max(0, Number(value || 0));
        if (bytes < 1024) {
            return `${Math.round(bytes)} B`;
        }
        const units = ["KiB", "MiB", "GiB"];
        let amount = bytes / 1024;
        let unit = units[0];
        for (let index = 1; index < units.length && amount >= 1024; index += 1) {
            amount /= 1024;
            unit = units[index];
        }
        const digits = amount >= 100 ? 0 : (amount >= 10 ? 1 : 2);
        return `${amount.toFixed(digits)} ${unit}`;
    }

    function formatRate(value) {
        return Number(value || 0) > 0 ? `${formatBytes(value)}/s` : "正在测速...";
    }

    function formatEta(value) {
        const seconds = Math.max(0, Math.ceil(Number(value || 0)));
        if (!seconds) {
            return "";
        }
        if (seconds < 60) {
            return `预计剩余 ${seconds} 秒`;
        }
        const minutes = Math.floor(seconds / 60);
        const remainder = seconds % 60;
        return remainder
            ? `预计剩余 ${minutes} 分 ${remainder} 秒`
            : `预计剩余 ${minutes} 分钟`;
    }

    function createTransferMeter(options) {
        const settings = options && typeof options === "object" ? options : {};
        const totalItems = positiveInteger(settings.totalItems);
        const now = typeof settings.now === "function"
            ? settings.now
            : (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
        const onProgress = typeof settings.onProgress === "function" ? settings.onProgress : (() => {});
        const itemTotals = new Array(totalItems).fill(0);
        let receivedBytes = 0;
        let completedItems = 0;
        let failedItems = 0;
        const startedAt = Number(now());
        const samples = [{at: startedAt, bytes: 0}];

        function setItemTotal(index, value) {
            itemTotals[index] = positiveInteger(value);
        }

        function addBytes(value) {
            receivedBytes += positiveInteger(value);
        }

        function completeItem(success) {
            completedItems += 1;
            if (!success) {
                failedItems += 1;
            }
        }

        function snapshot(stage) {
            const currentAt = Number(now());
            const previous = samples[samples.length - 1];
            if (previous && previous.at === currentAt) {
                previous.bytes = receivedBytes;
            } else {
                samples.push({at: currentAt, bytes: receivedBytes});
            }
            const cutoff = currentAt - SPEED_WINDOW_MS;
            while (samples.length > 2 && samples[1].at <= cutoff) {
                samples.shift();
            }
            const first = samples[0];
            const durationMs = Math.max(0, currentAt - first.at);
            const bytesPerSecond = durationMs > 0
                ? ((receivedBytes - first.bytes) * 1000) / durationMs
                : 0;
            const totalKnown = totalItems > 0 && itemTotals.every((value) => value > 0);
            const totalBytes = totalKnown
                ? itemTotals.reduce((sum, value) => sum + value, 0)
                : 0;
            const percent = totalKnown && totalBytes > 0
                ? Math.min(100, (receivedBytes / totalBytes) * 100)
                : null;
            const etaSeconds = (
                totalKnown
                && totalBytes > receivedBytes
                && currentAt - startedAt >= MIN_ETA_SAMPLE_MS
                && bytesPerSecond > 0
            ) ? (totalBytes - receivedBytes) / bytesPerSecond : null;
            return Object.freeze({
                stage: String(stage || "fetching"),
                totalItems,
                completedItems,
                failedItems,
                receivedBytes,
                totalBytes,
                totalKnown,
                percent,
                bytesPerSecond,
                etaSeconds,
                elapsedMs: Math.max(0, currentAt - startedAt),
            });
        }

        function emit(stage) {
            const value = snapshot(stage);
            onProgress(value);
            return value;
        }

        return Object.freeze({setItemTotal, addBytes, completeItem, snapshot, emit});
    }

    function createCrcTable() {
        const table = new Uint32Array(256);
        for (let index = 0; index < 256; index += 1) {
            let value = index;
            for (let bit = 0; bit < 8; bit += 1) {
                value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
            }
            table[index] = value >>> 0;
        }
        return table;
    }

    function crc32Update(state, value) {
        crcTable = crcTable || createCrcTable();
        let crc = state >>> 0;
        for (const byte of bytesView(value)) {
            crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8);
        }
        return crc >>> 0;
    }

    function crc32(value) {
        return (crc32Update(0xffffffff, value) ^ 0xffffffff) >>> 0;
    }

    function writeUint16(view, offset, value) {
        view.setUint16(offset, value & 0xffff, true);
    }

    function writeUint32(view, offset, value) {
        view.setUint32(offset, value >>> 0, true);
    }

    function zipHeader(length) {
        const bytes = new Uint8Array(length);
        return {bytes, view: new DataView(bytes.buffer)};
    }

    function validDate(value) {
        if (value === undefined || value === null || value === "") {
            return null;
        }
        const candidate = value instanceof Date
            ? new Date(value.getTime())
            : new Date(value);
        return Number.isFinite(candidate.getTime()) ? candidate : null;
    }

    function normalizeZipModifiedAt(value, fallback) {
        const date = validDate(value) || validDate(fallback) || new Date();
        const year = Math.min(2107, Math.max(1980, date.getFullYear()));
        const month = Math.min(12, Math.max(1, date.getMonth() + 1));
        const lastDay = new Date(year, month, 0).getDate();
        return Object.freeze({
            year,
            month,
            day: Math.min(lastDay, Math.max(1, date.getDate())),
            hour: Math.min(23, Math.max(0, date.getHours())),
            minute: Math.min(59, Math.max(0, date.getMinutes())),
            second: Math.min(59, Math.max(0, date.getSeconds())),
        });
    }

    function encodeDosDateTime(value, fallback) {
        const normalized = normalizeZipModifiedAt(value, fallback);
        return Object.freeze({
            time: (
                (normalized.hour << 11)
                | (normalized.minute << 5)
                | Math.floor(normalized.second / 2)
            ) & 0xffff,
            date: (
                ((normalized.year - 1980) << 9)
                | (normalized.month << 5)
                | normalized.day
            ) & 0xffff,
            normalized,
        });
    }

    function normalizeZipEntry(entry, archiveCreatedAt) {
        const chunks = (entry.chunks || [entry.bytes || new Uint8Array()]).map(bytesView);
        const size = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
        let resolvedCrc = entry.crc;
        if (resolvedCrc === undefined) {
            let crcState = 0xffffffff;
            for (const chunk of chunks) {
                crcState = crc32Update(crcState, chunk);
            }
            resolvedCrc = (crcState ^ 0xffffffff) >>> 0;
        }
        const modifiedAt = encodeDosDateTime(entry.modifiedAt, archiveCreatedAt);
        return {
            filename: sanitizeZipEntryPath(entry.filename, "image"),
            chunks,
            size,
            crc: resolvedCrc >>> 0,
            modificationTime: modifiedAt.time,
            modificationDate: modifiedAt.date,
        };
    }

    function createStoreZip(entries, options) {
        const settings = options && typeof options === "object" ? options : {};
        const archiveCreatedAt = validDate(settings.createdAt) || new Date();
        const normalized = (entries || []).map((entry) => (
            normalizeZipEntry(entry, archiveCreatedAt)
        ));
        const localParts = [];
        const centralParts = [];
        let offset = 0;
        for (const entry of normalized) {
            const name = textEncoder.encode(entry.filename);
            const local = zipHeader(30 + name.length);
            writeUint32(local.view, 0, 0x04034b50);
            writeUint16(local.view, 4, 20);
            writeUint16(local.view, 6, 0x0800);
            writeUint16(local.view, 8, 0);
            writeUint16(local.view, 10, entry.modificationTime);
            writeUint16(local.view, 12, entry.modificationDate);
            writeUint32(local.view, 14, entry.crc);
            writeUint32(local.view, 18, entry.size);
            writeUint32(local.view, 22, entry.size);
            writeUint16(local.view, 26, name.length);
            writeUint16(local.view, 28, 0);
            local.bytes.set(name, 30);
            localParts.push(local.bytes, ...entry.chunks);

            const central = zipHeader(46 + name.length);
            writeUint32(central.view, 0, 0x02014b50);
            writeUint16(central.view, 4, 20);
            writeUint16(central.view, 6, 20);
            writeUint16(central.view, 8, 0x0800);
            writeUint16(central.view, 10, 0);
            writeUint16(central.view, 12, entry.modificationTime);
            writeUint16(central.view, 14, entry.modificationDate);
            writeUint32(central.view, 16, entry.crc);
            writeUint32(central.view, 20, entry.size);
            writeUint32(central.view, 24, entry.size);
            writeUint16(central.view, 28, name.length);
            writeUint16(central.view, 30, 0);
            writeUint16(central.view, 32, 0);
            writeUint16(central.view, 34, 0);
            writeUint16(central.view, 36, 0);
            writeUint32(central.view, 38, 0);
            writeUint32(central.view, 42, offset);
            central.bytes.set(name, 46);
            centralParts.push(central.bytes);
            offset += local.bytes.byteLength + entry.size;
        }
        const centralSize = centralParts.reduce((sum, part) => sum + part.byteLength, 0);
        const end = zipHeader(22);
        writeUint32(end.view, 0, 0x06054b50);
        writeUint16(end.view, 4, 0);
        writeUint16(end.view, 6, 0);
        writeUint16(end.view, 8, normalized.length);
        writeUint16(end.view, 10, normalized.length);
        writeUint32(end.view, 12, centralSize);
        writeUint32(end.view, 16, offset);
        writeUint16(end.view, 20, 0);
        return new Blob([...localParts, ...centralParts, end.bytes], {type: "application/zip"});
    }

    function photoError(errorCode, message) {
        const error = new Error(message || "图片下载失败，请稍后重试。");
        error.errorCode = errorCode;
        return error;
    }

    function safeErrorCode(error) {
        const code = String(error && error.errorCode || "BROWSER_PHOTO_FETCH_FAILED");
        return /^[A-Z0-9_]{1,64}$/.test(code) ? code : "BROWSER_PHOTO_FETCH_FAILED";
    }

    function safeErrorMessage(error, options) {
        const code = safeErrorCode(error);
        const messages = {
            UPSTREAM_PHOTO_FETCH_FAILED: "解析已成功，但浏览器无法读取图片，本次未保存。可能与网络或来源访问限制有关，具体原因无法确认。不会转为服务器下载。",
            UPSTREAM_PHOTO_INVALID_ITEM: "图片下载授权已失效，请重新解析。",
            BROWSER_PHOTO_CANCELLED: "图片下载已取消。",
            BROWSER_PHOTO_CORS_UNAVAILABLE: "浏览器无法直接读取这些图片。",
            BROWSER_PHOTO_FETCH_TIMEOUT: "图片资源响应超时，请重试下载。",
            BROWSER_PHOTO_HTTP_FAILED: "图片来源返回 HTTP 错误，浏览器未能获取图片。",
            BROWSER_PHOTO_ITEM_TOO_LARGE: "单张图片超过浏览器安全下载上限。",
            BROWSER_PHOTO_ARCHIVE_TOO_LARGE: "图片总量超过浏览器安全打包上限。",
            BROWSER_PHOTO_SERVER_REQUIRED: "当前图片需要服务器打包。",
            IMAGE_CONTENT_INVALID: "上游返回的内容不是有效图片。",
            IMAGE_FORMAT_UNDETERMINED: "无法确认图片真实格式，已停止保存。",
            BROWSER_PHOTO_NO_SUCCESS: "没有图片下载成功。",
            BROWSER_LIVEPHOTO_MOTION_INVALID: "实况视频获取失败。",
            BROWSER_LIVEPHOTO_STILL_FETCH_FAILED: "原图获取失败。",
            BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED: "实况视频获取失败。",
            BROWSER_LIVEPHOTO_SIZE_MISMATCH: "实况素材内容不完整，请重新解析。",
            BROWSER_LIVEPHOTO_PAIR_TOO_LARGE: "该实况素材过大，暂无法在浏览器中打包。",
            BROWSER_LIVEPHOTO_PAIR_INVALID: "实况素材信息无效，请重新解析。",
        };
        if (code === "BROWSER_PHOTO_NO_SUCCESS" && options && options.upstream) {
            const firstCode = String(error && error.firstFailureCode || "");
            const reason = firstCode !== code && Object.hasOwn(messages, firstCode) ? messages[firstCode] : "";
            return "解析已成功，但浏览器未能获取任何图片，本次未保存 ZIP。"
                + (reason ? ` 首个失败提示：${reason}` : "可能与网络或来源访问限制有关，具体原因无法确认。")
                + " 不会转为服务器下载。";
        }
        return messages[code] || "图片下载失败，请稍后重试。";
    }

    function abortError(signal) {
        if (signal && signal.aborted) {
            return photoError("BROWSER_PHOTO_CANCELLED", "图片下载已取消。");
        }
        return null;
    }

    function responseHeader(response, name) {
        return response && response.headers && typeof response.headers.get === "function"
            ? response.headers.get(name) || ""
            : "";
    }

    function withIdleTimeout(promise, timeoutMs, onTimeout) {
        const boundedTimeoutMs = positiveInteger(timeoutMs) || FETCH_IDLE_TIMEOUT_MS;
        let timeoutId = null;
        const timeoutPromise = new Promise((_resolve, reject) => {
            timeoutId = setTimeout(() => {
                if (typeof onTimeout === "function") {
                    onTimeout();
                }
                reject(photoError("BROWSER_PHOTO_FETCH_TIMEOUT"));
            }, boundedTimeoutMs);
        });
        return Promise.race([Promise.resolve(promise), timeoutPromise])
            .finally(() => clearTimeout(timeoutId));
    }

    async function readImageResponse(response, item, context) {
        if (isUpstreamPhoto(item) && (response.redirected
            || (response.url && response.url !== item.url))) {
            if (response.body && typeof response.body.cancel === "function") {
                await response.body.cancel().catch(() => {});
            }
            throw photoError("IMAGE_CONTENT_INVALID");
        }
        if (
            item.source_platform === "xiaohongshu"
            && !isSafeXhsImageUrl(response.url || item.url)
        ) {
            if (response.body && typeof response.body.cancel === "function") {
                await response.body.cancel().catch(() => {});
            }
            throw photoError("IMAGE_CONTENT_INVALID");
        }
        const contentLength = positiveInteger(responseHeader(response, "Content-Length"));
        if (contentLength > context.itemMaxBytes) {
            if (response.body && typeof response.body.cancel === "function") {
                await response.body.cancel().catch(() => {});
            }
            throw photoError("BROWSER_PHOTO_ITEM_TOO_LARGE");
        }
        if (contentLength) {
            context.aggregate.declaredBytes += contentLength;
            if (context.aggregate.declaredBytes > context.archiveMaxBytes) {
                if (response.body && typeof response.body.cancel === "function") {
                    await response.body.cancel().catch(() => {});
                }
                context.controller.abort();
                throw photoError("BROWSER_PHOTO_ARCHIVE_TOO_LARGE");
            }
        }
        if (!context.preserveItemTotal) {
            context.meter.setItemTotal(context.index, contentLength);
        }
        if (!response.body || typeof response.body.getReader !== "function") {
            throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
        }
        const reader = response.body.getReader();
        const chunks = [];
        const prefixParts = [];
        let prefixLength = 0;
        let itemBytes = 0;
        let crcState = 0xffffffff;
        while (true) {
            const cancelled = abortError(context.signal);
            if (cancelled) {
                try {
                    await reader.cancel();
                } catch (error) {
                    // The explicit cancellation remains authoritative.
                }
                throw cancelled;
            }
            let read;
            try {
                read = await withIdleTimeout(
                    reader.read(),
                    context.idleTimeoutMs,
                    () => {
                        context.itemController.abort();
                        Promise.resolve(reader.cancel()).catch(() => {});
                    },
                );
            } catch (error) {
                if (safeErrorCode(error) === "BROWSER_PHOTO_FETCH_TIMEOUT") {
                    throw error;
                }
                const cancelled = abortError(context.signal);
                if (cancelled) {
                    throw cancelled;
                }
                throw photoError("BROWSER_PHOTO_FETCH_FAILED");
            }
            if (read.done) {
                break;
            }
            const chunk = bytesView(read.value);
            if (!chunk.byteLength) {
                continue;
            }
            itemBytes += chunk.byteLength;
            context.aggregate.receivedBytes += chunk.byteLength;
            if (itemBytes > context.itemMaxBytes) {
                context.controller.abort();
                throw photoError("BROWSER_PHOTO_ITEM_TOO_LARGE");
            }
            if (context.aggregate.receivedBytes > context.archiveMaxBytes) {
                context.controller.abort();
                throw photoError("BROWSER_PHOTO_ARCHIVE_TOO_LARGE");
            }
            chunks.push(chunk);
            crcState = crc32Update(crcState, chunk);
            if (prefixLength < 4096) {
                const prefixChunk = chunk.subarray(0, Math.min(chunk.length, 4096 - prefixLength));
                prefixParts.push(prefixChunk);
                prefixLength += prefixChunk.length;
            }
            context.meter.addBytes(chunk.byteLength);
            context.meter.emit("fetching");
        }
        if (!itemBytes) {
            throw photoError("IMAGE_CONTENT_INVALID");
        }
        const prefix = new Uint8Array(prefixLength);
        let prefixOffset = 0;
        for (const part of prefixParts) {
            prefix.set(part, prefixOffset);
            prefixOffset += part.length;
        }
        const detected = detectImageFormat(prefix, {
            contentType: responseHeader(response, "Content-Type"),
            url: response.url || item.url,
        });
        const requiresMagic = Boolean(
            item.require_magic === true
            || isUpstreamPhoto(item)
            || item.requireMagic === true
            || item.source_platform === "xiaohongshu"
            || item.provider === "parsehub_xhs_images"
        );
        if (detected.errorCode || (requiresMagic && detected.confidence !== "magic")) {
            throw photoError(detected.errorCode || "IMAGE_FORMAT_UNDETERMINED");
        }
        return {
            chunks,
            size: itemBytes,
            crc: (crcState ^ 0xffffffff) >>> 0,
            detected,
        };
    }

    async function fetchImage(item, context) {
        const fetchImpl = context.fetchImpl;
        const itemController = new AbortController();
        const releaseSignal = bridgeAbortSignal(context.signal, itemController);
        let response;
        try {
            response = await withIdleTimeout(
                fetchImpl(item.url, {
                    method: "GET",
                    cache: "no-store",
                    credentials: "omit",
                    referrerPolicy: "no-referrer",
                    signal: itemController.signal,
                    ...(isUpstreamPhoto(item) ? {redirect: "error", mode: "cors"} : {}),
                }),
                context.idleTimeoutMs,
                () => itemController.abort(),
            );
        } catch (error) {
            const cancelled = abortError(context.signal);
            if (cancelled) {
                throw cancelled;
            }
            if (safeErrorCode(error) === "BROWSER_PHOTO_FETCH_TIMEOUT") {
                throw error;
            }
            throw photoError(isUpstreamPhoto(item) ? "UPSTREAM_PHOTO_FETCH_FAILED" : "BROWSER_PHOTO_CORS_UNAVAILABLE");
        } finally {
            if (!response) {
                releaseSignal();
            }
        }
        try {
            if (!response || response.type === "opaque" || !response.body) {
                throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
            }
            if (!response.ok) {
                throw photoError("BROWSER_PHOTO_HTTP_FAILED", `图片资源返回 HTTP ${response.status || 0}。`);
            }
            return await readImageResponse(response, item, {
                ...context,
                itemController,
                signal: itemController.signal,
            });
        } finally {
            releaseSignal();
        }
    }

    async function readMotionResponse(response, item, context) {
        const contentLength = positiveInteger(responseHeader(response, "Content-Length"));
        if (contentLength > context.itemMaxBytes) {
            if (response.body && typeof response.body.cancel === "function") {
                await response.body.cancel().catch(() => {});
            }
            throw photoError("BROWSER_LIVEPHOTO_PAIR_TOO_LARGE");
        }
        if (contentLength) {
            context.aggregate.declaredBytes += contentLength;
            if (context.aggregate.declaredBytes > context.archiveMaxBytes) {
                if (response.body && typeof response.body.cancel === "function") {
                    await response.body.cancel().catch(() => {});
                }
                context.controller.abort();
                throw photoError("BROWSER_LIVEPHOTO_PAIR_TOO_LARGE");
            }
        }
        if (!context.preserveItemTotal) {
            context.meter.setItemTotal(context.index, contentLength);
        }
        if (!response.body || typeof response.body.getReader !== "function") {
            throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
        }
        const reader = response.body.getReader();
        const chunks = [];
        const prefixParts = [];
        let prefixLength = 0;
        let itemBytes = 0;
        let crcState = 0xffffffff;
        while (true) {
            const cancelled = abortError(context.signal);
            if (cancelled) {
                await Promise.resolve(reader.cancel()).catch(() => {});
                throw cancelled;
            }
            let read;
            try {
                read = await withIdleTimeout(
                    reader.read(),
                    context.idleTimeoutMs,
                    () => {
                        context.itemController.abort();
                        Promise.resolve(reader.cancel()).catch(() => {});
                    },
                );
            } catch (error) {
                if (safeErrorCode(error) === "BROWSER_PHOTO_FETCH_TIMEOUT") {
                    throw error;
                }
                const cancelled = abortError(context.signal);
                if (cancelled) {
                    throw cancelled;
                }
                throw photoError("BROWSER_PHOTO_FETCH_FAILED");
            }
            if (read.done) {
                break;
            }
            const chunk = bytesView(read.value);
            if (!chunk.byteLength) {
                continue;
            }
            itemBytes += chunk.byteLength;
            context.aggregate.receivedBytes += chunk.byteLength;
            if (
                itemBytes > context.itemMaxBytes
                || context.aggregate.receivedBytes > context.archiveMaxBytes
            ) {
                context.controller.abort();
                throw photoError("BROWSER_LIVEPHOTO_PAIR_TOO_LARGE");
            }
            chunks.push(chunk);
            crcState = crc32Update(crcState, chunk);
            if (prefixLength < 4096) {
                const part = chunk.subarray(0, Math.min(chunk.length, 4096 - prefixLength));
                prefixParts.push(part);
                prefixLength += part.length;
            }
            context.meter.addBytes(chunk.byteLength);
            context.meter.emit("fetching_motion");
        }
        if (!itemBytes) {
            throw photoError("BROWSER_LIVEPHOTO_MOTION_INVALID");
        }
        const prefix = new Uint8Array(prefixLength);
        let offset = 0;
        for (const part of prefixParts) {
            prefix.set(part, offset);
            offset += part.length;
        }
        if (!detectMp4Magic(prefix)) {
            throw photoError("BROWSER_LIVEPHOTO_MOTION_INVALID");
        }
        const expectedSize = positiveInteger(item.expected_size_bytes || item.expectedSizeBytes);
        if (expectedSize && expectedSize !== itemBytes) {
            throw photoError("BROWSER_LIVEPHOTO_SIZE_MISMATCH");
        }
        return {
            chunks,
            size: itemBytes,
            crc: (crcState ^ 0xffffffff) >>> 0,
            format: "mp4",
            mimeType: "video/mp4",
        };
    }

    async function fetchMotion(item, context) {
        const itemController = new AbortController();
        const releaseSignal = bridgeAbortSignal(context.signal, itemController);
        let response;
        try {
            response = await withIdleTimeout(
                context.fetchImpl(item.url, {
                    method: "GET",
                    cache: "no-store",
                    credentials: "omit",
                    referrerPolicy: "no-referrer",
                    signal: itemController.signal,
                }),
                context.idleTimeoutMs,
                () => itemController.abort(),
            );
        } catch (error) {
            const cancelled = abortError(context.signal);
            if (cancelled) {
                throw cancelled;
            }
            if (safeErrorCode(error) === "BROWSER_PHOTO_FETCH_TIMEOUT") {
                throw error;
            }
            throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
        } finally {
            if (!response) {
                releaseSignal();
            }
        }
        try {
            if (!response || response.type === "opaque" || !response.body) {
                throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
            }
            if (!response.ok) {
                throw photoError("BROWSER_PHOTO_HTTP_FAILED");
            }
            return await readMotionResponse(response, item, {
                ...context,
                itemController,
                signal: itemController.signal,
            });
        } finally {
            releaseSignal();
        }
    }

    function defaultHandoff(blob, filename) {
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = filename;
        link.rel = "noopener noreferrer";
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    }

    function runtimeFetch(settings) {
        if (typeof settings.fetchImpl === "function") {
            return settings.fetchImpl;
        }
        if (typeof fetch === "function") {
            return fetch.bind(globalThis);
        }
        throw photoError("BROWSER_PHOTO_CORS_UNAVAILABLE");
    }

    function bridgeAbortSignal(externalSignal, controller) {
        if (!externalSignal) {
            return () => {};
        }
        if (externalSignal.aborted) {
            controller.abort();
            return () => {};
        }
        const abort = () => controller.abort();
        externalSignal.addEventListener("abort", abort, {once: true});
        return () => externalSignal.removeEventListener("abort", abort);
    }

    function normalizedPhotoItem(item, index, baseStem) {
        return {
            ...item,
            type: "image",
            url: String(item && item.url || "").trim(),
            filenameStem: filenameStem(
                item && (item.filenameStem || item.filename_stem || item.filename),
                `${baseStem}_${String(index + 1).padStart(2, "0")}`,
            ),
        };
    }

    function isUpstreamPhoto(item) {
        return Boolean(item && String(item.asset_id || "").startsWith("upstream:"));
    }

    function assertPhotoItem(item) {
        if (isUpstreamPhoto(item)) {
            let url;
            try { url = new URL(item.url); } catch (error) { /* Rejected below. */ }
            if (item.browser_upstream_bound !== true || item.source_platform !== "upstream"
                || !["yt_dlp_generic", "gallery_dl_generic", "parsehub_generic"].includes(item.provider)
                || !url || url.protocol !== "https:" || url.username || url.password
                || url.hash || (url.port && url.port !== "443")) {
                throw photoError("UPSTREAM_PHOTO_INVALID_ITEM");
            }
        }
        if (!item || item.type !== "image" || !item.url) {
            throw photoError("BROWSER_PHOTO_INVALID_ITEM", "图片下载信息无效，请重新解析。");
        }
        if (item.requires_cookie === true || item.requiresCookie === true) {
            throw photoError("BROWSER_PHOTO_SERVER_REQUIRED", "当前图片需要服务器生成文件。");
        }
        if (item.requires_merge === true || item.requiresMerge === true) {
            throw photoError("BROWSER_PHOTO_SERVER_REQUIRED", "当前图片需要服务器生成文件。");
        }
        if (
            item.source_platform === "xiaohongshu"
            && !isSafeXhsImageUrl(item.url)
        ) {
            throw photoError("BROWSER_PHOTO_INVALID_ITEM", "图片下载信息无效，请重新解析。");
        }
    }

    function validatedUpstreamImageUrl(item, route) {
        const bound = route && route.route_type === "direct" && Array.isArray(route.items)
            && route.items.length === 1 ? route.items[0] : null;
        if (!isUpstreamPhoto(item) || !bound || bound.asset_id !== item.asset_id
            || bound.url !== item.url || bound.type !== item.type || bound.type !== "image") {
            throw photoError("UPSTREAM_PHOTO_INVALID_ITEM");
        }
        assertPhotoItem(bound);
        return bound.url;
    }

    function supportsSelection(items, saveFormat) {
        const sourceItems = Array.isArray(items) ? items : [];
        return Boolean(
            sourceItems.length
            && String(saveFormat || "original") === "original"
            && sourceItems.every((item) => {
                const logicalType = logicalItemType(item);
                if (!item || item.requires_cookie === true || item.requiresCookie === true) {
                    return false;
                }
                if (item.requires_merge === true || item.requiresMerge === true) {
                    return false;
                }
                if (logicalType === "livephoto") {
                    return Boolean(
                        String(item.asset_id || item.assetId || "").trim()
                        && String(item.pair_token || item.pairToken || "").trim()
                        && String(item.descriptor_url || item.descriptorUrl || "").trim()
                    );
                }
                return logicalType === "image" && Boolean(String(item.url || "").trim());
            })
        );
    }

    async function downloadImage(item, options) {
        const settings = options && typeof options === "object" ? options : {};
        const baseStem = filenameStem(settings.filenameStem || item.filenameStem || item.filename, "image");
        const normalized = normalizedPhotoItem(item, 0, baseStem);
        assertPhotoItem(normalized);
        const controller = new AbortController();
        const releaseSignal = bridgeAbortSignal(settings.signal, controller);
        const meter = createTransferMeter({
            totalItems: 1,
            onProgress: settings.onProgress,
            now: settings.now,
        });
        const aggregate = {receivedBytes: 0, declaredBytes: 0};
        meter.emit("connecting");
        try {
            const result = await fetchImage(normalized, {
                index: 0,
                fetchImpl: runtimeFetch(settings),
                itemMaxBytes: positiveInteger(settings.itemMaxBytes) || ITEM_MAX_BYTES,
                archiveMaxBytes: positiveInteger(settings.itemMaxBytes) || ITEM_MAX_BYTES,
                aggregate,
                meter,
                controller,
                signal: controller.signal,
                idleTimeoutMs: positiveInteger(settings.idleTimeoutMs) || FETCH_IDLE_TIMEOUT_MS,
            });
            meter.completeItem(true);
            meter.emit("saving");
            const filename = filenameWithSuffix(baseStem, `.${result.detected.extension}`);
            const blob = new Blob(result.chunks, {type: result.detected.mimeType});
            const handoff = typeof settings.handoff === "function" ? settings.handoff : defaultHandoff;
            await handoff(blob, filename);
            meter.emit("completed");
            return Object.freeze({
                filename,
                format: result.detected.format,
                mimeType: result.detected.mimeType,
                bytes: result.size,
                metadataMismatch: result.detected.metadataMismatch,
                transport: "browser_photo",
            });
        } finally {
            releaseSignal();
        }
    }

    function failureText(failures) {
        return (failures || [])
            .map((failure) => `图片 ${String(failure.index + 1).padStart(2, "0")}: ${failure.errorCode}`)
            .join("\n");
    }

    async function createArchive(items, options) {
        const settings = options && typeof options === "object" ? options : {};
        const archiveCreatedAt = validDate(settings.createdAt) || new Date();
        const sourceItems = Array.isArray(items) ? items : [];
        if (sourceItems.length < 2) {
            throw photoError("BROWSER_PHOTO_INVALID_ITEM", "至少选择两张图片才能打包。");
        }
        const baseStem = filenameStem(settings.baseFilename, "images");
        const normalizedItems = sourceItems.map((item, index) => normalizedPhotoItem(item, index, baseStem));
        normalizedItems.forEach(assertPhotoItem);
        const controller = new AbortController();
        const releaseSignal = bridgeAbortSignal(settings.signal, controller);
        const meter = createTransferMeter({
            totalItems: normalizedItems.length,
            onProgress: settings.onProgress,
            now: settings.now,
        });
        const aggregate = {receivedBytes: 0, declaredBytes: 0};
        const results = new Array(normalizedItems.length);
        const failures = [];
        const itemMaxBytes = positiveInteger(settings.itemMaxBytes) || ITEM_MAX_BYTES;
        const archiveMaxBytes = positiveInteger(settings.archiveMaxBytes) || ARCHIVE_MAX_BYTES;
        const concurrency = Math.max(1, Math.min(
            positiveInteger(settings.concurrency) || FETCH_CONCURRENCY,
            normalizedItems.length,
        ));
        const fetchImpl = runtimeFetch(settings);
        let nextIndex = 0;
        let fatalError = null;
        meter.emit("connecting");

        async function worker() {
            while (!controller.signal.aborted) {
                const index = nextIndex;
                nextIndex += 1;
                if (index >= normalizedItems.length) {
                    return;
                }
                try {
                    const result = await fetchImage(normalizedItems[index], {
                        index,
                        fetchImpl,
                        itemMaxBytes,
                        archiveMaxBytes,
                        aggregate,
                        meter,
                        controller,
                        signal: controller.signal,
                        idleTimeoutMs: positiveInteger(settings.idleTimeoutMs) || FETCH_IDLE_TIMEOUT_MS,
                    });
                    results[index] = result;
                    meter.completeItem(true);
                    meter.emit("fetching");
                } catch (error) {
                    const code = safeErrorCode(error);
                    if (code === "BROWSER_PHOTO_CANCELLED") {
                        if (!fatalError) {
                            fatalError = error;
                        }
                        return;
                    }
                    if (
                        code === "BROWSER_PHOTO_CORS_UNAVAILABLE"
                        && aggregate.receivedBytes === 0
                    ) {
                        fatalError = error;
                        controller.abort();
                        return;
                    }
                    if (
                        code === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE"
                        || code === "BROWSER_PHOTO_ITEM_TOO_LARGE"
                    ) {
                        fatalError = error;
                        controller.abort();
                        return;
                    }
                    failures.push({index, errorCode: code});
                    meter.completeItem(false);
                    meter.emit("fetching");
                }
            }
        }

        try {
            await Promise.all(Array.from({length: concurrency}, () => worker()));
            if (settings.signal && settings.signal.aborted) {
                throw photoError("BROWSER_PHOTO_CANCELLED");
            }
            if (fatalError) {
                throw fatalError;
            }
            const entries = [];
            results.forEach((result, index) => {
                if (!result) {
                    return;
                }
                entries.push({
                    filename: filenameWithSuffix(
                        baseStem,
                        `_${String(index + 1).padStart(2, "0")}.${result.detected.extension}`,
                    ),
                    chunks: result.chunks,
                    crc: result.crc,
                });
            });
            if (!entries.length) {
                const error = photoError("BROWSER_PHOTO_NO_SUCCESS");
                error.firstFailureCode = failures.sort((left, right) => left.index - right.index)[0]?.errorCode || "";
                throw error;
            }
            failures.sort((left, right) => left.index - right.index);
            if (failures.length) {
                entries.push({
                    filename: "failed.txt",
                    chunks: [textEncoder.encode(failureText(failures))],
                });
            }
            meter.emit("assembling");
            const blob = createStoreZip(entries, {createdAt: archiveCreatedAt});
            if (blob.size > archiveMaxBytes + (entries.length * 512)) {
                throw photoError("BROWSER_PHOTO_ARCHIVE_TOO_LARGE");
            }
            const filename = filenameWithSuffix(baseStem, "_images.zip");
            meter.emit("saving");
            const handoff = typeof settings.handoff === "function" ? settings.handoff : defaultHandoff;
            await handoff(blob, filename);
            meter.emit("completed");
            return Object.freeze({
                filename,
                blob,
                successCount: entries.length - (failures.length ? 1 : 0),
                failureCount: failures.length,
                receivedBytes: aggregate.receivedBytes,
                transport: "browser_photo_archive",
                zipImplementation: "STORE_ONLY_INTERNAL",
            });
        } finally {
            releaseSignal();
        }
    }

    async function createLivePhotoPairArchive(pair, options) {
        const settings = options && typeof options === "object" ? options : {};
        const archiveCreatedAt = validDate(settings.createdAt) || new Date();
        if (!pair || !pair.still || !pair.motion) {
            throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
        }
        const still = normalizedPhotoItem({
            ...pair.still,
            require_magic: true,
        }, 0, "livephoto_still");
        const motion = {
            ...pair.motion,
            type: "video",
            url: String(pair.motion.url || "").trim(),
            filename: sanitizeFilename(pair.motion.filename, "livephoto_motion.mp4"),
        };
        assertPhotoItem(still);
        if (
            !motion.url
            || String(motion.format_class || "").toLowerCase() !== "mp4"
            || !/\.mp4$/i.test(motion.filename)
        ) {
            throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
        }
        const controller = new AbortController();
        const releaseSignal = bridgeAbortSignal(settings.signal, controller);
        const meter = createTransferMeter({
            totalItems: 2,
            onProgress: settings.onProgress,
            now: settings.now,
        });
        const aggregate = {receivedBytes: 0, declaredBytes: 0};
        const archiveMaxBytes = positiveInteger(settings.archiveMaxBytes) || ARCHIVE_MAX_BYTES;
        const fetchImpl = runtimeFetch(settings);
        meter.emit("connecting");
        try {
            let stillResult;
            try {
                stillResult = await fetchImage(still, {
                    index: 0,
                    fetchImpl,
                    itemMaxBytes: positiveInteger(settings.stillMaxBytes) || ITEM_MAX_BYTES,
                    archiveMaxBytes,
                    aggregate,
                    meter,
                    controller,
                    signal: controller.signal,
                    idleTimeoutMs: positiveInteger(settings.idleTimeoutMs) || FETCH_IDLE_TIMEOUT_MS,
                });
            } catch (error) {
                const code = safeErrorCode(error);
                if (
                    code === "BROWSER_PHOTO_CANCELLED"
                    || code === "BROWSER_PHOTO_ITEM_TOO_LARGE"
                    || code === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE"
                    || code === "BROWSER_LIVEPHOTO_PAIR_TOO_LARGE"
                ) {
                    throw error;
                }
                throw photoError("BROWSER_LIVEPHOTO_STILL_FETCH_FAILED");
            }
            const expectedStillSize = positiveInteger(
                pair.still.expected_size_bytes || pair.still.expectedSizeBytes,
            );
            if (expectedStillSize && expectedStillSize !== stillResult.size) {
                throw photoError("BROWSER_LIVEPHOTO_SIZE_MISMATCH");
            }
            meter.completeItem(true);
            meter.emit("fetching_motion");
            let motionResult;
            try {
                motionResult = await fetchMotion(motion, {
                    index: 1,
                    fetchImpl,
                    itemMaxBytes: positiveInteger(settings.motionMaxBytes) || LIVEPHOTO_MOTION_MAX_BYTES,
                    archiveMaxBytes,
                    aggregate,
                    meter,
                    controller,
                    signal: controller.signal,
                    idleTimeoutMs: positiveInteger(settings.idleTimeoutMs) || FETCH_IDLE_TIMEOUT_MS,
                });
            } catch (error) {
                const code = safeErrorCode(error);
                if (
                    code === "BROWSER_PHOTO_CANCELLED"
                    || code === "BROWSER_LIVEPHOTO_PAIR_TOO_LARGE"
                    || code === "BROWSER_LIVEPHOTO_SIZE_MISMATCH"
                ) {
                    throw error;
                }
                throw photoError("BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED");
            }
            meter.completeItem(true);
            meter.emit("assembling");
            const stillFilename = filenameWithSuffix(
                filenameStem(pair.still.filename, "livephoto_still"),
                `.${stillResult.detected.extension}`,
            );
            const motionFilename = filenameWithSuffix(
                filenameStem(
                    String(motion.filename || "").replace(/\.mp4$/i, ""),
                    "livephoto_motion",
                ),
                ".mp4",
            );
            const entries = [
                {filename: stillFilename, chunks: stillResult.chunks, crc: stillResult.crc},
                {filename: motionFilename, chunks: motionResult.chunks, crc: motionResult.crc},
            ];
            const blob = createStoreZip(entries, {createdAt: archiveCreatedAt});
            if (blob.size > archiveMaxBytes + 1024) {
                throw photoError("BROWSER_LIVEPHOTO_PAIR_TOO_LARGE");
            }
            const archiveFilename = sanitizeFilename(
                pair.archive_filename || pair.archiveFilename,
                "livephoto.zip",
            );
            if (!/\.zip$/i.test(archiveFilename)) {
                throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
            }
            meter.emit("saving");
            const handoff = typeof settings.handoff === "function" ? settings.handoff : defaultHandoff;
            await handoff(blob, archiveFilename);
            meter.emit("completed");
            return Object.freeze({
                filename: archiveFilename,
                blob,
                entryCount: 2,
                entries: Object.freeze([stillFilename, motionFilename]),
                receivedBytes: aggregate.receivedBytes,
                stillBytes: stillResult.size,
                motionBytes: motionResult.size,
                transport: "browser_livephoto_pair_archive",
                zipImplementation: "STORE_ONLY_INTERNAL",
            });
        } finally {
            releaseSignal();
        }
    }

    function logicalItemType(item) {
        return String(item && (item.logical_type || item.logicalType || item.type) || "");
    }

    function logicalDisplayOrdinal(item, index) {
        return positiveInteger(item && (item.display_ordinal || item.displayOrdinal)) || index + 1;
    }

    function normalizeLivePhotoPair(pair) {
        if (!pair || !pair.still || !pair.motion) {
            throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
        }
        const still = normalizedPhotoItem({
            ...pair.still,
            require_magic: true,
        }, 0, "livephoto_still");
        const motion = {
            ...pair.motion,
            type: "video",
            url: String(pair.motion.url || "").trim(),
            filename: sanitizeFilename(pair.motion.filename, "livephoto_motion.mp4"),
        };
        assertPhotoItem(still);
        if (
            !motion.url
            || String(motion.format_class || "").toLowerCase() !== "mp4"
            || !/\.mp4$/i.test(motion.filename)
        ) {
            throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
        }
        return {still, motion};
    }

    async function createLogicalSelectionArchive(items, options) {
        const settings = options && typeof options === "object" ? options : {};
        const sourceItems = Array.isArray(items) ? items : [];
        if (sourceItems.length < 2) {
            throw photoError("BROWSER_PHOTO_INVALID_ITEM", "至少选择两个素材才能打包。");
        }
        const resolveLivePhoto = settings.resolveLivePhoto;
        const archiveCreatedAt = validDate(settings.createdAt) || new Date();
        const baseStem = filenameStem(settings.baseFilename, "selected");
        const controller = new AbortController();
        const releaseSignal = bridgeAbortSignal(settings.signal, controller);
        const meter = createTransferMeter({
            totalItems: sourceItems.length,
            onProgress: settings.onProgress,
            now: settings.now,
        });
        const aggregate = {receivedBytes: 0, declaredBytes: 0};
        const archiveMaxBytes = positiveInteger(settings.archiveMaxBytes) || ARCHIVE_MAX_BYTES;
        const imageMaxBytes = positiveInteger(settings.itemMaxBytes) || ITEM_MAX_BYTES;
        const motionMaxBytes = positiveInteger(settings.motionMaxBytes) || LIVEPHOTO_MOTION_MAX_BYTES;
        const idleTimeoutMs = positiveInteger(settings.idleTimeoutMs) || FETCH_IDLE_TIMEOUT_MS;
        const fetchImpl = runtimeFetch(settings);
        const entries = [];
        meter.emit("connecting");
        try {
            for (let index = 0; index < sourceItems.length; index += 1) {
                const cancelled = abortError(controller.signal);
                if (cancelled) {
                    throw cancelled;
                }
                const item = sourceItems[index];
                const logicalType = logicalItemType(item);
                const ordinal = String(logicalDisplayOrdinal(item, index)).padStart(2, "0");
                if (logicalType === "image") {
                    const normalized = normalizedPhotoItem(item, index, baseStem);
                    assertPhotoItem(normalized);
                    const result = await fetchImage(normalized, {
                        index,
                        fetchImpl,
                        itemMaxBytes: imageMaxBytes,
                        archiveMaxBytes,
                        aggregate,
                        meter,
                        controller,
                        signal: controller.signal,
                        idleTimeoutMs,
                    });
                    entries.push({
                        filename: `${ordinal}.${result.detected.extension}`,
                        chunks: result.chunks,
                        crc: result.crc,
                    });
                    meter.completeItem(true);
                    meter.emit("fetching");
                    continue;
                }
                if (logicalType !== "livephoto" || typeof resolveLivePhoto !== "function") {
                    throw photoError("BROWSER_LIVEPHOTO_PAIR_INVALID");
                }

                meter.emit("resolving_livephoto");
                const pair = await resolveLivePhoto(item, {signal: controller.signal});
                const {still, motion} = normalizeLivePhotoPair(pair);
                const expectedStillSize = positiveInteger(
                    pair.still.expected_size_bytes || pair.still.expectedSizeBytes,
                );
                const expectedMotionSize = positiveInteger(
                    pair.motion.expected_size_bytes || pair.motion.expectedSizeBytes,
                );
                if (expectedStillSize && expectedMotionSize) {
                    meter.setItemTotal(index, expectedStillSize + expectedMotionSize);
                }
                meter.emit("fetching_still");
                let stillResult;
                try {
                    stillResult = await fetchImage(still, {
                        index,
                        fetchImpl,
                        itemMaxBytes: imageMaxBytes,
                        archiveMaxBytes,
                        aggregate,
                        meter,
                        controller,
                        signal: controller.signal,
                        idleTimeoutMs,
                        preserveItemTotal: true,
                    });
                } catch (error) {
                    const code = safeErrorCode(error);
                    if (
                        code === "BROWSER_PHOTO_CANCELLED"
                        || code === "BROWSER_PHOTO_ITEM_TOO_LARGE"
                        || code === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE"
                    ) {
                        throw error;
                    }
                    throw photoError("BROWSER_LIVEPHOTO_STILL_FETCH_FAILED");
                }
                if (expectedStillSize && expectedStillSize !== stillResult.size) {
                    throw photoError("BROWSER_LIVEPHOTO_SIZE_MISMATCH");
                }

                meter.emit("fetching_motion");
                let motionResult;
                try {
                    motionResult = await fetchMotion(motion, {
                        index,
                        fetchImpl,
                        itemMaxBytes: motionMaxBytes,
                        archiveMaxBytes,
                        aggregate,
                        meter,
                        controller,
                        signal: controller.signal,
                        idleTimeoutMs,
                        preserveItemTotal: true,
                    });
                } catch (error) {
                    const code = safeErrorCode(error);
                    if (
                        code === "BROWSER_PHOTO_CANCELLED"
                        || code === "BROWSER_LIVEPHOTO_PAIR_TOO_LARGE"
                        || code === "BROWSER_LIVEPHOTO_SIZE_MISMATCH"
                    ) {
                        throw error;
                    }
                    throw photoError("BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED");
                }

                const folder = `${ordinal}_livephoto`;
                entries.push(
                    {
                        filename: `${folder}/${ordinal}_still.${stillResult.detected.extension}`,
                        chunks: stillResult.chunks,
                        crc: stillResult.crc,
                    },
                    {
                        filename: `${folder}/${ordinal}_motion.mp4`,
                        chunks: motionResult.chunks,
                        crc: motionResult.crc,
                    },
                );
                meter.completeItem(true);
                meter.emit("fetching");
            }

            meter.emit("assembling");
            const blob = createStoreZip(entries, {createdAt: archiveCreatedAt});
            if (blob.size > archiveMaxBytes + (entries.length * 512)) {
                throw photoError("BROWSER_PHOTO_ARCHIVE_TOO_LARGE");
            }
            const filename = filenameWithSuffix(baseStem, "_selected.zip");
            meter.emit("saving");
            const handoff = typeof settings.handoff === "function" ? settings.handoff : defaultHandoff;
            await handoff(blob, filename);
            meter.emit("completed");
            return Object.freeze({
                filename,
                blob,
                logicalItemCount: sourceItems.length,
                entryCount: entries.length,
                entries: Object.freeze(entries.map((entry) => entry.filename)),
                receivedBytes: aggregate.receivedBytes,
                transport: "browser_logical_media_archive",
                zipImplementation: "STORE_ONLY_INTERNAL",
            });
        } finally {
            releaseSignal();
        }
    }

    return Object.freeze({
        ITEM_MAX_BYTES,
        ARCHIVE_MAX_BYTES,
        LIVEPHOTO_MOTION_MAX_BYTES,
        FETCH_CONCURRENCY,
        FETCH_IDLE_TIMEOUT_MS,
        SPEED_WINDOW_MS,
        MIN_ETA_SAMPLE_MS,
        detectImageFormat,
        isSafeXhsImageUrl,
        detectImageMagic,
        detectMp4Magic,
        sanitizeFilename,
        sanitizeZipEntryPath,
        filenameStem,
        filenameWithSuffix,
        formatBytes,
        formatRate,
        formatEta,
        createTransferMeter,
        crc32,
        normalizeZipModifiedAt,
        encodeDosDateTime,
        createStoreZip,
        supportsSelection,
        validatedUpstreamImageUrl,
        downloadImage,
        createArchive,
        createLivePhotoPairArchive,
        createLogicalSelectionArchive,
        safeErrorCode,
        safeErrorMessage,
    });
}));
