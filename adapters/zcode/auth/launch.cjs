"use strict";

const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const HELPER_GLOBAL = "__zcodeAuthSessionCandidate";
const HELPER_FILE = path.join(__dirname, "zai-session.cjs");

// nomes portados para o bundle de 02/11/2026
const PATCHES = [
  [
    "business request",
    'n.resolve(h,()=>o(`${Kgo}/api/auth/z/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:h})}))',
    'n.resolve(h,()=>globalThis.__zcodeAuthSessionCandidate.persistedBusiness(h,e.locationStore,()=>o(`${Kgo}/api/auth/z/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:h})})))',
  ],
  [
    "resolver store",
    "Yht({httpClient:e.httpClient??r0i(e.env)})",
    "Yht({httpClient:e.httpClient??r0i(e.env),locationStore:e.credentialStore??NP({env:e.env})})",
  ],
  // "oauth ready persistence" saiu do conjunto: o bundle de 19/10/2026 já persiste as
  // credenciais zai nativamente (if(n==="zai")await b.saveZaiLoginCredentials(...)).
  [
    "login store",
    "obc({accessToken:R.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u})",
    "obc({accessToken:R.accessToken,env:t,httpClient:e.httpClient,family:n,resolver:e.apiKeyResolver,signal:u,credentialStore:v})",
  ],
  [
    "business invalidation",
    "n.invalidate(v.business)",
    "await n.invalidate(v.business),await globalThis.__zcodeAuthSessionCandidate.invalidateBusinessByToken(v.business.token,e.locationStore)",
  ],
  [
    "native credential CAS",
    "async deleteIfValue(i,a){let c=pj(i),u=OK(a)",
    "async saveIfValue(i,a,c){let u=pj(i),f=a===null?null:OK(a),g=o.encrypt(OK(c)),_=!1;return await Hq(n,h=>{let w=h[u];(w===void 0?null:o.decrypt(w))===f&&(h[u]=g,_=!0)}),_},async deleteIfValue(i,a){let c=pj(i),u=OK(a)",
  ],
  [
    "async bundle lifecycle",
    "ttt();NPc();async function NPc(){",
    "ttt();globalThis.__zcodeAuthSessionCandidate.runBundleMain(NPc);async function NPc(){",
  ],
];

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
    'if(!f||f.mode!=="individual-coding-plan")throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`)',
    'if(!f)throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`);if(f.mode==="start-plan"){let z=(await e.load("zcodejwttoken"))?.trim();if(!z)throw new Error("project_token_login_required");return{headersApplied:!0,requestAuth:{apiKey:z}}}if(f.mode!=="individual-coding-plan")throw new Error(`Standalone Account Provider \\u8BF7\\u6C42\\u8EAB\\u4EFD\\u65E0\\u6548: ${u}`)',
  ],
];

function patchSource(source, options = {}) {
  if (typeof source !== "string") throw new Error("auth launcher source inválida");
  let patched = source;
  for (const [name, needle, replacement] of PATCHES) {
    const count = patched.split(needle).length - 1;
    if (count !== 1) throw new Error(`auth launcher patch drift: ${name}`);
    patched = patched.replace(needle, replacement);
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

module.exports = { HELPER_GLOBAL, PATCHES, START_PLAN_PATCHES, patchSource, compileBundle, main };

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : "auth launcher falhou"}\n`);
    process.exitCode = 1;
  }
}
