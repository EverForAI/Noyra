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
