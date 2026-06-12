"use strict";

const crypto = require("crypto");

const BASE_URL = "https://dy.kukutool.com";
const RESPONSE_KEY = "12345678901234567890123456789013";
const STANDARD_B64 =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const CUSTOM_B64 =
  "ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_";
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36";
const MAX_RESPONSE_CHARS = 10 * 1024 * 1024;
const MAX_DISCOVERY_SCRIPTS = 24;
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
  const awemeId = String(process.argv[2] || "");
  if (!/^\d{15,22}$/.test(awemeId)) {
    throw new Error("invalid aweme_id");
  }

  const requestURL = `https://www.douyin.com/video/${awemeId}`;
  const headers = {
    Accept: "application/json",
    "Content-Type": "application/json",
    Origin: BASE_URL,
    Referer: `${BASE_URL}/zh`,
    "User-Agent": USER_AGENT,
  };

  let ticket;
  try {
    // The known profile avoids loading the homepage and every JS bundle on
    // each request. Discovery remains the recovery path when the profile rotates.
    ticket = await requestAuthTicket(FALLBACK_PROFILES, headers, requestURL);
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
  const parseResponse = await fetch(`${BASE_URL}/api/parse`, {
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
  const parsed = await readJson(parseResponse);
  if (!parseResponse.ok || parsed.status !== 0) {
    throw upstreamError("parse", parseResponse.status, parsed);
  }

  const result = parsed.encrypt
    ? decryptResponse(parsed.data, parsed.iv)
    : parsed.data;
  process.stdout.write(JSON.stringify(result));
}

async function discoverProfiles(headers) {
  try {
    const pageResponse = await fetch(`${BASE_URL}/`, {
      headers,
      signal: AbortSignal.timeout(5000),
    });
    if (!pageResponse.ok) {
      return FALLBACK_PROFILES;
    }
    const html = await pageResponse.text();
    const scriptUrls = Array.from(
      html.matchAll(/src="([^"]+\.js[^"]*)"/g),
      (match) => new URL(match[1], BASE_URL).href
    )
      .filter((url) => new URL(url).origin === BASE_URL)
      .filter((url, index, values) => values.indexOf(url) === index)
      .slice(0, MAX_DISCOVERY_SCRIPTS);
    const preferred = scriptUrls.filter((url) => /\/9802-[^/]+\.js/.test(url));
    const remaining = scriptUrls.filter((url) => !preferred.includes(url));
    const batches = preferred.length ? [preferred, remaining] : [scriptUrls];
    for (const urls of batches) {
      if (!urls.length) {
        continue;
      }
      const scripts = await Promise.allSettled(
        urls.map(async (url) => {
          const response = await fetch(url, {
            headers,
            signal: AbortSignal.timeout(4000),
          });
          return response.ok ? readText(response) : "";
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
      const response = await fetch(`${BASE_URL}/api/${profile.authRoute}`, {
        method: "POST",
        headers,
        body: JSON.stringify({ requestURL, pagePath: "", mode: "single" }),
        signal: AbortSignal.timeout(6000),
      });
      const auth = await readJson(response);
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

async function readJson(response) {
  const text = await readText(response);
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`upstream returned invalid JSON (${response.status})`);
  }
}

async function readText(response) {
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > MAX_RESPONSE_CHARS) {
    throw new Error(`upstream response is too large (${response.status})`);
  }
  const text = await response.text();
  if (text.length > MAX_RESPONSE_CHARS) {
    throw new Error(`upstream response is too large (${response.status})`);
  }
  return text;
}

function upstreamError(stage, status, payload) {
  const reason = payload?.reason || payload?.message || payload?.error;
  return new Error(`${stage} failed (${status}): ${reason || "unknown error"}`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
