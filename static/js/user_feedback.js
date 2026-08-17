(function (root, factory) {
    "use strict";

    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        root.AnimeImagesUserFeedback = api;
        api.init(root.document);
    }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
    "use strict";

    const SOURCE_PLATFORM_PATHS = Object.freeze([
        ["/batch", "batch"],
        ["/xhs", "xhs"],
        ["/tiktok", "tiktok"],
        ["/bilibili", "bilibili"],
    ]);
    const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
    const ALLOWED_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

    function pagePath(value) {
        try {
            const parsed = new URL(String(value || "/"), "https://feedback.invalid");
            return parsed.pathname || "/";
        } catch (error) {
            return "/";
        }
    }

    function sourcePlatform(pathValue) {
        const normalizedPath = pagePath(pathValue);
        const matched = SOURCE_PLATFORM_PATHS.find(function (entry) {
            return normalizedPath === entry[0] || normalizedPath.startsWith(`${entry[0]}/`);
        });
        return matched ? matched[1] : "home";
    }

    function viewportDimension(value) {
        const dimension = Number.parseInt(value, 10);
        return Number.isInteger(dimension) && dimension >= 1 && dimension <= 10000
            ? String(dimension)
            : "";
    }

    function buildMetadata(locationValue, viewportValue) {
        const locationObject = locationValue || {};
        const viewportObject = viewportValue || {};
        const currentPath = pagePath(locationObject.href || locationObject.pathname || "/");
        return Object.freeze({
            page_path: currentPath,
            source_platform: sourcePlatform(currentPath),
            viewport_width: viewportDimension(viewportObject.innerWidth),
            viewport_height: viewportDimension(viewportObject.innerHeight),
        });
    }

    function imageValidationMessage(file) {
        if (!file) {
            return "";
        }
        if (Number(file.size || 0) > MAX_IMAGE_BYTES) {
            return "问题截图不能超过 5 MB。";
        }
        if (file.type && !ALLOWED_IMAGE_TYPES.has(String(file.type).toLowerCase())) {
            return "仅支持 JPG、PNG 或 WebP 图片。";
        }
        return "";
    }

    function setFieldValue(form, name, value) {
        const field = form ? form.querySelector(`[name="${name}"]`) : null;
        if (field) {
            field.value = value;
        }
    }

    function applyMetadata(form, locationValue, viewportValue) {
        const metadata = buildMetadata(locationValue, viewportValue);
        Object.entries(metadata).forEach(function (entry) {
            setFieldValue(form, entry[0], entry[1]);
        });
        return metadata;
    }

    async function readResponsePayload(response) {
        try {
            const payload = await response.json();
            return payload && typeof payload === "object" ? payload : {};
        } catch (error) {
            return {};
        }
    }

    async function submit(form, fetchImplementation) {
        const fetchImpl = fetchImplementation || (root && root.fetch);
        if (!form || typeof fetchImpl !== "function") {
            throw new Error("当前浏览器无法提交反馈，请稍后重试。");
        }

        const response = await fetchImpl(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {
                Accept: "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        const payload = await readResponsePayload(response);
        if (!response.ok || payload.ok !== true) {
            throw new Error(
                typeof payload.message === "string" && payload.message
                    ? payload.message
                    : "反馈提交失败，请稍后重试。"
            );
        }
        return payload;
    }

    function init(documentObject) {
        if (!documentObject || typeof documentObject.querySelector !== "function") {
            return;
        }

        const modal = documentObject.querySelector("#userFeedbackModal");
        const form = documentObject.querySelector("#user-feedback-form");
        if (!modal || !form || form.dataset.feedbackBound === "true") {
            return;
        }
        form.dataset.feedbackBound = "true";

        const fields = form.querySelector("[data-feedback-form-fields]");
        const success = form.querySelector("[data-feedback-success]");
        const status = form.querySelector("[data-feedback-status]");
        const submitButton = form.querySelector("[data-feedback-submit]");
        const submitLabel = form.querySelector("[data-feedback-submit-label]");
        const spinner = form.querySelector("[data-feedback-spinner]");
        const dismissLabel = form.querySelector("[data-feedback-dismiss-label]");
        const imageInput = form.querySelector('input[name="image"]');

        function resetPresentation() {
            if (fields) {
                fields.hidden = false;
            }
            if (success) {
                success.hidden = true;
            }
            if (status) {
                status.textContent = "";
                status.classList.remove("text-danger", "text-muted");
            }
            if (submitButton) {
                submitButton.disabled = false;
                submitButton.hidden = false;
            }
            if (submitLabel) {
                submitLabel.textContent = "提交反馈";
            }
            if (spinner) {
                spinner.hidden = true;
            }
            if (dismissLabel) {
                dismissLabel.textContent = "取消";
            }
        }

        modal.addEventListener("show.bs.modal", function () {
            resetPresentation();
            applyMetadata(form, root.location, root);
        });

        modal.addEventListener("hidden.bs.modal", function () {
            form.reset();
            resetPresentation();
            applyMetadata(form, root.location, root);
        });

        form.addEventListener("submit", async function (event) {
            event.preventDefault();
            applyMetadata(form, root.location, root);
            const imageError = imageValidationMessage(
                imageInput && imageInput.files ? imageInput.files[0] : null
            );
            if (imageError) {
                if (status) {
                    status.textContent = imageError;
                    status.classList.remove("text-muted");
                    status.classList.add("text-danger");
                }
                return;
            }
            if (submitButton) {
                submitButton.disabled = true;
            }
            if (submitLabel) {
                submitLabel.textContent = "正在提交";
            }
            if (spinner) {
                spinner.hidden = false;
            }
            if (status) {
                status.textContent = "正在安全提交反馈...";
                status.classList.remove("text-danger");
                status.classList.add("text-muted");
            }

            try {
                await submit(form);
                if (fields) {
                    fields.hidden = true;
                }
                if (success) {
                    success.hidden = false;
                }
                if (status) {
                    status.textContent = "";
                }
                if (submitButton) {
                    submitButton.hidden = true;
                }
                if (dismissLabel) {
                    dismissLabel.textContent = "关闭";
                }
            } catch (error) {
                if (status) {
                    status.textContent = error && error.message
                        ? error.message
                        : "反馈提交失败，请稍后重试。";
                    status.classList.remove("text-muted");
                    status.classList.add("text-danger");
                }
                if (submitButton) {
                    submitButton.disabled = false;
                }
                if (submitLabel) {
                    submitLabel.textContent = "重新提交";
                }
            } finally {
                if (spinner) {
                    spinner.hidden = true;
                }
            }
        });

        applyMetadata(form, root.location, root);
    }

    return Object.freeze({
        pagePath,
        sourcePlatform,
        viewportDimension,
        buildMetadata,
        imageValidationMessage,
        applyMetadata,
        submit,
        init,
    });
});
