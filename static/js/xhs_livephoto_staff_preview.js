(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesXhsLivePhotoStaffPreview = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    const SAFE_MOTION_HOST = /^(?:sns-video-v[0-9]+|sns-bak-v[0-9]+)\.xhscdn\.com$/;
    const SAFE_ERROR_TEXT = "实况预览加载失败，可重新解析后再试。";

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

    function createController(documentObject) {
        if (!documentObject || typeof documentObject.addEventListener !== "function") {
            return null;
        }
        let activeCard = null;

        function cardParts(card) {
            return {
                still: card && card.querySelector("[data-livephoto-still]"),
                video: card && card.querySelector("[data-livephoto-motion]"),
                button: card && card.querySelector("[data-livephoto-toggle]"),
                label: card && card.querySelector("[data-livephoto-toggle-label]"),
                error: card && card.querySelector("[data-livephoto-error]"),
            };
        }

        function restoreCard(card) {
            if (!card) {
                return;
            }
            const parts = cardParts(card);
            if (parts.video) {
                parts.video.pause();
                parts.video.removeAttribute("src");
                if (typeof parts.video.load === "function") {
                    parts.video.load();
                }
                parts.video.hidden = true;
            }
            if (parts.still) {
                parts.still.hidden = false;
            }
            if (parts.button) {
                parts.button.setAttribute("aria-pressed", "false");
            }
            if (parts.label) {
                parts.label.textContent = "播放实况";
            }
            if (card.classList) {
                card.classList.remove("is-active");
            }
            if (activeCard === card) {
                activeCard = null;
            }
        }

        function showFailure(card) {
            restoreCard(card);
            const error = cardParts(card).error;
            if (error) {
                error.textContent = SAFE_ERROR_TEXT;
                error.hidden = false;
            }
        }

        async function playCard(card, motionUrl) {
            const parts = cardParts(card);
            if (!parts.video || !parts.still || !isSafeMotionUrl(motionUrl)) {
                showFailure(card);
                return false;
            }
            if (activeCard && activeCard !== card) {
                restoreCard(activeCard);
            }
            if (parts.error) {
                parts.error.hidden = true;
            }
            activeCard = card;
            parts.video.src = motionUrl;
            parts.video.hidden = false;
            try {
                await parts.video.play();
            } catch (error) {
                showFailure(card);
                return false;
            }
            if (activeCard !== card) {
                return false;
            }
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
            return true;
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
            if (activeCard === card) {
                restoreCard(card);
                return;
            }
            void playCard(card, button.dataset.motionUrl || "");
        }

        function handleMediaError(event) {
            const video = event.target;
            if (!video || !video.matches || !video.matches("[data-livephoto-motion]")) {
                return;
            }
            const card = video.closest("[data-livephoto-card]");
            if (card) {
                showFailure(card);
            }
        }

        function destroy() {
            if (activeCard) {
                restoreCard(activeCard);
            }
            documentObject.removeEventListener("click", handleClick);
            documentObject.removeEventListener("error", handleMediaError, true);
        }

        documentObject.addEventListener("click", handleClick);
        documentObject.addEventListener("error", handleMediaError, true);
        return Object.freeze({destroy, playCard, restoreCard});
    }

    if (typeof document !== "undefined") {
        const controller = createController(document);
        if (typeof window !== "undefined" && controller) {
            window.addEventListener("pagehide", controller.destroy, {once: true});
        }
    }

    return Object.freeze({
        SAFE_ERROR_TEXT,
        createController,
        isSafeMotionUrl,
    });
}));
