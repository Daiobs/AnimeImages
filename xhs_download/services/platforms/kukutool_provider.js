"use strict";

const crypto = require("crypto");

const BASE_URL = "https://dy.kukutool.com";
const BASE_ORIGIN = new URL(BASE_URL).origin;
const RESPONSE_KEY = "12345678901234567890123456789013";
const STANDARD_B64 =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const CUSTOM_B64 =
  "ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_";
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36";
const MAX_RESPONSE_CHARS = 10 * 1024 * 1024;
const MAX_DISCOVERY_PAGE_CHARS = 1024 * 1024;
const MAX_DISCOVERY_SCRIPT_CHARS = 2 * 1024 * 1024;
const MAX_DISCOVERY_SCRIPTS = 6;
const DISCOVERY_BATCH_SIZE = 2;
const MAX_REDIRECTS = 3;
const FALLBACK_PROFILES = [
  {
    id: "2026060303",
    authRoute: "auth-9e25f1",
    authResponseFields: { key: "k_9e25f1", seed: "s_9e25f1" },
    parseRequestFields: {
      key: "k_9e25f1",
      payload: "p_9e25f1",
      iv: "i_9e25f1",
      version: "r_9e25f1",
    },
  },
  {
    id: "2026060302",
    authRoute: "auth-86a05b",
    authResponseFields: { key: "k_86a05b", seed: "s_86a05b" },
    parseRequestFields: {
      key: "k_86a05b",
      payload: "p_86a05b",
      iv: "i_86a05b",
      version: "r_86a05b",
    },
  },
];

async function main() {
  const requestURL = normalizeRequestURL(process.argv[2]);
  const headers = {
    Accept: "application/json",
    "Content-Type": "application/json",
    Origin: BASE_URL,
    Referer: `${BASE_URL}/zh`,
    "User-Agent": USER_AGENT,
  };

  const configuredProfile = readConfiguredProfile();
  const initialProfiles = uniqueProfiles([
    ...(configuredProfile ? [configuredProfile] : []),
    ...FALLBACK_PROFILES,
  ]);
  let ticket;
  try {
    // The known profile avoids loading the homepage and every JS bundle on
    // each request. Discovery remains the recovery path when the profile rotates.
    ticket = await requestAuthTicket(initialProfiles, headers, requestURL);
  } catch (fallbackError) {
    const profiles = await discoverProfiles(headers);
    try {
      ticket = await requestAuthTicket(profiles, headers, requestURL);
    } catch (discoveryError) {
      discoveryError.cause = fallbackError;
      throw discoveryError;
    }
  }

  const { profile, auth, cookie } = ticket;
  const authKey = auth[profile.authResponseFields.key];
  const authSeed = auth[profile.authResponseFields.seed];
  const params = {
    requestURL,
    captchaKey: "",
    captchaInput: "",
    totalSuccessCount: "0",
    successCount: "0",
    firstSuccessDate: "",
    pagePath: "",
    uwx_id: "",
    isMobile: "false",
    geoipIp: "",
  };
  const envelope = encryptRequest(params, authKey, authSeed);
  const fields = profile.parseRequestFields;
  const parseResponse = await fetchSameOrigin(`${BASE_URL}/api/parse`, {
    method: "POST",
    headers: { ...headers, Cookie: cookie },
    body: JSON.stringify({
      version: 3,
      [fields.key]: authKey,
      [fields.payload]: envelope.payload,
      [fields.iv]: envelope.iv,
      [fields.version]: 1,
    }),
    signal: AbortSignal.timeout(10000),
  });
  const parsed = await readJson(parseResponse, "PARSE");
  if (!parseResponse.ok || parsed.status !== 0) {
    throw upstreamError("parse", parseResponse.status, parsed);
  }

  const result = parsed.encrypt
    ? decryptResponse(parsed.data, parsed.iv)
    : parsed.data;
  process.stdout.write(JSON.stringify({ result, profile }));
}

function normalizeRequestURL(value) {
  const input = String(value || "").trim();
  if (/^\d{15,22}$/.test(input)) {
    return `https://www.douyin.com/video/${input}`;
  }
  let parsed;
  try {
    parsed = new URL(input);
  } catch {
    throw new Error("invalid request_url");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error("invalid request_url");
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    /^127\./.test(hostname) ||
    hostname === "::1"
  ) {
    throw new Error("invalid request_url");
  }
  return parsed.href;
}

async function discoverProfiles(headers) {
  try {
    const pageResponse = await fetchSameOrigin(`${BASE_URL}/`, {
      headers,
      signal: AbortSignal.timeout(5000),
    });
    if (!pageResponse.ok) {
      return FALLBACK_PROFILES;
    }
    const html = await readText(pageResponse, MAX_DISCOVERY_PAGE_CHARS);
    const scriptUrls = Array.from(
      html.matchAll(/src="([^"]+\.js[^"]*)"/g),
      (match) => new URL(match[1], BASE_URL).href
    )
      .filter((url) => new URL(url).origin === BASE_ORIGIN)
      .filter((url, index, values) => values.indexOf(url) === index)
      .slice(0, MAX_DISCOVERY_SCRIPTS);
    const preferred = scriptUrls.filter((url) => /\/9802-[^/]+\.js/.test(url));
    const remaining = scriptUrls.filter((url) => !preferred.includes(url));
    const batches = preferred.length ? [preferred, remaining] : [scriptUrls];
    for (const urls of batches) {
      if (!urls.length) {
        continue;
      }
      for (let index = 0; index < urls.length; index += DISCOVERY_BATCH_SIZE) {
        const batch = urls.slice(index, index + DISCOVERY_BATCH_SIZE);
        const scripts = await Promise.allSettled(
          batch.map(async (url) => {
            const response = await fetchSameOrigin(url, {
              headers,
              signal: AbortSignal.timeout(4000),
            });
            return response.ok
              ? readText(response, MAX_DISCOVERY_SCRIPT_CHARS)
              : "";
          })
        );
        for (const script of scripts) {
          if (script.status !== "fulfilled") {
            continue;
          }
          const profiles = extractProfiles(script.value);
          if (profiles.length) {
            return profiles;
          }
        }
      }
    }
  } catch {
    return FALLBACK_PROFILES;
  }
  return FALLBACK_PROFILES;
}

function extractProfiles(source) {
  const marker = source.indexOf('{"activeProfileId"');
  if (marker !== -1) {
    const jsonText = readJsonObject(source, marker);
    if (jsonText) {
      try {
        const config = JSON.parse(jsonText);
        const profiles = Array.isArray(config.profiles) ? config.profiles : [];
        const normalized = profiles
          .filter(
            (profile) =>
              profile?.id &&
              profile?.authRoute &&
              profile?.authResponseFields?.key &&
              profile?.authResponseFields?.seed &&
              profile?.parseRequestFields?.key &&
              profile?.parseRequestFields?.payload &&
              profile?.parseRequestFields?.iv &&
              profile?.parseRequestFields?.version
          )
          .map((profile) => ({
            id: String(profile.id),
            authRoute: String(profile.authRoute),
            authResponseFields: {
              key: String(profile.authResponseFields.key),
              seed: String(profile.authResponseFields.seed),
            },
            parseRequestFields: {
              key: String(profile.parseRequestFields.key),
              payload: String(profile.parseRequestFields.payload),
              iv: String(profile.parseRequestFields.iv),
              version: String(profile.parseRequestFields.version),
            },
          }));
        normalized.sort((left, right) => {
          if (left.id === config.activeProfileId) return -1;
          if (right.id === config.activeProfileId) return 1;
          return 0;
        });
        if (normalized.length) {
          return normalized;
        }
      } catch {
        // Fall through to the compact bundle parser.
      }
    }
  }

  const activeId = source.match(/"activeProfileId":"([^"]+)"/)?.[1] || "";
  const pattern =
    /"id":"([^"]+)","authRoute":"([^"]+)","authResponseFields":\{"key":"([^"]+)","seed":"([^"]+)"\},"parseRequestFields":\{"key":"([^"]+)","payload":"([^"]+)","iv":"([^"]+)","version":"([^"]+)"\}/g;
  const profiles = Array.from(source.matchAll(pattern), (match) => ({
    id: match[1],
    authRoute: match[2],
    authResponseFields: { key: match[3], seed: match[4] },
    parseRequestFields: {
      key: match[5],
      payload: match[6],
      iv: match[7],
      version: match[8],
    },
  }));
  profiles.sort((left, right) => {
    if (left.id === activeId) return -1;
    if (right.id === activeId) return 1;
    return 0;
  });
  return profiles;
}

function readJsonObject(source, start) {
  let depth = 0;
  let quoted = false;
  let escaped = false;
  for (let index = start; index < source.length; index += 1) {
    const character = source[index];
    if (quoted) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        quoted = false;
      }
      continue;
    }
    if (character === '"') {
      quoted = true;
    } else if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + 1);
      }
    }
  }
  return "";
}

async function requestAuthTicket(profiles, headers, requestURL) {
  let lastError = null;
  for (const profile of profiles) {
    try {
      const response = await fetchSameOrigin(
        `${BASE_URL}/api/${profile.authRoute}`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ requestURL, pagePath: "", mode: "single" }),
          signal: AbortSignal.timeout(6000),
        }
      );
      const auth = await readJson(response, "AUTH");
      const authKey = auth[profile.authResponseFields.key];
      const authSeed = auth[profile.authResponseFields.seed];
      if (!response.ok || !authKey || !authSeed) {
        lastError = upstreamError("auth", response.status, auth);
        continue;
      }
      return {
        profile,
        auth,
        cookie: String(response.headers.get("set-cookie") || "").split(";")[0],
      };
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("no usable upstream profile");
}

function encryptRequest(params, authKey, authSeed) {
  const key = crypto
    .createHash("sha256")
    .update(`${authKey}:${authSeed}`)
    .digest();
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([
    cipher.update(JSON.stringify(params)),
    cipher.final(),
    cipher.getAuthTag(),
  ]);
  return {
    payload: encrypted.toString("base64"),
    iv: iv.toString("base64"),
  };
}

function decryptResponse(data, iv) {
  const ciphertext = Buffer.from(decodeCustomBase64(transform(data)), "base64");
  const decodedIv = Buffer.from(decodeCustomBase64(transform(iv)), "base64");
  const decipher = crypto.createDecipheriv(
    "aes-256-cbc",
    Buffer.from(RESPONSE_KEY),
    decodedIv
  );
  const plaintext = Buffer.concat([
    decipher.update(ciphertext),
    decipher.final(),
  ]).toString("utf8");
  return JSON.parse(plaintext);
}

function transform(value) {
  const xor = Array.from(String(value), (character) =>
    String.fromCharCode(character.charCodeAt(0) ^ 0x5a)
  ).join("");
  let reversed = "";
  for (let index = 0; index < xor.length; index += 8) {
    reversed += Array.from(xor.slice(index, index + 8)).reverse().join("");
  }
  return reversed;
}

function decodeCustomBase64(value) {
  return Array.from(value, (character) => {
    const index = CUSTOM_B64.indexOf(character);
    return index === -1 ? character : STANDARD_B64[index];
  }).join("");
}

async function readJson(response, stage = "UNKNOWN") {
  const text = await readText(response);
  try {
    return JSON.parse(text);
  } catch {
    const error = new Error(`upstream returned invalid JSON (${response.status})`);
    const phase = ["AUTH", "PARSE"].includes(stage) ? stage : "UNKNOWN";
    const status = Number.isInteger(response.status) && response.status >= 100 && response.status <= 599
      ? response.status : "UNKNOWN";
    const mime = String(response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
    const type = !mime ? "MISSING" : mime === "application/json" || mime.endsWith("+json") ? "JSON"
      : ["text/html", "application/xhtml+xml"].includes(mime) ? "HTML" : mime.startsWith("text/") ? "TEXT" : "OTHER";
    error.safeDetail = `SERVICE_JSON_${phase}_HTTP_${status}_TYPE_${type}`;
    throw error;
  }
}

async function readText(response, maxChars = MAX_RESPONSE_CHARS) {
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > maxChars) {
    throw new Error(`upstream response is too large (${response.status})`);
  }
  const text = await response.text();
  if (text.length > maxChars) {
    throw new Error(`upstream response is too large (${response.status})`);
  }
  return text;
}

function readConfiguredProfile() {
  const source = String(process.env.KUKUTOOL_PROFILE_JSON || "").trim();
  if (!source) {
    return null;
  }
  try {
    const profile = JSON.parse(source);
    return isValidProfile(profile) ? profile : null;
  } catch {
    return null;
  }
}

function isValidProfile(profile) {
  return Boolean(
    profile?.id &&
      profile?.authRoute &&
      profile?.authResponseFields?.key &&
      profile?.authResponseFields?.seed &&
      profile?.parseRequestFields?.key &&
      profile?.parseRequestFields?.payload &&
      profile?.parseRequestFields?.iv &&
      profile?.parseRequestFields?.version
  );
}

function uniqueProfiles(profiles) {
  const seen = new Set();
  return profiles.filter((profile) => {
    if (!isValidProfile(profile)) {
      return false;
    }
    const identity = `${profile.id}:${profile.authRoute}`;
    if (seen.has(identity)) {
      return false;
    }
    seen.add(identity);
    return true;
  });
}

async function fetchSameOrigin(input, options = {}) {
  let currentURL = new URL(input, BASE_URL);
  let requestOptions = { ...options, redirect: "manual" };

  for (let redirectCount = 0; redirectCount <= MAX_REDIRECTS; redirectCount += 1) {
    if (currentURL.protocol !== "https:" || currentURL.origin !== BASE_ORIGIN) {
      throw new Error("upstream redirect left the allowed origin");
    }

    const response = await fetch(currentURL, requestOptions);
    if (response.status < 300 || response.status >= 400) {
      return response;
    }

    const location = response.headers.get("location");
    if (!location || redirectCount === MAX_REDIRECTS) {
      throw new Error(`upstream redirect rejected (${response.status})`);
    }
    if (response.body) {
      await response.body.cancel();
    }
    currentURL = new URL(location, currentURL);

    if (
      response.status === 303 ||
      ((response.status === 301 || response.status === 302) &&
        String(requestOptions.method || "GET").toUpperCase() === "POST")
    ) {
      const nextHeaders = { ...(requestOptions.headers || {}) };
      delete nextHeaders["Content-Type"];
      delete nextHeaders["content-type"];
      requestOptions = {
        ...requestOptions,
        method: "GET",
        body: undefined,
        headers: nextHeaders,
      };
    }
  }

  throw new Error("upstream redirect limit exceeded");
}

function upstreamError(stage, status, payload) {
  const reason = payload?.reason || payload?.message || payload?.error;
  return new Error(`${stage} failed (${status}): ${reason || "unknown error"}`);
}

function safeFailureDetail(error) {
  if (/^SERVICE_JSON_(AUTH|PARSE|UNKNOWN)_HTTP_([1-5]\d{2}|UNKNOWN)_TYPE_(JSON|HTML|TEXT|OTHER|MISSING)$/.test(error?.safeDetail || "")) {
    return error.safeDetail;
  }
  const message = String(error?.message || "");
  const status = message.match(/^(?:auth failed|parse failed|upstream returned invalid JSON|upstream response is too large|upstream redirect rejected) \(([45]\d\d)\)/);
  if (status) return `SERVICE_HTTP_${status[1]}`;
  if (message.startsWith("auth failed (")) return "SERVICE_AUTH_REJECTED";
  if (message.startsWith("parse failed (")) return "SERVICE_PARSE_REJECTED";
  if (message.startsWith("upstream returned invalid JSON (")) return "SERVICE_INVALID_JSON";
  if (message.startsWith("upstream response is too large (")) return "SERVICE_RESPONSE_TOO_LARGE";
  if (message.startsWith("upstream redirect")) return "SERVICE_REDIRECT_REJECTED";
  if (message === "fetch failed") return "SERVICE_NETWORK_ERROR";
  return "SERVICE_HELPER_FAILED";
}

main().catch((error) => {
  process.stderr.write(`KUKUTOOL_SAFE_ERROR:${safeFailureDetail(error)}\n`);
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
