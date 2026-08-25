(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesXhsLivePhotoStaffPreview = api;
        if (root.document) {
            api.ensureController(root.document);
        }
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    const SAFE_MOTION_HOST = /^(?:sns-video-v[0-9]+|sns-bak-v[0-9]+)\.xhscdn\.com$/;
    const STARTUP_TIMEOUT_MS = 8000;
    const SAFE_STATE_VALUES = Object.freeze([
        "idle",
        "loading",
        "metadata",
        "canplay",
        "playing",
        "ended",
        "stopped",
        "startup_timeout",
        "network_error",
        "decode_error",
        "source_not_supported",
        "policy_rejected",
        "aborted",
    ]);
    const SAFE_STATE_SET = new Set(SAFE_STATE_VALUES);
    const SAFE_ERROR_TEXT = "实况预览加载失败，可重新解析后再试。";
    const SAFE_ERROR_TEXT_BY_STATE = Object.freeze({
        startup_timeout: "实况媒体加载超时，可重新解析后再试。",
        network_error: "实况媒体请求失败，可重新解析后再试。",
        decode_error: "当前浏览器无法解码该实况媒体。",
        source_not_supported: "当前实况媒体格式暂不受此浏览器支持。",
        policy_rejected: "当前实况媒体未通过安全校验。",
        aborted: "实况播放已中止，可重新尝试。",
    });
    const STATUS_TEXT_BY_STATE = Object.freeze({
        idle: "实况照片",
        loading: "正在加载",
        metadata: "已读取媒体",
        canplay: "可以播放",
        playing: "正在播放",
        ended: "播放结束",
        stopped: "已停止",
        startup_timeout: "加载超时",
        network_error: "媒体请求失败",
        decode_error: "无法解码",
        source_not_supported: "格式不受支持",
        policy_rejected: "安全校验未通过",
        aborted: "播放已中止",
    });

    let browserController = null;
    let browserDocument = null;

    function isSafeMotionUrl(value) {
        try {
            const parsed = new URL(String(value || ""));
            return (
                parsed.protocol === "https:"
                && !parsed.username
                && !parsed.password
                && !parsed.port
                && SAFE_MOTION_HOST.test(parsed.hostname.toLowerCase())
                && parsed.pathname !== "/"
            );
        } catch (error) {
            return false;
        }
    }

    function classifyMediaError(errorCode, playError) {
        if (Number(errorCode) === 1) {
            return "aborted";
        }
        if (Number(errorCode) === 2) {
            return "network_error";
        }
        if (Number(errorCode) === 3) {
            return "decode_error";
        }
        if (Number(errorCode) === 4) {
            return "source_not_supported";
        }
        const errorName = String(playError && playError.name || "");
        if (errorName === "AbortError") {
            return "aborted";
        }
        if (errorName === "NotSupportedError") {
            return "source_not_supported";
        }
        return "network_error";
    }

    function disableLooping(root) {
        if (!root || typeof root.querySelectorAll !== "function") {
            return;
        }
        for (const video of root.querySelectorAll("[data-livephoto-motion]")) {
            video.loop = false;
        }
    }

    function createController(documentObject, options) {
        if (!documentObject || typeof documentObject.addEventListener !== "function") {
            return null;
        }
        const settings = options || {};
        const setTimer = settings.setTimeout || setTimeout;
        const clearTimer = settings.clearTimeout || clearTimeout;
        const startupTimeoutMs = Number.isFinite(settings.startupTimeoutMs)
            ? Math.max(1, settings.startupTimeoutMs)
            : STARTUP_TIMEOUT_MS;
        let activeAttempt = null;
        let attemptSequence = 0;

        disableLooping(documentObject);

        function cardParts(card) {
            return {
                still: card && card.querySelector("[data-livephoto-still]"),
                video: card && card.querySelector("[data-livephoto-motion]"),
                button: card && card.querySelector("[data-livephoto-toggle]"),
                label: card && card.querySelector("[data-livephoto-toggle-label]"),
                playIcon: card && card.querySelector("[data-livephoto-play-icon]"),
                spinner: card && card.querySelector("[data-livephoto-spinner]"),
                status: card && card.querySelector("[data-livephoto-status]"),
                error: card && card.querySelector("[data-livephoto-error]"),
                errorText: card && card.querySelector("[data-livephoto-error-text]"),
            };
        }

        function setCardState(card, state) {
            const safeState = SAFE_STATE_SET.has(state) ? state : "network_error";
            const parts = cardParts(card);
            if (card.dataset) {
                card.dataset.livephotoState = safeState;
            } else if (typeof card.setAttribute === "function") {
                card.setAttribute("data-livephoto-state", safeState);
            }
            if (parts.status) {
                parts.status.textContent = STATUS_TEXT_BY_STATE[safeState];
            }
            if (parts.spinner) {
                parts.spinner.hidden = safeState !== "loading";
            }
            if (parts.playIcon) {
                parts.playIcon.hidden = safeState === "loading";
            }
            return safeState;
        }

        function hideError(parts) {
            if (parts.error) {
                parts.error.hidden = true;
            }
        }

        function showError(parts, state) {
            if (parts.errorText) {
                parts.errorText.textContent = SAFE_ERROR_TEXT_BY_STATE[state] || SAFE_ERROR_TEXT;
            }
            if (parts.error) {
                parts.error.hidden = false;
            }
        }

        function unloadVideo(video) {
            if (!video) {
                return;
            }
            video.loop = false;
            video.pause();
            video.removeAttribute("src");
            if (typeof video.load === "function") {
                video.load();
            }
            video.hidden = true;
        }

        function cleanupAttempt(attempt) {
            if (!attempt) {
                return;
            }
            if (attempt.timeoutId !== null) {
                clearTimer(attempt.timeoutId);
                attempt.timeoutId = null;
            }
            for (const [name, handler] of attempt.listeners) {
                attempt.parts.video.removeEventListener(name, handler);
            }
            attempt.listeners = [];
        }

        function resetCard(card, state, labelText) {
            const parts = cardParts(card);
            unloadVideo(parts.video);
            if (parts.still) {
                parts.still.hidden = false;
            }
            if (parts.button) {
                parts.button.setAttribute("aria-pressed", "false");
            }
            if (parts.label) {
                parts.label.textContent = labelText || "播放实况";
            }
            if (card.classList) {
                card.classList.remove("is-active");
            }
            setCardState(card, state || "stopped");
            return parts;
        }

        function isCurrent(attempt) {
            return Boolean(
                attempt
                && activeAttempt === attempt
                && attempt.generation === attemptSequence
            );
        }

        function failAttempt(attempt, state) {
            if (!isCurrent(attempt)) {
                return false;
            }
            cleanupAttempt(attempt);
            activeAttempt = null;
            attemptSequence += 1;
            const parts = resetCard(attempt.card, state, "重试播放");
            showError(parts, state);
            return false;
        }

        function finishAttempt(attempt) {
            if (!isCurrent(attempt)) {
                return false;
            }
            cleanupAttempt(attempt);
            activeAttempt = null;
            attemptSequence += 1;
            setCardState(attempt.card, "ended");
            const parts = resetCard(attempt.card, "idle", "播放实况");
            hideError(parts);
            return true;
        }

        function stopActiveAttempt(state) {
            if (!activeAttempt) {
                return;
            }
            const attempt = activeAttempt;
            activeAttempt = null;
            attemptSequence += 1;
            cleanupAttempt(attempt);
            const parts = resetCard(attempt.card, state || "stopped", "播放实况");
            hideError(parts);
        }

        function showStandaloneFailure(card, state) {
            if (activeAttempt) {
                stopActiveAttempt("stopped");
            }
            const parts = resetCard(card, state, "重试播放");
            showError(parts, state);
            return false;
        }

        function registerAttemptListener(attempt, name, handler) {
            attempt.parts.video.addEventListener(name, handler);
            attempt.listeners.push([name, handler]);
        }

        function playCard(card, motionUrl) {
            const parts = cardParts(card);
            if (!parts.video || !parts.still) {
                return Promise.resolve(showStandaloneFailure(card, "source_not_supported"));
            }
            if (!isSafeMotionUrl(motionUrl)) {
                return Promise.resolve(showStandaloneFailure(card, "policy_rejected"));
            }
            if (activeAttempt) {
                stopActiveAttempt("stopped");
            }

            attemptSequence += 1;
            const attempt = {
                card,
                generation: attemptSequence,
                listeners: [],
                parts,
                timeoutId: null,
            };
            activeAttempt = attempt;
            hideError(parts);
            parts.still.hidden = false;
            parts.video.hidden = true;
            if (parts.button) {
                parts.button.setAttribute("aria-pressed", "false");
            }
            if (parts.label) {
                parts.label.textContent = "正在加载";
            }
            if (card.classList) {
                card.classList.remove("is-active");
            }
            setCardState(card, "loading");

            registerAttemptListener(attempt, "loadedmetadata", () => {
                if (isCurrent(attempt)) {
                    setCardState(card, "metadata");
                }
            });
            registerAttemptListener(attempt, "canplay", () => {
                if (isCurrent(attempt)) {
                    setCardState(card, "canplay");
                }
            });
            registerAttemptListener(attempt, "playing", () => {
                if (!isCurrent(attempt)) {
                    return;
                }
                if (attempt.timeoutId !== null) {
                    clearTimer(attempt.timeoutId);
                    attempt.timeoutId = null;
                }
                parts.video.hidden = false;
                parts.still.hidden = true;
                if (parts.button) {
                    parts.button.setAttribute("aria-pressed", "true");
                }
                if (parts.label) {
                    parts.label.textContent = "停止实况";
                }
                if (card.classList) {
                    card.classList.add("is-active");
                }
                setCardState(card, "playing");
            });
            registerAttemptListener(attempt, "ended", () => {
                finishAttempt(attempt);
            });
            registerAttemptListener(attempt, "error", () => {
                failAttempt(
                    attempt,
                    classifyMediaError(parts.video.error && parts.video.error.code),
                );
            });
            registerAttemptListener(attempt, "abort", () => {
                failAttempt(attempt, "aborted");
            });

            parts.video.loop = false;
            parts.video.src = motionUrl;
            parts.video.load();
            attempt.timeoutId = setTimer(() => {
                failAttempt(attempt, "startup_timeout");
            }, startupTimeoutMs);

            let playResult;
            try {
                playResult = parts.video.play();
            } catch (error) {
                return Promise.resolve(
                    failAttempt(
                        attempt,
                        classifyMediaError(parts.video.error && parts.video.error.code, error),
                    ),
                );
            }
            return Promise.resolve(playResult).then(
                () => isCurrent(attempt),
                (error) => failAttempt(
                    attempt,
                    classifyMediaError(parts.video.error && parts.video.error.code, error),
                ),
            );
        }

        function restoreCard(card) {
            if (activeAttempt && activeAttempt.card === card) {
                stopActiveAttempt("stopped");
                return;
            }
            const parts = resetCard(card, "stopped", "播放实况");
            hideError(parts);
        }

        function handleClick(event) {
            const button = event.target && event.target.closest
                ? event.target.closest("[data-livephoto-toggle]")
                : null;
            if (!button) {
                return;
            }
            const card = button.closest("[data-livephoto-card]");
            if (!card) {
                return;
            }
            event.preventDefault();
            if (activeAttempt && activeAttempt.card === card) {
                stopActiveAttempt("stopped");
                return;
            }
            void playCard(card, button.dataset.motionUrl || "");
        }

        function destroy() {
            stopActiveAttempt("stopped");
            documentObject.removeEventListener("click", handleClick);
        }

        documentObject.addEventListener("click", handleClick);
        return Object.freeze({destroy, playCard, restoreCard});
    }

    function ensureController(documentObject) {
        disableLooping(documentObject);
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
        SAFE_ERROR_TEXT,
        SAFE_ERROR_TEXT_BY_STATE,
        SAFE_STATES: SAFE_STATE_VALUES,
        STARTUP_TIMEOUT_MS,
        classifyMediaError,
        createController,
        disableLooping,
        ensureController,
        isSafeMotionUrl,
    });
}));
