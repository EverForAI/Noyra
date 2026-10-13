const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const source = readFileSync(join(__dirname, "../../src/noyra/web/admin.js"), "utf8");
function functionsBetween(start, end) {
  const from = source.indexOf(start);
  const until = source.indexOf(end, from + start.length);
  assert.ok(from >= 0 && until > from);
  return source.slice(from, until);
}
function context(extra = {}) {
  const nodes = new Map();
  const sandbox = {
    csrfToken: "synthetic-session", overviewLoadVersion: 0, latestDiagnostics: null,
    adminSessionGeneration: 0, adminReadVersions: new Map(), activeLoginAttempt: null,
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", textContent: "", value:"", dataset:{},
        options:[], querySelector:()=>null, replaceChildren(){this.innerHTML="";this.textContent="";} });
      return nodes.get(selector);
    },
    esc: value => String(value ?? "").replaceAll("<", "&lt;"),
    formatTimestamp: value => value || "none",
    formatInteger: value => String(value),
    summaryRows: rows => JSON.stringify(rows),
    errorText: error => error.code || error.message,
    lifecycleLabels: { active: "active" }, logStatusLabels: {},
    modelPoolLabels: { economy: "economy", deep: "deep" },
    activityLabel: row => row.summary, activityStatus: row => row.status,
    setStatus: (selector, value) => {
      if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", textContent: "" });
      nodes.get(selector).textContent = value;
    },
    logout: async () => {}, ...extra,
  };
  const isolated = vm.createContext(sandbox);
  vm.runInContext(functionsBetween("function beginAdminRead(", "let activeSection ="), isolated);
  return { sandbox: isolated, nodes };
}

test("wallet amounts retain exact values across zero and maximum precision", () => {
  const { sandbox } = context({ walletAssetLookup: () => new Map([
    ["eth", { decimals: 18, symbol: "ETH" }],
    ["zero", { decimals: 0, symbol: "Z" }],
    ["max", { decimals: 255, symbol: "M" }],
  ]) });
  vm.runInContext(functionsBetween("function walletAmount(", "function walletObservationLabel("), sandbox);
  assert.equal(sandbox.walletAmount("900719925474099312345", "eth"), "900.719925474099312345 ETH");
  assert.equal(sandbox.walletAmount("0", "eth"), "0 ETH");
  assert.equal(sandbox.walletAmount("42", "zero"), "42 Z");
  assert.equal(sandbox.walletAmount("1", "max"), "0." + "0".repeat(254) + "1 M");
  assert.ok(sandbox.walletAmount("900719925474099312345", "missing").startsWith("900719925474099312345 "));
  assert.ok(!sandbox.walletAmount(9007199254740992, "eth").includes("ETH"));
  assert.ok(!sandbox.walletAmount("1e18", "eth").includes("ETH"));
});

function modelContext(extra = {}) {
  const { sandbox, nodes } = context({ integerFormatter: { format: String }, ...extra });
  vm.runInContext(functionsBetween("const parameterHelp =", "function addParameterHelp("), sandbox);
  vm.runInContext(functionsBetween("const SQLITE_INT64_MAX =", "function submitButton("), sandbox);
  vm.runInContext(functionsBetween("function readModelLimits(", "async function loadModels("), sandbox);
  return { sandbox, nodes };
}

function requestContext(extra = {}) {
  const { sandbox, nodes } = context({ upgradeErrorLabels: {}, ...extra });
  vm.runInContext(functionsBetween("const describeRequestError =", "function showSection("), sandbox);
  return { sandbox, nodes };
}

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

for (const oldOutcome of ["success", "failure"]) {
  test(`old login ${oldOutcome} cannot unlock a newer login after logout`, async () => {
    const oldLoad = deferred(), nextSession = deferred();
    const button = { disabled: false };
    let calls = 0;
    const { sandbox } = context({ csrfToken: "", submitButton: () => button,
      stopUpgradePolling: () => {}, stopRuntimeLogPolling: () => {},
      clearChat: () => {}, clearWallet: () => {}, clearPublicPublications: () => {}, document: { querySelectorAll: () => [] },
      renderedModels: new Map(), publicPostActionKeys: new Map(), publicPostLoadVersion: 0,
      modelLoadVersion: 0, usageLoadVersion: 0, usageResourceVersion: 0, healthLoadVersion: 0,
      healthPages: { model: 0, search: 0 },
      request: path => path.endsWith("/logout") ? Promise.resolve({}) :
        ++calls === 1 ? Promise.resolve({ csrf_token: "first" }) : nextSession.promise,
      loadAll: () => oldLoad.promise,
    });
    vm.runInContext(functionsBetween("async function login(", "async function loadOverview("), sandbox);
    sandbox.$("#login-token").value = "synthetic";
    const event = { preventDefault() {}, currentTarget: {} };
    const old = sandbox.login(event);
    await new Promise(setImmediate);
    await sandbox.logout();
    assert.equal(button.disabled, false);
    sandbox.$("#login-token").value = "synthetic-new";
    const fresh = sandbox.login(event);
    await sandbox.login(event); // Double submit cannot create a second session request.
    assert.equal(calls, 2);
    assert.equal(button.disabled, true);
    if (oldOutcome === "failure") oldLoad.reject(new Error("old private error"));
    else oldLoad.resolve();
    await old;
    assert.equal(button.disabled, true);
    assert.ok(!sandbox.$("#login-status").textContent.includes("old private error"));
    sandbox.loadAll = async () => {};
    nextSession.resolve({ csrf_token: "second" });
    await fresh;
    assert.equal(button.disabled, false);
    assert.equal(sandbox.activeLoginAttempt, null);
    assert.equal(sandbox.csrfToken, "second");
  });
}

test("current login error restores submit ownership and allows retry", async () => {
  const button = { disabled: false };
  const { sandbox } = context({ submitButton: () => button,
    request: async () => { throw new Error("current login error"); } });
  vm.runInContext(functionsBetween("async function login(", "async function logout("), sandbox);
  sandbox.$("#login-token").value = "synthetic";
  await sandbox.login({ preventDefault() {}, currentTarget: {} });
  assert.equal(button.disabled, false);
  assert.equal(sandbox.activeLoginAttempt, null);
  assert.equal(sandbox.$("#login-status").textContent, "current login error");
});

test("cost-only protection rejects unknown and zero-priced drafts but allows free or bounded alternatives", () => {
  const { sandbox } = modelContext();
  const payload={limit_enabled:{attempts:false,input_tokens:false,output_tokens:false,cost:true},pricing_status:"unknown",input_usd_per_million:"0.000000",output_usd_per_million:"0.000000"};
  assert.throws(()=>sandbox.validateCostProtection(payload), /成本上限/);
  payload.pricing_status="priced";
  assert.throws(()=>sandbox.validateCostProtection(payload), /成本上限/);
  payload.pricing_status="free";
  assert.doesNotThrow(()=>sandbox.validateCostProtection(payload));
  payload.pricing_status="unknown";payload.limit_enabled.attempts=true;
  assert.doesNotThrow(()=>sandbox.validateCostProtection(payload));
  payload.limit_enabled.attempts=false;payload.input_usd_per_million="0.000001";
  assert.doesNotThrow(()=>sandbox.validateCostProtection(payload));
});

for (const method of ["GET", "POST"]) {
  for (const completion of ["success", "unauthorized", "network", "body"]) {
    test(`${method} request discards ${completion} from a previous session`, async () => {
      let resolve;
      let reject;
      const response = new Promise((yes, no) => { resolve = yes; reject = no; });
      const { sandbox } = requestContext({ fetch: () => response });
      const pending = sandbox.request("/api/admin/fixture", { method });
      if (completion === "body") {
        let bodyResolve;
        const body = new Promise(yes => { bodyResolve = yes; });
        resolve({ ok:true, json:() => body });
        await Promise.resolve();
        sandbox.adminSessionGeneration++;
        sandbox.csrfToken = "another-session";
        bodyResolve({ private_metadata:"old" });
      } else {
        sandbox.adminSessionGeneration++;
        sandbox.csrfToken = "another-session";
        if (completion === "network") reject(new Error("old network error"));
        else resolve({ ok:completion === "success", status:401,
          json:async() => ({ private_metadata:"old", error:"unauthorized" }) });
      }
      await assert.rejects(pending, error => {
        assert.equal(error.staleSession, true);
        assert.equal(error.status, undefined);
        assert.equal(error.code, "admin_session_changed");
        return true;
      });
    });
  }
}

test("current-session request keeps CSRF, normal data and HTTP errors", async () => {
  let headers;
  const { sandbox } = requestContext({ fetch:async(_, options) => {
    headers=options.headers;
    return { ok:true, json:async() => ({ saved:true }) };
  } });
  const result=await sandbox.request("/api/admin/fixture", { method:"POST", body:"{}" });
  assert.equal(result.saved, true);
  assert.equal(headers["X-CSRF-Token"], "synthetic-session");
  sandbox.fetch=async() => ({ok:false,status:401,json:async() => ({error:"unauthorized"})});
  await assert.rejects(sandbox.request("/api/admin/fixture"), error => error.status === 401);
});

test("stale write results cannot reopen a chat or replace current status", async () => {
  let resolve;
  let handler;
  let selected=0;
  const response = new Promise(yes => { resolve=yes; });
  const { sandbox, nodes } = requestContext({
    fetch:() => response, prompt:() => "new chat", selectChat:() => { selected++; },
    loadMailbox:async() => {},
  });
  sandbox.$("#new-conversation").addEventListener=(_, action) => { handler=action; };
  const status=sandbox.$("#message-status"); status.textContent="new-session-status";
  vm.runInContext(functionsBetween('$("#new-conversation").addEventListener', 'for(const [id,action]'), sandbox);
  const pending=handler();
  sandbox.adminSessionGeneration++; sandbox.csrfToken="another-session";
  resolve({ok:true,json:async() => ({conversation_id:"old-private-window",title:"private title"})});
  await pending;
  assert.equal(selected,0);
  assert.equal(nodes.get("#message-status").textContent,"new-session-status");
});

test("each model limit combination ignores disabled values and requires enabled values", () => {
  const { sandbox } = modelContext();
  const fields = { attempts:"daily_attempts", input_tokens:"daily_input_tokens", output_tokens:"daily_output_tokens", cost:"daily_cost_limit_usd" };
  for (const attribute of ["data-draft-limit", "data-limit-dimension"]) {
    for (let mask = 0; mask < 16; mask++) {
      const enabled = Object.fromEntries(Object.keys(fields).map((name, i) => [name, Boolean(mask & (1 << i))]));
      const form = { elements:{}, querySelector:selector => ({ checked:enabled[selector.match(/="([^"]+)"/)[1]] }) };
      for (const [dimension, field] of Object.entries(fields)) form.elements[field] = { value:enabled[dimension] ? "0":"invalid ignored value" };
      const result = sandbox.readModelLimits(form, attribute);
      assert.deepEqual(JSON.parse(JSON.stringify(result.limit_enabled)), enabled);
      for (const [dimension, field] of Object.entries(fields)) {
        assert.equal(field in result, enabled[dimension]);
        if (enabled[dimension]) {
          form.elements[field].value = "";
          assert.throws(() => sandbox.readModelLimits(form, attribute), error => error.budgetField === field);
          form.elements[field].value = "0";
        }
      }
    }
  }
});

test("model remaining allowance is exact and includes reservation only once", () => {
  const { sandbox } = modelContext();
  assert.equal(sandbox.modelBudgetRemaining("100", "35"), "65");
  assert.equal(sandbox.modelBudgetRemaining("10", "35"), "0");
  assert.equal(sandbox.modelBudgetRemaining("9223372036854775807", "1"), "9223372036854775806");
});

test("model deletion warning uses complete pool summary rather than filtered page size", () => {
  const { sandbox } = modelContext();
  const item={pool:"economy",label:"resource",status:"active",key_count:2,available_key_count:1,
    pool_summary:{active_resources:1,available_resources:1}};
  assert.ok(sandbox.modelDeletionMessage(item).includes("最后一个启用资源"));
  item.pool_summary.active_resources=3;
  assert.ok(sandbox.modelDeletionMessage(item).includes("没有其他具有可用密钥"));
  item.pool_summary.available_resources=2;
  assert.ok(!sandbox.modelDeletionMessage(item).includes("可能无法"));
});

function modelDetails() {
  const content={innerHTML:"",insertAdjacentHTML:(_position,html)=>{content.innerHTML+=html;}};
  return { dataset:{id:"resource",loadVersion:"1"}, isConnected:true,
    querySelector:()=>content, content };
}

test("model keys are read on expansion, retry locally and reject stale session responses", async () => {
  let calls=0;
  let resolve;
  const { sandbox }=modelContext({modelLoadVersion:1,renderedModels:new Map([["resource",{group_id:"resource",key_count:1}]]),
    renderModelKey:(_item,key)=>key.key_id,request:async()=>{calls++;if(calls===1)throw new Error("offline");return [{key_id:"metadata"}];}});
  const details=modelDetails();
  await sandbox.loadModelKeys(details);
  assert.ok(details.content.innerHTML.includes("data-model-keys-retry"));
  await sandbox.loadModelKeys(details);
  assert.equal(details.content.innerHTML,"metadata");
  await sandbox.loadModelKeys(details);
  assert.equal(calls,2);
  const stale=modelDetails();
  sandbox.request=()=>new Promise(done=>{resolve=done;});
  const pending=sandbox.loadModelKeys(stale);
  await new Promise(done=>setImmediate(done));
  sandbox.renderedModels.clear();
  resolve([{key_id:"late private metadata"}]);
  await pending;
  assert.ok(!stale.content.innerHTML.includes("late private metadata"));
  assert.equal(stale.dataset.loaded,undefined);
});

test("failed list refresh does not disable key details in the preserved list", async () => {
  const { sandbox }=modelContext({modelLoadVersion:2,renderedModels:new Map([["resource",{group_id:"resource",key_count:1}]]),
    renderModelKey:(_item,key)=>key.key_id,request:async()=>[{key_id:"preserved metadata"}]});
  const details=modelDetails();
  await sandbox.loadModelKeys(details);
  assert.equal(details.content.innerHTML,"preserved metadata");
});

test("expanding many key details bounds concurrent requests and skips detached queued panels", async () => {
  let inFlight=0,max=0;
  const completions=[];
  const { sandbox }=modelContext({modelLoadVersion:1,renderedModels:new Map([["resource",{group_id:"resource",key_count:0}]]),
    request:()=>{max=Math.max(max,++inFlight);return new Promise(resolve=>completions.push(()=>{inFlight--;resolve([]);}));}});
  const panels=Array.from({length:9},()=>modelDetails());
  const pending=panels.map(panel=>sandbox.loadModelKeys(panel));
  await new Promise(done=>setImmediate(done));
  assert.equal(completions.length,4);
  for(const panel of panels.slice(4))panel.isConnected=false;
  for(const complete of completions)complete();
  await Promise.all(pending);
  assert.equal(max,4);
  assert.equal(completions.length,4);
});

test("declining draft connection test never invokes the provider endpoint", async () => {
  let handler,writes=0;
  const { sandbox }=context({$:()=>({addEventListener:(_event,callback)=>{handler=callback;}}),
    window:{confirm:()=>false},request:async()=>{writes++;}});
  vm.runInContext(functionsBetween('$("#admin-model-test").addEventListener', '$("#admin-model-discover").addEventListener'), sandbox);
  const button={disabled:false};
  await handler({currentTarget:button});
  assert.equal(writes,0);
  assert.equal(button.disabled,false);
});

test("wallet freshness uses the effective server observation threshold", () => {
  const { sandbox } = context({ walletObservationHealth: {
    evaluated_at: "2026-10-10T00:01:00Z", stale_after_seconds: 60,
  } });
  vm.runInContext(functionsBetween("function walletObservationLabel(", "function walletRPCHost("), sandbox);
  const fresh = sandbox.walletObservationLabel("2026-10-10T00:00:55Z");
  const expired = sandbox.walletObservationLabel("2026-10-09T23:59:55Z");
  assert.notEqual(fresh, expired);
  sandbox.walletObservationHealth.stale_after_seconds = 120;
  assert.equal(sandbox.walletObservationLabel("2026-10-09T23:59:55Z"), fresh);
  assert.notEqual(sandbox.walletObservationLabel("2026-10-10T00:02:00Z"), fresh);
});

test("wallet pages navigate stable cursors and reset when the filter changes", async () => {
  const paths=[];
  const { sandbox } = context({URLSearchParams, walletListPages:new Map(), walletListVersions:new Map(), walletListEpoch:0,
    request:async path=>{paths.push(path);return {items:[{id:paths.length}],total:52,has_more:true,next_cursor:"synthetic-next"};},
  });
  vm.runInContext(functionsBetween("async function readWalletPage(", "async function loadWalletRegistry("),sandbox);
  await sandbox.readWalletPage("acquisitions",{status:"queued"});
  await sandbox.readWalletPage("acquisitions",{status:"queued",move:"next"});
  assert.ok(paths[1].includes("cursor=synthetic-next"));
  assert.equal(sandbox.walletListPages.get("acquisitions").items.length,1);
  await sandbox.readWalletPage("acquisitions",{status:"queued",move:"previous"});
  assert.ok(!paths[2].includes("cursor="));
  await sandbox.readWalletPage("acquisitions",{status:"unknown",move:"next"});
  assert.ok(!paths[3].includes("cursor="));
  assert.equal(sandbox.walletListPages.get("acquisitions").index,0);
});

test("wallet page responses cannot repopulate a cleared session", async () => {
  let resolve;
  const { sandbox } = context({URLSearchParams, walletListPages:new Map(), walletListVersions:new Map(), walletListEpoch:0,
    request:()=>new Promise(done=>{resolve=done;}),
  });
  vm.runInContext(functionsBetween("async function readWalletPage(", "async function loadWalletRegistry("),sandbox);
  const pending=sandbox.readWalletPage("orders");
  sandbox.walletListEpoch++;
  sandbox.walletListVersions.clear();
  resolve({items:[{id:"private"}],total:1,has_more:false,next_cursor:null});
  assert.equal(await pending,null);
  assert.equal(sandbox.walletListPages.size,0);
});

test("wallet order controls recheck the selected page and require a confirmation", async () => {
  const code=functionsBetween('$("#wallet-order-list").addEventListener("click"', '$("#admin-model-form").addEventListener');
  for(const action of ["execute","confirm","reject","cancel"]){
    const calls=[];
    let handler;
    const {sandbox}=context({walletOrders:[{order_id:"o",status:action==="execute"||action==="cancel" ? "reserved":"awaiting_confirmation",amount:"10",asset_id:"a",network_id:"n",recipient_address:"recipient"}],
      $:()=>({addEventListener:(_event,fn)=>{handler=fn;}}),loadWalletEconomy:async options=>{calls.push(options);},
      request:async path=>{calls.push(path);},walletRefreshAfterWrite:async()=>{},
      walletAmount:()=>"10 ETH",confirm:()=>false,prompt:()=>"test reason",setStatus:()=>{},
    });
    vm.runInContext(code,sandbox);
    const button={dataset:{walletOrderAction:action,orderId:"o"},disabled:false};
    await handler({target:{closest:()=>button}});
    assert.equal(calls.length,1);
    assert.equal(calls[0].move,"current");
    assert.equal(button.disabled,false);
  }
});

test("wallet order controls reject changed state and submit only the confirmed action", async () => {
  const code=functionsBetween('$("#wallet-order-list").addEventListener("click"', '$("#admin-model-form").addEventListener');
  for(const action of ["execute","confirm","reject","cancel"]){
    for(const changed of [true,false]){
      const writes=[];
      let handler;
      const {sandbox}=context({walletOrders:[],
        $:()=>({addEventListener:(_event,fn)=>{handler=fn;}}),
        loadWalletEconomy:async()=>{sandbox.walletOrders=[{order_id:"o",status:changed ? "confirmed":(["execute","cancel"].includes(action) ? "reserved":"awaiting_confirmation"),amount:"10",asset_id:"a",network_id:"n",recipient_address:"recipient"}];},
        request:async(path,options)=>{writes.push({path,options});},walletRefreshAfterWrite:async()=>{},
        walletAmount:()=>"10 ETH",confirm:()=>true,prompt:()=>"test reason",setStatus:()=>{},
      });
      vm.runInContext(code,sandbox);
      await handler({target:{closest:()=>({dataset:{walletOrderAction:action,orderId:"o"}})}});
      assert.equal(writes.length,changed ? 0:1);
      if(!changed){
        assert.equal(writes[0].path,`/api/admin/wallet-orders/o/${action}`);
        assert.equal(writes[0].options.method,"POST");
        assert.deepEqual(JSON.parse(writes[0].options.body),["reject","cancel"].includes(action) ? {reason:"test reason"}:{});
      }
    }
  }
});

test("overview reads only summary sources and preserves partial failure visibility", async () => {
  const paths = [];
  const { sandbox, nodes } = context({ request: async path => {
    paths.push(path);
    if (path === "/api/state") return { lifecycle: { state: "active" }, online: true };
    if (path === "/api/admin/lifecycle-summary") throw new Error("synthetic-unavailable");
    if (path.startsWith("/api/admin/activity-logs")) return { items: [] };
    return { cognition: { enabled: true, pending_interactions: [], waiting_interaction_tasks: [] },
      integrity: { status: "ok" }, operator_health: { archive_key: { configured: false } },
      wallet_execution: { configured: false }, unknown: { model_calls: 0 } };
  } });
  vm.runInContext(functionsBetween("async function loadOverview()", "function renderModelObservability("), sandbox);
  const failures = await sandbox.loadOverview();
  assert.equal(failures.length, 1);
  assert.equal(paths.length, 4);
  assert.ok(paths.every(path => !path.includes("upgrade") && !path.includes("wallet-") && !path.includes("model-resources")));
  assert.ok(nodes.get("#overview-attention").innerHTML.includes('data-priority="risk"'));
  assert.ok(!nodes.get("#overview-attention").innerHTML.includes('data-priority="blocker"'));
});

test("overview sorts blocking findings before risk and reminders and escapes text", () => {
  const { sandbox, nodes } = context();
  vm.runInContext(functionsBetween("async function loadOverview()", "function renderModelObservability("), sandbox);
  sandbox.renderOverviewAttention({ cognition: { enabled: true, pending_interactions: [{}] },
    storage: { cognition_allowed: false }, unknown: { model_calls: 1 } },
  [{ label: "<script>", error: new Error("<unsafe>") }]);
  const html = nodes.get("#overview-attention").innerHTML;
  assert.ok(html.indexOf('data-priority="blocker"') < html.indexOf('data-priority="risk"'));
  assert.ok(html.indexOf('data-priority="risk"') < html.indexOf('data-priority="reminder"'));
  assert.ok(!html.includes("<script>"));
});

test("overview does not treat missing cognition or optional encryption as blockers", () => {
  const { sandbox, nodes } = context();
  vm.runInContext(functionsBetween("async function loadOverview()", "function renderModelObservability("), sandbox);
  sandbox.renderOverviewAttention({operator_health:{at_rest:{ready:false,enforced:false}}});
  assert.ok(!nodes.get("#overview-attention").innerHTML.includes('data-priority="blocker"'));
  sandbox.renderOverviewAttention({cognition:{enabled:false}});
  assert.ok(nodes.get("#overview-attention").innerHTML.includes('data-priority="blocker"'));
});

test("overview retains previously loaded events and lifecycle when their reads fail", async () => {
  const { sandbox, nodes } = context({request:async()=>{throw new Error("offline");}});
  sandbox.$("#overview-events").innerHTML="older events";
  sandbox.$("#overview-lifecycle").innerHTML="older lifecycle";
  vm.runInContext(functionsBetween("async function loadOverview()", "function renderModelObservability("), sandbox);
  await sandbox.loadOverview();
  assert.equal(nodes.get("#overview-events").innerHTML,"older events");
  assert.equal(nodes.get("#overview-lifecycle").innerHTML,"older lifecycle");
  assert.ok(nodes.get("#overview-updated").textContent.includes("·"));
});

test("failed version checks display the server error without losing the installed version", async () => {
  const { sandbox, nodes } = context({ upgradeCheckResult: {},
    request: async () => { throw { status: 503, code: "upgrade_no_stable_release" }; },
    setStatus: (selector, value) => { sandbox.$(selector).textContent = value; },
  });
  sandbox.$("#upgrade-current-version").textContent = "installed-synthetic";
  vm.runInContext(functionsBetween("async function loadUpgradeCheck(", "async function loadUpgradePanel("), sandbox);
  assert.equal(await sandbox.loadUpgradeCheck(), null);
  assert.equal(nodes.get("#upgrade-current-version").textContent, "installed-synthetic");
  assert.equal(nodes.get("#upgrade-commit-title").textContent, "upgrade_no_stable_release");
});

test("chat logout clears local drafts without invoking a retired erasure control", () => {
  const chat={current:{conversation_id:"c"},windows:[{}],rows:[{}],drafts:new Map([["c","draft"]]),
    keys:new Map([["c","key"]]),generation:1,listGeneration:2,timer:42};
  const {sandbox,nodes}=context({chat,clearQQIdentity:()=>{},clearTimeout:()=>{}});
  for(const id of ["#mailbox-list","#conversation-list"]) sandbox.$(id).replaceChildren=()=>{};
  vm.runInContext(functionsBetween("function clearChat()", "async function loadMailbox("),sandbox);
  sandbox.clearChat();
  assert.equal(chat.current,null);
  assert.equal(chat.drafts.size,0);
  assert.equal(chat.keys.size,0);
  assert.equal(chat.generation,2);
  assert.equal(chat.listGeneration,3);
  assert.equal(nodes.get("#admin-message").value,"");
  assert.ok(!source.includes("erasure-assessment"));
  const html=readFileSync(join(__dirname,"../../src/noyra/web/admin.html"),"utf8");
  assert.ok(!html.includes("assess-conversation-erasure"));
  assert.ok(!html.includes("conversation-erasure-assessment"));
});

const readModules = [
  ["loadSearchProviders", "async function loadSearchProviders(", "async function loadCapabilities(", [[],{mode:"auto"}]],
  ["loadCapabilities", "async function loadCapabilities(", "async function loadChannels(", [[]]],
  ["loadMigration", "async function loadMigration(", "function summaryRows(", [{allowed_target_ids:[],allowed_regions:[]},[],[],[]]],
  ["loadChannels", "async function loadChannels(", "let qqIdentity =", [[],[]]],
  ["loadRuntime", "async function loadRuntime(", "async function loadAll(", [{cognition:{}},{usage:{}}]],
  ["loadSetupGuide", "async function loadSetupGuide(", "const chat =", [[],[],[],{}]],
  ["loadRetention", "async function loadRetention(", "async function runRetention(", [{}]],
];
for (const [name,start,end,payloads] of readModules) {
  test(`${name} discards old session responses even after a new login`,async()=>{
    const waiting=[];
    let renders=0;
    const {sandbox,nodes}=context({channelBindings:[],renderSetupGuide:()=>renders++,renderRetention:()=>renders++,
      request:()=>new Promise(resolve=>waiting.push(resolve))});
    vm.runInContext(functionsBetween(start,end),sandbox);
    const pending=sandbox[name]();
    sandbox.adminSessionGeneration++;
    sandbox.csrfToken="another-session";
    waiting.forEach((resolve,i)=>resolve(payloads[i]));
    await pending;
    assert.equal(renders,0);
    assert.equal(nodes.size,0);
    assert.equal(sandbox.channelBindings.length,0);
  });
  test(`${name} preserves latest refresh when an older response finishes last`,async()=>{
    const waiting=[];
    const {sandbox,nodes}=context({channelBindings:[],renderSetupGuide:()=>{},renderRetention:()=>{},
      request:()=>new Promise(resolve=>waiting.push(resolve))});
    vm.runInContext(functionsBetween(start,end),sandbox);
    const first=sandbox[name]();
    const count=waiting.length;
    const second=sandbox[name]();
    waiting.slice(count).forEach((resolve,i)=>resolve(payloads[i]));
    await second;
    const snapshot=JSON.stringify([...nodes]);
    waiting.slice(0,count).forEach((resolve,i)=>resolve(payloads[i]));
    await first;
    assert.equal(JSON.stringify([...nodes]),snapshot);
  });
}

test("stale private read errors do not become new-session errors",async()=>{
  let reject;
  const {sandbox}=context({request:()=>new Promise((_resolve,no)=>reject=no)});
  vm.runInContext(functionsBetween("async function loadCapabilities(","async function loadChannels("),sandbox);
  const pending=sandbox.loadCapabilities();
  sandbox.adminSessionGeneration++;
  reject({status:401,code:"unauthorized"});
  await pending;
});

test("logout clears every private panel, loaded option, cache and open dialog",async()=>{
  let closed=0;
  const form={dataset:{dirty:"1",revision:"7",lengthRevision:"9"},reset(){}};
  const secret={value:"synthetic secret"};
  const {sandbox,nodes}=context({request:async()=>null,stopUpgradePolling:()=>{},stopRuntimeLogPolling:()=>{},
    clearChat:()=>{},clearWallet:()=>{}, clearPublicPublications:()=>{},document:{querySelectorAll:selector=>{
      if(selector==="#admin-shell form")return [form];
      if(selector.includes("input[type=password]"))return [secret];
      if(selector==="dialog[open]")return [{close:()=>closed++}];
      return [];
    }},renderedModels:new Map([["private",{}]]),publicPostActionKeys:new Map(),
    publicPostLoadVersion:0,modelLoadVersion:0,usageLoadVersion:0,usageResourceVersion:0,
    healthLoadVersion:0,healthPages:{model:50,search:50}});
  vm.runInContext(functionsBetween("async function logout(","async function loadOverview("),sandbox);
  for(const id of ["search-provider-list","capability-list","channel-list","binding-list",
    "migration-policy-summary","migration-target-list","migration-proposal-list","migration-task-list",
    "runtime-summary","public-post-usage","setup-guide-list","model-observability-summary","retention-summary"])
    sandbox.$("#"+id).innerHTML="private data";
  await sandbox.logout();
  assert.equal(sandbox.csrfToken,"");
  assert.equal(secret.value,"");
  assert.deepEqual(form.dataset,{});
  assert.equal(sandbox.renderedModels.size,0);
  assert.equal(sandbox.channelBindings.length,0);
  assert.equal(closed,1);
  assert.ok([...nodes.values()].every(node=>!node.innerHTML.includes("private data")));
});

for (const stale of [false,true]) for (const failed of [false,true]) {
  test(`write refresh isolates session: stale=${stale}, failed=${failed}`,async()=>{
    let finish,reject;
    const waiting=new Promise((yes,no)=>{finish=yes;reject=no;});
    const {sandbox}=context();
    vm.runInContext(functionsBetween("async function walletRefreshAfterWrite(","function walletNetworkLookup("),sandbox);
    const pending=sandbox.walletRefreshAfterWrite("#status","saved",()=>waiting);
    if(stale){sandbox.adminSessionGeneration++;sandbox.csrfToken="new-session";sandbox.$("#status").textContent="new-session";}
    if(failed) reject(stale ? {staleSession:true} : {code:"read_failed"}); else finish();
    await pending;
    assert.equal(sandbox.$("#status").textContent,stale ? "new-session" : failed ? "saved；读取更新失败，请刷新核对。read_failed" : "saved");
  });
}

for (const module of ["chat","retention"]) for (const failed of [false,true]) {
  test(`${module} write continuation and buttons ignore old session (${failed ? "failure" : "success"})`,async()=>{
    let handler,finish,reject,entered;
    const reading=new Promise(yes=>entered=yes);
    const refresh=()=>{entered();return new Promise((yes,no)=>{finish=yes;reject=no;});};
    const button={disabled:false};
    const form={dataset:{revision:"1",dirty:"1"},querySelectorAll:()=>[]};
    const {sandbox}=context({submitButton:()=>button,request:async()=>({}),loadRetention:refresh,loadChatMessages:refresh,
      chat:{current:{status:"active",conversation_id:"one"},keys:new Map(),drafts:new Map()},crypto:{randomUUID:()=>"key"}});
    const selector=module==="chat" ? "#admin-message-form" : "#retention-settings-form";
    sandbox.$(selector).addEventListener=(_kind,callback)=>handler=callback;
    sandbox.$("#admin-message").value="hello";
    vm.runInContext(module==="chat" ? functionsBetween('$("#admin-message-form").addEventListener', '$("#wallet-enqueue-asset")') : functionsBetween('$("#retention-settings-form").addEventListener("submit"','let usageLoadVersion ='),sandbox);
    const pending=handler({preventDefault(){},currentTarget:form});
    await reading;
    sandbox.adminSessionGeneration++;sandbox.csrfToken="new-session";
    const status=module==="chat" ? "#message-status" : "#retention-status";
    sandbox.$(status).textContent="new-session";button.disabled=true;
    if(failed)reject({staleSession:true});else finish();
    await pending;
    assert.equal(sandbox.$(status).textContent,"new-session");
    assert.equal(button.disabled,true);
  });
}

test("dirty migration refresh retains its editing revision and all draft values",async()=>{
  let revision=1;
  const {sandbox}=context({request:async path=>path.endsWith("policy") ? {revision,max_cost_microusd:100,allowed_target_ids:[],allowed_regions:[]} : []});
  const form=sandbox.$("#migration-policy-form");
  const cost=sandbox.$("#migration-max-cost");form.elements=[cost];
  vm.runInContext(functionsBetween("async function loadMigration(","function summaryRows("),sandbox);
  await sandbox.loadMigration();assert.equal(form.dataset.revision,"1");
  form.dataset.dirty="1";cost.value="7";revision=2;
  await sandbox.loadMigration();assert.equal(form.dataset.revision,"1");assert.equal(cost.value,"7");
  delete form.dataset.dirty;
  await sandbox.loadMigration();assert.equal(form.dataset.revision,"2");assert.equal(cost.value,100);
});

function migrationSubmitContext(request,confirm=()=>true) {
  let handler;
  const {sandbox}=context({request,window:{confirm},loadMigration:async()=>{}});
  const form=sandbox.$("#migration-policy-form");form.elements=[];form.dataset={revision:"1",dirty:"1"};
  form.addEventListener=(_kind,callback)=>handler=callback;
  for(const [name,value] of Object.entries({"approval-mode":"disabled","wallet-mode":"external_signer_rebind","cooldown":"7","max-cost":"0","min-free":"0","trust":"3","downtime":"3600"}))sandbox.$("#migration-"+name).value=value;
  vm.runInContext(functionsBetween('$("#migration-policy-form").addEventListener("submit"','$("#admin-message-form")'),sandbox);
  return {sandbox,form,submit:()=>handler({preventDefault(){},currentTarget:form})};
}

test("migration old draft cannot submit with another writer's latest revision",async()=>{
  const writes=[];
  const {sandbox,form,submit}=migrationSubmitContext(async(_path,options)=>{if(options)writes.push(JSON.parse(options.body));return {revision:2};});
  await submit();assert.equal(writes.length,0);assert.equal(form.dataset.dirty,"1");assert.equal(form.dataset.revision,"1");
  assert.match(sandbox.$("#migration-status").textContent,/草稿已保留/);
});

test("migration captures draft before GET and preserves final backend CAS conflict",async()=>{
  let finish,saved;
  const {sandbox,form,submit}=migrationSubmitContext((_path,options)=>{
    if(!options)return new Promise(yes=>finish=yes);
    saved=JSON.parse(options.body);return Promise.reject({status:409});
  });
  const control={disabled:false};form.elements=[control];
  const pending=submit();assert.equal(control.disabled,true);
  sandbox.$("#migration-max-cost").value="100";
  finish({revision:1});await pending;
  assert.equal(saved.expected_revision,1);assert.equal(saved.max_cost_microusd,0);
  assert.equal(form.dataset.dirty,"1");assert.equal(control.disabled,false);
  assert.match(sandbox.$("#migration-status").textContent,/版本冲突/);
});

test("migration risk confirmation still blocks entering automatic mode",async()=>{
  let writes=0,confirmations=0;
  const {sandbox,form,submit}=migrationSubmitContext(async(_path,options)=>{if(options)writes++;return {revision:1,approval_mode:"disabled"};},()=>{confirmations++;return false;});
  sandbox.$("#migration-approval-mode").value="policy_auto";
  await submit();assert.equal(writes,0);assert.equal(confirmations,1);assert.equal(form.dataset.dirty,"1");
});
