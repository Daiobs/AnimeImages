"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const preferences = require("../static/js/parse_preferences.js");
const settingsUi = require("../static/js/project_settings.js");

class MemoryStorage {
    constructor(initialValues) {
        this.values = new Map(Object.entries(initialValues || {}));
        this.writeCount = 0;
    }

    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }

    setItem(key, value) {
        this.writeCount += 1;
        this.values.set(key, String(value));
    }
}

function storedJson(storage, key) {
    return JSON.parse(storage.getItem(key));
}

function projectSettings(overrides) {
    const values = overrides || {};
    return {
        version: 2,
        parsing: {
            ultra_hd: values.ultra_hd === undefined ? true : values.ultra_hd,
            youtube_merge: values.youtube_merge === undefined
                ? false
                : values.youtube_merge,
        },
        download: {
            location_mode: values.location_mode || "browser_default",
        },
    };
}

test("new users default to Ultra on, YouTube merge off, and browser downloads", () => {
    const storage = new MemoryStorage();

    const result = preferences.loadProjectSettings({storage});

    assert.deepEqual(result, projectSettings());
    assert.deepEqual(storedJson(storage, preferences.PROJECT_STORAGE_KEY), result);
});

test("valid project settings win and are sanitized to the v2 public schema", () => {
    const storage = new MemoryStorage({
        [preferences.PROJECT_STORAGE_KEY]: JSON.stringify({
            version: 2,
            parsing: {
                ultra_hd: false,
                youtube_merge: true,
                provider_order: ["private"],
            },
            download: {
                location_mode: "ask_when_supported",
                signed_url: "must-not-survive",
            },
            manifest: "must-not-survive",
        }),
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            ultra_hd: true,
            youtube_merge: false,
        }),
    });

    const result = preferences.loadProjectSettings({storage});

    assert.deepEqual(result, projectSettings({
        ultra_hd: false,
        youtube_merge: true,
        location_mode: "ask_when_supported",
    }));
    assert.deepEqual(storedJson(storage, preferences.PROJECT_STORAGE_KEY), result);
});

test("existing shared true and false values are preserved during v2 migration", () => {
    const storage = new MemoryStorage({
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            ultra_hd: false,
            youtube_merge: true,
        }),
    });

    assert.deepEqual(preferences.loadProjectSettings({storage}), projectSettings({
        ultra_hd: false,
        youtube_merge: true,
    }));
});

test("legacy single-page settings migrate before Batch values", () => {
    const storage = new MemoryStorage({
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: true,
            youtube_merge_enabled: false,
            use_file_picker: true,
        }),
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            links: "https://example.test/one",
            ultra_hd: false,
            youtube_merge: true,
        }),
    });

    assert.deepEqual(preferences.loadProjectSettings({storage}), projectSettings({
        ultra_hd: true,
        youtube_merge: false,
        location_mode: "ask_when_supported",
    }));
});

test("legacy Batch preferences migrate when no newer preference exists", () => {
    const storage = new MemoryStorage({
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            links: "legacy links",
            ultra_hd: false,
            youtube_merge: true,
        }),
    });

    assert.deepEqual(preferences.loadProjectSettings({storage}), projectSettings({
        ultra_hd: false,
        youtube_merge: true,
    }));
});

test("legacy file-picker false maps to browser_default", () => {
    const storage = new MemoryStorage({
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            use_file_picker: false,
        }),
    });

    assert.equal(
        preferences.loadProjectSettings({storage}).download.location_mode,
        "browser_default",
    );
});

test("missing and invalid fields fall through each legacy source then defaults", () => {
    const storage = new MemoryStorage({
        [preferences.PROJECT_STORAGE_KEY]: JSON.stringify({
            version: 2,
            parsing: {ultra_hd: false},
            download: {location_mode: "invalid"},
        }),
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            youtube_merge: "true",
        }),
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            youtube_merge_enabled: true,
            use_file_picker: "true",
        }),
    });

    assert.deepEqual(preferences.loadProjectSettings({
        storage,
        defaults: {
            parsing: {ultra_hd: true, youtube_merge: false},
            download: {location_mode: "ask_when_supported"},
        },
    }), projectSettings({
        ultra_hd: false,
        youtube_merge: true,
        location_mode: "ask_when_supported",
    }));
});

test("migration is idempotent and does not rewrite canonical settings", () => {
    const storage = new MemoryStorage({
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: false,
            youtube_merge_enabled: true,
            use_file_picker: true,
        }),
    });

    const first = preferences.loadProjectSettings({storage});
    const writesAfterFirstLoad = storage.writeCount;
    const second = preferences.loadProjectSettings({storage});

    assert.deepEqual(second, first);
    assert.equal(storage.writeCount, writesAfterFirstLoad);
});

test("project updates accept typed fields and preserve omitted intent", () => {
    const storage = new MemoryStorage({
        [preferences.PROJECT_STORAGE_KEY]: JSON.stringify(projectSettings({
            ultra_hd: false,
            youtube_merge: false,
            location_mode: "browser_default",
        })),
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            save_dir: "legacy-path",
        }),
    });

    const result = preferences.updateProjectSettings({
        parsing: {ultra_hd: "true", youtube_merge: true},
        download: {location_mode: "ask_when_supported"},
    }, {storage});

    assert.deepEqual(result, projectSettings({
        ultra_hd: false,
        youtube_merge: true,
        location_mode: "ask_when_supported",
    }));
    assert.deepEqual(storedJson(storage, preferences.SHARED_STORAGE_KEY), {
        version: 1,
        ultra_hd: false,
        youtube_merge: true,
    });
    assert.deepEqual(storedJson(storage, preferences.SINGLE_LEGACY_STORAGE_KEY), {
        save_dir: "legacy-path",
        ultra_hd_enabled: false,
        youtube_merge_enabled: true,
        use_file_picker: true,
    });
});

test("compatibility parsing API reads and writes the same project contract", () => {
    const storage = new MemoryStorage();

    const saved = preferences.update({ultra_hd: false}, {storage});

    assert.deepEqual(saved, {
        version: 1,
        ultra_hd: false,
        youtube_merge: false,
    });
    assert.deepEqual(preferences.load({storage}), saved);
    assert.equal(
        storedJson(storage, preferences.PROJECT_STORAGE_KEY).parsing.ultra_hd,
        false,
    );
});

test("Batch v1 links migrate to a v2 links-only draft", () => {
    const storage = new MemoryStorage({
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            links: "https://example.test/one\nhttps://example.test/two",
            ultra_hd: true,
            youtube_merge: true,
            run_token: "must-not-migrate",
        }),
    });

    const result = preferences.loadBatchDraft({storage});

    assert.deepEqual(result, {
        version: 2,
        links: "https://example.test/one\nhttps://example.test/two",
    });
    assert.deepEqual(Object.keys(storedJson(
        storage,
        preferences.BATCH_DRAFT_STORAGE_KEY,
    )), ["version", "links"]);
});

test("saving a Batch draft cannot persist preferences or parsed media", () => {
    const storage = new MemoryStorage();

    preferences.saveBatchDraft("two safe links", {storage});

    assert.deepEqual(storedJson(storage, preferences.BATCH_DRAFT_STORAGE_KEY), {
        version: 2,
        links: "two safe links",
    });
    const serialized = storage.getItem(preferences.BATCH_DRAFT_STORAGE_KEY);
    assert.doesNotMatch(serialized, /ultra_hd|youtube_merge|signed|manifest|card_html/);
});

test("control application preserves stored intent while disabled fields submit zero", () => {
    const ultraInput = {checked: false, disabled: true};
    const youtubeInput = {checked: false, disabled: false};
    const ultraHidden = {value: "1"};
    const youtubeHidden = {value: "0"};
    const intended = {version: 1, ultra_hd: true, youtube_merge: true};

    const effective = preferences.applyToControls(intended, {
        ultraHdInput: ultraInput,
        ultraHdHiddenInput: ultraHidden,
        youtubeMergeInput: youtubeInput,
        youtubeMergeHiddenInput: youtubeHidden,
    });

    assert.deepEqual(effective, {ultra_hd: false, youtube_merge: true});
    assert.equal(ultraHidden.value, "0");
    assert.equal(youtubeHidden.value, "1");
    assert.equal(intended.ultra_hd, true);
});

test("the shared Settings save patch omits disabled parse fields", () => {
    const patch = settingsUi.buildEnabledProjectPatch({
        ultraInput: {checked: false, disabled: true},
        youtubeInput: {checked: true, disabled: false},
        locationInputs: [
            {checked: false, value: "browser_default"},
            {checked: true, value: "ask_when_supported"},
        ],
    });

    assert.deepEqual(patch, {
        parsing: {youtube_merge: true},
        download: {location_mode: "ask_when_supported"},
    });
});

test("storage access failures never block rendered project defaults", () => {
    const storage = {
        getItem() {
            throw new Error("blocked");
        },
        setItem() {
            throw new Error("blocked");
        },
    };

    assert.doesNotThrow(() => preferences.loadProjectSettings({storage}));
    assert.deepEqual(preferences.loadProjectSettings({storage}), projectSettings());
});

test("cross-tab subscription accepts only complete v2 settings events", () => {
    const listeners = new Map();
    const eventTarget = {
        addEventListener(name, listener) {
            listeners.set(name, listener);
        },
        removeEventListener(name) {
            listeners.delete(name);
        },
    };
    const received = [];
    const unsubscribe = preferences.subscribeProjectSettings(
        (value) => received.push(value),
        {eventTarget},
    );
    const listener = listeners.get("storage");

    listener({key: "another-key", newValue: JSON.stringify(projectSettings())});
    listener({
        key: preferences.PROJECT_STORAGE_KEY,
        newValue: JSON.stringify({version: 2, parsing: {ultra_hd: true}}),
    });
    listener({
        key: preferences.PROJECT_STORAGE_KEY,
        newValue: JSON.stringify(projectSettings({
            ultra_hd: false,
            youtube_merge: true,
            location_mode: "ask_when_supported",
        })),
    });

    assert.deepEqual(received, [projectSettings({
        ultra_hd: false,
        youtube_merge: true,
        location_mode: "ask_when_supported",
    })]);
    unsubscribe();
    assert.equal(listeners.has("storage"), false);
});

test("modal data parsing keeps the documented boolean defaults", () => {
    assert.equal(settingsUi.booleanData("true", false), true);
    assert.equal(settingsUi.booleanData("false", true), false);
    assert.equal(settingsUi.booleanData(undefined, true), true);
    assert.equal(settingsUi.booleanData(undefined, false), false);
});
