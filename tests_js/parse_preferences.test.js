"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const preferences = require("../static/js/parse_preferences.js");

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

test("valid shared values win and storage is reduced to the public schema", () => {
    const storage = new MemoryStorage({
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            ultra_hd: false,
            youtube_merge: true,
            signed_url: "must-not-survive",
        }),
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: true,
            youtube_merge_enabled: false,
        }),
    });

    assert.deepEqual(preferences.load({storage}), {
        version: 1,
        ultra_hd: false,
        youtube_merge: true,
    });
    assert.deepEqual(Object.keys(storedJson(storage, preferences.SHARED_STORAGE_KEY)), [
        "version",
        "ultra_hd",
        "youtube_merge",
    ]);
});

test("legacy single-page booleans migrate before Batch v1 values", () => {
    const storage = new MemoryStorage({
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: true,
            youtube_merge_enabled: false,
        }),
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            links: "https://example.test/one",
            ultra_hd: false,
            youtube_merge: true,
        }),
    });

    assert.deepEqual(preferences.load({storage}), {
        version: 1,
        ultra_hd: true,
        youtube_merge: false,
    });
});

test("missing fields fall through new, single legacy, Batch v1, then defaults", () => {
    const storage = new MemoryStorage({
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            ultra_hd: false,
        }),
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            youtube_merge_enabled: "true",
        }),
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd: true,
            youtube_merge: true,
        }),
    });

    assert.deepEqual(preferences.load({
        storage,
        defaults: {ultra_hd: true, youtube_merge: false},
    }), {
        version: 1,
        ultra_hd: false,
        youtube_merge: true,
    });
});

test("invalid JSON and non-booleans safely use rendered defaults", () => {
    const storage = new MemoryStorage({
        [preferences.SHARED_STORAGE_KEY]: "{invalid",
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: 1,
            youtube_merge_enabled: null,
        }),
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd: "true",
            youtube_merge: 0,
        }),
    });

    assert.deepEqual(preferences.load({
        storage,
        defaults: {ultra_hd: true, youtube_merge: false},
    }), {
        version: 1,
        ultra_hd: true,
        youtube_merge: false,
    });
});

test("migration is idempotent and does not rewrite canonical values", () => {
    const storage = new MemoryStorage({
        [preferences.SINGLE_LEGACY_STORAGE_KEY]: JSON.stringify({
            ultra_hd_enabled: true,
            youtube_merge_enabled: false,
        }),
    });

    const first = preferences.load({storage});
    const writesAfterFirstLoad = storage.writeCount;
    const second = preferences.load({storage});

    assert.deepEqual(second, first);
    assert.equal(storage.writeCount, writesAfterFirstLoad);
});

test("updates accept only real booleans and preserve the other preference", () => {
    const storage = new MemoryStorage({
        [preferences.SHARED_STORAGE_KEY]: JSON.stringify({
            version: 1,
            ultra_hd: true,
            youtube_merge: false,
        }),
    });

    const result = preferences.update({
        ultra_hd: "false",
        youtube_merge: true,
    }, {storage});

    assert.deepEqual(result, {
        version: 1,
        ultra_hd: true,
        youtube_merge: true,
    });
});

test("Batch v1 links migrate to a v2 draft without preferences", () => {
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
    assert.deepEqual(Object.keys(storedJson(storage, preferences.BATCH_DRAFT_STORAGE_KEY)), [
        "version",
        "links",
    ]);
});

test("a valid Batch v2 draft wins and is sanitized to links only", () => {
    const storage = new MemoryStorage({
        [preferences.BATCH_DRAFT_STORAGE_KEY]: JSON.stringify({
            version: 2,
            links: "current links",
            ultra_hd: true,
            card_html: "must-not-survive",
        }),
        [preferences.BATCH_DRAFT_LEGACY_STORAGE_KEY]: JSON.stringify({links: "legacy links"}),
    });

    assert.deepEqual(preferences.loadBatchDraft({storage}), {
        version: 2,
        links: "current links",
    });
    assert.deepEqual(storedJson(storage, preferences.BATCH_DRAFT_STORAGE_KEY), {
        version: 2,
        links: "current links",
    });
});

test("saving a Batch draft cannot persist parse results or preferences", () => {
    const storage = new MemoryStorage();

    preferences.saveBatchDraft("two safe links", {storage});

    assert.deepEqual(storedJson(storage, preferences.BATCH_DRAFT_STORAGE_KEY), {
        version: 2,
        links: "two safe links",
    });
});

test("control application preserves intent while forcing disabled fields off", () => {
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
    assert.equal(ultraInput.checked, false);
    assert.equal(ultraHidden.value, "0");
    assert.equal(youtubeInput.checked, true);
    assert.equal(youtubeHidden.value, "1");
    assert.equal(intended.ultra_hd, true);
});

test("storage access failures never block rendered defaults or form controls", () => {
    const storage = {
        getItem() {
            throw new Error("blocked");
        },
        setItem() {
            throw new Error("blocked");
        },
    };

    assert.doesNotThrow(() => preferences.load({
        storage,
        defaults: {ultra_hd: true, youtube_merge: false},
    }));
    assert.deepEqual(preferences.load({
        storage,
        defaults: {ultra_hd: true, youtube_merge: false},
    }), {
        version: 1,
        ultra_hd: true,
        youtube_merge: false,
    });
});

test("cross-tab subscription accepts only the shared key with a complete schema", () => {
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
    const unsubscribe = preferences.subscribe((value) => received.push(value), {eventTarget});
    const listener = listeners.get("storage");

    listener({key: "another-key", newValue: JSON.stringify({version: 1, ultra_hd: true, youtube_merge: true})});
    listener({key: preferences.SHARED_STORAGE_KEY, newValue: JSON.stringify({version: 1, ultra_hd: "true", youtube_merge: true})});
    listener({key: preferences.SHARED_STORAGE_KEY, newValue: JSON.stringify({version: 1, ultra_hd: false, youtube_merge: true})});

    assert.deepEqual(received, [{version: 1, ultra_hd: false, youtube_merge: true}]);
    unsubscribe();
    assert.equal(listeners.has("storage"), false);
});
