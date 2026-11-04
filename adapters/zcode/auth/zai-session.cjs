"use strict";

const crypto = require("node:crypto");

const STORAGE_KEY = "zcode-candidate:zai-business-session:v1";
const CACHE_SKEW_MS = 120_000;
const MAX_SESSION_MS = 30 * 24 * 60 * 60 * 1000;
const CONSERVATIVE_SESSION_MS = 7 * 24 * 60 * 60 * 1000;

const pendingByStore = new WeakMap();
const pendingWithoutStore = new Map();
const storeLocks = new WeakMap();

function safeError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fingerprint(oauthToken) {
  if (typeof oauthToken !== "string" || oauthToken.trim() === "") {
    throw safeError("project_token_login_required");
  }
  return crypto.createHash("sha256").update(oauthToken, "utf8").digest("hex");
}

function storageAvailable(store) {
  return store !== null && typeof store === "object" &&
    typeof store.load === "function" && typeof store.save === "function" &&
    typeof store.deleteIfValue === "function" && typeof store.saveIfValue === "function";
}

function assertStorage(store) {
  if (!storageAvailable(store)) throw safeError("business_session_storage_failed");
}

function withStoreLock(store, operation) {
  if (!storageAvailable(store)) return operation();
  const previous = storeLocks.get(store) || Promise.resolve();
  const current = previous.catch(() => {}).then(operation);
  let cleanup;
  cleanup = current.then(
    () => {
      if (storeLocks.get(store) === cleanup) storeLocks.delete(store);
    },
    () => {
      if (storeLocks.get(store) === cleanup) storeLocks.delete(store);
    },
  );
  storeLocks.set(store, cleanup);
  return current;
}

async function loadRecord(store) {
  assertStorage(store);
  let raw;
  try {
    raw = await store.load(STORAGE_KEY);
  } catch {
    throw safeError("business_session_storage_failed");
  }
  if (raw === null || raw === undefined || raw === "") return { raw: null, record: null };
  if (typeof raw !== "string") return { raw, record: null };
  try {
    const record = JSON.parse(raw);
    if (!validRecord(record)) return { raw, record: null };
    return { raw, record };
  } catch {
    return { raw, record: null };
  }
}

function validRecord(record) {
  return isObject(record) && record.version === 1 &&
    typeof record.oauthFingerprint === "string" && /^[a-f0-9]{64}$/.test(record.oauthFingerprint) &&
    isObject(record.business) && [0, 200, "0", "200"].includes(record.business.code) &&
    isObject(record.business.data) && typeof record.business.data.access_token === "string" &&
    record.business.data.access_token.trim() !== "" && Number.isFinite(record.expiresAt) &&
    record.expiresAt > 0;
}

function readJwtExpiry(token, now = Date.now()) {
  try {
    const payload = token.split(".")[1];
    if (!payload) return undefined;
    const value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (!isObject(value) || !Object.prototype.hasOwnProperty.call(value, "exp")) return undefined;
    if (typeof value.exp !== "number" || !Number.isFinite(value.exp) || value.exp <= 0) {
      throw safeError("project_token_login_failed");
    }
    const expiresAt = value.exp * 1000;
    if (!Number.isFinite(expiresAt) || expiresAt <= now) throw safeError("project_token_login_failed");
    return expiresAt;
  } catch (error) {
    if (error?.code === "project_token_login_failed") throw error;
    return undefined;
  }
}

function relativeExpiry(data, requestedAt) {
  const expiresIn = data.expires_in ?? data.expiresIn;
  if (expiresIn === undefined) return undefined;
  if (typeof expiresIn !== "number" || !Number.isFinite(expiresIn) || expiresIn < 0) {
    throw safeError("project_token_login_failed");
  }
  return requestedAt + expiresIn * 1000;
}

function sanitizeResponse(response, requestedAt) {
  if (!isObject(response) || response.success === false) {
    throw safeError("project_token_login_failed");
  }
  if (response.code !== undefined && ![0, 200, "0", "200"].includes(response.code)) {
    throw safeError("project_token_login_failed");
  }
  const data = isObject(response.data) ? response.data : response;
  const token = data.access_token ?? data.accessToken;
  if (typeof token !== "string" || token.trim() === "") {
    throw safeError("project_token_login_failed");
  }
  const expiresIn = data.expires_in ?? data.expiresIn;
  const normalizedData = { access_token: token.trim() };
  if (expiresIn !== undefined) normalizedData.expires_in = expiresIn;
  const jwtExpiry = readJwtExpiry(normalizedData.access_token, requestedAt);
  const expiresAt = [
    jwtExpiry,
    relativeExpiry(normalizedData, requestedAt),
  ].filter((value) => value !== undefined).concat(
    requestedAt + (jwtExpiry === undefined && expiresIn === undefined
      ? CONSERVATIVE_SESSION_MS : MAX_SESSION_MS),
  ).reduce((minimum, value) => Math.min(minimum, value));
  if (!Number.isFinite(expiresAt) || expiresAt <= requestedAt) {
    throw safeError("project_token_login_failed");
  }
  return {
    response: { code: response.code === undefined ? 0 : response.code, data: normalizedData },
    expiresAt,
  };
}

function cacheResponse(record, now) {
  const remaining = Math.max(0, Math.floor((record.expiresAt - now) / 1000));
  return {
    code: 0,
    data: { access_token: record.business.data.access_token, expires_in: remaining },
  };
}

function getPending(store, key) {
  const map = storageAvailable(store) ? pendingByStore.get(store) : pendingWithoutStore;
  if (storageAvailable(store) && !map) {
    const created = new Map();
    pendingByStore.set(store, created);
    return created;
  }
  return map;
}

async function saveIfSafe(store, record, existing) {
  if (!storageAvailable(store)) return;
  const replacement = JSON.stringify(record);
  try {
    if (existing && existing.raw !== null && existing.raw !== undefined && existing.raw !== "") {
      await store.saveIfValue(STORAGE_KEY, existing.raw, replacement);
      return;
    }
    await store.saveIfValue(STORAGE_KEY, null, replacement);
  } catch {
    throw safeError("business_session_storage_failed");
  }
}

async function persistedBusiness(oauthToken, locationStore, request) {
  const oauth = typeof oauthToken === "string" ? oauthToken.trim() : oauthToken;
  const oauthFingerprint = fingerprint(oauth);
  if (typeof request !== "function") throw safeError("project_token_login_failed");
  const pending = getPending(locationStore, oauthFingerprint);
  if (pending.has(oauthFingerprint)) return pending.get(oauthFingerprint);
  const operation = (async () => {
    const now = Date.now();
    let existing = null;
    if (locationStore !== undefined && locationStore !== null) {
      assertStorage(locationStore);
      existing = await loadRecord(locationStore);
      if (existing.record && existing.record.oauthFingerprint === oauthFingerprint &&
          existing.record.expiresAt - now > CACHE_SKEW_MS) {
        return cacheResponse(existing.record, now);
      }
    }
    const raw = await request();
    const normalized = sanitizeResponse(raw, now);
    if (storageAvailable(locationStore)) {
      await saveIfSafe(locationStore, {
        version: 1,
        oauthFingerprint,
        business: normalized.response,
        expiresAt: normalized.expiresAt,
      }, existing);
    }
    return normalized.response;
  })();
  pending.set(oauthFingerprint, operation);
  try {
    return await operation;
  } finally {
    pending.delete(oauthFingerprint);
  }
}

async function invalidateBusiness(oauthToken, businessToken, locationStore) {
  if (!storageAvailable(locationStore)) return false;
  const oauthFingerprint = fingerprint(oauthToken);
  let loaded = await loadRecord(locationStore);
  if (!loaded.record || loaded.record.oauthFingerprint !== oauthFingerprint ||
      loaded.record.business.data.access_token !== businessToken) return false;
  try {
    return await locationStore.deleteIfValue(STORAGE_KEY, loaded.raw);
  } catch {
    throw safeError("business_session_storage_failed");
  }
}

// Para o site de invalidação que não tem o token oauth no escopo (401 em /api/biz/):
// o business token é único por login, basta ele para casar o registro.
async function invalidateBusinessByToken(businessToken, locationStore) {
  if (!storageAvailable(locationStore)) return false;
  let loaded = await loadRecord(locationStore);
  if (!loaded.record || loaded.record.business.data.access_token !== businessToken) return false;
  try {
    return await locationStore.deleteIfValue(STORAGE_KEY, loaded.raw);
  } catch {
    throw safeError("business_session_storage_failed");
  }
}

module.exports = {
  STORAGE_KEY,
  CACHE_SKEW_MS,
  persistedBusiness,
  invalidateBusiness,
  invalidateBusinessByToken,
  _private: { fingerprint, readJwtExpiry, sanitizeResponse, validRecord, withStoreLock },
};
