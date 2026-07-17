(function (root, factory) {
    "use strict";

    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        root.AnimeImagesParsePreferences = api;
    }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
    "use strict";

    const SHARED_STORAGE_KEY = "animeimages.parsePreferences.v1";
    const SINGLE_LEGACY_STORAGE_KEY = "xhsDownloadSettings";
    const BATCH_DRAFT_LEGACY_STORAGE_KEY = "animeimages.batchParseDraft.v1";
    const BATCH_DRAFT_STORAGE_KEY = "animeimages.batchParseDraft.v2";
    const SHARED_VERSION = 1;
    const BATCH_DRAFT_VERSION = 2;
    const PREFERENCE_FIELDS = ["ultra_hd", "youtube_merge"];
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
        return {
            ultra_hd: typeof source.ultra_hd === "boolean" ? source.ultra_hd : false,
            youtube_merge: typeof source.youtube_merge === "boolean" ? source.youtube_merge : false,
        };
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

    function load(options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const defaults = preferenceDefaults(settings.defaults);
        const shared = validSharedFields(readStoredObject(storage, SHARED_STORAGE_KEY));
        const singleLegacy = readStoredObject(storage, SINGLE_LEGACY_STORAGE_KEY) || {};
        const batchLegacy = readStoredObject(storage, BATCH_DRAFT_LEGACY_STORAGE_KEY) || {};
        const preferences = {version: SHARED_VERSION};

        PREFERENCE_FIELDS.forEach((field) => {
            const singleLegacyField = SINGLE_LEGACY_FIELDS[field];
            if (typeof shared[field] === "boolean") {
                preferences[field] = shared[field];
            } else if (typeof singleLegacy[singleLegacyField] === "boolean") {
                preferences[field] = singleLegacy[singleLegacyField];
            } else if (typeof batchLegacy[field] === "boolean") {
                preferences[field] = batchLegacy[field];
            } else {
                preferences[field] = defaults[field];
            }
        });

        writeStoredObject(storage, SHARED_STORAGE_KEY, preferences);
        return preferences;
    }

    function update(patch, options) {
        const settings = isObject(options) ? options : {};
        const storage = getStorage(settings);
        const current = load(settings);
        const changes = isObject(patch) ? patch : {};
        const next = {
            version: SHARED_VERSION,
            ultra_hd: current.ultra_hd,
            youtube_merge: current.youtube_merge,
        };

        PREFERENCE_FIELDS.forEach((field) => {
            if (typeof changes[field] === "boolean") {
                next[field] = changes[field];
            }
        });
        writeStoredObject(storage, SHARED_STORAGE_KEY, next);
        return next;
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
            ultra_hd: false,
            youtube_merge: false,
        };
        const targets = isObject(controls) ? controls : {};

        function applyField(field, input, hiddenInput) {
            const enabled = Boolean(input && !input.disabled && values[field]);
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

    function subscribe(callback, options) {
        const settings = isObject(options) ? options : {};
        const eventTarget = settings.eventTarget || root;
        if (!eventTarget || typeof eventTarget.addEventListener !== "function") {
            return function () {};
        }
        const listener = function (event) {
            if (!event || event.key !== SHARED_STORAGE_KEY) {
                return;
            }
            const preferences = completeSharedPreferences(parseStoredObject(event.newValue));
            if (preferences && typeof callback === "function") {
                callback(preferences);
            }
        };
        eventTarget.addEventListener("storage", listener);
        return function () {
            if (typeof eventTarget.removeEventListener === "function") {
                eventTarget.removeEventListener("storage", listener);
            }
        };
    }

    return Object.freeze({
        SHARED_STORAGE_KEY,
        SINGLE_LEGACY_STORAGE_KEY,
        BATCH_DRAFT_LEGACY_STORAGE_KEY,
        BATCH_DRAFT_STORAGE_KEY,
        load,
        update,
        loadBatchDraft,
        saveBatchDraft,
        applyToControls,
        subscribe,
    });
});
