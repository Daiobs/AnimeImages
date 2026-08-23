(function (root, factory) {
    const protocol = root && root.AnimeImagesOrdinaryMediaStreamingProtocol
        ? root.AnimeImagesOrdinaryMediaStreamingProtocol
        : (typeof module === "object" && module.exports
            ? require("./ordinary_media_streaming_protocol.js")
            : null);
    const api = factory(protocol);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.AnimeImagesOrdinaryMediaStreaming = api;
    }
}(typeof window !== "undefined" ? window : globalThis, function (protocol) {
    "use strict";

    const DEFAULT_WORKER_URL = "/ordinary-media-download-sw.js";
    const DEFAULT_WORKER_SCOPE = "/";
    const DEFAULT_START_TIMEOUT_MS = 8000;
    const DEFAULT_MESSAGE_TIMEOUT_MS = 4000;
    const DEFAULT_STATUS_POLL_MS = 1000;
    const DEFAULT_TELEMETRY_ENDPOINT = "/media/browser-download-telemetry/";
    const TELEMETRY_SCHEMA_VERSION = 1;
    const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
    const FAILURE_TELEMETRY_STATES = new Set(["failed", "cancelled"]);
    const SAFE_WORKER_STATES = new Set([
        "activated",
        "activating",
        "installed",
        "installing",
        "redundant",
        "none",
        "unknown",
    ]);
    const STATE_ORDER = Object.freeze({
        prepared: 0,
        fetching: 1,
        completed: 2,
        failed: 2,
        cancelled: 2,
    });

    function streamingError(errorCode, message, options) {
        const settings = options && typeof options === "object" ? options : {};
        const error = new Error(message || errorCode);
        error.errorCode = errorCode;
        error.streamingStarted = Boolean(settings.streamingStarted);
        error.status = settings.status || null;
        return error;
    }

    function normalizeWorkerState(value) {
        const state = String(value || "").toLowerCase();
        return SAFE_WORKER_STATES.has(state) ? state : (state ? "unknown" : "none");
    }

    function normalizePageVisibility(value) {
        const visibility = String(value || "").toLowerCase();
        return ["visible", "hidden", "prerender"].includes(visibility)
            ? visibility
            : "unknown";
    }

    function detectBrowserFamily(navigatorObject) {
        const userAgent = String(navigatorObject && navigatorObject.userAgent || "");
        if (/Firefox\//i.test(userAgent)) {
            return "firefox";
        }
        if (/Safari\//i.test(userAgent) && !/(?:Chrome|Chromium|CriOS|Edg|OPR)\//i.test(userAgent)) {
            return "safari";
        }
        if (/(?:Chrome|Chromium|CriOS|Edg|OPR)\//i.test(userAgent)) {
            return "chromium";
        }
        return "unknown";
    }

    function readCookie(documentObject, name) {
        const prefix = `${String(name || "")}=`;
        const cookies = String(documentObject && documentObject.cookie || "").split(";");
        for (const rawCookie of cookies) {
            const cookie = rawCookie.trim();
            if (cookie.startsWith(prefix)) {
                try {
                    return decodeURIComponent(cookie.slice(prefix.length));
                } catch (error) {
                    return "";
                }
            }
        }
        return "";
    }

    function hasCompleteFinalRedirectObservation(status) {
        if (!status || typeof status !== "object") {
            return false;
        }
        return [
            "initialMediaFamily",
            "observedFinalHost",
            "observedFinalHostClass",
            "redirected",
        ].every((field) => Object.prototype.hasOwnProperty.call(status, field));
    }

    function createTerminalTelemetryPayload(status, environment, options) {
        const safe = protocol.safeSnapshot(status);
        const env = environment && typeof environment === "object" ? environment : {};
        const settings = options && typeof options === "object" ? options : {};
        const telemetryId = protocol.normalizeDownloadId(settings.telemetryId)
            || safe.downloadId;
        const payload = {
            schema_version: TELEMETRY_SCHEMA_VERSION,
            telemetry_id: telemetryId,
            download_id: safe.downloadId,
            provider: safe.provider,
            media_host: safe.mediaHost,
            transport: "browser_streaming",
            state: safe.state,
            error_code: safe.errorCode,
            failure_class: safe.failureClass,
            failure_stage: safe.failureStage || "PRE_START_FAILURE",
            streaming_started: Boolean(safe.streamingStarted),
            expected_bytes: safe.expectedBytes,
            received_bytes: safe.receivedBytes,
            total_bytes: safe.totalBytes,
            registration_to_fetch_ms: safe.registrationToFetchMs,
            fetch_reject_ms: safe.fetchRejectMs,
            time_to_first_chunk_ms: safe.timeToFirstChunkMs,
            stream_duration_ms: safe.streamDurationMs,
            navigator_online: Boolean(env.navigatorOnline),
            page_visibility: normalizePageVisibility(env.pageVisibility),
            worker_controller_state: normalizeWorkerState(env.workerControllerState),
            worker_active_state: normalizeWorkerState(env.workerActiveState),
            worker_waiting: Boolean(env.workerWaiting),
            worker_installing: Boolean(env.workerInstalling),
            protocol_version: safe.protocolVersion,
            browser_family: ["chromium", "firefox", "safari"].includes(env.browserFamily)
                ? env.browserFamily
                : "unknown",
        };
        if (safe.responseRejectReason) {
            Object.assign(payload, {
                response_reject_reason: safe.responseRejectReason,
                http_status: safe.httpStatus,
                content_type_class: safe.contentTypeClass,
                final_media_host: safe.finalMediaHost,
            });
            if (hasCompleteFinalRedirectObservation(status)) {
                Object.assign(payload, {
                    initial_media_host: safe.mediaHost,
                    initial_media_family: safe.initialMediaFamily,
                    observed_final_host: safe.observedFinalHost,
                    observed_final_host_class: safe.observedFinalHostClass,
                    redirected: safe.redirected,
                });
            }
        }
        return Object.freeze(payload);
    }

    function createTerminalTelemetryReporter(options) {
        const settings = options && typeof options === "object" ? options : {};
        const fetchImpl = settings.fetchImpl || (
            typeof fetch === "function" ? fetch.bind(globalThis) : null
        );
        const documentObject = settings.documentObject || (
            typeof document !== "undefined" ? document : null
        );
        const cryptoObject = settings.cryptoObject || (
            typeof crypto !== "undefined" ? crypto : null
        );
        const endpoint = String(settings.endpoint || DEFAULT_TELEMETRY_ENDPOINT);
        const reportedDownloadIds = new Set();

        function report(status, environment) {
            const safe = protocol.safeSnapshot(status);
            if (
                !safe.downloadId
                || !FAILURE_TELEMETRY_STATES.has(safe.state)
                || reportedDownloadIds.has(safe.downloadId)
            ) {
                return false;
            }
            reportedDownloadIds.add(safe.downloadId);

            const csrfToken = String(
                settings.csrfToken || readCookie(documentObject, "csrftoken") || "",
            );
            if (!fetchImpl || !csrfToken) {
                return false;
            }
            let telemetryId = safe.downloadId;
            try {
                telemetryId = protocol.randomDownloadId(cryptoObject);
            } catch (error) {
                // The opaque download ID is already a valid non-sensitive fallback.
            }
            const payload = createTerminalTelemetryPayload(status, environment, {telemetryId});
            try {
                const pending = fetchImpl(endpoint, {
                    method: "POST",
                    credentials: "same-origin",
                    cache: "no-store",
                    keepalive: true,
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrfToken,
                    },
                    body: JSON.stringify(payload),
                });
                if (pending && typeof pending.catch === "function") {
                    pending.catch(() => {});
                }
            } catch (error) {
                // Telemetry is best-effort and never participates in download control flow.
            }
            return true;
        }

        return Object.freeze({report});
    }

    function supportsStreamingEnvironment(environment) {
        const env = environment && typeof environment === "object" ? environment : {};
        const navigatorObject = env.navigatorObject || (
            typeof navigator !== "undefined" ? navigator : null
        );
        const cryptoObject = env.cryptoObject || (
            typeof crypto !== "undefined" ? crypto : null
        );
        const MessageChannelConstructor = env.MessageChannelConstructor || (
            typeof MessageChannel !== "undefined" ? MessageChannel : null
        );
        return Boolean(
            protocol
            && navigatorObject
            && navigatorObject.serviceWorker
            && typeof navigatorObject.serviceWorker.register === "function"
            && cryptoObject
            && typeof cryptoObject.getRandomValues === "function"
            && MessageChannelConstructor
        );
    }

    function waitForWorkerActivation(registration, options) {
        const settings = options && typeof options === "object" ? options : {};
        const timeoutMs = Number(settings.timeoutMs || DEFAULT_START_TIMEOUT_MS);
        const schedule = settings.schedule || setTimeout;
        const clearSchedule = settings.clearSchedule || clearTimeout;
        if (registration && registration.active) {
            return Promise.resolve(registration.active);
        }
        const candidate = registration && (registration.installing || registration.waiting);
        if (!candidate) {
            return Promise.reject(streamingError(
                "STREAMING_WORKER_NOT_AVAILABLE",
                "浏览器流式下载服务尚未就绪。",
            ));
        }
        return new Promise((resolve, reject) => {
            let settled = false;
            const finish = (callback, value) => {
                if (settled) {
                    return;
                }
                settled = true;
                clearSchedule(timer);
                if (typeof candidate.removeEventListener === "function") {
                    candidate.removeEventListener("statechange", handleStateChange);
                }
                callback(value);
            };
            const handleStateChange = () => {
                if (candidate.state === "activated") {
                    finish(resolve, registration.active || candidate);
                } else if (candidate.state === "redundant") {
                    finish(reject, streamingError(
                        "STREAMING_WORKER_ACTIVATION_FAILED",
                        "浏览器流式下载服务启动失败。",
                    ));
                }
            };
            const timer = schedule(() => finish(reject, streamingError(
                "STREAMING_WORKER_ACTIVATION_TIMEOUT",
                "浏览器流式下载服务启动超时。",
            )), timeoutMs);
            if (typeof candidate.addEventListener === "function") {
                candidate.addEventListener("statechange", handleStateChange);
            }
            handleStateChange();
        });
    }

    function waitForWorkerControl(navigatorObject, options) {
        const settings = options && typeof options === "object" ? options : {};
        const serviceWorker = navigatorObject && navigatorObject.serviceWorker;
        const timeoutMs = Number(settings.timeoutMs || DEFAULT_START_TIMEOUT_MS);
        const schedule = settings.schedule || setTimeout;
        const clearSchedule = settings.clearSchedule || clearTimeout;
        if (serviceWorker && serviceWorker.controller) {
            return Promise.resolve(serviceWorker.controller);
        }
        if (!serviceWorker || typeof serviceWorker.addEventListener !== "function") {
            return Promise.reject(streamingError(
                "STREAMING_PAGE_NOT_CONTROLLED",
                "浏览器流式下载服务尚未接管当前页面。",
            ));
        }
        return new Promise((resolve, reject) => {
            let settled = false;
            const finish = (callback, value) => {
                if (settled) {
                    return;
                }
                settled = true;
                clearSchedule(timer);
                serviceWorker.removeEventListener("controllerchange", handleControllerChange);
                callback(value);
            };
            const handleControllerChange = () => {
                if (serviceWorker.controller) {
                    finish(resolve, serviceWorker.controller);
                }
            };
            const timer = schedule(() => finish(reject, streamingError(
                "STREAMING_PAGE_CONTROL_TIMEOUT",
                "浏览器流式下载服务接管页面超时。",
            )), timeoutMs);
            serviceWorker.addEventListener("controllerchange", handleControllerChange);
            handleControllerChange();
        });
    }

    function createStreamingClient(options) {
        const settings = options && typeof options === "object" ? options : {};
        const navigatorObject = settings.navigatorObject || (
            typeof navigator !== "undefined" ? navigator : null
        );
        const documentObject = settings.documentObject || (
            typeof document !== "undefined" ? document : null
        );
        const locationObject = settings.locationObject || (
            typeof location !== "undefined" ? location : null
        );
        const cryptoObject = settings.cryptoObject || (
            typeof crypto !== "undefined" ? crypto : null
        );
        const MessageChannelConstructor = settings.MessageChannelConstructor || (
            typeof MessageChannel !== "undefined" ? MessageChannel : null
        );
        const schedule = settings.schedule || setTimeout;
        const clearSchedule = settings.clearSchedule || clearTimeout;
        const workerUrl = String(settings.workerUrl || DEFAULT_WORKER_URL);
        const workerScope = String(settings.workerScope || DEFAULT_WORKER_SCOPE);
        const startTimeoutMs = Number(settings.startTimeoutMs || DEFAULT_START_TIMEOUT_MS);
        const messageTimeoutMs = Number(settings.messageTimeoutMs || DEFAULT_MESSAGE_TIMEOUT_MS);
        const statusPollMs = Number(settings.statusPollMs || DEFAULT_STATUS_POLL_MS);
        const telemetryReporter = settings.telemetryReporter || createTerminalTelemetryReporter({
            csrfToken: settings.csrfToken,
            cryptoObject,
            documentObject,
            endpoint: settings.telemetryEndpoint,
            fetchImpl: settings.telemetryFetchImpl,
        });
        const listeners = new Map();
        const allListeners = new Set();
        const latestStatuses = new Map();
        const navigationFrames = new Map();
        let registrationPromise = null;
        let currentRegistration = null;
        let messageListenerAttached = false;

        function cleanupNavigationFrame(downloadId) {
            const frame = navigationFrames.get(downloadId);
            if (!frame) {
                return;
            }
            navigationFrames.delete(downloadId);
            if (typeof frame.remove === "function") {
                frame.remove();
            }
        }

        function capabilityAvailable() {
            return supportsStreamingEnvironment({
                navigatorObject,
                cryptoObject,
                MessageChannelConstructor,
            });
        }

        function emit(status, emitOptions) {
            if (!status || !status.downloadId) {
                return null;
            }
            const shouldReportTelemetry = !emitOptions
                || emitOptions.reportTelemetry !== false;
            const safe = protocol.safeSnapshot(status);
            const previous = latestStatuses.get(safe.downloadId);
            if (previous) {
                const previousOrder = STATE_ORDER[previous.state] || 0;
                const nextOrder = STATE_ORDER[safe.state] || 0;
                const staleTimestamp = safe.updatedAt < previous.updatedAt;
                const staleBytes = (
                    safe.updatedAt === previous.updatedAt
                    && safe.receivedBytes < previous.receivedBytes
                );
                const staleState = (
                    safe.updatedAt === previous.updatedAt
                    && safe.receivedBytes === previous.receivedBytes
                    && nextOrder < previousOrder
                );
                const terminalRegression = (
                    TERMINAL_STATES.has(previous.state)
                    && !TERMINAL_STATES.has(safe.state)
                );
                if (staleTimestamp || staleBytes || staleState || terminalRegression) {
                    return previous;
                }
            }
            latestStatuses.set(safe.downloadId, safe);
            if (TERMINAL_STATES.has(safe.state)) {
                cleanupNavigationFrame(safe.downloadId);
            }
            if (
                shouldReportTelemetry
                && FAILURE_TELEMETRY_STATES.has(safe.state)
            ) {
                const serviceWorker = navigatorObject && navigatorObject.serviceWorker;
                const controller = serviceWorker && serviceWorker.controller;
                try {
                    telemetryReporter.report(safe, {
                        navigatorOnline: Boolean(navigatorObject && navigatorObject.onLine),
                        pageVisibility: documentObject && documentObject.visibilityState,
                        workerControllerState: controller && controller.state,
                        workerActiveState: currentRegistration
                            && currentRegistration.active
                            && currentRegistration.active.state,
                        workerWaiting: Boolean(currentRegistration && currentRegistration.waiting),
                        workerInstalling: Boolean(
                            currentRegistration && currentRegistration.installing
                        ),
                        browserFamily: detectBrowserFamily(navigatorObject),
                    });
                } catch (error) {
                    // A diagnostic path must not affect status delivery or fallback decisions.
                }
            }
            allListeners.forEach((callback) => callback(safe));
            const callbacks = listeners.get(safe.downloadId);
            if (callbacks) {
                callbacks.forEach((callback) => callback(safe));
            }
            return safe;
        }

        function handleWorkerMessage(event) {
            const message = event && event.data && typeof event.data === "object"
                ? event.data
                : {};
            if (message.status) {
                emit(message.status, {
                    reportTelemetry: message.telemetryOwner !== false,
                });
            }
        }

        function attachMessageListener() {
            if (
                messageListenerAttached
                || !navigatorObject
                || !navigatorObject.serviceWorker
                || typeof navigatorObject.serviceWorker.addEventListener !== "function"
            ) {
                return;
            }
            messageListenerAttached = true;
            navigatorObject.serviceWorker.addEventListener("message", handleWorkerMessage);
        }

        async function registration() {
            if (!capabilityAvailable()) {
                throw streamingError(
                    "STREAMING_CAPABILITY_UNAVAILABLE",
                    "当前浏览器不支持流式下载。",
                );
            }
            if (!registrationPromise) {
                registrationPromise = (async () => {
                    let registered;
                    try {
                        registered = await navigatorObject.serviceWorker.register(workerUrl, {
                            scope: workerScope,
                            updateViaCache: "none",
                        });
                    } catch (error) {
                        throw streamingError(
                            "STREAMING_REGISTRATION_FAILED",
                            "浏览器流式下载服务注册失败。",
                        );
                    }
                    currentRegistration = registered;
                    const active = await waitForWorkerActivation(registered, {
                        timeoutMs: startTimeoutMs,
                        schedule,
                        clearSchedule,
                    });
                    const controller = await waitForWorkerControl(navigatorObject, {
                        timeoutMs: startTimeoutMs,
                        schedule,
                        clearSchedule,
                    });
                    attachMessageListener();
                    return {registration: registered, active, controller};
                })().catch((error) => {
                    registrationPromise = null;
                    throw error;
                });
            }
            return registrationPromise;
        }

        async function postMessage(message) {
            const registered = await registration();
            const worker = registered.controller
                || navigatorObject.serviceWorker.controller
                || registered.registration.active
                || registered.active;
            if (!worker || typeof worker.postMessage !== "function") {
                throw streamingError(
                    "STREAMING_WORKER_NOT_ACTIVE",
                    "浏览器流式下载服务尚未就绪。",
                );
            }
            return new Promise((resolve, reject) => {
                const channel = new MessageChannelConstructor();
                const timer = schedule(() => reject(streamingError(
                    "STREAMING_MESSAGE_TIMEOUT",
                    "浏览器流式下载服务响应超时。",
                )), messageTimeoutMs);
                channel.port1.onmessage = (event) => {
                    clearSchedule(timer);
                    const response = event && event.data && typeof event.data === "object"
                        ? event.data
                        : {};
                    if (response.status) {
                        emit(response.status, {
                            reportTelemetry: response.telemetryOwner !== false,
                        });
                    }
                    resolve(response);
                };
                worker.postMessage(message, [channel.port2]);
            });
        }

        function subscribe(downloadId, callback) {
            const id = protocol.normalizeDownloadId(downloadId);
            if (!id || typeof callback !== "function") {
                return () => {};
            }
            if (!listeners.has(id)) {
                listeners.set(id, new Set());
            }
            listeners.get(id).add(callback);
            if (latestStatuses.has(id)) {
                callback(latestStatuses.get(id));
            }
            return () => {
                const callbacks = listeners.get(id);
                if (!callbacks) {
                    return;
                }
                callbacks.delete(callback);
                if (!callbacks.size) {
                    listeners.delete(id);
                }
            };
        }

        function subscribeAll(callback) {
            if (typeof callback !== "function") {
                return () => {};
            }
            allListeners.add(callback);
            return () => allListeners.delete(callback);
        }

        function launchNavigation(downloadId) {
            if (!documentObject || !documentObject.body) {
                throw streamingError(
                    "STREAMING_NAVIGATION_UNAVAILABLE",
                    "浏览器无法建立下载任务。",
                );
            }
            const frame = documentObject.createElement("iframe");
            frame.name = `animeimages-download-${downloadId}`;
            frame.hidden = true;
            frame.style.display = "none";
            documentObject.body.appendChild(frame);
            navigationFrames.set(downloadId, frame);
            const link = documentObject.createElement("a");
            link.href = protocol.downloadPath(downloadId, workerScope);
            link.target = frame.name;
            link.style.display = "none";
            documentObject.body.appendChild(link);
            try {
                link.click();
            } catch (error) {
                cleanupNavigationFrame(downloadId);
                throw error;
            } finally {
                link.remove();
            }
        }

        async function getStatus(downloadId) {
            const response = await postMessage({
                type: "GET_DOWNLOAD_STATUS",
                downloadId,
            });
            if (!response.ok || !response.status) {
                throw streamingError(
                    response.code || "STREAMING_STATUS_UNAVAILABLE",
                    "无法读取浏览器下载状态。",
                );
            }
            return latestStatuses.get(protocol.normalizeDownloadId(downloadId))
                || protocol.safeSnapshot(response.status);
        }

        async function listActiveDownloads() {
            if (!capabilityAvailable()) {
                return [];
            }
            try {
                const response = await postMessage({type: "LIST_ACTIVE_DOWNLOADS"});
                if (!response.ok || !Array.isArray(response.statuses)) {
                    return [];
                }
                return response.statuses.map(emit).filter(Boolean);
            } catch (error) {
                return [];
            }
        }

        async function cancel(downloadId) {
            const response = await postMessage({
                type: "CANCEL_DOWNLOAD",
                downloadId,
            });
            if (!response.ok) {
                throw streamingError(
                    response.code || "STREAMING_CANCEL_FAILED",
                    "取消下载失败。",
                    {streamingStarted: true},
                );
            }
            return response.status ? protocol.safeSnapshot(response.status) : null;
        }

        async function deletePrepared(downloadId) {
            const response = await postMessage({type: "DELETE_DOWNLOAD", downloadId});
            if (
                (response && response.code === "DOWNLOAD_ALREADY_STARTED")
                || (response && response.status && response.status.streamingStarted)
            ) {
                throw streamingError(
                    "DOWNLOAD_ALREADY_STARTED",
                    "浏览器下载已经开始。",
                    {streamingStarted: true, status: response.status || null},
                );
            }
            return Boolean(response && response.ok && response.deleted);
        }

        function waitForTerminal(downloadId, onProgress) {
            return new Promise((resolve, reject) => {
                let pollTimer = null;
                let finished = false;
                let unsubscribe = () => {};
                const finish = (callback, value) => {
                    if (finished) {
                        return;
                    }
                    finished = true;
                    if (pollTimer !== null) {
                        clearSchedule(pollTimer);
                    }
                    unsubscribe();
                    callback(value);
                };
                const handleStatus = (status) => {
                    if (typeof onProgress === "function") {
                        onProgress(status);
                    }
                    if (status.state === "completed") {
                        finish(resolve, status);
                    } else if (status.state === "failed" || status.state === "cancelled") {
                        finish(reject, streamingError(
                            status.errorCode || `STREAMING_${status.state.toUpperCase()}`,
                            status.state === "cancelled" ? "下载已取消。" : "浏览器流式下载失败。",
                            {streamingStarted: true, status},
                        ));
                    }
                };
                const poll = async () => {
                    if (finished) {
                        return;
                    }
                    try {
                        handleStatus(await getStatus(downloadId));
                    } catch (error) {
                        // A worker message may still complete the waiter.
                    }
                    if (!finished) {
                        pollTimer = schedule(poll, statusPollMs);
                    }
                };
                const subscribedUnsubscribe = subscribe(downloadId, handleStatus);
                unsubscribe = subscribedUnsubscribe;
                if (finished) {
                    unsubscribe();
                    return;
                }
                pollTimer = schedule(poll, statusPollMs);
            });
        }

        async function start(item, options) {
            const startOptions = options && typeof options === "object" ? options : {};
            const onProgress = typeof startOptions.onProgress === "function"
                ? startOptions.onProgress
                : (() => {});
            const downloadId = protocol.randomDownloadId(cryptoObject);
            const descriptor = protocol.createDescriptor({
                downloadId,
                url: item && (item.url || item.video_url),
                filename: startOptions.filename || (item && item.filename),
                provider: item && item.provider,
                expectedBytes: item && item.size_is_estimate
                    ? 0
                    : (item && (item.size_bytes || item.sizeBytes)),
            }, {
                allowLoopbackHttp: Boolean(
                    locationObject
                    && protocol.isLoopbackHostname(locationObject.hostname)
                ),
            });
            let registered = false;
            let streamingStarted = false;
            let navigationAttempted = false;
            let preparedDescriptorDeleted = false;
            let stopStartWaiter = () => {};
            function createStartWaiter() {
                return new Promise((resolve, reject) => {
                    let settled = false;
                    let unsubscribe = () => {};
                    const finish = (callback, value) => {
                        if (settled) {
                            return;
                        }
                        clearSchedule(timer);
                        settled = true;
                        unsubscribe();
                        callback(value);
                    };
                    const timer = schedule(async () => {
                        try {
                            const status = await getStatus(downloadId);
                            if (
                                status.streamingStarted
                                || status.state === "fetching"
                                || status.state === "completed"
                            ) {
                                streamingStarted = true;
                                finish(resolve, status);
                                return;
                            }
                            const deleted = await deletePrepared(downloadId);
                            preparedDescriptorDeleted = deleted;
                            if (!deleted && navigationAttempted) {
                                throw streamingError(
                                    "STREAMING_START_STATE_UNKNOWN",
                                    "无法确认浏览器下载任务状态。",
                                    {streamingStarted: true},
                                );
                            }
                            finish(reject, streamingError(
                                "STREAMING_START_TIMEOUT",
                                "浏览器下载任务未能启动。",
                            ));
                        } catch (error) {
                            finish(reject, (
                                error && error.streamingStarted
                                    ? error
                                    : streamingError(
                                        "STREAMING_START_STATE_UNKNOWN",
                                        "无法确认浏览器下载任务状态。",
                                        {streamingStarted: navigationAttempted},
                                    )
                            ));
                        }
                    }, startTimeoutMs);
                    const subscribedUnsubscribe = subscribe(downloadId, (status) => {
                        onProgress(status);
                        if (
                            status.state === "cancelled"
                            && status.errorCode === "DESCRIPTOR_DELETED"
                            && !status.streamingStarted
                        ) {
                            return;
                        }
                        if (
                            status.streamingStarted
                            || status.state === "fetching"
                            || status.state === "completed"
                        ) {
                            streamingStarted = true;
                            finish(resolve, status);
                        } else if (status.state === "failed" || status.state === "cancelled") {
                            finish(reject, streamingError(
                                status.errorCode || "STREAMING_START_FAILED",
                                "浏览器下载任务启动失败。",
                                {
                                    streamingStarted: (
                                        streamingStarted || Boolean(status.streamingStarted)
                                    ),
                                    status,
                                },
                            ));
                        }
                    });
                    unsubscribe = subscribedUnsubscribe;
                    if (settled) {
                        unsubscribe();
                    }
                    stopStartWaiter = () => {
                        clearSchedule(timer);
                        unsubscribe();
                    };
                });
            }

            try {
                const response = await postMessage({
                    type: "REGISTER_DOWNLOAD",
                    descriptor,
                });
                if (!response.ok) {
                    throw streamingError(
                        response.code || "STREAMING_DESCRIPTOR_REJECTED",
                        "浏览器下载信息注册失败。",
                    );
                }
                registered = true;
                const startWaiter = createStartWaiter();
                navigationAttempted = true;
                launchNavigation(downloadId);
                const startedStatus = await startWaiter;
                const completion = waitForTerminal(downloadId, onProgress);
                return {
                    downloadId,
                    transport: "browser_streaming",
                    startedStatus,
                    completion,
                    cancel: () => cancel(downloadId),
                };
            } catch (error) {
                stopStartWaiter();
                if (registered && !streamingStarted && !preparedDescriptorDeleted) {
                    try {
                        const deleted = await deletePrepared(downloadId);
                        preparedDescriptorDeleted = deleted;
                        if (!deleted && navigationAttempted) {
                            throw streamingError(
                                "STREAMING_START_STATE_UNKNOWN",
                                "无法确认浏览器下载任务状态。",
                                {streamingStarted: true},
                            );
                        }
                    } catch (deleteError) {
                        if (navigationAttempted || (deleteError && deleteError.streamingStarted)) {
                            deleteError.streamingStarted = true;
                            throw deleteError;
                        }
                    }
                }
                if (error && navigationAttempted && !preparedDescriptorDeleted) {
                    error.streamingStarted = true;
                } else if (error && typeof error.streamingStarted !== "boolean") {
                    error.streamingStarted = streamingStarted;
                }
                cleanupNavigationFrame(downloadId);
                throw error;
            }
        }

        async function close() {
            if (
                messageListenerAttached
                && navigatorObject
                && navigatorObject.serviceWorker
                && typeof navigatorObject.serviceWorker.removeEventListener === "function"
            ) {
                navigatorObject.serviceWorker.removeEventListener("message", handleWorkerMessage);
            }
            messageListenerAttached = false;
            listeners.clear();
            allListeners.clear();
            latestStatuses.clear();
            [...navigationFrames.keys()].forEach(cleanupNavigationFrame);
        }

        return Object.freeze({
            cancel,
            capabilityAvailable,
            close,
            getStatus,
            listActiveDownloads,
            registration,
            start,
            subscribe,
            subscribeAll,
        });
    }

    let sharedClient = null;

    function getSharedClient(options) {
        if (!sharedClient || options) {
            sharedClient = createStreamingClient(options);
        }
        return sharedClient;
    }

    return Object.freeze({
        DEFAULT_MESSAGE_TIMEOUT_MS,
        DEFAULT_START_TIMEOUT_MS,
        DEFAULT_STATUS_POLL_MS,
        DEFAULT_TELEMETRY_ENDPOINT,
        DEFAULT_WORKER_SCOPE,
        DEFAULT_WORKER_URL,
        TELEMETRY_SCHEMA_VERSION,
        TERMINAL_STATES,
        createTerminalTelemetryPayload,
        createTerminalTelemetryReporter,
        createStreamingClient,
        detectBrowserFamily,
        getSharedClient,
        normalizePageVisibility,
        normalizeWorkerState,
        readCookie,
        streamingError,
        supportsStreamingEnvironment,
        waitForWorkerActivation,
        waitForWorkerControl,
    });
}));
