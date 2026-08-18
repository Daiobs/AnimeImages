(function (root, factory) {
    "use strict";

    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        root.AnimeImagesProjectSettingsUI = api;
    }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
    "use strict";

    const CHANGE_EVENT = "animeimages:project-settings-changed";

    function booleanData(value, fallback) {
        const normalized = String(value === undefined ? "" : value).toLowerCase();
        if (normalized === "true") {
            return true;
        }
        if (normalized === "false") {
            return false;
        }
        return Boolean(fallback);
    }

    function buildEnabledProjectPatch(controls) {
        const values = controls || {};
        const parsing = {};
        if (values.ultraInput && !values.ultraInput.disabled) {
            parsing.ultra_hd = Boolean(values.ultraInput.checked);
        }
        if (values.youtubeInput && !values.youtubeInput.disabled) {
            parsing.youtube_merge = Boolean(values.youtubeInput.checked);
        }
        const selectedLocation = (values.locationInputs || []).find(
            (input) => input && input.checked
        );
        const patch = {parsing};
        if (selectedLocation) {
            patch.download = {location_mode: selectedLocation.value};
        }
        return patch;
    }

    function init() {
        if (!root || !root.document) {
            return;
        }
        const modal = root.document.getElementById("projectSettingsModal");
        const settingsApi = root.AnimeImagesProjectSettings
            || root.AnimeImagesParsePreferences;
        if (!modal || !settingsApi) {
            return;
        }

        const ultraInput = modal.querySelector("#project-ultra-hd-setting");
        const youtubeInput = modal.querySelector("#project-youtube-merge-setting");
        const locationInputs = Array.from(
            modal.querySelectorAll('input[name="project-download-location"]')
        );
        const saveButton = modal.querySelector("#project-save-settings-btn");
        const defaults = {
            parsing: {
                ultra_hd: booleanData(modal.dataset.defaultUltraHd, true),
                youtube_merge: booleanData(modal.dataset.defaultYoutubeMerge, false),
            },
            download: {location_mode: "browser_default"},
        };
        let committedSettings = settingsApi.loadProjectSettings({defaults});
        let saveCommitted = false;
        let modalOpen = false;

        function updateLocationPresentation() {
            locationInputs.forEach((input) => {
                const label = input.closest("label");
                if (!label) {
                    return;
                }
                label.classList.toggle("btn-brand", input.checked);
                label.classList.toggle("btn-outline-secondary", !input.checked);
                label.setAttribute("aria-pressed", String(input.checked));
            });
        }

        function applySettings(settings) {
            if (!settings || !settings.parsing || !settings.download) {
                return;
            }
            if (ultraInput) {
                ultraInput.checked = Boolean(settings.parsing.ultra_hd)
                    && !ultraInput.disabled;
            }
            if (youtubeInput) {
                youtubeInput.checked = Boolean(settings.parsing.youtube_merge)
                    && !youtubeInput.disabled;
            }
            locationInputs.forEach((input) => {
                input.checked = input.value === settings.download.location_mode;
            });
            if (!locationInputs.some((input) => input.checked) && locationInputs[0]) {
                locationInputs[0].checked = true;
            }
            updateLocationPresentation();
        }

        function dispatchChange(settings) {
            if (typeof root.CustomEvent !== "function") {
                return;
            }
            root.dispatchEvent(new root.CustomEvent(CHANGE_EVENT, {
                detail: {settings, source: "save"},
            }));
        }

        applySettings(committedSettings);
        locationInputs.forEach((input) => {
            input.addEventListener("change", updateLocationPresentation);
        });

        modal.addEventListener("show.bs.modal", () => {
            modalOpen = true;
            saveCommitted = false;
            applySettings(committedSettings);
        });
        modal.addEventListener("hidden.bs.modal", () => {
            modalOpen = false;
            if (!saveCommitted) {
                applySettings(committedSettings);
            }
            saveCommitted = false;
        });

        if (saveButton) {
            saveButton.addEventListener("click", () => {
                committedSettings = settingsApi.updateProjectSettings(
                    buildEnabledProjectPatch({
                        ultraInput,
                        youtubeInput,
                        locationInputs,
                    }),
                    {defaults},
                );
                saveCommitted = true;
                applySettings(committedSettings);
                dispatchChange(committedSettings);
            });
        }

        settingsApi.subscribeProjectSettings((settings) => {
            committedSettings = settings;
            if (!modalOpen) {
                applySettings(committedSettings);
            }
        });
    }

    const api = Object.freeze({
        CHANGE_EVENT,
        booleanData,
        buildEnabledProjectPatch,
        init,
    });

    if (root && root.document) {
        if (root.document.readyState === "loading") {
            root.document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    }
    return api;
});
