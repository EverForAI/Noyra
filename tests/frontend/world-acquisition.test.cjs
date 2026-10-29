const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = readFileSync(join(__dirname, "../../src/noyra/web/admin.js"), "utf8");
function block(start, end) {
  const from = source.indexOf(start); const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from); return source.slice(from, to);
}
function context(request = async () => payload()) {
  const nodes = new Map();
  const sandbox = vm.createContext({csrfToken:"synthetic", adminSessionGeneration:0,
    adminReadVersions:new Map(), request,
    $:selector => { if(!nodes.has(selector)) nodes.set(selector, {innerHTML:"",textContent:""}); return nodes.get(selector); },
    esc:value => String(value ?? "").replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    summaryRows:rows => JSON.stringify(rows), formatTimestamp:value=>value || "未记录",
    formatInteger:value=>String(value),errorText:error=>error.message,
    setStatus:(selector,value)=>{sandbox.$(selector).textContent=value;},
  });
  vm.runInContext(block("function beginAdminRead(", "let activeSection ="),sandbox);
  vm.runInContext(block("function renderWorldAcquisition(", "function observationBytes("),sandbox);
  return {sandbox,nodes};
}
function payload() { return {format:"noyra-world-acquisition/v1",sampled_at:"now",scope:"since_tracking_started",
  current_classifier_version:2,source_limit:25,truncated:false,retained_version_limit_per_source:4096,
  items:[{name:"News",source_status:"active",tracking:"available",checks:3,unchanged_checks:2,
    outcome:"unchanged",fingerprint_version:1,baseline:"fresh",new_item_versions:1,retained_entry_versions:1}]}; }

test("acquisition diagnostics explain unchanged success and cumulative scope", async()=>{
  let calls=0; const {sandbox,nodes}=context(async path=>{calls++;assert.equal(path,"/api/admin/world-acquisition");return payload();});
  assert.equal(calls,0); await sandbox.loadWorldAcquisition(); assert.equal(calls,1);
  const html=nodes.get("#world-acquisition-summary").innerHTML;
  assert.match(html,/采集版本/); assert.match(html,/含片段/);
  assert.match(html,/检查成功，没有新内容/); assert.match(html,/非每日统计/); assert.match(html,/历史观察保留原记录/);
  assert.match(nodes.get("#world-acquisition-status").textContent,/未触发采集或模型调用/);
});

test("acquisition errors preserve old evidence and never fabricate zeros",async()=>{
  const {sandbox,nodes}=context(async()=>{throw new Error("temporarily unavailable");});
  sandbox.$("#world-acquisition-summary").innerHTML="previous evidence";
  await sandbox.loadWorldAcquisition();
  assert.equal(nodes.get("#world-acquisition-summary").innerHTML,"previous evidence");
  assert.match(nodes.get("#world-acquisition-status").textContent,/保留旧显示，不按零处理/);
});

test("untracked state, invalid counts and names are rendered honestly and safely",()=>{
  const {sandbox,nodes}=context(); const data=payload(); data.items[0].tracking="uninitialized";
  data.items[0].name="<script>bad</script>"; sandbox.renderWorldAcquisition(data);
  let html=nodes.get("#world-acquisition-summary").innerHTML;
  assert.ok(!html.includes("<script>")); assert.match(html,/不能推断采集成功或失败/);
  data.items[0].tracking="available";data.items[0].checks=Infinity;data.truncated=true;
  sandbox.renderWorldAcquisition(data);html=nodes.get("#world-acquisition-summary").innerHTML;
  assert.match(html,/未读取/);assert.match(html,/本次已截断/);
});

test("late acquisition results cannot refill a logged-out session",async()=>{
  let resolve; const pending=new Promise(done=>{resolve=done;});
  const {sandbox,nodes}=context(()=>pending); const read=sandbox.loadWorldAcquisition();
  sandbox.adminSessionGeneration++;sandbox.csrfToken="";resolve(payload());await read;
  assert.ok(!nodes.has("#world-acquisition-summary"));
});

test("newer acquisition refresh owns the rendered result",async()=>{
  let resolve;let calls=0; const old=new Promise(done=>{resolve=done;});
  const latest=payload();latest.items[0].name="new evidence";
  const {sandbox,nodes}=context(()=>++calls===1?old:Promise.resolve(latest));
  const before=sandbox.loadWorldAcquisition();await sandbox.loadWorldAcquisition();resolve(payload());await before;
  assert.match(nodes.get("#world-acquisition-summary").innerHTML,/new evidence/);
});
