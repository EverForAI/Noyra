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
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", textContent: "" });
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
  return { sandbox: vm.createContext(sandbox), nodes };
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
