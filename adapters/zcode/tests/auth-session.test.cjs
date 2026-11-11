"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { test } = require("node:test");

const AUTH_MODULE = process.env.AUTH_SESSION_MODULE || path.join(__dirname, "..", "auth", "zai-session.cjs");
const auth = require(AUTH_MODULE);
const launcher = require(path.join(__dirname, "..", "auth", "launch.cjs"));

const OAUTH_A = "synthetic-oauth-account-a";
const OAUTH_B = "synthetic-oauth-account-b";
const BUSINESS_A = "synthetic-business-account-a";
const BUSINESS_B = "synthetic-business-account-b";

class FakeStore {
  constructor() {
    this.values = new Map();
    this.loads = 0;
    this.saves = 0;
    this.casSaves = 0;
  }

  async load(key) {
    this.loads += 1;
    return this.values.has(key) ? this.values.get(key) : null;
  }

  async save(key, value) {
    this.saves += 1;
    this.values.set(key, value);
  }

  async saveIfValue(key, expected, value) {
    this.casSaves += 1;
    const current = this.values.has(key) ? this.values.get(key) : null;
    if (current !== expected) return false;
    this.values.set(key, value);
    return true;
  }

  async deleteIfValue(key, expected) {
    if (this.values.get(key) !== expected) return false;
    this.values.delete(key);
    return true;
  }
}

function businessResponse(token = BUSINESS_A, expiresIn = 3600, extra = {}) {
  return { code: 0, data: { access_token: token, expires_in: expiresIn, ...extra } };
}

function jwtWithExp(exp) {
  const payload = Buffer.from(JSON.stringify({ exp })).toString("base64url");
  return `synthetic.${payload}.signature`;
}

function stored(store) {
  return JSON.parse(store.values.get(auth.STORAGE_KEY));
}

function createNativeStore(filePath) {
  const bundlePath = [
    "/opt/ZCode/resources/glm/zcode.cjs",
    path.join(os.homedir(), ".local", "share", "zcode-cli", "glm", "zcode.cjs"),
  ].find((candidate) => fs.existsSync(candidate));
  assert.ok(bundlePath, "bundle nativo ausente");
  const filename = path.resolve(bundlePath);
  const source = launcher.patchSource(fs.readFileSync(filename, "utf8"));
  // nomes minificados mudam a cada release: descobre pela forma, não pelo nome
  const lifecycle = /([\w$]+)\(\);globalThis\.__zcodeAuthSessionCandidate\.runBundleMain\(([\w$]+)\);async function \2\(\)/g;
  assert.equal([...source.matchAll(lifecycle)].length, 1);
  const factory = /=e\.credentialStore\?\?([\w$]+)\(\{env:[\w$]+\}\),[\w$]+=await [\w$]+\.captureRecoveryGuard\(\)/.exec(source);
  assert.ok(factory, "fábrica do credential store ausente");
  // lazy-init do módulo que define a fábrica (o `var X=W(()=>{` que contém o registro dela)
  const registered = source.indexOf(`r(${factory[1]},"`);
  const moduleInit = /([\w$]+)=[\w$]+\(\(\)=>\{$/.exec(source.slice(0, source.lastIndexOf("(()=>{", registered) + 6));
  assert.ok(moduleInit, "inicializador do módulo do credential store ausente");
  const isolated = source.replace(lifecycle, (_m, init, main) =>
    `${init}();${moduleInit[1]}();module.exports={createStore:${factory[1]}};async function ${main}()`);
  const bundle = new Module(filename, module);
  bundle.filename = filename;
  bundle.paths = Module._nodeModulePaths(path.dirname(filename));
  bundle._compile(isolated, filename);
  assert.equal(typeof bundle.exports.createStore, "function");
  return bundle.exports.createStore({
    filePath,
    env: { ZCODE_CREDENTIAL_SECRET: "synthetic-cas-secret" },
  });
}

test("request inicial salva somente a sessão mínima", async () => {
  const store = new FakeStore();
  const result = await auth.persistedBusiness(OAUTH_A, store, async () => businessResponse());
  assert.equal(result.data.access_token, BUSINESS_A);
  assert.equal(stored(store).business.data.access_token, BUSINESS_A);
  assert.equal(stored(store).oauthFingerprint.length, 64);
  assert.equal(stored(store).business.extra, undefined);
  assert.equal(store.saves, 0);
  assert.equal(store.casSaves, 1);
});

test("CAS create-if-absent preserva a resposta nova entre stores concorrentes", async () => {
  const shared = { values: new Map(), loads: 0 };
  let releaseLoads;
  const loadsReady = new Promise((resolve) => { releaseLoads = resolve; });
  let releaseOld;
  const oldReady = new Promise((resolve) => { releaseOld = resolve; });
  function sharedStore() {
    const store = { saves: 0, casSaves: 0 };
    return {
      async load(key) {
        shared.loads += 1;
        if (shared.loads === 2) releaseLoads();
        await loadsReady;
        return shared.values.has(key) ? shared.values.get(key) : null;
      },
      async save() {
        store.saves += 1;
        throw new Error("save must not bypass CAS");
      },
      async saveIfValue(key, expected, value) {
        store.casSaves += 1;
        const current = shared.values.has(key) ? shared.values.get(key) : null;
        if (current !== expected) return false;
        shared.values.set(key, value);
        return true;
      },
      async deleteIfValue(key, expected) {
        const current = shared.values.has(key) ? shared.values.get(key) : null;
        if (current !== expected) return false;
        shared.values.delete(key);
        return true;
      },
      stats: store,
    };
  }
  const oldStore = sharedStore();
  const newStore = sharedStore();
  const old = auth.persistedBusiness(OAUTH_A, oldStore, async () => {
    await oldReady;
    return businessResponse(BUSINESS_A);
  });
  const newer = auth.persistedBusiness(OAUTH_B, newStore, async () => businessResponse(BUSINESS_B));
  await newer;
  releaseOld();
  await old;
  const final = JSON.parse(shared.values.get(auth.STORAGE_KEY));
  assert.equal(shared.loads, 2);
  assert.equal(final.business.data.access_token, BUSINESS_B);
  assert.equal(oldStore.stats.saves, 0);
  assert.equal(newStore.stats.saves, 0);
  assert.equal(oldStore.stats.casSaves, 1);
  assert.equal(newStore.stats.casSaves, 1);
});

test("CAS nativo criptografado cria, rejeita concorrência e atualiza em arquivo temporário", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "zcode-native-cas-"));
  const file = path.join(directory, "credentials.json");
  const key = "synthetic-cas-key";
  try {
    const store = createNativeStore(file);
    assert.equal(await store.load(key), null);
    assert.equal(await store.saveIfValue(key, null, "v1"), true);
    assert.equal(await store.saveIfValue(key, null, "v2"), false);
    assert.equal(await store.saveIfValue(key, "v1", "v2"), true);
    assert.equal(await store.load(key), "v2");
    const record = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.match(record[key], /^enc:v\d+:/);
    assert.notEqual(record[key], "v2");
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("cache fake é reutilizado entre chamadas e ajusta expires_in", async () => {
  const store = new FakeStore();
  let requests = 0;
  await auth.persistedBusiness(OAUTH_A, store, async () => {
    requests += 1;
    return businessResponse(BUSINESS_A, 3600);
  });
  const result = await auth.persistedBusiness(OAUTH_A, store, async () => {
    requests += 1;
    return businessResponse(BUSINESS_B, 3600);
  });
  assert.equal(requests, 1);
  assert.equal(result.data.access_token, BUSINESS_A);
  assert.ok(result.data.expires_in <= 3600);
});

test("cache fake atravessa processos sem expor o valor", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "zcode-auth-") );
  const file = path.join(directory, "store.json");
  const script = `
    const fs = require("node:fs");
    const auth = require(${JSON.stringify(AUTH_MODULE)});
    const file = ${JSON.stringify(file)};
    const store = {
      async load(k) { const data = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : {}; return data[k] ?? null; },
      async save(k, v) { const data = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : {}; data[k] = v; fs.writeFileSync(file, JSON.stringify(data)); },
      async saveIfValue(k, expected, value) { const data = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : {}; const current = data[k] === undefined ? null : data[k]; if (current !== expected) return false; data[k] = value; fs.writeFileSync(file, JSON.stringify(data)); return true; },
      async deleteIfValue(k, v) { const data = JSON.parse(fs.readFileSync(file)); if (data[k] !== v) return false; delete data[k]; fs.writeFileSync(file, JSON.stringify(data)); return true; }
    };
    auth.persistedBusiness(${JSON.stringify(OAUTH_A)}, store, async () => ({code:0,data:{access_token:${JSON.stringify(BUSINESS_A)},expires_in:3600}})).then(() => process.exit(0));
  `;
  const first = spawnSync(process.execPath, ["-e", script], { encoding: "utf8" });
  assert.equal(first.status, 0, first.stderr);
  const second = spawnSync(process.execPath, ["-e", script.replace("BUSINESS_A", "BUSINESS_B")], { encoding: "utf8" });
  assert.equal(second.status, 0, second.stderr);
  assert.doesNotMatch(fs.readFileSync(file, "utf8"), /synthetic-oauth/);
});

test("dois processos com resultados fora de ordem preservam a sessão nova via CAS", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "zcode-auth-cas-"));
  const file = path.join(directory, "store.json");
  const oldRecord = {
    version: 1,
    oauthFingerprint: auth._private.fingerprint(OAUTH_A),
    business: { code: 0, data: { access_token: BUSINESS_A, expires_in: 1 } },
    expiresAt: Date.now() - 1,
  };
  fs.writeFileSync(file, JSON.stringify({ [auth.STORAGE_KEY]: JSON.stringify(oldRecord) }));
  const script = `
    const fs = require("node:fs");
    const auth = require(${JSON.stringify(AUTH_MODULE)});
    const file = ${JSON.stringify(file)};
    const lockFile = file + ".lock";
    const oauth = process.argv[1];
    const token = process.argv[2];
    const stale = process.argv[4] ? JSON.parse(process.argv[4]) : undefined;
    async function mutate(mutator) {
      let fd;
      while (fd === undefined) {
        try { fd = fs.openSync(lockFile, "wx"); }
        catch (error) { if (error.code !== "EEXIST") throw error; await new Promise((r) => setTimeout(r, 1)); }
      }
      try {
        const data = JSON.parse(fs.readFileSync(file));
        const result = await mutator(data);
        fs.writeFileSync(file, JSON.stringify(data));
        return result;
      } finally { fs.closeSync(fd); fs.unlinkSync(lockFile); }
    }
    const store = {
      async load(k) { if (stale !== undefined) return stale; const data = JSON.parse(fs.readFileSync(file)); return data[k] ?? null; },
      async save(k, v) { return mutate(async (data) => { data[k] = v; }); },
      async saveIfValue(k, expected, value) { return mutate(async (data) => { const current = data[k] === undefined ? null : data[k]; if (current !== expected) return false; data[k] = value; return true; }); },
      async deleteIfValue(k, v) { return mutate(async (data) => { if (data[k] !== v) return false; delete data[k]; return true; }); }
    };
    auth.persistedBusiness(oauth, store, async () => ({code:0,data:{access_token:token,expires_in:3600}})).then(() => process.exit(0), () => process.exit(1));
  `;
  const first = spawnSync(process.execPath, ["-e", script, OAUTH_B, BUSINESS_B], { encoding: "utf8" });
  assert.equal(first.status, 0, first.stderr);
  const stale = JSON.stringify(JSON.stringify(oldRecord));
  const second = spawnSync(process.execPath, ["-e", script, OAUTH_A, BUSINESS_A, "0", stale], { encoding: "utf8" });
  assert.equal(second.status, 0, second.stderr);
  const final = JSON.parse(JSON.parse(fs.readFileSync(file, "utf8"))[auth.STORAGE_KEY]);
  assert.equal(final.oauthFingerprint, auth._private.fingerprint(OAUTH_B));
  assert.equal(final.business.data.access_token, BUSINESS_B);
});

test("exp JWT e expires_in limitam o prazo", async () => {
  const jwt = jwtWithExp(Math.floor(Date.now() / 1000) + 900);
  const store = new FakeStore();
  await auth.persistedBusiness(OAUTH_A, store, async () => businessResponse(jwt, 3600));
  assert.ok(stored(store).expiresAt <= Date.now() + 901_000);
  const noServerExpiry = new FakeStore();
  await auth.persistedBusiness(OAUTH_B, noServerExpiry, async () => ({ code: 200, data: { access_token: BUSINESS_B } }));
  assert.ok(stored(noServerExpiry).expiresAt <= Date.now() + 7 * 24 * 60 * 60 * 1000 + 1000);
});

test("accessToken/expiresIn são aceitos e prazo JWT inválido não ganha fallback", async () => {
  const store = new FakeStore();
  const result = await auth.persistedBusiness(OAUTH_A, store, async () => ({
    code: 200,
    accessToken: BUSINESS_A,
    expiresIn: 300,
  }));
  assert.equal(result.data.access_token, BUSINESS_A);
  assert.equal(result.data.expires_in, 300);
  for (const exp of [0, -1, "900", "invalid"]) {
    const invalid = new FakeStore();
    const token = jwtWithExp(exp);
    await assert.rejects(
      auth.persistedBusiness(OAUTH_A, invalid, async () => businessResponse(token, undefined)),
      /project_token_login_failed/,
    );
    assert.equal(invalid.saves, 0);
  }
  const expired = new FakeStore();
  await assert.rejects(
    auth.persistedBusiness(OAUTH_A, expired, async () => businessResponse(jwtWithExp(Math.floor(Date.now() / 1000) - 1), undefined)),
    /project_token_login_failed/,
  );
});

test("resposta inválida nunca é salva", async () => {
  for (const response of [
    { code: 401, data: { access_token: BUSINESS_A } },
    { success: false, data: { access_token: BUSINESS_A } },
    { code: 0, data: {} },
  ]) {
    const store = new FakeStore();
    await assert.rejects(auth.persistedBusiness(OAUTH_A, store, async () => response), /project_token_login_failed/);
    assert.equal(store.saves, 0);
  }
});

test("fingerprint diferente não reutiliza sessão e prazo expirado busca novamente", async () => {
  const store = new FakeStore();
  let requests = 0;
  await auth.persistedBusiness(OAUTH_A, store, async () => { requests += 1; return businessResponse(BUSINESS_A); });
  const other = await auth.persistedBusiness(OAUTH_B, store, async () => { requests += 1; return businessResponse(BUSINESS_B); });
  assert.equal(other.data.access_token, BUSINESS_B);
  assert.equal(requests, 2);
  store.values.set(auth.STORAGE_KEY, JSON.stringify({ ...stored(store), expiresAt: Date.now() + 1000 }));
  await auth.persistedBusiness(OAUTH_B, store, async () => { requests += 1; return businessResponse(BUSINESS_B); });
  assert.equal(requests, 3);
  const boundary = new FakeStore();
  boundary.values.set(auth.STORAGE_KEY, JSON.stringify({
    ...stored(store),
    expiresAt: Date.now() + auth.CACHE_SKEW_MS,
  }));
  let boundaryRequests = 0;
  await auth.persistedBusiness(OAUTH_B, boundary, async () => {
    boundaryRequests += 1;
    return businessResponse(BUSINESS_B);
  });
  assert.equal(boundaryRequests, 1);
});

test("requisições concorrentes para a mesma conta são deduplicadas", async () => {
  const store = new FakeStore();
  let requests = 0;
  const request = async () => { requests += 1; await new Promise((resolve) => setTimeout(resolve, 10)); return businessResponse(); };
  const results = await Promise.all(Array.from({ length: 8 }, () => auth.persistedBusiness(OAUTH_A, store, request)));
  assert.equal(requests, 1);
  assert.deepEqual(results.map((item) => item.data.access_token), Array(8).fill(BUSINESS_A));
});

test("cleanup da trava local não deixa rejeição em voo", async () => {
  const store = new FakeStore();
  await assert.rejects(auth._private.withStoreLock(store, async () => {
    throw new Error("synthetic-lock-failure");
  }), /synthetic-lock-failure/);
  assert.equal(await auth._private.withStoreLock(store, async () => "ok"), "ok");
});

test("invalidação CAS não remove sessão nova e falha transitória preserva cache", async () => {
  const store = new FakeStore();
  await auth.persistedBusiness(OAUTH_A, store, async () => businessResponse(BUSINESS_A));
  const old = store.values.get(auth.STORAGE_KEY);
  const fresh = JSON.stringify({ ...stored(store), business: { code: 0, data: { access_token: BUSINESS_B, expires_in: 3600 } }, expiresAt: Date.now() + 3600_000 });
  store.deleteIfValue = async (key, expected) => { store.values.set(key, fresh); return false; };
  store.values.set(auth.STORAGE_KEY, old);
  assert.equal(await auth.invalidateBusiness(OAUTH_A, BUSINESS_A, store), false);
  assert.equal(store.values.get(auth.STORAGE_KEY), fresh);
  store.deleteIfValue = FakeStore.prototype.deleteIfValue;
  store.values.set(auth.STORAGE_KEY, JSON.stringify({ ...stored(store), expiresAt: Date.now() + 1000 }));
  await assert.rejects(auth.persistedBusiness(OAUTH_A, store, async () => { throw new Error("synthetic-timeout"); }), /synthetic-timeout/);
  assert.ok(store.values.has(auth.STORAGE_KEY));
});

test("CAS de save preserva resultado mais novo fora de ordem", async () => {
  const store = new FakeStore();
  await auth.persistedBusiness(OAUTH_A, store, async () => businessResponse(BUSINESS_A));
  const observed = store.values.get(auth.STORAGE_KEY);
  const newer = JSON.stringify({
    ...stored(store),
    oauthFingerprint: auth._private.fingerprint(OAUTH_B),
    business: { code: 0, data: { access_token: BUSINESS_B, expires_in: 3600 } },
    expiresAt: Date.now() + 3600_000,
  });
  store.saveIfValue = async (key, expected) => {
    assert.equal(expected, observed);
    store.values.set(key, newer);
    return false;
  };
  const result = await auth.persistedBusiness(OAUTH_B, store, async () => businessResponse(BUSINESS_A));
  assert.equal(result.data.access_token, BUSINESS_A);
  assert.equal(stored(store).business.data.access_token, BUSINESS_B);
});

test("cache malformado é tratado como miss e storage failure é seguro", async () => {
  const store = new FakeStore();
  store.values.set(auth.STORAGE_KEY, "not-json");
  await auth.persistedBusiness(OAUTH_A, store, async () => businessResponse());
  const broken = {
    load: async () => { throw new Error("synthetic-storage-detail"); },
    save: async () => {}, saveIfValue: async () => false, deleteIfValue: async () => false,
  };
  await assert.rejects(auth.persistedBusiness(OAUTH_A, broken, async () => businessResponse()), /business_session_storage_failed/);
  const error = await auth.persistedBusiness(OAUTH_A, broken, async () => businessResponse()).catch((caught) => caught);
  assert.equal(error.code, "business_session_storage_failed");
  assert.doesNotMatch(error.message, /synthetic-storage-detail/);
  let requests = 0;
  let casSaveAttempts = 0;
  const brokenSave = {
    load: async () => null,
    save: async () => { throw new Error("synthetic-save-detail"); },
    saveIfValue: async () => { casSaveAttempts += 1; throw new Error("synthetic-cas-save-detail"); },
    deleteIfValue: async () => false,
  };
  const saveError = await auth.persistedBusiness(OAUTH_B, brokenSave, async () => {
    requests += 1;
    return businessResponse(BUSINESS_B);
  }).catch((caught) => caught);
  assert.equal(requests, 1);
  assert.equal(casSaveAttempts, 1);
  assert.equal(saveError.code, "business_session_storage_failed");
  assert.doesNotMatch(saveError.message, /synthetic-cas-save-detail/);
});

test("launcher falha fechado em drift e aplica os pontos de integração da fixture", async () => {
  const drift = launcher.PATCHES.map(([, needle]) => needle).join("\n");
  assert.throws(() => launcher.patchSource(drift.replace("n.invalidate(v.business)", "n.invalidate(v.other)")), /patch drift/);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "zcode-launch-") );
  const fixture = path.join(directory, "fixture.cjs");
  const source = [
    "const e={locationStore:null,httpClient:null,env:{},credentialStore:null}; const h='synthetic-oauth'; const n={resolve:async(_g,fn)=>fn(),invalidate:()=>{}}; const o=async()=>({code:0,data:{access_token:'synthetic-business',expires_in:300}}); const nativeStore={async deleteIfValue(i,a){let c=pj(i),u=OK(a),f=!1;return await Hq(async g=>{let w=g[c];w===void 0||await o.decrypt(w)!==u||(delete g[c],f=!0)}),f}};",
    "function Yht(){return n.resolve(h,()=>o(`${Kgo}/api/auth/z/login`,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},body:JSON.stringify({token:h})}))}",
    "function obc(e){globalThis.__oauthStore=e.credentialStore;globalThis.__oauthEvents.push('business');if(process.argv.includes('--oauth-failure'))throw new Error('synthetic-business-failure');return Promise.resolve({token:'synthetic-business'});} function resolverMarker(e){return Yht({httpClient:e.httpClient??r0i(e.env)})}",
    "const Nae=async(value)=>value; async function Ooi(e){const b={saveZaiLoginCredentials:async()=>{if(process.argv.includes('--persist-failure'))throw new Error('synthetic-persist-failure');globalThis.__oauthEvents.push('oauth')}};const n='zai';const t={};const u=undefined;const v={loginStore:'fixture'};const R=await Promise.resolve({accessToken:'synthetic-oauth',token:'synthetic-jwt',user:{}}),N=await Nae(obc({accessToken:R.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u}),u);if(n==='zai')await b.saveZaiLoginCredentials({accessToken:R.accessToken,jwtToken:R.token,user:R.user});return N;} async function oauthReady(){return Ooi({httpClient:null});}",
    "async function retry(){const v={business:{token:'synthetic-business'}}; if(n.wasRejected&&n.wasRejected(v.business)){n.invalidate(v.business)}}",
    "function ttt(){};ttt();NPc();async function NPc(){await new Promise((resolve)=>setImmediate(resolve));globalThis.__fixtureArgv=process.argv[1];globalThis.__oauthEvents=[];globalThis.__oauthStore=null;try{await oauthReady();globalThis.__oauthResult='success';}catch(error){globalThis.__oauthResult='failed';}if(process.argv[2]==='--version')process.stdout.write('fixture-version\\n');}",
  ].join("\n");
  fs.writeFileSync(fixture, source, "utf8");
  assert.doesNotThrow(() => launcher.compileBundle(fixture, ["--version"]));
  await new Promise((resolve) => setImmediate(() => setImmediate(() => setImmediate(resolve))));
  assert.equal(globalThis.__fixtureArgv, path.resolve(fixture));
  assert.equal(process.argv[1], path.resolve(__filename));
  assert.deepEqual(globalThis.__oauthEvents, ["business", "oauth"]);
  assert.deepEqual(globalThis.__oauthStore, { loginStore: "fixture" });
  assert.equal(globalThis.__oauthResult, "success");
  launcher.compileBundle(fixture, ["--oauth-failure"]);
  await new Promise((resolve) => setImmediate(() => setImmediate(() => setImmediate(resolve))));
  assert.deepEqual(globalThis.__oauthEvents, ["business"]);
  assert.deepEqual(globalThis.__oauthStore, { loginStore: "fixture" });
  assert.equal(globalThis.__oauthResult, "failed");
  launcher.compileBundle(fixture, ["--persist-failure"]);
  await new Promise((resolve) => setImmediate(() => setImmediate(() => setImmediate(resolve))));
  assert.deepEqual(globalThis.__oauthEvents, ["business"]);
  assert.deepEqual(globalThis.__oauthStore, { loginStore: "fixture" });
  assert.equal(globalThis.__oauthResult, "failed");
  const patched = launcher.patchSource(source);
  assert.match(patched, /persistedBusiness/);
  assert.match(patched, /locationStore:e\.credentialStore/);
  assert.match(patched, /invalidateBusiness/);
  assert.match(patched, /saveIfValue/);
  assert.match(patched, /saveZaiLoginCredentials/);
});

test("candidate preserva os caminhos estáveis do launcher e da admissão", () => {
  const candidate = fs.readFileSync(path.join(__dirname, "..", "bin", "zcode-cli-auth-candidate"), "utf8");
  assert.match(candidate, /\$HOME\/\.local\/bin\/zcode_headless_admit\.py/);
  assert.match(candidate, /\$HOME\/\.local\/share\/zcode-worker\/auth\/launch\.cjs/);
  assert.match(candidate, /python3 \"\$HELPER\" --exec node \"\$LAUNCHER\"/);
});

test("launcher rejeita pontos ausentes e duplicados antes de compilar", () => {
  const source = launcher.PATCHES.map(([, needle]) => needle).join("\n");
  for (const [, needle] of launcher.PATCHES) {
    assert.throws(() => launcher.patchSource(source.replace(needle, "")), /patch drift/);
    assert.throws(() => launcher.patchSource(source + "\n" + needle), /patch drift/);
  }
});

test("launcher aplica Start Plan só com opt-in e falha com drift próprio", () => {
  assert.equal(launcher.PATCHES.length, 6);
  assert.equal(launcher.START_PLAN_PATCHES.length, 2);
  const base = launcher.PATCHES.map(([, needle]) => needle).join("\n");
  const startNeedles = launcher.START_PLAN_PATCHES.map(([, needle]) => needle).join("\n");
  const src = `${base}\n${startNeedles}`;
  const plain = launcher.patchSource(src);
  for (const [, needle, replacement] of launcher.START_PLAN_PATCHES) {
    assert.ok(plain.includes(needle));
    assert.ok(!plain.includes(replacement));
  }
  const withPlan = launcher.patchSource(src, { startPlan: true });
  for (const [, needle, replacement] of launcher.START_PLAN_PATCHES) {
    assert.ok(withPlan.includes(replacement));
  }
  const missing = src.replace(launcher.START_PLAN_PATCHES[0][1], "");
  assert.doesNotThrow(() => launcher.patchSource(missing));
  assert.throws(
    () => launcher.patchSource(missing, { startPlan: true }),
    (error) => error instanceof Error && error.message === "auth launcher start-plan drift",
  );
  const duplicated = `${src}\n${launcher.START_PLAN_PATCHES[1][1]}`;
  assert.throws(
    () => launcher.patchSource(duplicated, { startPlan: true }),
    (error) => error instanceof Error && error.message === "auth launcher start-plan drift",
  );
});

test("fixture executável exercita a autenticação Start Plan inserida no bundle", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "zcode-start-plan-"));
  const fixture = path.join(directory, "fixture.cjs");
  const source = [
    "const e={locationStore:null,httpClient:null,env:{},credentialStore:null};const h='synthetic-oauth';const n={resolve:async(_g,fn)=>fn(),invalidate:()=>{}};const o=async()=>({code:0,data:{access_token:'synthetic-business',expires_in:300}});const nativeStore={async deleteIfValue(i,a){let c=pj(i),u=OK(a),f=!1;return await Hq(async g=>{let w=g[c];w===void 0||await o.decrypt(w)!==u||(delete g[c],f=!0)}),f}};",
    "function Yht(){return n.resolve(h,()=>o(`${Kgo}/api/auth/z/login`,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},body:JSON.stringify({token:h})}))}",
    "function resolverMarker(e){return Yht({httpClient:e.httpClient??r0i(e.env)})}",
    "function obc(e){return Promise.resolve({token:'synthetic-business'})}const Nae=async(value)=>value;async function Ooi(e){const b={saveZaiLoginCredentials:async()=>{}};const n='zai';const t={};const u=undefined;const v={loginStore:'fixture'};const R=await Promise.resolve({accessToken:'synthetic-oauth',token:'synthetic-jwt',user:{}}),N=await Nae(obc({accessToken:R.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u}),u);if(n==='zai')await b.saveZaiLoginCredentials({accessToken:R.accessToken,jwtToken:R.token,user:R.user});return N;}",
    "async function retry(){const v={business:{token:'synthetic-business'}};if(n.wasRejected&&n.wasRejected(v.business)){n.invalidate(v.business)}}",
    "const catalog={individual:{type:'zhipu-account',mode:'individual-coding-plan',accountType:'coding'},start:{type:'zhipu-account',mode:'start-plan',accountType:'coding'},team:{type:'zhipu-account',mode:'team',accountType:'coding'}};function accepts(c,u){return c?.type===\"zhipu-account\"&&c.mode===\"individual-coding-plan\"&&c.accountType&&u}",
    "async function applyHeaders(e,u,d){if(!d||d.mode!==\"individual-coding-plan\")throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`);return{headersApplied:!0,individual:!0}}",
    "async function runScenarios(){globalThis.__startPlanOutcomes=[];const out=globalThis.__startPlanOutcomes;const loader=(value)=>({load:async(key)=>key==='zcodejwttoken'?value:undefined});const attempt=async(name,value,account)=>{try{const applied=await applyHeaders(loader(value),'synthetic-url',account);out.push(name+':ok:headers='+applied.headersApplied+':apiKey='+(applied.requestAuth?applied.requestAuth.apiKey:'none'));}catch(error){out.push(name+':throw:'+error.message);}};await attempt('valid','  synthetic-start-jwt  ',catalog.start);await attempt('missing',null,catalog.start);await attempt('empty','',catalog.start);await attempt('blank','   ',catalog.start);await attempt('team','synthetic-team-jwt',catalog.team);await attempt('individual','synthetic-individual-jwt',catalog.individual);await attempt('anonymous',null,null);out.push('catalog:individual:'+accepts(catalog.individual,'catalog-token'));out.push('catalog:start:'+accepts(catalog.start,'catalog-token'));out.push('catalog:team:'+accepts(catalog.team,'catalog-token'));}",
    "function ttt(){};ttt();NPc();async function NPc(){globalThis.__startPlanReady=runScenarios();}",
  ].join("\n");
  fs.writeFileSync(fixture, source, "utf8");
  const previousFlag = process.env.ZCODE_WORKER_START_PLAN_PATCHES;
  process.env.ZCODE_WORKER_START_PLAN_PATCHES = "1";
  try {
    launcher.compileBundle(fixture, []);
    await new Promise((resolve) => setImmediate(resolve));
    await globalThis.__startPlanReady;
  } finally {
    if (previousFlag === undefined) delete process.env.ZCODE_WORKER_START_PLAN_PATCHES;
    else process.env.ZCODE_WORKER_START_PLAN_PATCHES = previousFlag;
  }
  assert.deepEqual(globalThis.__startPlanOutcomes, [
    "valid:ok:headers=true:apiKey=synthetic-start-jwt",
    "missing:throw:project_token_login_required",
    "empty:throw:project_token_login_required",
    "blank:throw:project_token_login_required",
    "team:throw:Standalone Account Provider \u8BF7\u6C42\u8EAB\u4EFD\u65E0\u6548: synthetic-url",
    "individual:ok:headers=true:apiKey=none",
    "anonymous:throw:Standalone Account Provider \u8BF7\u6C42\u8EAB\u4EFD\u65E0\u6548: synthetic-url",
    "catalog:individual:catalog-token",
    "catalog:start:catalog-token",
    "catalog:team:false",
  ]);
});
