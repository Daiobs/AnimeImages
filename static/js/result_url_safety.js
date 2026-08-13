(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesResultUrlSafety = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function () {
    "use strict";

    function sameOriginPath(value, locationObject) {
        if (!value || !locationObject || !locationObject.origin) {
            return "";
        }
        try {
            const parsed = new URL(String(value), locationObject.origin);
            return parsed.origin === locationObject.origin ? parsed.pathname : "";
        } catch (error) {
            return "";
        }
    }

    function scrubServerRenderedResultUrl(options) {
        const settings = options && typeof options === "object" ? options : {};
        const locationObject = settings.locationObject;
        const historyObject = settings.historyObject;
        if (!locationObject || !historyObject || typeof historyObject.replaceState !== "function") {
            return false;
        }

        const resultsPath = sameOriginPath(settings.resultsUrl, locationObject);
        const homePath = sameOriginPath(settings.homeUrl, locationObject);
        if (!resultsPath || !homePath || locationObject.pathname !== resultsPath) {
            return false;
        }

        historyObject.replaceState(historyObject.state || {}, "", homePath);
        return true;
    }

    return Object.freeze({
        sameOriginPath,
        scrubServerRenderedResultUrl,
    });
}));
