"use strict";

const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const HELPER_GLOBAL = "__zcodeAuthSessionCandidate";
const HELPER_FILE = path.join(__dirname, "zai-session.cjs");

// CAS de escrita que o store nativo não expõe; nomes locais com "$" não colidem com o bundle.
function casSave(key, val, mutate, cipher) {
  return `async saveIfValue($k,$e,$n){let $s=${key}($k),$x=$e===null?null:${val}($e),$c=await ${cipher}.encrypt(${val}($n)),$d=!1;` +
    `return await ${mutate}(async $m=>{let $w=$m[$s];($w===void 0?null:await ${cipher}.decrypt($w))===$x&&($m[$s]=$c,$d=!0)}),$d},`;
}

// nomes portados para o bundle 3.18.0 (10/11/2026); FLEX cobre renomeação do minificador
const PATCHES = [
  [
    "business request",
    'n.resolve(h,()=>o(`${zxo}/api/auth/z/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:h})}))',
    'n.resolve(h,()=>globalThis.__zcodeAuthSessionCandidate.persistedBusiness(h,e.locationStore,()=>o(`${zxo}/api/auth/z/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:h})})))',
  ],
  [
    "resolver store",
    "ivt({httpClient:e.httpClient??ZIi(e.env)})",
    "ivt({httpClient:e.httpClient??ZIi(e.env),locationStore:e.credentialStore??KE({env:e.env})})",
  ],
  // "oauth ready persistence" saiu do conjunto: o bundle de 19/10/2026 já persiste as
  // credenciais zai nativamente (if(n==="zai")await b.saveZaiLoginCredentials(...)).
  [
    "login store",
    "OOc({accessToken:B.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u})",
    "OOc({accessToken:B.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u,credentialStore:v})",
  ],
  [
    "business invalidation",
    "n.invalidate(v.business)",
    "await n.invalidate(v.business),await globalThis.__zcodeAuthSessionCandidate.invalidateBusinessByToken(v.business.token,e.locationStore)",
  ],
  [
    "native credential CAS",
    "async deleteIfValue(h,_){let v=Yj(h),S=awe(_)",
    `${casSave("Yj", "awe", "a", "o")}async deleteIfValue(h,_){let v=Yj(h),S=awe(_)`,
  ],
  [
    "async bundle lifecycle",
    "Zrt();NUc();async function NUc(){",
    "Zrt();globalThis.__zcodeAuthSessionCandidate.runBundleMain(NUc);async function NUc(){",
  ],
];

// Fallback por forma: se o literal não casa (o minificador renomeou identificadores),
// casa a mesma estrutura com nomes livres. Exige ocorrência única; senão, drift.
const ID = "([\\w$]+)";
const HELPER = "globalThis.__zcodeAuthSessionCandidate";

function once(source, pattern, build) {
  const matches = [...source.matchAll(new RegExp(pattern, "g"))];
  if (matches.length !== 1) return null;
  const m = matches[0];
  return source.slice(0, m.index) + build(m) + source.slice(m.index + m[0].length);
}

// Variável do credential store no fluxo de login e a fábrica dele ({env}).
function loginStoreNames(source) {
  const anchor = ID + "=e\\.credentialStore\\?\\?" + ID + "\\(\\{env:[\\w$]+\\}\\),[\\w$]+=await \\1\\.captureRecoveryGuard\\(\\)";
  const found = [...source.matchAll(new RegExp(anchor, "g"))];
  if (found.length !== 1) return null;
  return { store: found[0][1], factory: found[0][2] };
}

const LOGIN_CALL = "([\\w$]+\\(`\\$\\{[\\w$]+\\}/api/auth/z/login`,\\{method:\"POST\",headers:\\{\"Content-Type\":\"application/json\"\\},body:JSON\\.stringify\\(\\{token:\\2\\}\\)\\}\\))";
const CAS_SHAPE = "async deleteIfValue\\(" + ID + "," + ID + "\\)\\{let " + ID + "=" + ID + "\\(\\1\\)," + ID + "=" + ID + "\\(\\2\\)," + ID +
  "=!1;return await " + ID + "\\(async " + ID + "=>\\{let " + ID + "=\\9\\[\\3\\];\\10===void 0\\|\\|await " + ID + "\\.decrypt\\(";

const FLEX = {
  "business request": (s) => once(s, ID + "\\.resolve\\(" + ID + ",\\(\\)=>" + LOGIN_CALL + "\\)",
    (m) => `${m[1]}.resolve(${m[2]},()=>${HELPER}.persistedBusiness(${m[2]},e.locationStore,()=>${m[3]}))`),
  "resolver store": (s) => {
    const names = loginStoreNames(s);
    return names && once(s, ID + "\\(\\{httpClient:e\\.httpClient\\?\\?" + ID + "\\(e\\.env\\)\\}\\)",
      (m) => `${m[1]}({httpClient:e.httpClient??${m[2]}(e.env),locationStore:e.credentialStore??${names.factory}({env:e.env})})`);
  },
  "login store": (s) => {
    const names = loginStoreNames(s);
    return names && once(s, ID + "\\(\\{accessToken:" + ID + "\\.accessToken,env:" + ID + ",httpClient:e\\.httpClient,family:" + ID +
      ",resolver:e\\.apiKeyResolver,signal:" + ID + "\\}\\)", (m) => `${m[0].slice(0, -2)},credentialStore:${names.store}})`);
  },
  "business invalidation": (s) => once(s, ID + "\\.invalidate\\(" + ID + "\\.business\\)",
    (m) => `await ${m[0]},await ${HELPER}.invalidateBusinessByToken(${m[2]}.business.token,e.locationStore)`),
  "native credential CAS": (s) => once(s, CAS_SHAPE, (m) => casSave(m[4], m[6], m[8], m[11]) + m[0]),
  "async bundle lifecycle": (s) => once(s, ID + "\\(\\);" + ID + "\\(\\);async function \\2\\(\\)\\{",
    (m) => `${m[1]}();${HELPER}.runBundleMain(${m[2]});async function ${m[2]}(){`),
};

// Start Plan no CLI standalone: o catálogo de conta só aceitava
// individual-coding-plan e o refresh de headers rejeitava o modo start-plan.
// O app usa start-plan com o `zcodejwttoken` como apiKey e sem assinatura
// (requiresClientRequestSigning === false para esse modo).
const START_PLAN_PATCHES = [
  [
    "start-plan catalog",
    'c?.type==="zhipu-account"&&c.mode==="individual-coding-plan"&&c.accountType&&u',
    'c?.type==="zhipu-account"&&(c.mode==="individual-coding-plan"||c.mode==="start-plan")&&c.accountType&&u',
  ],
  [
    "start-plan runtime headers",
    'if(!d||d.mode!=="individual-coding-plan")throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`)',
    'if(!d)throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`);if(d.mode==="start-plan"){let z=(await e.load("zcodejwttoken"))?.trim();if(!z)throw new Error("project_token_login_required");return{headersApplied:!0,requestAuth:{apiKey:z}}}if(d.mode!=="individual-coding-plan")throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`)',
  ],
];

function patchSource(source, options = {}) {
  if (typeof source !== "string") throw new Error("auth launcher source inválida");
  let patched = source;
  for (const [name, needle, replacement] of PATCHES) {
    const count = patched.split(needle).length - 1;
    const next = count === 1 ? patched.replace(needle, () => replacement) : count === 0 ? FLEX[name](patched) : null;
    if (typeof next !== "string") throw new Error(`auth launcher patch drift: ${name}`);
    patched = next;
  }
  if (options.startPlan === true) {
    for (const [, needle] of START_PLAN_PATCHES) {
      const count = patched.split(needle).length - 1;
      if (count !== 1) throw new Error("auth launcher start-plan drift");
    }
    for (const [, needle, replacement] of START_PLAN_PATCHES) {
      patched = patched.replace(needle, replacement);
    }
  }
  return patched;
}

function compileBundle(bundlePath, argv) {
  const filename = path.resolve(bundlePath);
  let source;
  try {
    source = fs.readFileSync(filename, "utf8");
  } catch {
    throw new Error("auth launcher bundle indisponível");
  }
  const patched = patchSource(source, { startPlan: process.env.ZCODE_WORKER_START_PLAN_PATCHES === "1" });
  const originalArgv = process.argv;
  const helper = require(HELPER_FILE);
  globalThis[HELPER_GLOBAL] = {
    ...helper,
    runBundleMain(mainFunction) {
      const pending = Promise.resolve().then(mainFunction);
      const restore = () => {
        process.argv = originalArgv;
      };
      pending.then(restore, () => {
        restore();
        process.exitCode = 1;
      });
      return pending;
    },
  };
  process.argv = [originalArgv[0], filename, ...argv];
  const bundle = new Module(filename, module);
  bundle.filename = filename;
  bundle.paths = Module._nodeModulePaths(path.dirname(filename));
  try {
    return bundle._compile(patched, filename);
  } catch (error) {
    process.argv = originalArgv;
    throw error;
  }
}

function main(argv = process.argv.slice(2)) {
  const [bundlePath, ...bundleArgv] = argv;
  if (!bundlePath) throw new Error("auth launcher bundle obrigatório");
  return compileBundle(bundlePath, bundleArgv);
}

module.exports = { HELPER_GLOBAL, FLEX, PATCHES, START_PLAN_PATCHES, patchSource, compileBundle, main };

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : "auth launcher falhou"}\n`);
    process.exitCode = 1;
  }
}
