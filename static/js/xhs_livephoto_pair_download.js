(function (root, factory) {
    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesXhsLivePhotoPair = api;
        if (root.document) {
            api.ensureController(root.document);
        }
    }
}(typeof window !== "undefined" ? window : globalThis, function (root) {
    "use strict";

    const SAFE_ERROR_TEXT = Object.freeze({
        XHS_LIVEPHOTO_PAIR_EXPIRED: "资源已失效，请重新解析。",
        XHS_LIVEPHOTO_PAIR_USE_LIMIT: "资源已失效，请重新解析。",
        BROWSER_PHOTO_CORS_UNAVAILABLE: "当前实况节点暂不可下载。",
        BROWSER_PHOTO_FETCH_TIMEOUT: "实况素材响应超时，请重试。",
        BROWSER_LIVEPHOTO_MOTION_INVALID: "实况视频获取失败。",
        BROWSER_LIVEPHOTO_MOTION_FETCH_FAILED: "实况视频获取失败。",
        BROWSER_LIVEPHOTO_STILL_FETCH_FAILED: "原图获取失败。",
        BROWSER_LIVEPHOTO_SIZE_MISMATCH: "实况素材内容不完整，请重新解析。",
        BROWSER_LIVEPHOTO_PAIR_TOO_LARGE: "该实况素材过大，暂无法在浏览器中打包。",
        BROWSER_PHOTO_ITEM_TOO_LARGE: "该实况素材过大，暂无法在浏览器中打包。",
        BROWSER_PHOTO_ARCHIVE_TOO_LARGE: "该实况素材过大，暂无法在浏览器中打包。",
    });
    let browserController = null;
    let browserDocument = null;
    const buttonDisabledBeforeBusy = new WeakMap();

    function csrfToken(documentObject) {
        const input = documentObject && documentObject.querySelector
            ? documentObject.querySelector("[name=csrfmiddlewaretoken]")
            : null;
        if (input && input.value) {
            return input.value;
        }
        const cookie = String(documentObject && documentObject.cookie || "")
            .split(";")
            .map((part) => part.trim())
            .find((part) => part.startsWith("csrftoken="));
        return cookie ? decodeURIComponent(cookie.slice("csrftoken=".length)) : "";
    }

    async function requestPairDescriptor(pairToken, endpoint, options) {
        const settings = options && typeof options === "object" ? options : {};
        const fetchImpl = settings.fetchImpl || (root && root.fetch && root.fetch.bind(root));
        if (!fetchImpl || !pairToken || !endpoint) {
            const error = new Error("实况资源信息无效，请重新解析。");
            error.errorCode = "XHS_LIVEPHOTO_PAIR_INVALID";
            throw error;
        }
        let response;
        try {
            response = await fetchImpl(endpoint, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": settings.csrfToken || csrfToken(root && root.document),
                },
                body: JSON.stringify({pair_token: pairToken}),
                signal: settings.signal,
            });
        } catch (cause) {
            const error = new Error("实况资源服务暂时不可用，请稍后重试。");
            error.errorCode = cause && cause.name === "AbortError"
                ? "BROWSER_PHOTO_CANCELLED"
                : "XHS_LIVEPHOTO_PAIR_NETWORK_ERROR";
            throw error;
        }
        let payload;
        try {
            payload = await response.json();
        } catch (cause) {
            payload = null;
        }
        if (!response.ok || !payload || payload.ok !== true) {
            const error = new Error("实况资源获取失败，请重新解析。");
            error.errorCode = String(payload && payload.error_code || "XHS_LIVEPHOTO_PAIR_INVALID");
            throw error;
        }
        if (
            !payload.still
            || payload.still.media_type !== "image"
            || !payload.motion
            || payload.motion.media_type !== "video"
            || payload.motion.format_class !== "mp4"
            || !payload.archive_filename
        ) {
            const error = new Error("实况资源信息无效，请重新解析。");
            error.errorCode = "XHS_LIVEPHOTO_PAIR_INVALID";
            throw error;
        }
        return payload;
    }

    function safeErrorMessage(error, photoApi) {
        const code = String(error && error.errorCode || "XHS_LIVEPHOTO_PAIR_INVALID");
        if (SAFE_ERROR_TEXT[code]) {
            return SAFE_ERROR_TEXT[code];
        }
        if (photoApi && typeof photoApi.safeErrorMessage === "function") {
            return photoApi.safeErrorMessage(error);
        }
        return "实况素材下载失败，请稍后重试。";
    }

    function setCardBusy(card, busy) {
        if (!card || !card.querySelectorAll) {
            return;
        }
        for (const button of card.querySelectorAll("[data-livephoto-download-original], [data-livephoto-download-pair]")) {
            if (busy) {
                if (!buttonDisabledBeforeBusy.has(button)) {
                    buttonDisabledBeforeBusy.set(button, Boolean(button.disabled));
                }
                button.disabled = true;
                continue;
            }
            if (buttonDisabledBeforeBusy.has(button)) {
                button.disabled = buttonDisabledBeforeBusy.get(button);
                buttonDisabledBeforeBusy.delete(button);
            }
        }
    }

    function setStatus(card, text, isError) {
        const status = card && card.querySelector
            ? card.querySelector("[data-livephoto-download-status]")
            : null;
        if (!status) {
            return;
        }
        status.textContent = String(text || "");
        status.classList.toggle("text-danger", Boolean(isError));
        status.classList.toggle("text-muted", !isError);
    }

    function pairProgressText(snapshot) {
        const stage = String(snapshot && snapshot.stage || "");
        if (stage === "fetching_motion") {
            return "正在下载实况视频";
        }
        if (stage === "assembling") {
            return "正在生成压缩包";
        }
        if (stage === "saving" || stage === "completed") {
            return "已交给浏览器保存";
        }
        return "正在下载原图";
    }

    function createController(documentObject, options) {
        if (!documentObject || typeof documentObject.addEventListener !== "function") {
            return null;
        }
        const settings = options && typeof options === "object" ? options : {};
        const photoApi = settings.photoApi || (root && root.AnimeImagesBrowserPhoto);
        const loadDescriptor = settings.requestPairDescriptor || requestPairDescriptor;
        const activeOperations = new Set();

        async function downloadOriginal(button, card, signal) {
            if (!photoApi || typeof photoApi.downloadImage !== "function") {
                throw Object.assign(new Error(), {errorCode: "BROWSER_PHOTO_CORS_UNAVAILABLE"});
            }
            setStatus(card, "正在下载原图", false);
            try {
                await photoApi.downloadImage({
                    type: "image",
                    url: String(button.dataset.originalUrl || ""),
                    filename: String(button.dataset.originalFilename || "image"),
                    filenameStem: String(button.dataset.originalFilename || "image"),
                    require_magic: true,
                    source_platform: "xiaohongshu",
                }, {
                    signal,
                    onProgress(snapshot) {
                        setStatus(
                            card,
                            snapshot && snapshot.stage === "completed"
                                ? "已交给浏览器保存"
                                : "正在下载原图",
                            false,
                        );
                    },
                });
            } catch (error) {
                const code = String(error && error.errorCode || "");
                if (
                    code === "BROWSER_PHOTO_CANCELLED"
                    || code === "BROWSER_PHOTO_ITEM_TOO_LARGE"
                    || code === "BROWSER_PHOTO_ARCHIVE_TOO_LARGE"
                ) {
                    throw error;
                }
                throw Object.assign(new Error(), {
                    errorCode: "BROWSER_LIVEPHOTO_STILL_FETCH_FAILED",
                });
            }
        }

        async function downloadPair(button, card, signal) {
            if (!photoApi || typeof photoApi.createLivePhotoPairArchive !== "function") {
                throw Object.assign(new Error(), {errorCode: "BROWSER_PHOTO_CORS_UNAVAILABLE"});
            }
            setStatus(card, "正在准备实况素材", false);
            const descriptor = await loadDescriptor(
                String(button.dataset.livephotoPairToken || ""),
                String(button.dataset.livephotoDescriptorUrl || ""),
                {
                    fetchImpl: settings.fetchImpl,
                    csrfToken: settings.csrfToken || csrfToken(documentObject),
                    signal,
                },
            );
            await photoApi.createLivePhotoPairArchive(descriptor, {
                signal,
                onProgress(snapshot) {
                    setStatus(card, pairProgressText(snapshot), false);
                },
            });
        }

        async function handleClick(event) {
            const target = event.target && event.target.closest
                ? event.target.closest("[data-livephoto-download-original], [data-livephoto-download-pair]")
                : null;
            if (!target) {
                return;
            }
            const card = target.closest("[data-livephoto-item]");
            if (!card || target.disabled) {
                return;
            }
            event.preventDefault();
            const operationController = new AbortController();
            activeOperations.add(operationController);
            setCardBusy(card, true);
            try {
                if (target.hasAttribute("data-livephoto-download-pair")) {
                    await downloadPair(target, card, operationController.signal);
                } else {
                    await downloadOriginal(target, card, operationController.signal);
                }
            } catch (error) {
                setStatus(card, safeErrorMessage(error, photoApi), true);
            } finally {
                activeOperations.delete(operationController);
                setCardBusy(card, false);
            }
        }

        documentObject.addEventListener("click", handleClick);
        return Object.freeze({
            cancelAll() {
                for (const controller of activeOperations) {
                    controller.abort();
                }
            },
            destroy() {
                for (const controller of activeOperations) {
                    controller.abort();
                }
                documentObject.removeEventListener("click", handleClick);
            },
            downloadOriginal,
            downloadPair,
        });
    }

    function ensureController(documentObject) {
        if (browserController && browserDocument === documentObject) {
            return browserController;
        }
        if (browserController) {
            browserController.destroy();
        }
        browserDocument = documentObject;
        browserController = createController(documentObject);
        return browserController;
    }

    return Object.freeze({
        cancelAll() {
            if (browserController && typeof browserController.cancelAll === "function") {
                browserController.cancelAll();
            }
        },
        createController,
        ensureController,
        pairProgressText,
        requestPairDescriptor,
        safeErrorMessage,
        setCardBusy,
    });
}));
