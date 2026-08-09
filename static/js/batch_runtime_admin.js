(function () {
    "use strict";

    const form = document.getElementById("batchparseruntimesetting_form");
    const savedState = document.getElementById("batch-runtime-saved-state");
    const dirtyState = document.getElementById("batch-runtime-dirty-state");
    const confirmation = document.getElementById("batch-runtime-public-confirm");
    const confirmationField = document.getElementById("id_public_expansion_confirmed");
    const currentLabel = document.getElementById("batch-runtime-confirm-current");
    const targetLabel = document.getElementById("batch-runtime-confirm-target");
    const impactList = document.getElementById("batch-runtime-confirm-impacts");
    const cancelButton = document.getElementById("batch-runtime-confirm-cancel");
    const applyButton = document.getElementById("batch-runtime-confirm-apply");

    if (!form || !savedState || !dirtyState || !confirmation || !confirmationField) {
        return;
    }

    const modeLabels = {
        disabled: "关闭",
        staff: "仅管理员",
        public_gray: "公开灰度",
        public: "全站开放",
    };
    const modeExpansions = new Set([
        "disabled:public_gray",
        "disabled:public",
        "staff:public_gray",
        "staff:public",
        "public_gray:public",
    ]);
    const excludedNames = new Set([
        "csrfmiddlewaretoken",
        "runtime_version",
        "public_expansion_confirmed",
        "_save",
    ]);
    const editableFields = Array.from(form.elements).filter(function (field) {
        return field.name && !excludedNames.has(field.name) && !field.disabled;
    });
    const initialValues = new Map();
    const serverUnpersisted = savedState.dataset.serverUnpersisted === "true";
    let confirmedSubmit = false;
    let pendingSubmitter = null;
    let submitting = false;

    function fieldValue(field) {
        if (field.type === "checkbox" || field.type === "radio") {
            return field.checked ? "1" : "0";
        }
        return String(field.value || "");
    }

    editableFields.forEach(function (field) {
        initialValues.set(field, fieldValue(field));
    });

    function updateDirtyState() {
        const dirty = serverUnpersisted || editableFields.some(function (field) {
            return initialValues.get(field) !== fieldValue(field);
        });
        dirtyState.hidden = !dirty;
        form.dataset.dirty = dirty ? "true" : "false";
        if (dirty) {
            confirmationField.value = "0";
            confirmedSubmit = false;
        }
    }

    function checked(name) {
        const field = form.elements.namedItem(name);
        return Boolean(field && field.checked);
    }

    function proposedState() {
        return {
            accessMode: String(form.elements.namedItem("access_mode").value || "staff"),
            rolloutPercent: Number(form.elements.namedItem("rollout_percent").value || 0),
            publicUltraHd: checked("public_ultra_hd_enabled"),
            publicYoutubeMerge: checked("public_youtube_merge_enabled"),
        };
    }

    function persistedState() {
        return {
            accessMode: savedState.dataset.savedAccessMode || "staff",
            rolloutPercent: Number(savedState.dataset.savedRolloutPercent || 0),
            publicUltraHd: savedState.dataset.savedPublicUltraHd === "true",
            publicYoutubeMerge: savedState.dataset.savedPublicYoutubeMerge === "true",
        };
    }

    function publicExpansionReasons(previous, proposed) {
        const reasons = [];
        if (modeExpansions.has(previous.accessMode + ":" + proposed.accessMode)) {
            reasons.push("access_mode");
        }
        if (
            previous.accessMode === "public_gray" &&
            proposed.accessMode === "public_gray" &&
            proposed.rolloutPercent > previous.rolloutPercent
        ) {
            reasons.push("rollout_percent");
        }
        if (!previous.publicUltraHd && proposed.publicUltraHd) {
            reasons.push("public_ultra_hd_enabled");
        }
        if (!previous.publicYoutubeMerge && proposed.publicYoutubeMerge) {
            reasons.push("public_youtube_merge_enabled");
        }
        return reasons;
    }

    function stateLabel(state) {
        let label = modeLabels[state.accessMode] || state.accessMode;
        if (state.accessMode === "public_gray") {
            label += " " + state.rolloutPercent + "%";
        }
        return label +
            "；访客超清 " + (state.publicUltraHd ? "开启" : "关闭") +
            "；访客 YouTube 合并 " + (state.publicYoutubeMerge ? "开启" : "关闭");
    }

    function showConfirmation(previous, proposed, reasons) {
        if (currentLabel) {
            currentLabel.textContent = stateLabel(previous);
        }
        if (targetLabel) {
            targetLabel.textContent = stateLabel(proposed);
        }
        if (impactList) {
            impactList.replaceChildren();
            const impactMessages = {
                access_mode: "更多未登录用户可以看到并使用批量解析。",
                rollout_percent: "公开灰度覆盖比例将提高。",
                public_ultra_hd_enabled: "允许访客请求抖音原画/超清资源。",
                public_youtube_merge_enabled: "允许访客请求 YouTube 高画质合并。",
            };
            reasons.forEach(function (reason) {
                const item = document.createElement("li");
                item.textContent = impactMessages[reason];
                impactList.appendChild(item);
            });
            const boundary = document.createElement("li");
            boundary.textContent = "Staff-only raw-link 工具不会因此公开。";
            impactList.appendChild(boundary);
        }
        if (typeof confirmation.showModal === "function") {
            confirmation.showModal();
            return;
        }
        if (window.confirm("确认扩大批量解析公网访问并立即生效？")) {
            confirmationField.value = "1";
            confirmedSubmit = true;
            submitting = true;
            form.requestSubmit(pendingSubmitter || undefined);
        }
    }

    editableFields.forEach(function (field) {
        field.addEventListener("input", updateDirtyState);
        field.addEventListener("change", updateDirtyState);
    });

    form.addEventListener("submit", function (event) {
        if (confirmedSubmit || confirmationField.value === "1") {
            submitting = true;
            return;
        }
        const previous = persistedState();
        const proposed = proposedState();
        const reasons = publicExpansionReasons(previous, proposed);
        if (!reasons.length) {
            submitting = true;
            return;
        }
        event.preventDefault();
        pendingSubmitter = event.submitter || null;
        showConfirmation(previous, proposed, reasons);
    });

    if (cancelButton) {
        cancelButton.addEventListener("click", function () {
            confirmationField.value = "0";
            confirmation.close();
        });
    }

    confirmation.addEventListener("cancel", function () {
        confirmationField.value = "0";
    });

    if (applyButton) {
        applyButton.addEventListener("click", function () {
            confirmationField.value = "1";
            confirmedSubmit = true;
            confirmation.close();
            submitting = true;
            form.requestSubmit(pendingSubmitter || undefined);
        });
    }

    window.addEventListener("beforeunload", function (event) {
        if (!submitting && form.dataset.dirty === "true") {
            event.preventDefault();
            event.returnValue = "";
        }
    });

    updateDirtyState();
})();
