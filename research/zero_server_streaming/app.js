(function () {
    "use strict";

    const protocol = window.AnimeImagesStreamingProtocol;
    const LOCAL_DESCRIPTOR_BROKER = "http://127.0.0.1:8767/descriptor";
    const state = {
        registration: null,
        prepared: null,
        startedAt: 0,
    };

    function randomId() {
        const bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    }

    function byId(id) {
        return document.getElementById(id);
    }

    function setStatus(message, tone) {
        const node = byId("status");
        node.textContent = message;
        node.dataset.tone = tone || "neutral";
    }

    function setStats(value) {
        byId("stats").textContent = JSON.stringify(value, null, 2);
    }

    async function activeWorker() {
        const registration = state.registration || await navigator.serviceWorker.ready;
        return registration.active || registration.waiting || registration.installing;
    }

    async function postMessage(message) {
        const worker = await activeWorker();
        if (!worker) {
            throw new Error("SERVICE_WORKER_NOT_ACTIVE");
        }
        const channel = new MessageChannel();
        const response = new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => reject(new Error("SERVICE_WORKER_ACK_TIMEOUT")), 3000);
            channel.port1.onmessage = (event) => {
                window.clearTimeout(timer);
                resolve(event.data || {});
            };
        });
        worker.postMessage(message, [channel.port2]);
        return response;
    }

    function descriptorFromControls() {
        return protocol.createDescriptor({
            id: randomId(),
            url: byId("media-url").value,
            filename: byId("filename").value,
            expectedBytes: byId("expected-bytes").value,
        });
    }

    async function registerDescriptor(descriptor) {
        const response = await postMessage({
            type: "REGISTER_DESCRIPTOR",
            descriptor,
        });
        if (!response.ok) {
            throw new Error(response.code || "DESCRIPTOR_REJECTED");
        }
        state.prepared = Object.freeze({
            id: descriptor.id,
            expectedBytes: descriptor.expectedBytes,
        });
        byId("media-url").value = "";
        byId("download-prepared").disabled = false;
        setStatus("Descriptor prepared in Service Worker memory.", "success");
        setStats({
            phase: "prepared",
            id: descriptor.id,
            expectedBytes: descriptor.expectedBytes,
        });
    }

    async function loadLocalDescriptor() {
        const response = await fetch(LOCAL_DESCRIPTOR_BROKER, {
            method: "GET",
            credentials: "omit",
            cache: "no-store",
            referrerPolicy: "no-referrer",
        });
        if (!response.ok) {
            throw new Error("LOCAL_DESCRIPTOR_UNAVAILABLE");
        }
        const payload = await response.json();
        byId("media-url").value = String(payload.url || "");
        byId("filename").value = protocol.sanitizeFilename(payload.filename);
        byId("expected-bytes").value = String(protocol.positiveInteger(payload.expectedBytes));
        setStatus("One-time real descriptor loaded in page memory.", "success");
        setStats({
            phase: "real_descriptor_loaded",
            provider: String(payload.provider || "unknown"),
            hostname: String(payload.hostname || "unknown"),
            expectedBytes: protocol.positiveInteger(payload.expectedBytes),
        });
    }

    function launchDownload(descriptor) {
        state.startedAt = performance.now();
        const link = document.createElement("a");
        link.href = protocol.downloadPath(descriptor.id);
        link.textContent = "download";
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setStatus("Download navigation issued.", "neutral");
    }

    function applyControlledSource(sizeBytes, rateMiB) {
        byId("media-url").value = `http://127.0.0.1:8766/bytes/${sizeBytes}?rate_mib=${rateMiB}`;
        byId("filename").value = `animeimages-sw-${sizeBytes}.mp4`;
        byId("expected-bytes").value = String(sizeBytes);
    }

    async function initialize() {
        if (!("serviceWorker" in navigator) || !("ReadableStream" in window) || !("TransformStream" in window)) {
            setStatus("Required browser streaming APIs are unavailable.", "error");
            return;
        }
        state.registration = await navigator.serviceWorker.register("./sw.js", {scope: "./"});
        await navigator.serviceWorker.ready;
        if (!navigator.serviceWorker.controller) {
            setStatus("Service Worker installed. Reload once to obtain control.", "neutral");
            byId("reload-control").hidden = false;
            return;
        }
        setStatus("Service Worker controls this page.", "success");
        byId("prepare").disabled = false;
        byId("route-on-click").disabled = false;
    }

    navigator.serviceWorker.addEventListener("message", (event) => {
        const message = event.data && typeof event.data === "object" ? event.data : {};
        if (!state.prepared || message.id !== state.prepared.id) {
            return;
        }
        const elapsedMs = Math.round(performance.now() - state.startedAt);
        if (message.type === "STREAM_STARTED") {
            setStatus("Browser download response started.", "success");
        } else if (message.type === "STREAM_COMPLETED") {
            setStatus("Service Worker finished the CDN stream.", "success");
        } else if (message.type === "STREAM_FAILED") {
            setStatus(`Streaming failed: ${message.code}`, "error");
        }
        setStats({
            phase: message.type,
            id: message.id,
            receivedBytes: Number(message.receivedBytes || 0),
            totalBytes: Number(message.totalBytes || 0),
            elapsedMs: Number(message.elapsedMs || elapsedMs),
            pageElapsedMs: elapsedMs,
        });
    });

    byId("preset-100").addEventListener("click", () => applyControlledSource(100 * 1024 * 1024, 8));
    byId("preset-300").addEventListener("click", () => applyControlledSource(300 * 1024 * 1024, 12));
    byId("reload-control").addEventListener("click", () => location.reload());
    byId("load-real-descriptor").addEventListener("click", async () => {
        try {
            await loadLocalDescriptor();
        } catch (error) {
            setStatus(String(error && error.message || "Descriptor load failed"), "error");
        }
    });
    byId("prepare").addEventListener("click", async () => {
        try {
            await registerDescriptor(descriptorFromControls());
        } catch (error) {
            setStatus(String(error && error.message || "Prepare failed"), "error");
        }
    });
    byId("download-prepared").addEventListener("click", () => {
        if (!state.prepared) {
            return;
        }
        const descriptor = state.prepared;
        byId("download-prepared").disabled = true;
        launchDownload(descriptor);
    });
    byId("route-on-click").addEventListener("click", async () => {
        try {
            const descriptor = descriptorFromControls();
            await registerDescriptor(descriptor);
            launchDownload(descriptor);
        } catch (error) {
            setStatus(String(error && error.message || "Route-on-click failed"), "error");
        }
    });

    initialize().catch((error) => setStatus(String(error && error.message || error), "error"));
}());
