(function (root, factory) {
    "use strict";

    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        root.AnimeImagesParsePreferences = api;
        root.AnimeImagesProjectSettings = api;
    }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
    "use strict";

    const PROJECT_STORAGE_KEY = "animeimages.settings.v2";
    const SHARED_STORAGE_KEY = "animeimages.parsePreferences.v1";
    const SINGLE_LEGACY_STORAGE_KEY = "xhsDownloadSettings";
    const BATCH_DRAFT_LEGACY_STORAGE_KEY = "animeimages.batchParseDraft.v1";
    const BATCH_DRAFT_STORAGE_KEY = "animeimages.batchParseDraft.v2";
    const PROJECT_VERSION = 2;
    const SHARED_VERSION = 1;
    const BATCH_DRAFT_VERSION = 2;
    const PREFERENCE_FIELDS = ["ultra_hd", "youtube_merge"];
    const LOCATION_MODES = Object.freeze([
        "browser_default",
        "ask_when_supported",
    ]);
    const SINGLE_LEGACY_FIELDS = {
        ultra_hd: "ultra_hd_enabled",
        youtube_merge: "youtube_merge_enabled",
    };

    function isObject(value) {
        return Boolean(value) && typeof value === "object" && !Array.isArray(value);
    }

    function parseStoredObject(rawValue) {
        if (typeof rawValue !== "string" || !rawValue.trim()) {
            return null;
        }
        try {
            const value = JSON.parse(rawValue);
            return isObject(value) ? value : null;
        } catch (error) {
            return null;
        }
    }

    function getStorage(options) {
        if (isObject(options) && Object.prototype.hasOwnProperty.call(options, "storage")) {
            return options.storage;
        }
        try {
            return root && root.localStorage ? root.localStorage : null;
        } catch (error) {
            return null;
        }
    }

    function readStoredObject(storage, key) {
        if (!storage || typeof storage.getItem !== "function") {
            return null;
        }
        try {
            return parseStoredObject(storage.getItem(key));
        } catch (error) {
            return null;
        }
    }

    function writeStoredObject(storage, key, value) {
        if (!storage || typeof storage.setItem !== "function") {
            return false;
        }
        const serialized = JSON.stringify(value);
        try {
            if (typeof storage.getItem === "function" && storage.getItem(key) === serialized) {
                return true;
            }
            storage.setItem(key, serialized);
            return true;
        } catch (error) {
            return false;
        }
    }

    function preferenceDefaults(defaults) {
        const source = isObject(defaults) ? defaults : {};
        const parsing = isObject(source.parsing) ? source.parsing : source;
        return {
            ultra_hd: typeof parsing.ultra_hd === "boolean" ? parsing.ultra_hd : true,
            youtube_merge: typeof parsing.youtube_merge === "boolean"
                ? parsing.youtube_merge
                : false,
        };
    }

    function locationDefault(defaults) {
        const source = isObject(defaults) ? defaults : {};
        const download = isObject(source.download) ? source.download : source;
        return LOCATION_MODES.includes(download.location_mode)
            ? download.location_mode
            : "browser_default";
    }

    function validSharedFields(value) {
        const fields = {};
        if (!isObject(value) || value.version !== SHARED_VERSION) {
            return fields;
        }
        PREFERENCE_FIELDS.forEach((field) => {
            if (typeof value[field] === "boolean") {
                fields[field] = value[field];
            }
        });
        return fields;
    }

    function validProjectFields(value) {
        const fields = {parsing: {}, download: {}};
        if (!isObject(value) || value.version !== PROJECT_VERSION) {
            return fields;
        }
        const parsing = isObject(value.parsing) ? value.parsing : {};
        PREFERENCE_FIELDS.forEach((field) => {
            if (typeof parsing[field] === "boolean") {
                fields.parsing[field] = parsing[field];
            }
        });
        const download = isObject(value.download) ? value.download : {};
        if (LOCATION_MODES.includes(download.location_mode)) {
            fields.download.location_mode = download.location_mode;
        }
        return fields;
    }

    function completeSharedPreferences(value) {
        const fields = validSharedFields(value);
        if (PREFERENCE_FIELDS.every((field) => typeof fields[field] === "boolean")) {
            return {
                version: SHARED_VERSION,
                ultra_hd: fields.ultra_hd,
                youtube_merge: fields.youtube_merge,
            };
        }
        return null;
    }

    function completeProjectSettings(value) {
        const fields = validProjectFields(value);
        if (
            PREFERENCE_FIELDS.every(
                (field) => typeof fields.parsing[field] === "boolean"
            )
            && LOCATION_MODES.includes(fields.download.location_mode)
        ) {
            return {
                version: PROJECT_VERSION,
                parsing: {
                    ultra_hd: fields.parsing.ultra_hd,
                    youtube_merge: fields.parsing.youtube_merge,
                },
                download: {
                    location_mode: fields.download.location_mode,
                },
            };
        }
        return null;
    }

    function parsingPreferences(projectSettings) {
        const settings = completeProjectSettings(projectSettings);
        return {
            version: SHARED_VERSION,
            ultra_hd: Boolean(settings && settings.parsing.ultra_hd),
            youtube_merge: Boolean(settings && settings.parsing.youtube_merge),
        };
    }

    function loadProjectSettings(options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const defaults = preferenceDefaults(settings.defaults);
        const defaultLocation = locationDefault(settings.defaults);
        const project = validProjectFields(readStoredObject(storage, PROJECT_STORAGE_KEY));
        const shared = validSharedFields(readStoredObject(storage, SHARED_STORAGE_KEY));
        const singleLegacy = readStoredObject(storage, SINGLE_LEGACY_STORAGE_KEY) || {};
        const batchLegacy = readStoredObject(storage, BATCH_DRAFT_LEGACY_STORAGE_KEY) || {};
        const next = {
            version: PROJECT_VERSION,
            parsing: {},
            download: {},
        };

        PREFERENCE_FIELDS.forEach((field) => {
            const singleLegacyField = SINGLE_LEGACY_FIELDS[field];
            if (typeof project.parsing[field] === "boolean") {
                next.parsing[field] = project.parsing[field];
            } else if (typeof shared[field] === "boolean") {
                next.parsing[field] = shared[field];
            } else if (typeof singleLegacy[singleLegacyField] === "boolean") {
                next.parsing[field] = singleLegacy[singleLegacyField];
            } else if (typeof batchLegacy[field] === "boolean") {
                next.parsing[field] = batchLegacy[field];
            } else {
                next.parsing[field] = defaults[field];
            }
        });

        if (LOCATION_MODES.includes(project.download.location_mode)) {
            next.download.location_mode = project.download.location_mode;
        } else if (typeof singleLegacy.use_file_picker === "boolean") {
            next.download.location_mode = singleLegacy.use_file_picker
                ? "ask_when_supported"
                : "browser_default";
        } else {
            next.download.location_mode = defaultLocation;
        }

        writeStoredObject(storage, PROJECT_STORAGE_KEY, next);
        return next;
    }

    function mirrorLegacySettings(storage, projectSettings) {
        const shared = parsingPreferences(projectSettings);
        writeStoredObject(storage, SHARED_STORAGE_KEY, shared);

        const legacy = readStoredObject(storage, SINGLE_LEGACY_STORAGE_KEY) || {};
        writeStoredObject(storage, SINGLE_LEGACY_STORAGE_KEY, {
            ...legacy,
            ultra_hd_enabled: shared.ultra_hd,
            youtube_merge_enabled: shared.youtube_merge,
            use_file_picker: (
                projectSettings.download.location_mode === "ask_when_supported"
            ),
        });
    }

    function updateProjectSettings(patch, options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const current = loadProjectSettings(settings);
        const changes = isObject(patch) ? patch : {};
        const parsingChanges = isObject(changes.parsing) ? changes.parsing : changes;
        const downloadChanges = isObject(changes.download) ? changes.download : changes;
        const next = {
            version: PROJECT_VERSION,
            parsing: {...current.parsing},
            download: {...current.download},
        };

        PREFERENCE_FIELDS.forEach((field) => {
            if (typeof parsingChanges[field] === "boolean") {
                next.parsing[field] = parsingChanges[field];
            }
        });
        if (LOCATION_MODES.includes(downloadChanges.location_mode)) {
            next.download.location_mode = downloadChanges.location_mode;
        }

        writeStoredObject(storage, PROJECT_STORAGE_KEY, next);
        mirrorLegacySettings(storage, next);
        return next;
    }

    function load(options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const preferences = parsingPreferences(loadProjectSettings(settings));
        writeStoredObject(storage, SHARED_STORAGE_KEY, preferences);
        return preferences;
    }

    function update(patch, options) {
        return parsingPreferences(updateProjectSettings({parsing: patch}, options));
    }

    function loadBatchDraft(options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const current = readStoredObject(storage, BATCH_DRAFT_STORAGE_KEY);
        let links = "";

        if (
            current
            && current.version === BATCH_DRAFT_VERSION
            && typeof current.links === "string"
        ) {
            links = current.links;
        } else {
            const legacy = readStoredObject(storage, BATCH_DRAFT_LEGACY_STORAGE_KEY);
            if (legacy && typeof legacy.links === "string") {
                links = legacy.links;
            } else if (typeof settings.defaultLinks === "string") {
                links = settings.defaultLinks;
            }
        }

        const draft = {version: BATCH_DRAFT_VERSION, links};
        writeStoredObject(storage, BATCH_DRAFT_STORAGE_KEY, draft);
        return draft;
    }

    function saveBatchDraft(links, options) {
        const settings = isObject(options) ? options : {};
        const draft = {
            version: BATCH_DRAFT_VERSION,
            links: typeof links === "string" ? links : "",
        };
        writeStoredObject(getStorage(settings), BATCH_DRAFT_STORAGE_KEY, draft);
        return draft;
    }

    function applyToControls(preferences, controls) {
        const values = completeSharedPreferences(preferences) || {
            version: SHARED_VERSION,
            ultra_hd: true,
            youtube_merge: false,
        };
        const targets = isObject(controls) ? controls : {};

        function applyField(field, input, hiddenInput) {
            const available = input ? !input.disabled : true;
            const enabled = Boolean(available && values[field]);
            if (input) {
                input.checked = enabled;
            }
            if (hiddenInput) {
                hiddenInput.value = enabled ? "1" : "0";
            }
            return enabled;
        }

        return {
            ultra_hd: applyField(
                "ultra_hd",
                targets.ultraHdInput,
                targets.ultraHdHiddenInput
            ),
            youtube_merge: applyField(
                "youtube_merge",
                targets.youtubeMergeInput,
                targets.youtubeMergeHiddenInput
            ),
        };
    }

    function subscribeProjectSettings(callback, options) {
        const settings = isObject(options) ? options : {};
        const eventTarget = settings.eventTarget || root;
        if (!eventTarget || typeof eventTarget.addEventListener !== "function") {
            return function () {};
        }
        const listener = function (event) {
            if (!event || event.key !== PROJECT_STORAGE_KEY) {
                return;
            }
            const projectSettings = completeProjectSettings(
                parseStoredObject(event.newValue)
            );
            if (projectSettings && typeof callback === "function") {
                callback(projectSettings);
            }
        };
        eventTarget.addEventListener("storage", listener);
        return function () {
            if (typeof eventTarget.removeEventListener === "function") {
                eventTarget.removeEventListener("storage", listener);
            }
        };
    }

    function subscribe(callback, options) {
        return subscribeProjectSettings((projectSettings) => {
            if (typeof callback === "function") {
                callback(parsingPreferences(projectSettings));
            }
        }, options);
    }

    return Object.freeze({
        PROJECT_STORAGE_KEY,
        SHARED_STORAGE_KEY,
        SINGLE_LEGACY_STORAGE_KEY,
        BATCH_DRAFT_LEGACY_STORAGE_KEY,
        BATCH_DRAFT_STORAGE_KEY,
        LOCATION_MODES,
        loadProjectSettings,
        updateProjectSettings,
        parsingPreferences,
        load,
        update,
        loadBatchDraft,
        saveBatchDraft,
        applyToControls,
        subscribeProjectSettings,
        subscribe,
    });
});
