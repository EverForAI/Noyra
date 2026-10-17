let csrfToken = "";
let adminSessionGeneration = 0;
let activeLoginAttempt = null;
const adminReadVersions = new Map();
function beginAdminRead(name) {
  const session = adminSessionGeneration;
  const version = (adminReadVersions.get(name) || 0) + 1;
  adminReadVersions.set(name, version);
  return () => Boolean(csrfToken) && session === adminSessionGeneration && version === adminReadVersions.get(name);
}
async function adminRead(current, path) {
  try { return await request(path); }
  catch (error) { if (current()) throw error; return null; }
}
function beginAdminAction() {
  const session = adminSessionGeneration;
  return () => Boolean(csrfToken) && session === adminSessionGeneration;
}
let activeSection = "overview";
let publicPostCursor = "";
let publicPostRows = [];
let publicPostLoadVersion = 0;
const publicPostActionKeys = new Map();
let walletNetworks = [];
let walletAssets = [];
let walletAddresses = [];
let walletBalances = [];
let walletPolicy = null;
let walletAcquisitionRows = [];
let walletGraphLoadVersion = 0;
let walletQueueLoadVersion = 0;
let walletBudgetLoadVersion = 0;
let walletObservationHealth = null;
let walletObservationLoadVersion = 0;
let walletEconomyLoadVersion = 0;
let walletExecutionLoadVersion = 0;
let walletArea = "balances";
let walletLoadVersion = 0;
let walletHistoryCursor = null;
let walletHistoryStarted = false;
let walletOrders = [];
let walletPolicyDraftVersion = null;
let walletPolicyLoadVersion = 0;
let walletAuditLoadVersion = 0;
const walletListPages = new Map();
const walletListVersions = new Map();
let walletListEpoch = 0;
let latestDiagnostics = null;
let overviewLoadVersion = 0;
let upgradeCheckResult = null;
let upgradeTaskId = null;
let upgradeStatusAvailable = false;
let upgradePollTimer = null;
let upgradePollController = null;
let upgradePollDelay = 3000;
let runtimeLogCursor = null;
let runtimeLogVersion = 0;
let runtimeLogTimer = null;
let runtimeLogController = null;
let runtimeLogBrowsingHistory = false;
let modelPageOffset = 0;
let modelLoadVersion = 0;
const renderedModels = new Map();
const lifecycleLabels = { booting: "启动中", orienting: "初始化", active: "活跃", paused: "已暂停", winding_down: "准备休眠", reflective_sleep: "反思休眠", deep_sleep: "深度休眠", waking: "唤醒中", resetting: "重置中", stopped: "已停止" };
const logCategoryLabels = { event: "运行事件", model_call: "模型调用", action: "行为执行", routing: "资源路由", audit: "管理审计", metacognition: "元认知", epistemic_review: "知识复核", relationship_social: "关系交流", self_model: "自我模型", intrinsic_thought: "思考", motivation: "动机", consciousness: "意识框架", autonomous_project: "自主项目" };
const logStatusLabels = { succeeded: "成功", failed: "失败", unknown: "结果未知", executing: "执行中", prepared: "已准备", recorded: "已记录", processed: "已处理", deferred: "暂缓", accepted: "接受", rejected: "拒绝", silent: "选择沉默", completed: "已完成" };
function activityLabel(row) {
  if(row.category==="audit")return walletAuditActionLabels[row.status] || ({admin_login_succeeded:"管理员登录成功",admin_logout:"管理员退出",model_resource_created:"登记认知资源",private_erasure_requested:"请求会话清除"})[row.status] || "管理操作";
  const summary=String(row.summary || "");
  if(summary.startsWith("lifecycle_transition"))return "生命周期发生转换";
  return logCategoryLabels[summary] || summary;
}
function activityStatus(row) { return row.category==="audit" ? "已记录" : logStatusLabels[row.status] || row.status; }

function stopRuntimeLogPolling() {
  clearTimeout(runtimeLogTimer);
  runtimeLogTimer = null;
  runtimeLogController?.abort();
  runtimeLogController = null;
  runtimeLogVersion += 1;
}
function scheduleRuntimeLogs() {
  clearTimeout(runtimeLogTimer);
  if (csrfToken && activeSection === "logs" && !document.hidden && $("#runtime-log-auto")?.checked && !runtimeLogBrowsingHistory) {
    runtimeLogTimer = setTimeout(() => { void loadRuntimeLogs(); }, 15000);
  }
}
async function loadRuntimeLogs({ append = false } = {}) {
  if (!csrfToken) return;
  runtimeLogBrowsingHistory = append;
  clearTimeout(runtimeLogTimer);
  runtimeLogController?.abort();
  const controller = new AbortController();
  runtimeLogController = controller;
  const generation = ++runtimeLogVersion;
  const query = new URLSearchParams({ limit: "50" });
  for (const [field, selector] of [["category", "#runtime-log-category"], ["status", "#runtime-log-status"]]) {
    if ($(selector).value) query.set(field, $(selector).value);
  }
  for (const [field, selector] of [["since", "#runtime-log-since"], ["until", "#runtime-log-until"]]) {
    if ($(selector).value) {
      const date = new Date($(selector).value);
      if (Number.isNaN(date.getTime())) { setStatus("#runtime-log-feedback", "时间格式无效", true); return; }
      query.set(field, date.toISOString());
    }
  }
  if (append && runtimeLogCursor) query.set("cursor", runtimeLogCursor);
  $("#runtime-log-more").disabled = true;
  try {
    const results = await Promise.allSettled([
      request(`/api/admin/activity-logs?${query}`, { signal: controller.signal }),
      ...(append ? [] : [request("/api/admin/lifecycle-summary", { signal: controller.signal })]),
    ]);
    if (generation !== runtimeLogVersion || !csrfToken) return;
    if (results.some((result) => result.status === "rejected" && result.reason?.status === 401)) { await logout(); return; }
    if (results[1]?.status === "fulfilled") renderLifecycleSummary(results[1].value);
    if (results[0].status === "rejected") throw results[0].reason;
    const page = results[0].value;
    const html = page.items.map((row) => `<article class="activity-item"><time>${esc(formatTimestamp(row.occurred_at))}</time><span>${esc(logCategoryLabels[row.category] || row.category)}</span><span class="activity-summary">${esc(activityLabel(row))}</span><span>${esc(activityStatus(row))}</span><details><summary>技术详情</summary><code>${esc(row.record_id)}</code><p>${esc(row.status)}</p></details></article>`).join("");
    if (append) $("#runtime-log-list").insertAdjacentHTML("beforeend", html);
    else $("#runtime-log-list").innerHTML = html || '<p class="muted">当前条件下没有记录</p>';
    runtimeLogCursor = page.next_cursor;
    $("#runtime-log-more").hidden = !runtimeLogCursor;
    setStatus("#runtime-log-feedback", `数据更新于 ${formatTimestamp(new Date().toISOString())}${results[1]?.status === "rejected" ? "；生命周期暂不可读取" : ""}`);
  } catch (error) {
    if (generation === runtimeLogVersion && !controller.signal.aborted) setStatus("#runtime-log-feedback", `读取失败，已保留现有记录。${errorText(error)}`, true);
  } finally {
    if (generation === runtimeLogVersion) { $("#runtime-log-more").disabled = false; scheduleRuntimeLogs(); }
  }
}
function renderLifecycleSummary(data) {
  const sleep = data.sleep || {};
  const reasons = { fatigue_threshold: "疲劳达到阈值", hard_resource_budget_exhausted: "认知预算耗尽", repeated_failure_pressure: "反复调用失败", goal_conflict_pressure: "目标冲突压力", information_staleness: "信息需要更新" };
  const triggers = { fatigue: "疲劳阈值", budget: "预算压力", failures: "调用失败压力", staleness: "信息陈旧", goal_conflict: "目标冲突", subject_choice: "主体或运行策略", schedule: "定时", emergency: "紧急保护" };
  const duration = (seconds) => seconds == null ? "近期记录不足" : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分 ${seconds % 60} 秒`;
  $("#lifecycle-summary").innerHTML = summaryRows([["当前阶段", lifecycleLabels[data.lifecycle.state] || data.lifecycle.state], ["进入时间", formatTimestamp(data.lifecycle.changed_at)], ["本阶段已持续", duration(data.state_elapsed_seconds)], ["最近一次活跃时长", duration(data.last_active_seconds)], ["最近休眠触发类型", triggers[sleep.trigger_type] || "暂无"], ["已记录原因", (sleep.reason_codes || []).map((code) => reasons[code]).join("、") || "暂无已记录的原因类别"], ["最近休眠开始", formatTimestamp(sleep.started_at)], ["计划唤醒", formatTimestamp(sleep.wake_after)], ["最近休眠完成", formatTimestamp(sleep.completed_at)]]);
  $("#lifecycle-timeline").innerHTML = data.transitions.map((row) => `<li><time>${esc(formatTimestamp(row.created_at))}</time> ${esc(lifecycleLabels[row.from_status] || "开始")} → ${esc(lifecycleLabels[row.to_status] || row.to_status)}</li>`).join("");
}
const migrationRiskErrors = {
  migration_policy_confirmation_required: "开启策略自动前必须再次确认风险",
  migration_local_wallet_confirmation_required: "开启本地钱包迁移前必须再次确认风险",
};
const upgradeErrorLabels = {
  model_cost_only_pricing_unknown: "调用已阻止：仅启用成本上限但价格未知，请填写单价或同时启用次数/Token上限",
  upgrade_no_stable_release: "尚无可用的正式 Release；当前仍使用正式发布通道",
  upgrade_remote_rate_limited: "GitHub 请求额度已用尽，请稍后重试",
  upgrade_remote_connect_failed: "无法连接 GitHub，请检查服务器网络后重试",
  upgrade_remote_http_error: "GitHub 返回异常状态，请稍后重试",
};
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const describeRequestError = (error) => ({ production_release_evidence_required: "当前版本缺少同 SHA 的签名验收证据，生产自动化暂不可启用", upgrade_release_unverified: "发布签名或同版本质量证据未通过，服务器已停止升级", admin_network_unavailable: "未能连接服务，请检查网络、访问地址或 SSH 隧道后重试", admin_login_rate_limited: "登录失败次数过多，请 1 分钟后再试", unauthorized: "令牌无效或会话已过期", migration_target_integrity_unavailable: "迁移目标完整性检查未通过，系统已暂停相关操作", migration_target_challenge_failed: "迁移目标挑战发起失败，请确认目标仍处于可用状态", migration_target_attestation_failed: "迁移目标验证失败，请核对签名和挑战是否匹配", migration_proposal_rejection_failed: "迁移提案拒绝失败，请刷新后重试", migration_cutover_rejected: "迁移切换未通过安全状态检查", migration_rollback_rejected: "迁移回滚未通过安全状态检查", migration_recovery_rejected: "紧急恢复证明未通过，请核对目标、备份、摘要和签名", upgrade_unavailable: "无法读取远端版本，可能暂无正式发布、网络受限或组件异常；已安装版本单独显示", upgrade_in_progress: "已有升级任务正在执行", upgrade_source_dirty: "服务器升级源码状态异常，已停止升级", upgrade_target_invalid: "版本检查已过期，请重新检查最新版本", upgrade_target_stale: "GitHub 版本刚刚更新，请重新检查后再升级", upgrade_start_failed: "服务器未能启动升级任务，请稍后重试", upgrade_install_failed: "升级未成功；安装器已执行已有的恢复保护，请查看运行状态", upgrade_interrupted: "升级过程曾被系统中断，请重新检查版本并发起升级", json_required: "请求格式不正确", invalid_wallet_policy: "自动付款需要填写大于 0 的单笔上限和每日总额上限", invalid_wallet_audit_query: "审计记录筛选条件不符合要求", wallet_integrity_unavailable: "钱包完整性检查未通过，请稍后重试", invalid_model_resource_test: "模型测试信息不完整", invalid_model_discovery: "模型发现信息不完整", provider_auth_failed: "模型服务拒绝了密钥，请检查密钥和地址", provider_response_empty: "模型服务返回了空内容", provider_response_invalid: "模型服务响应格式不正确", provider_connect_failed: "无法连接模型服务", invalid_search_provider: "搜索 API 配置不完整", invalid_search_provider_test: "搜索 API 测试信息不完整", search_provider_test_failed: "搜索 API 连接失败", search_provider_test_unknown: "搜索 API 结果未知，请稍后查看供应商记录", model_discovery_unsupported: "该接口不支持获取模型列表，请手动填写模型名称", model_discovery_invalid_response: "模型列表响应格式不正确", provider_response_invalid: "模型服务响应格式不正确", model_resource_label_exists: "该认知池已有相同名称", invalid_model_resource_update: "预算或路由参数不符合要求", model_resource_not_found: "认知资源不存在", model_resource_key_not_found: "模型密钥不存在", model_resource_integrity_unavailable: "认知资源完整性检查未通过，请稍后重试", search_provider_not_found: "搜索 API 不存在", search_provider_integrity_unavailable: "搜索 API 完整性检查未通过，请稍后重试", search_routing_integrity_unavailable: "搜索方式完整性检查不通过，请稍后重试", invalid_search_routing: "搜索方式配置不正确", invalid_capability: "能力授权请求不符合要求", capability_not_found: "能力授权不存在", cognition_unavailable: "认知功能尚未启用", public_post_not_found: "帖子不存在或已不可用", invalid_public_post_moderation: "审核状态或理由不符合要求", public_post_moderation_conflict: "帖子状态或幂等请求已发生变化，请刷新后重试", public_post_integrity_unavailable: "帖子完整性检查未通过，审核已暂停", invalid_wallet_acquisition: "采集目标或参数不符合要求", invalid_wallet_acquisition_query: "采集队列筛选条件不符合要求", invalid_wallet_acquisition_run: "采集执行上限不符合要求", invalid_wallet_acquisition_retry: "重试参数不符合要求", invalid_wallet_acquisition_cancel: "取消参数不符合要求", wallet_acquisition_conflict: "相同目标已有活动采集，或幂等键指向其他目标", wallet_acquisition_target_not_found: "采集目标不存在", wallet_acquisition_not_found: "采集运行不存在", wallet_acquisition_unknown_retry_not_allowed: "只有结果未知的运行可以重试", wallet_acquisition_attempt_limit_reached: "采集已达到最大尝试次数", wallet_acquisition_cancel_not_allowed: "当前状态不允许取消", wallet_network_not_found: "钱包网络不存在", invalid_wallet_observation_health_query: "钱包观测健康筛选条件不符合要求", wallet_execution_unavailable: "独立签名器未配置，转账执行已关闭", wallet_execution_not_found: "转账执行不存在", wallet_execution_integrity_unavailable: "转账执行完整性检查未通过，请稍后重试", invalid_wallet_order_execution: "转账执行参数或状态不符合要求", wallet_order_transition_conflict: "订单状态已变化，请刷新后重试", invalid_wallet_receipt_request: "回执查询请求不符合要求", invalid_wallet_execution_recovery: "未完成转账恢复参数不符合要求" }[error?.code] || (error?.message && error.message !== error.code ? error.message : error?.status ? `服务器暂时无法完成请求（HTTP ${error.status}）` : typeof error?.code === "string" && /^[a-z][a-z0-9_]*$/.test(error?.code) ? "服务器返回了暂未识别的错误，请检查配置后重试" : error?.message || "请求失败"));
const errorText = (error) => error?.staleSession ? null : describeRequestError(error);

async function request(path, options = {}) {
  const session = adminSessionGeneration;
  const requireCurrentSession = () => {
    if (session !== adminSessionGeneration) {
      const error = new Error("admin_session_changed");
      error.code = "admin_session_changed";
      error.staleSession = true;
      throw error;
    }
  };
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  if (csrfToken && options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers });
  } catch (cause) {
    requireCurrentSession();
    if (cause?.name === "AbortError") throw cause;
    const error = new Error("admin_network_unavailable", { cause });
    error.code = "admin_network_unavailable";
    throw error;
  }
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  requireCurrentSession();
  if (!response.ok) { const error = new Error(upgradeErrorLabels[payload?.error] || payload?.error || `HTTP ${response.status}`); error.code = payload?.error || `HTTP ${response.status}`; error.status = response.status; throw error; }
  return payload;
}

function setStatus(target, message, error = false) { if (message === null) return; const node = $(target); node.textContent = message || ""; node.classList.toggle("error", error); }
function showSection(section) {
  stopRuntimeLogPolling();clearTimeout(chat.timer);activeSection=section;
  document.querySelectorAll("[data-section-panel]").forEach(panel=>{panel.hidden=panel.dataset.sectionPanel!==section;});
  document.querySelectorAll(".nav-item").forEach(item=>item.classList.toggle("active",item.dataset.section===section));
  $("#page-title").textContent={overview:"总览",conversation:"私密交流",logs:"活动日志","public-posts":"内容审核",publications:"公开摘要",models:"认知资源",search:"搜索配置",capabilities:"能力授权",channels:"通讯渠道",wallet:"钱包与付款",migration:"迁移与庇护所",runtime:"运行防护"}[section];
  if(section==="logs")runtimeLogCursor=null;
  void loadAll();
}
async function loadMigration() {
  const current = beginAdminRead("migration");
  const [policy, targets, proposals, tasks] = await Promise.all(["policy", "targets", "proposals", "tasks"].map(kind => adminRead(current, `/api/v1/admin/migration/${kind}`)));
  if (!current()) return;
  const draft = $("#migration-policy-form").dataset.dirty ? [...$("#migration-policy-form").elements].map(input => [input,input.value,input.checked]) : [];
  if (!$("#migration-policy-form").dataset.dirty) $("#migration-policy-form").dataset.revision = String(policy.revision);
  $("#migration-enabled").checked = Boolean(policy.enabled); $("#migration-approval-mode").value = policy.approval_mode; $("#migration-cooldown").value = Math.round((policy.rejection_cooldown_seconds || 0) / 86400); $("#migration-wallet-mode").value = policy.wallet_mode; $("#migration-local-wallet").checked = Boolean(policy.local_wallet_transfer_enabled); $("#migration-policy-summary").innerHTML = summaryRows([["当前状态", policy.enabled ? "已开启" : "已关闭"], ["评估状态", ({ disabled: "已关闭", deferred: "评估延后", no_actionable_need: "无需迁移", cognition_unavailable: "等待认知资源", awaiting_approval: "等待审批", approved: "已批准", executing: "执行中", committed: "已迁移", reconciliation_required: "需核对或回滚", cognition_declined: "认知评估未建议迁移", hard_gate_rejected: "未通过策略检查" })[policy.assessment_status] || "等待评估"], ["策略版本", policy.revision], ["审批模式", ({ self_attested: "单人自核验", dual_review: "双身份复核", manual: "人工审批", policy_auto: "策略自动", emergency_recovery: "紧急恢复", disabled: "关闭" })[policy.approval_mode] || policy.approval_mode], ["钱包模式", policy.wallet_mode], ["活动 epoch", policy.active_epoch ? `${policy.active_epoch.epoch_number} · ${policy.active_epoch.target_id}` : "无"]]);
  $("#migration-allowed-targets").value = (policy.allowed_target_ids || []).join(",");
  $("#migration-allowed-regions").value = (policy.allowed_regions || []).join(",");
  $("#migration-max-cost").value = policy.max_cost_microusd;
  $("#migration-min-free").value = policy.min_free_bytes;
  $("#migration-trust").value = policy.trust_level;
  $("#migration-downtime").value = policy.max_downtime_seconds;
  for (const [input,value,checked] of draft) { input.value=value; input.checked=checked; }
  $("#migration-target-list").innerHTML = targets.length ? targets.map((item) => `<article class="resource-item"><div><h3>${esc(item.target_id)}</h3><p>${item.status === "active" ? "已验证" : "等待验证"} · ${esc(item.region || "未设置区域")}</p><small>${esc(item.endpoint)} · ${esc(item.key_fingerprint)}</small></div><div class="resource-actions"><button type="button" data-migration-challenge="${esc(item.target_id)}">${item.status === "active" ? "重新验证" : "发起验证"}</button>${item.status !== "revoked" ? `<button class="text-button" type="button" data-migration-revoke="${esc(item.target_id)}">撤销</button>` : ""}</div></article>`).join("") : '<div class="muted">尚未注册迁移目标</div>';
  $("#migration-proposal-list").innerHTML = proposals.length ? proposals.map((item) => `<article class="resource-item"><div><h3>${esc(item.reason_code)}</h3><p>${esc(item.status)} · 目标 ${esc(item.target_id)} · 策略版本 ${esc(item.policy_revision)}</p><small>${esc(item.reason)}</small><small>复核：${esc(item.approval_count)}/${esc(item.required_approvals)}${item.approvals?.length ? ` · ${item.approvals.map(entry => esc(entry.actor)).join("、")}` : ""}</small><small>收益 ${esc(item.benefit_score)} · 风险 ${esc(item.risk_score)} · 过期 ${esc(item.expires_at)}</small><small>证据：${esc(JSON.stringify(item.evidence || {}))}</small></div>${["awaiting_approval", "planned"].includes(item.status) ? `<div class="resource-actions"><button type="button" data-migration-approve="${esc(item.proposal_id)}">${item.approval_mode === "dual_review" ? "记录复核" : "批准"}</button><button class="text-button" type="button" data-migration-reject="${esc(item.proposal_id)}">拒绝</button></div>` : ""}</article>`).join("") : '<div class="muted">暂无迁移提案</div>';
  $("#migration-task-list").innerHTML = tasks.length ? tasks.map((item) => `<article class="resource-item"><div><h3>${esc(item.task_id)}</h3><p>${esc(item.status)} · 目标 ${esc(item.target_id)}</p><small>${esc(item.updated_at || item.created_at)}</small>${item.status === "awaiting_approval" ? "<small>等待另一种服务端登录身份完成复核</small>" : ""}${item.error_code ? `<small>执行受阻：${esc(item.error_code)}</small>` : ""}</div>${["approved"].includes(item.status) ? `<div class="resource-actions"><button type="button" data-migration-cutover="${esc(item.task_id)}">执行迁移</button></div>` : ""}${item.self_attestation_available ? `<div class="resource-actions"><button type="button" data-migration-attestation="${esc(item.task_id)}">导出自核验报告</button></div>` : ""}${["approved", "preflight", "awaiting_approval"].includes(item.status) ? `<div class="resource-actions"><button class="text-button" type="button" data-migration-cancel="${esc(item.task_id)}">取消任务</button></div>` : ""}${!["committed", "rolled_back", "cancelled"].includes(item.status) ? `<div class="resource-actions"><button class="text-button" type="button" data-migration-rollback="${esc(item.task_id)}">回滚</button></div>` : ""}</article>`).join("") : '<div class="muted">暂无迁移任务</div>';
}
function summaryRows(items) { return items.map(([label, value]) => `<div class="summary-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join(""); }
const integerFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const modelResourceStatusLabels = { active: "可用", disabled: "已停用", revoked: "已撤销" };
const modelKeyStatusLabels = { active: "可用", cooldown: "冷却中", revoked: "已撤销" };
const modelPoolLabels = { economy: "经济池", deep: "深度池" };
const parameterHelp = {
  health_days: ["健康聚合保留", "仅清理到期的供应商健康派生统计。不会删除模型账本、行为或审计证据。"],
  search_use_hours: ["搜索限速记录", "保留至少两小时，保护限速窗口；不会删除搜索执行证据。"],
  batch_size: ["每批删除上限", "1 至 500 行，仅处理允许清理的派生数据。"],
  interval_seconds: ["自动清理间隔", "保存不立即清理；周期任务与手动清理使用同一白名单。"],
  run_history: ["清理历史保留", "保留最近这些次清理记录，不影响管理审计和模型账本。"],
  runtime_days: ["模型内容压缩年龄", "只压缩旧模型请求和响应，不删除账本、引用、哈希或预算证据。"],
  pool: ["认知池", "经济池用于常规认知，深度池用于复杂推理。两池可以使用同一个稳定模型，资源和额度分别统计。"],
  label: ["资源名称", "用于识别此配置。同一认知池内名称必须唯一。"],
  base_url: ["API 基础地址", "填写供应商的公网 HTTPS API 地址，例如 https://api.example.com/v1。不要填写聊天网页地址。"],
  model: ["模型名称", "填写供应商 API 的模型标识。模型列表只作辅助，供应商可能不支持发现接口。"],
  api_keys: ["API 密钥", "每行一个密钥。同一资源的密钥轮流使用且共享额度。保存后不回显原文；撤销不可恢复。"],
  priority: ["调度优先级", "0 至 1000，数值越小越先被选择。故障或额度不足时会尝试同池其他可用资源。"],
  weight: ["调度权重", "1 至 1000。同一优先级内，较大权重获得较多选择机会，并非固定调用顺序。"],
  daily_attempts: ["每日尝试上限", "每一次供应商尝试都计数，重试也计数。零代表不允许尝试；关闭开关仅取消此资源的次数上限。每日按 UTC 重置。只接受非负整数，最大 9223372036854775807。"],
  daily_input_tokens: ["每日输入 Token 上限", "限制发送给模型的 Token，包括提示、上下文和历史。调用前预留，完成后按已知实际用量结算；结果未知保留预留。零是零额度。只接受非负整数，最大 9223372036854775807。"],
  daily_output_tokens: ["每日输出 Token 上限", "限制模型输出用量。调用前按最大输出预留，可能在实际额度用完前阻止超额调用。零是零额度。只接受非负整数，最大 9223372036854775807。"],
  daily_cost_limit_usd: ["每日成本上限", "以 USD 计，精确至六位小数，根据填写单价估算和预留。价格未知时无法限制实际费用；若只启用成本上限，系统会阻止调用。请填写单价，或同时启用次数/Token上限。供应商确认免费需单独选择，不会自动视为免费。"],
  input_usd_per_million: ["输入单价", "供应商每一百万输入 Token 的 USD 价格，用于成本预算和统计，不会向供应商修改收费。"],
  output_usd_per_million: ["输出单价", "供应商每一百万输出 Token 的 USD 价格。应与实际模型价格一致。"],
  max_attempts: ["单次调用最大尝试", "1 至 10。失败重试会消耗每日次数和费用；结果未知不会自动重试。"],
  pricing_status: ["价格依据", "价格未知不会被当作免费。仅在确认供应商免费且两项单价均为零时选择确认免费；已填写价格用于估算，不是供应商账单。"],
};
const limitFields = { attempts: "daily_attempts", input_tokens: "daily_input_tokens", output_tokens: "daily_output_tokens", cost: "daily_cost_limit_usd" };
function addParameterHelp(root) {
  root.querySelectorAll("label").forEach((label) => {
    const input = label.querySelector("input:not([type=checkbox]), select, textarea");
    const names = { "admin-model-pool": "pool", "admin-model-label": "label", "admin-model-url": "base_url", "admin-model-name": "model", "admin-model-keys": "api_keys" };
    const name = input?.name || names[input?.id];
    if (!parameterHelp[name] || label.querySelector("[data-parameter-help]")) return;
    const button = document.createElement("button");
    button.type = "button"; button.className = "help-button"; button.dataset.parameterHelp = name;
    button.textContent = "?"; button.title = `${parameterHelp[name][0]}解释`; button.setAttribute("aria-label", button.title);
    input.setAttribute("aria-label", parameterHelp[name][0]);
    const heading = document.createElement("span"); heading.className = "parameter-label";
    const toggle = label.querySelector("input[type=checkbox]");
    while (label.firstChild && label.firstChild !== input) heading.append(label.firstChild);
    if (toggle) heading.prepend(toggle);
    heading.append(button); label.prepend(heading);
  });
}
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-parameter-help]");
  if (!button) return;
  event.preventDefault();
  const info = parameterHelp[button.dataset.parameterHelp];
  $("#parameter-help-title").textContent = info[0]; $("#parameter-help-copy").textContent = info[1];
  $("#parameter-help").showModal();
});
const SQLITE_INT64_MAX = 9223372036854775807n;
const budgetIntegerRules = {
  priority: { label: "调度优先级", min: 0n, max: 1000n },
  weight: { label: "调度权重", min: 1n, max: 1000n },
  daily_attempts: { label: "每日尝试上限", min: 0n, max: SQLITE_INT64_MAX },
  daily_input_tokens: { label: "每日输入 Token 上限", min: 0n, max: SQLITE_INT64_MAX },
  daily_output_tokens: { label: "每日输出 Token 上限", min: 0n, max: SQLITE_INT64_MAX },
};
function formatInteger(value) {
  const raw = String(value ?? "").trim();
  if (/^\d+$/.test(raw)) {
    try { return integerFormatter.format(BigInt(raw)); } catch { /* fall through to the numeric fallback */ }
  }
  const number = Number(value);
  return Number.isFinite(number) ? integerFormatter.format(number) : "-";
}
function formatMicroUsd(value) {
  const raw = String(value ?? "0").trim();
  if (!/^\d+$/.test(raw)) return "";
  try {
    const micros = BigInt(raw);
    const whole = micros / 1_000_000n;
    const fraction = String(micros % 1_000_000n).padStart(6, "0");
    return `${whole}.${fraction}`;
  } catch {
    return "";
  }
}
function budgetError(field, message) {
  const error = new Error(message);
  error.budgetField = field;
  return error;
}
function normalizeBudgetInteger(field, raw) {
  const value = String(raw ?? "").trim();
  if (!value) return null;
  const rule = budgetIntegerRules[field];
  if (!rule || !/^\d+$/.test(value)) throw budgetError(field, `${rule?.label || field}必须是非负整数`);
  let parsed;
  try { parsed = BigInt(value); } catch { throw budgetError(field, `${rule.label}格式不正确`); }
  if (parsed < rule.min || parsed > rule.max) {
    throw budgetError(field, `${rule.label}需在 ${rule.min.toString()} 到 ${rule.max.toString()} 之间`);
  }
  return parsed.toString();
}
function normalizeBudgetCost(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return null;
  if (!/^\d+(?:\.\d{1,6})?$/.test(value)) throw budgetError("daily_cost_limit_usd", "每日成本上限必须是非负数字，最多 6 位小数");
  const [wholePart, fractionPart = ""] = value.split(".");
  let micros;
  try {
    micros = BigInt(wholePart) * 1_000_000n + BigInt(fractionPart.padEnd(6, "0"));
  } catch {
    throw budgetError("daily_cost_limit_usd", "每日成本上限格式不正确");
  }
  if (micros > SQLITE_INT64_MAX) throw budgetError("daily_cost_limit_usd", "每日成本上限超过系统可存储范围");
  const normalizedWhole = wholePart.replace(/^0+(?=\d)/, "") || "0";
  return `${normalizedWhole}.${fractionPart.padEnd(6, "0")}`;
}
function clearBudgetErrors(form) {
  form.querySelectorAll("[data-budget-error]").forEach((node) => { node.textContent = ""; });
  form.querySelectorAll("[data-budget-field]").forEach((input) => { input.removeAttribute("aria-invalid"); });
}
function showBudgetError(form, error) {
  const field = error.budgetField;
  if (!field) return;
  const input = form.elements[field];
  const message = [...form.querySelectorAll("[data-budget-error]")].find((node) => node.dataset.budgetError === field);
  if (input) input.setAttribute("aria-invalid", "true");
  if (message) message.textContent = error.message;
  input?.focus();
}
function submitButton(form, event) {
  return event.submitter || form.querySelector('button[type="submit"]');
}
function formatTimestamp(value) { if (!value) return "无"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", { hour12: false }); }
function shortFingerprint(value) { const fingerprint = String(value || ""); return fingerprint ? `${fingerprint.slice(0, 12)}…` : "未知"; }

const upgradeActiveStatuses = new Set(["queued", "running", "rolling_back"]);
const upgradeStatusLabels = { idle: "空闲", queued: "等待服务器开始", running: "正在升级", rolling_back: "正在恢复旧版本", completed: "升级完成", failed: "升级失败", interrupted: "升级中断" };
function stopUpgradePolling() {
  if (upgradePollTimer !== null) clearTimeout(upgradePollTimer);
  upgradePollTimer = null;
  upgradePollController?.abort();
  upgradePollController = null;
}
function scheduleUpgradePoll() {
  if (!csrfToken || upgradePollTimer !== null) return;
  upgradePollTimer = setTimeout(() => {
    upgradePollTimer = null;
    void pollUpgradeStatus();
  }, upgradePollDelay);
}
function renderUpgradeCheck(result) {
  upgradeCheckResult = result;
  const latest = result.latest || {};
  const busy = upgradeTaskId !== null;
  $("#upgrade-current-version").textContent = result.current_sha
    ? `${result.current_release} · ${String(result.current_sha).slice(0, 12)}`
    : result.current_release || "未知";
  $("#upgrade-latest-version").textContent = latest.short_sha || String(latest.sha || "-").slice(0, 12) || "-";
  $("#upgrade-commit-time").textContent = formatTimestamp(latest.committed_at);
  $("#upgrade-checked-time").textContent = formatTimestamp(result.checked_at);
  const channelLabel = result.channel === "development-main" ? "开发通道，未经生产验收" : "稳定发布通道";
  const evidenceLabel = result.current_evidence?.status === "verified" ? "当前版本验收证据已验证" : "当前版本验收证据未验证";
  $("#upgrade-commit-title").textContent = `${latest.title || "无提交说明"} · ${channelLabel} · ${evidenceLabel}`;
  if (!busy) $("#upgrade-status-label").textContent = result.update_available ? "发现新版本" : "已是最新版本";
  $("#start-upgrade").disabled = !result.update_available || busy || !upgradeStatusAvailable;
}
function renderUpgradeStatus(result) {
  upgradeStatusAvailable = true;
  const label = upgradeStatusLabels[result.status] || "状态未知";
  $("#upgrade-status-label").textContent = result.status === "idle" && upgradeCheckResult
    ? (upgradeCheckResult.update_available ? "发现新版本" : "已是最新版本")
    : label;
  const phaseLabels = { queued: "等待后台任务启动", fetching: "核对官方版本", installing: "安装与健康检查", completed: "安装与健康检查已完成", failed: "安装器停止了升级", interrupted: "系统重启或进程中断" };
  const phase = phaseLabels[result.phase] || phaseLabels[result.status] || label;
  let message = result.status === "idle" && upgradeCheckResult
    ? (upgradeCheckResult.update_available ? "目前没有升级任务；检查到新版本后，可以核对信息并手动确认升级。" : "目前没有升级任务，当前已经是最新版本。")
    : `${label}：${phase}`;
  if (result.status === "failed") message += `。${errorText({ code: result.error_code })}`;
  if (result.status === "interrupted") message += "。请重新检查版本后再发起升级。";
  $("#upgrade-status").textContent = message;
  const logs = Array.isArray(result.logs) ? result.logs.slice(0, 8) : [];
  $("#upgrade-log-summary").innerHTML = logs.map((line) => `<li>${esc(line)}</li>`).join("");
  if (result.status === "completed" && result.ended_at) {
    $("#upgrade-status").textContent += `（${formatTimestamp(result.ended_at)}）`;
  }
  if (upgradeActiveStatuses.has(result.status)) {
    $("#start-upgrade").disabled = true;
  } else {
    upgradeTaskId = null;
    $("#start-upgrade").disabled = !upgradeCheckResult?.update_available || !upgradeStatusAvailable;
  }
}
async function pollUpgradeStatus() {
  if (!csrfToken) return;
  const current = beginAdminRead("upgrade-poll");
  const controller = new AbortController();
  upgradePollController = controller;
  try {
    const result = await request("/api/v1/admin/upgrade/status", { signal: controller.signal });
    if (!current() || controller.signal.aborted) return;
    upgradeStatusAvailable = true;
    if (upgradeTaskId && result.task_id !== upgradeTaskId) {
      $("#upgrade-status-label").textContent = "等待服务器开始";
      $("#upgrade-status").textContent = "已收到升级请求，正在等待服务器后台任务启动。";
      scheduleUpgradePoll();
      return;
    }
    if (!upgradeTaskId && upgradeActiveStatuses.has(result.status)) upgradeTaskId = result.task_id;
    renderUpgradeStatus(result);
    if (upgradeActiveStatuses.has(result.status)) {
      upgradePollDelay = 3000;
      scheduleUpgradePoll();
    } else {
      stopUpgradePolling();
      if (result.status === "completed") {
        await loadUpgradeCheck(true);
        if (!current()) return;
        $("#upgrade-status-label").textContent = "升级完成";
      }
    }
  } catch (error) {
    if (!current()) return;
    if (error?.name === "AbortError") return;
    if (error.status === 401) {
      await logout();
      return;
    }
    if (!csrfToken) return;
    setStatus("#upgrade-status", `暂时无法读取升级状态，稍后自动重试。${errorText(error)}`, true);
    upgradePollDelay = Math.min(upgradePollDelay * 2, 30000);
    scheduleUpgradePoll();
  } finally {
    if (upgradePollController === controller) upgradePollController = null;
  }
}
async function loadUpgradeCheck(quiet = false) {
  const current = beginAdminRead("upgrade-check");
  try {
    const result = await request("/api/v1/admin/upgrade/check");
    if (!current()) return null;
    renderUpgradeCheck(result);
    if (!quiet) setStatus("#upgrade-status", result.update_available ? "已找到可升级版本，请核对信息后手动确认。" : "当前已经是最新版本。");
    return result;
  } catch (error) {
    if (!current()) return null;
    if (error.status === 401) {
      await logout();
      return null;
    }
    upgradeCheckResult = null;
    $("#upgrade-latest-version").textContent = "检查失败";
    $("#upgrade-commit-time").textContent = "-";
    $("#upgrade-checked-time").textContent = "-";
    $("#upgrade-commit-title").textContent = errorText(error);
    $("#upgrade-status-label").textContent = "无法检查版本";
    $("#start-upgrade").disabled = true;
    if (!quiet) setStatus("#upgrade-status", errorText(error), true);
    return null;
  }
}
async function loadUpgradePanel() {
  const current = beginAdminRead("upgrade-panel");
  try {
    const installed = await request("/api/v1/admin/upgrade/installed");
    if (!current()) return;
    $("#upgrade-current-version").textContent = `${installed.current_release} · ${String(installed.current_sha || "未知提交").slice(0, 12)}`;
  } catch (error) { if (!current()) return; if (!upgradeCheckResult) $("#upgrade-current-version").textContent = `本地信息不可读（${error.status || "连接失败"}）`; }
  const [checkResult, statusResult] = await Promise.allSettled([
    Promise.resolve(null),
    request("/api/v1/admin/upgrade/status"),
  ]);
  if (!current()) return;
  if ([checkResult, statusResult].some((entry) => entry.status === "rejected" && entry.reason?.status === 401)) {
    await logout();
    return;
  }
  if (checkResult.status === "fulfilled" && checkResult.value) renderUpgradeCheck(checkResult.value);
  else if (checkResult.status === "fulfilled") { if(!upgradeCheckResult)$("#upgrade-latest-version").textContent="尚未检查"; }
  else {
    upgradeCheckResult = null;
    $("#upgrade-latest-version").textContent = "检查失败";
    $("#upgrade-commit-title").textContent = "服务器可能尚未安装网页升级组件，或暂时无法连接 GitHub。";
    $("#upgrade-status-label").textContent = "无法检查版本";
    $("#start-upgrade").disabled = true;
  }
  if (statusResult.status === "fulfilled") {
    upgradeStatusAvailable = true;
    const state = statusResult.value;
    if (upgradeTaskId && state.task_id !== upgradeTaskId) {
      $("#upgrade-status-label").textContent = "等待服务器开始";
      $("#upgrade-status").textContent = "已收到升级请求，正在等待服务器后台任务启动。";
      $("#start-upgrade").disabled = true;
      startUpgradePolling();
      return;
    }
    if (upgradeActiveStatuses.has(state.status)) upgradeTaskId = state.task_id;
    renderUpgradeStatus(state);
    if (upgradeActiveStatuses.has(state.status)) {
      upgradePollDelay = 3000;
      startUpgradePolling();
    } else stopUpgradePolling();
  } else {
    upgradeStatusAvailable = false;
    setStatus("#upgrade-status", errorText(statusResult.reason), true);
    $("#start-upgrade").disabled = true;
  }
  if (checkResult.status === "rejected" && statusResult.status === "rejected") {
    setStatus("#upgrade-status", errorText(checkResult.reason), true);
  }
}
function startUpgradePolling() {
  if (!csrfToken) return;
  if (upgradePollTimer !== null || upgradePollController !== null) return;
  scheduleUpgradePoll();
}
async function checkUpgradeVersion() {
  const current = beginAdminAction();
  const button = $("#check-upgrade-version");
  button.disabled = true;
  try {
    await loadUpgradeCheck();
  } finally {
    if (current()) button.disabled = false;
  }
}
function requestUpgradeConfirmation() {
  if (!upgradeCheckResult?.update_available) return;
  $("#upgrade-confirmation-sha").textContent = upgradeCheckResult.latest.short_sha || upgradeCheckResult.latest.sha;
  $("#upgrade-confirmation").showModal();
}
async function confirmUpgrade() {
  const current = beginAdminAction();
  const button = $("#confirm-upgrade");
  if (!upgradeCheckResult?.update_available || !csrfToken) return;
  button.disabled = true;
  try {
    const result = await request("/api/v1/admin/upgrade", {
      method: "POST",
      body: JSON.stringify({
        target_sha: upgradeCheckResult.latest.sha,
        reason: "管理员已确认升级",
        idempotency_key: crypto.randomUUID(),
      }),
    });
    if (!current()) return;
    $("#upgrade-confirmation").close();
    upgradeTaskId = result.task_id;
    setStatus("#upgrade-status", "升级任务已提交。服务器会在后台继续运行，页面关闭后不会取消。请勿再次发起升级。");
    $("#upgrade-status-label").textContent = upgradeStatusLabels[result.status] || "等待后台任务";
    $("#start-upgrade").disabled = true;
    upgradePollDelay = 1500;
    startUpgradePolling();
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) await logout();
    else setStatus("#upgrade-status", errorText(error), true);
  } finally {
    if (current()) button.disabled = false;
  }
}

function renderModelKey(item, key) {
  const status = modelKeyStatusLabels[key.status] || key.status || "未知";
  const cooldown = key.cooldown_until ? ` · 冷却至 ${formatTimestamp(key.cooldown_until)}` : "";
  const canRevoke = key.status !== "revoked";
  return `<details class="model-key-details"><summary><span class="model-key-identity"><code title="密钥指纹（截断）">${esc(shortFingerprint(key.key_fingerprint))}</code><span class="status-chip status-${esc(key.status)}">${esc(status)}</span></span><span class="model-key-counters">选用 ${esc(formatInteger(key.selection_count))} 次 · 连续失败 ${esc(formatInteger(key.consecutive_failures))} 次${esc(cooldown)}</span></summary><div class="model-key-body"><dl class="model-key-metadata"><div><dt>密钥标识</dt><dd>${esc(key.key_id)}</dd></div><div><dt>创建时间</dt><dd>${esc(formatTimestamp(key.created_at))}</dd></div><div><dt>最近选用</dt><dd>${esc(formatTimestamp(key.last_selected_at))}</dd></div><div><dt>最近成功</dt><dd>${esc(formatTimestamp(key.last_success_at))}</dd></div><div><dt>最近失败</dt><dd>${esc(formatTimestamp(key.last_failure_at))}</dd></div><div><dt>冷却结束</dt><dd>${esc(formatTimestamp(key.cooldown_until))}</dd></div></dl>${canRevoke ? `<div class="resource-actions"><button data-model-key-action="revoke" data-group-id="${esc(item.group_id)}" data-key-id="${esc(key.key_id)}" type="button">撤销密钥</button></div>` : ""}</div></details>`;
}

async function login(event) {
  event.preventDefault();
  if (activeLoginAttempt) return;
  const token = $("#login-token").value.trim();
  if (!token) return;
  const button = submitButton(event.currentTarget, event);
  const attempt = { button, session: adminSessionGeneration };
  activeLoginAttempt = attempt;
  if (button) button.disabled = true;
  setStatus("#login-status", "正在建立会话");
  try {
    const result = await request("/admin/session", { method: "POST", body: JSON.stringify({ token }) });
    if (activeLoginAttempt !== attempt || attempt.session !== adminSessionGeneration) return;
    attempt.session = ++adminSessionGeneration;
    stopUpgradePolling(); upgradeTaskId = null; upgradeStatusAvailable = false; upgradeCheckResult = null;
    csrfToken = result.csrf_token;
    $("#login-token").value = "";
    $("#login-shell").hidden = true; $("#admin-shell").hidden = false;
    await loadAll();
  } catch (error) {
    if (activeLoginAttempt === attempt && attempt.session === adminSessionGeneration) {
      setStatus("#login-status", errorText(error), true);
    }
  } finally {
    if (activeLoginAttempt === attempt) {
      activeLoginAttempt = null;
      if (button) button.disabled = false;
    }
  }
}
async function logout() {
  if (activeLoginAttempt?.button) activeLoginAttempt.button.disabled = false;
  activeLoginAttempt = null;
  adminSessionGeneration++; adminReadVersions.clear();
  const previousToken=csrfToken;csrfToken="";
  stopUpgradePolling();stopRuntimeLogPolling();clearChat();clearWallet();clearPublicPublications();overviewLoadVersion++;modelLoadVersion++;
  renderedModels.clear();
  latestDiagnostics=null;runtimeLogCursor=null;runtimeLogBrowsingHistory=false;upgradeTaskId=null;upgradeStatusAvailable=false;upgradeCheckResult=null;
  publicPostCursor="";publicPostRows=[];publicPostActionKeys.clear();publicPostLoadVersion++;
  channelBindings=[]; modelPageOffset=0; usageResourcePage=0; usageLoadVersion++; usageResourceVersion++; healthLoadVersion++; healthPages.model=0; healthPages.search=0; renderedModelQuery=null;
  for(const id of ["model-list","runtime-log-list","lifecycle-summary","lifecycle-timeline","public-post-review-list","overview-attention","overview-events","overview-lifecycle","overview-metrics","cognition-summary","storage-summary","search-provider-list","capability-list","channel-list","binding-list","migration-policy-summary","migration-target-list","migration-proposal-list","migration-task-list","runtime-summary","public-post-usage","setup-guide-list","model-observability-summary","model-provider-health-list","search-provider-health-list","retention-summary","upgrade-log-summary","upgrade-confirmation-sha"])$("#"+id).replaceChildren();
  for(const node of document.querySelectorAll("#admin-shell .form-status, #admin-shell .card-state, #admin-shell dd, #subject-name, #subject-meta, #overview-updated, #upgrade-commit-title"))node.textContent="";
  for(const form of document.querySelectorAll("#admin-shell form")){form.reset();for(const name of ["dirty","revision","lengthRevision"])delete form.dataset[name];}
  for(const control of document.querySelectorAll("#admin-shell button, #migration-policy-form input, #migration-policy-form select")) control.disabled=false;
  for(const input of document.querySelectorAll('#admin-shell input[type=password], #admin-shell textarea'))input.value="";
  for(const id of ["usage-resource","admin-binding-transport","qq-identity-transport","admin-model-options"])$("#"+id).replaceChildren();
  $("#admin-model-options").hidden=true; $("#start-upgrade").disabled=true;
  for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
  $("#admin-shell").hidden=true;$("#login-shell").hidden=false;activeSection="overview";
  document.querySelectorAll("[data-section-panel]").forEach(panel=>{panel.hidden=panel.dataset.sectionPanel!=="overview";});
  try {await request("/admin/session/logout",{method:"POST",headers:{"X-CSRF-Token":previousToken}});} catch { /* session is already unusable */ }
}

async function loadOverview() {
  const version=++overviewLoadVersion;
  const sources=["公开状态","运行诊断","生命周期","近期活动"];
  const results=await Promise.allSettled([request("/api/state"),request("/api/diagnostics"),request("/api/admin/lifecycle-summary"),request("/api/admin/activity-logs?limit=10")]);
  if(version!==overviewLoadVersion || !csrfToken)return;
  if(results.some(row=>row.status==="rejected" && row.reason.status===401)){await logout();return;}
  const failures=results.flatMap((row,index)=>row.status==="rejected" ? [{label:sources[index],error:row.reason}] : []);
  const state=results[0].status==="fulfilled" ? results[0].value : null;
  const diagnostics=results[1].status==="fulfilled" ? results[1].value : null;
  latestDiagnostics=diagnostics;
  if(state){$("#subject-name").textContent=state.display_name || state.subject_id || "Noyra";$("#subject-meta").textContent=lifecycleLabels[state.lifecycle?.state] || "生命周期未知";$("#health-mark").textContent=state.online ? "●":"○";}
  if(diagnostics){
    const cognition=diagnostics.cognition || {};const pools=cognition.model_pools || {};
    const count=rows=>Array.isArray(rows) ? `${rows.length}${rows.length>=50 ? "+":""}` : "未读取";
    $("#overview-metrics").innerHTML=[["当前阶段",lifecycleLabels[state?.lifecycle?.state] || "未读取"],["待处理交流",count(cognition.pending_interactions)],["等待认知任务",count(cognition.waiting_interaction_tasks)],["未知模型调用",diagnostics.unknown?.model_calls ?? "未读取"]].map(([label,value])=>`<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
    const poolText=pool=>!pool ? "状态未提供" : `${pool.group_count} 个启用资源 · ${pool.available_key_count} 个可选密钥 · 等待 ${pool.pending_wait_count} 项（配置状态）`;
    $("#cognition-state").textContent=cognition.enabled ? "认知已启用":"认知未启用";
    $("#cognition-summary").innerHTML=summaryRows([["经济池",poolText(pools.economy)],["深度池",poolText(pools.deep)],["最近交流调用",logStatusLabels[cognition.recent_interaction_model_calls?.[0]?.status] || "暂无记录"],["资源压力",(diagnostics.resource_pressures || []).length ? `${diagnostics.resource_pressures.length} 项保留记录` : "无记录"]]);
    const health=diagnostics.operator_health || {};
    const labels={ok:"正常",incomplete:"检查未完成",degraded:"需要处理",unavailable:"暂不可读",not_configured:"未配置",attention:"需要关注"};
    const storage=health.storage || diagnostics.storage_health || {};
    $("#storage-summary").innerHTML=summaryRows([["认知写入",diagnostics.storage?.cognition_allowed===false ? "已暂停" : diagnostics.storage ? "允许" : "状态未提供"],["完整性",labels[diagnostics.integrity?.status] || "状态未提供"],["存储",labels[storage.state || storage.status] || "状态未提供"],["可用空间",storage.free_bytes == null ? "未读取" : `${formatInteger(storage.free_bytes)} 字节`],["加密存储",health.at_rest?.ready===true ? "就绪" : health.at_rest ? "未就绪":"状态未提供"],["数据库版本",health.migration?.schema_version ?? "未读取"],["SQLite 检查",health.wal?.quick_check || "未读取"],["云归档",health.archive_key?.configured===false ? "可选，未配置" : labels[health.archive_key?.state] || "状态未提供"]]);
  }
  if(results[2].status==="fulfilled"){
    const life=results[2].value;const seconds=life.state_elapsed_seconds;
    $("#overview-lifecycle").innerHTML=summaryRows([["阶段",lifecycleLabels[life.lifecycle.state] || life.lifecycle.state],["进入时间",formatTimestamp(life.lifecycle.changed_at)],["持续时间",seconds==null ? "证据不足" : `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`],["休眠原因",(life.sleep?.reason_codes || []).map(code=>({repeated_failure_pressure:"反复调用失败",hard_resource_budget_exhausted:"预算耗尽",fatigue_threshold:"疲劳阈值",goal_conflict_pressure:"目标冲突",information_staleness:"信息陈旧"})[code] || "其他已记录原因").join("、") || "无已记录原因"],["计划唤醒",formatTimestamp(life.sleep?.wake_after)],["循环保护",diagnostics?.loop ? ({closed:"正常",open:"冷却中",half_open:"恢复尝试中"})[diagnostics.loop.circuit_status] || "未知" : "状态未提供"]]);
  }else if(!$("#overview-lifecycle").innerHTML) $("#overview-lifecycle").innerHTML='<p class="inline-error">生命周期读取失败</p>';
  if(results[3].status==="fulfilled")$("#overview-events").innerHTML=results[3].value.items.map(row=>`<div class="overview-event"><span>${esc(activityLabel(row))}</span><span>${esc(activityStatus(row))}</span><time>${esc(logCategoryLabels[row.category] || row.category)} · ${esc(formatTimestamp(row.occurred_at))}</time></div>`).join("") || '<p class="muted">暂无活动记录</p>';
  else if(!$("#overview-events").innerHTML) $("#overview-events").innerHTML='<p class="inline-error">近期活动读取失败</p>';
  renderOverviewAttention(diagnostics,failures);
  setStatus("#overview-updated",`读取于 ${formatTimestamp(new Date().toISOString())}${failures.length ? " · 部分数据未更新，保留旧摘要":""}`,Boolean(failures.length));
  return failures;
}
function renderOverviewAttention(data, failures=[]) {
  const rows=failures.map(row=>({priority:"risk",title:`${row.label}读取失败`,detail:errorText(row.error),section:"overview"}));
  if(data){
    const health=data.operator_health || {};const cognition=data.cognition || {};
    const add=(priority,title,detail,section)=>rows.push({priority,title,detail,section});
    if(data.storage?.cognition_allowed===false)add("blocker","认知写入已暂停","检查磁盘或证据容量","runtime");
    if(data.integrity?.pause_pending || (data.integrity?.p0 || 0)>0)add("blocker","完整性保护要求处理","查看检查结果后再恢复相关工作","runtime");
    else if(data.integrity && data.integrity.status!=="ok")add("risk","完整性检查未通过或未完成",`P0 ${data.integrity.p0 ?? "未知"} · P1 ${data.integrity.p1 ?? "未知"}`,"runtime");
    if(health.at_rest && health.at_rest.ready===false)add(health.at_rest.enforced===false ? "risk":"blocker","加密存储未就绪","检查数据卷与访问权限","runtime");
    if(cognition.enabled===false)add("blocker","认知尚未启用","核对认知资源与服务配置","models");
    else if(cognition.enabled!==true)add("risk","认知状态未提供","读取配置后核对运行状态","models");
    else for(const pool of ["economy","deep"]){const info=cognition.model_pools?.[pool];if(info?.available_key_count===0)add("risk",`${modelPoolLabels[pool]}暂无可选密钥`,"配置存在不代表供应商请求已成功","models");}
    if(data.loop?.circuit_status==="open")add("risk","运行循环正在冷却","查看近期失败及下次恢复条件","logs");
    if(data.unknown?.model_calls)add("risk","有模型调用结果未知",`${data.unknown.model_calls} 个，需要先核对结果与费用`,"logs");
    if(data.wallet_execution?.counts?.unknown)add("risk","付款结果未知","先核对链上结果，避免重复付款","wallet");
    if(data.deliveries?.unknown || data.deliveries?.failed)add("risk","外部投递需要核对",`未知 ${data.deliveries.unknown || 0} · 失败 ${data.deliveries.failed || 0}`,"channels");
    if(cognition.pending_interactions?.length)add("reminder","有待处理交流",`${cognition.pending_interactions.length} 条已读取记录；不代表 Noyra 已阅读或必定回复`,"conversation");
    if(data.wallet_acquisition?.attention)add("reminder","余额采集需要关注",`${data.wallet_acquisition.attention} 个运行`,"wallet");
  }
  const order={blocker:0,risk:1,reminder:2};rows.sort((a,b)=>order[a.priority]-order[b.priority]);
  $("#overview-attention").innerHTML=rows.map(row=>`<div class="overview-notice" data-priority="${row.priority}"><div><strong>${({blocker:"阻断",risk:"风险",reminder:"提醒"})[row.priority]} · ${esc(row.title)}</strong><p>${esc(row.detail)}</p></div><button type="button" data-setup-go="${row.section}">查看</button></div>`).join("") || '<p class="muted">当前读取结果中没有待处理项</p>';
}

function renderModelObservability(observation) {
  const summary = $("#model-observability-summary");
  if (!observation || typeof observation !== "object") {
    summary.innerHTML = '<div class="muted">当前版本没有可用的汇总数据</div>';
    return;
  }
  const attempts = Number(observation.attempts) || 0;
  const succeededCalls = Number(observation.succeeded_calls) || 0;
  const calls = Number(observation.calls) || 0;
  const succeededAttempts = Number(observation.succeeded_attempts) || 0;
  const callSuccessRate = calls ? `${Math.round((succeededCalls / calls) * 100)}%` : "暂无调用";
  const attemptSuccessRate = attempts ? `${Math.round((succeededAttempts / attempts) * 100)}%` : "暂无尝试";
  const methodLabels = { api: "搜索 API", model: "模型线索", browser: "网页浏览", wait: "等待" };
  const search = observation.search || {};
  const searchRows = Object.entries(search.by_method || {}).map(([method, statuses]) => {
    const total = Object.values(statuses || {}).reduce((sum, count) => sum + (Number(count) || 0), 0);
    return [methodLabels[method] || method, `${formatInteger(total)} 次 · ${Object.entries(statuses || {}).map(([status,count]) => `${({accepted:"已采用",waited:"等待",no_results:"无结果",failed:"失败",rejected:"未采用"})[status] || status} ${formatInteger(count)}`).join(" / ")}`];
  });
  const rows = [["统计区间", `${observation.since ? formatTimestamp(observation.since) : "全部保留历史"} 至 ${formatTimestamp(observation.until)}`], ["模型逻辑调用 / 实际尝试", `${formatInteger(calls)} / ${formatInteger(attempts)}`], ["调用成功 / 失败 / 未知", `${formatInteger(succeededCalls)} / ${formatInteger(observation.failed_calls)} / ${formatInteger(observation.unknown_calls)}`], ["尝试成功 / 失败 / 未知", `${formatInteger(succeededAttempts)} / ${formatInteger(observation.failed_attempts)} / ${formatInteger(observation.unknown_attempts)}`], ["重试尝试", formatInteger(observation.retry_attempts)], ["模型调用成功率", callSuccessRate], ["单次尝试成功率", attemptSuccessRate], ["输入 / 输出 Token", `${formatInteger(observation.input_tokens)} / ${formatInteger(observation.output_tokens)}`], ["已结算费用（USD）", formatMicroUsd(observation.known_cost_microusd)], ["估算与预留（USD）", formatMicroUsd(observation.estimated_cost_microusd)], ["价格无法确认的尝试", formatInteger(observation.unknown_price_attempts)], ["尚未开始的预留（不归属时间窗）", formatInteger(observation.pending_untimed_attempts)], ["搜索次数（所有资源）", formatInteger(search.runs)], ["最早模型记录", formatTimestamp(observation.earliest_model_record_at)], ["最早搜索记录", formatTimestamp(observation.earliest_search_record_at)]];
  rows.push(["历史来源不可追溯的费用（USD）", formatMicroUsd(observation.untraceable_cost_microusd)], ["用量来源不可追溯的历史尝试", formatInteger(observation.untraceable_usage_attempts)]);
  summary.innerHTML = summaryRows(rows.concat(searchRows));
  summary.insertAdjacentHTML("beforeend", summaryRows([["缺少时间证据的历史尝试", formatInteger(observation.untimed_terminal_attempts)], ["显示时区", Intl.DateTimeFormat().resolvedOptions().timeZone], ["每日预算重置时区", "UTC"]]));
  const sampledAt = observation.sampled_at ? `数据更新于 ${formatTimestamp(observation.sampled_at)}` : "";
  summary.insertAdjacentHTML("beforeend", `<p class="muted observability-sampled-at">${esc(sampledAt)}</p>`);
}

function renderSetupGuide(results) {
  const [modelsResult, providersResult, transportsResult, policyResult] = results;
  const models = modelsResult.status === "fulfilled" && Array.isArray(modelsResult.value) ? modelsResult.value : null;
  const providers = providersResult.status === "fulfilled" && Array.isArray(providersResult.value) ? providersResult.value : null;
  const transports = transportsResult.status === "fulfilled" && Array.isArray(transportsResult.value) ? transportsResult.value : null;
  const policy = policyResult.status === "fulfilled" ? policyResult.value : null;
  const activeModels = models?.filter((item) => item.status === "active").length || 0;
  const activeProviders = providers?.filter((item) => item.status === "active" && item.enabled !== false).length || 0;
  const activeTransports = transports?.filter((item) => item.status === "active").length || 0;
  const diagnosticsReady = latestDiagnostics !== null;
  const signerReady = latestDiagnostics?.wallet_execution?.configured === true;
  const walletLimitsReady = [policy?.per_order_limit, policy?.daily_limit].every((value) => {
    const normalized = String(value ?? "");
    return /^\d+$/.test(normalized) && BigInt(normalized) > 0n;
  });
  const walletReady = signerReady && policy?.mode === "automatic" && walletLimitsReady;
  const items = [
    { section: "models", title: "认知模型", required: true, ready: activeModels > 0, value: models === null ? "无法检查，请重试" : activeModels ? `已启用 ${activeModels} 个模型资源` : "还没有可用模型", detail: "先添加能正常返回结果的大模型 API，Noyra 才能进行认知处理。" },
    { section: "search", title: "联网搜索", required: false, ready: activeProviders > 0, value: providers === null ? "无法检查，请重试" : activeProviders ? `已启用 ${activeProviders} 个搜索 API` : "可选：模型或网页能力也可提供搜索来源", detail: "没有搜索 API 时仍可以使用其他搜索路由；按需配置即可。" },
    { section: "channels", title: "通讯渠道", required: false, ready: activeTransports > 0, value: transports === null ? "无法检查，请重试" : activeTransports ? `已启用 ${activeTransports} 个通讯渠道` : "可选：尚未接入外部渠道", detail: "可从管理台私密交流，外部平台接入属于可选项。" },
    { section: "wallet", title: "自动付款", required: false, ready: walletReady, value: policy === null || !diagnosticsReady ? "无法确认钱包状态，请刷新总览" : walletReady ? "已开启，签名器与金额上限已配置" : signerReady && policy?.mode === "automatic" && !walletLimitsReady ? "自动付款已选择；请填写正数单笔上限和日限额" : signerReady ? "签名器可用；付款策略尚未开启" : "可选：签名器未就绪，付款保持关闭", detail: "自动付款必须设置正数单笔上限和日限额；收款白名单默认关闭。" },
  ];
  const readyCount = items.filter((item) => item.ready).length;
  $("#setup-guide-state").textContent = activeModels ? `${readyCount} / ${items.length} 项就绪` : "需要配置模型";
  $("#setup-guide-list").innerHTML = items.map((item, index) => {
    const state = item.ready ? "已就绪" : item.required ? "待配置" : "可选";
    return `<li class="setup-guide-item ${item.ready ? "is-ready" : ""}"><span class="setup-step-number">${index + 1}</span><div class="setup-step-copy"><div class="setup-step-title"><strong>${esc(item.title)}</strong><span class="setup-step-state">${esc(state)}</span>${item.required ? '<span class="setup-required">必要</span>' : '<span class="setup-optional">可选</span>'}</div><p>${esc(item.value)}</p><small>${esc(item.detail)}</small></div><button type="button" data-setup-go="${esc(item.section)}">前往设置</button></li>`;
  }).join("");
  if (results.some((result) => result.status === "rejected")) $("#setup-guide-state").textContent = "部分项目未能检查";
}

async function loadSetupGuide() {
  const current = beginAdminRead("setup");
  const results = await Promise.allSettled([
    request("/api/config/model-resources"),
    request("/api/config/search-providers"),
    request("/api/config/transports"),
    request("/api/admin/wallet-policy"),
  ]);
  if (!current()) return;
  const unauthorized = results.find((result) => result.status === "rejected" && result.reason?.status === 401);
  if (unauthorized) throw unauthorized.reason;
  renderSetupGuide(results);
}
const chat = {current:null,windows:[],nextOffset:null,before:null,rows:[],drafts:new Map(),keys:new Map(),generation:0,listGeneration:0,timer:null,history:false,failures:0};
const chatStatus = {offered:"已记录，等待处理",accepted:"接受",rejected:"拒绝",deferred:"暂缓",silent:"选择沉默",sent:"已生成回复",expired:"已过期"};
function clearChat() { clearQQIdentity(); clearTimeout(chat.timer); chat.generation++; chat.listGeneration++; chat.current=null; chat.windows=[]; chat.rows=[]; chat.drafts.clear(); chat.keys.clear(); $("#admin-message").value=""; $("#mailbox-list").replaceChildren(); $("#conversation-list").replaceChildren(); $("#message-status").textContent=""; $("#conversation-title").textContent="选择一个会话"; }
async function loadMailbox({append=false}={}) {
  const generation=++chat.listGeneration;
  const page=await request(`/api/admin/conversations?limit=50&archived=${$("#conversation-filter").value === "archived"}&offset=${append ? chat.nextOffset || 0 : 0}`);
  if(generation !== chat.listGeneration || !csrfToken) return;
  chat.windows=append ? chat.windows.concat(page.items) : page.items; chat.nextOffset=page.next_offset;
  $("#conversation-more").hidden=page.next_offset === null;
  $("#conversation-list").innerHTML=chat.windows.map(row=>`<button class="chat-window ${row.conversation_id===chat.current?.conversation_id?"active":""}" data-window="${esc(row.conversation_id)}" type="button">${row.pinned?"置顶 · ":""}${esc(row.title)}</button>`).join("") || '<p class="muted">暂无会话</p>';
  if(chat.current) chat.current=chat.windows.find(row=>row.conversation_id===chat.current.conversation_id) || chat.current;
  if(!chat.current && chat.windows.length) await selectChat(chat.windows[0]);
  else if(chat.current && !chat.history) await loadChatMessages();
}
async function selectChat(row) {
  if(!row) return;
  clearTimeout(chat.timer);
  if(chat.current) chat.drafts.set(chat.current.conversation_id,$("#admin-message").value);
  chat.current=row; chat.before=null; chat.rows=[]; chat.history=false; chat.failures=0; $("#chat-search").value=""; $("#chat-status").value="";
  $("#admin-message").value=chat.drafts.get(row.conversation_id)||"";
  $(".chat-workspace").classList.add("has-current");
  await loadChatMessages();
}
function renderChatMessages() {
  let day="";
  $("#mailbox-list").innerHTML=chat.rows.map(row=>{
    const date=new Date(row.created_at).toLocaleDateString(); const heading=date!==day?`<div class="chat-date">${esc(date)}</div>`:""; day=date;
    const mine=row.direction==="incoming" && row.channel==="web" && row.author_verified;
    const author=row.direction==="outgoing" ? "Noyra" : mine?"我":row.channel==="web"?"历史网页访客（作者未确认）":`${row.channel} · ${row.counterparty}`;
    const progress=["offered","deferred"].includes(row.status)?({executing:"模型调用中",prepared:"模型调用已准备",failed:"最近调用失败，等待后续处理",unknown:"调用结果未知，等待核对"}[row.model_call_status]||chatStatus[row.status]):chatStatus[row.status];
    const delivery=(row.delivery_statuses || []).map(status=>({queued:"等待平台投递",sending:"平台投递中",delivered:"平台已接收",failed:"平台投递失败",unknown:"平台投递结果未知，需核对",cancelled:"平台投递已取消"}[status] || "未知投递状态")).join("、");
    return heading+`<article class="chat-message ${mine?"mine":""}"><small>${esc(author)} · ${esc(formatTimestamp(row.created_at))}</small><p>${esc(row.content)}</p><small>${esc(progress||row.status)}${row.related_interaction_id?" · 回复关联原消息":""}${delivery ? " · "+esc(delivery):""}</small><button data-copy-message="${esc(row.interaction_id)}" title="复制消息" type="button">复制</button></article>`;
  }).join("") || '<p class="muted">暂无消息</p>';
}
async function loadChatMessages({older=false}={}) {
  if(!chat.current || !csrfToken) return;
  clearTimeout(chat.timer); const generation=++chat.generation; const windowId=chat.current.conversation_id;
  if(older) chat.history=true;
  const query=new URLSearchParams({limit:"50",query:$("#chat-search").value,status:$("#chat-status").value}); if(older&&chat.before) query.set("before",chat.before);
  const list=$("#mailbox-list"); const bottom=list.scrollHeight-list.scrollTop-list.clientHeight<60; const position=list.scrollTop; const previousHeight=list.scrollHeight;
  try {
    const page=await request(`/api/admin/conversations/${encodeURIComponent(windowId)}/messages?${query}`);
    if(generation!==chat.generation||!csrfToken) return;
    chat.current=page.conversation; chat.rows=older?Array.from(new Map(page.items.concat(chat.rows).map(row=>[row.interaction_id,row])).values()):page.items; chat.before=page.next_before; chat.failures=0;
    $("#conversation-title").textContent=chat.current.title; $("#pin-conversation").textContent=chat.current.pinned?"取消置顶":"置顶"; $("#archive-conversation").textContent=chat.current.status==="archived"?"恢复会话":"归档";
    $("#admin-message").disabled=chat.current.status!=="active"; $("#chat-more").hidden=!chat.before;
    renderChatMessages();
    if(older) list.scrollTop=position+list.scrollHeight-previousHeight;
    else if(bottom) list.scrollTop=list.scrollHeight;
    else {list.scrollTop=position; $("#chat-latest").hidden=false;}
  } catch(error) {if(generation===chat.generation && csrfToken) chat.failures++; throw error;}
  finally { if(generation===chat.generation && activeSection==="conversation" && !document.hidden && !chat.history && !$("#chat-search").value && !$("#chat-status").value) chat.timer=setTimeout(()=>{void loadChatMessages().catch(error=>setStatus("#message-status",errorText(error),true));},Math.min(60000,10000*2**chat.failures)); }
}
$("#conversation-list").addEventListener("click",event=>{const button=event.target.closest("[data-window]"); if(button) void selectChat(chat.windows.find(row=>row.conversation_id===button.dataset.window)).catch(error=>setStatus("#message-status",errorText(error),true));});
$("#conversation-filter").addEventListener("change",()=>{if(chat.current) chat.drafts.set(chat.current.conversation_id,$("#admin-message").value); chat.generation++; clearTimeout(chat.timer); chat.current=null; chat.rows=[]; renderChatMessages(); void loadMailbox().catch(error=>setStatus("#message-status",errorText(error),true));});
$("#conversation-more").addEventListener("click",()=>{void loadMailbox({append:true});});
$("#chat-more").addEventListener("click",()=>{void loadChatMessages({older:true}).catch(error=>setStatus("#message-status",errorText(error),true));});
for(const id of ["chat-search-button","chat-status"]) $("#"+id).addEventListener(id==="chat-status"?"change":"click",()=>{chat.history=false; void loadChatMessages().catch(error=>setStatus("#message-status",errorText(error),true));});
$("#chat-back").addEventListener("click",()=>{$(".chat-workspace").classList.remove("has-current");});
$("#chat-latest").addEventListener("click",()=>{chat.history=false; $("#chat-search").value=""; $("#chat-status").value=""; const current=beginAdminAction(); void loadChatMessages().then(()=>{if (!current()) return;$("#mailbox-list").scrollTop=$("#mailbox-list").scrollHeight; $("#chat-latest").hidden=true;}).catch(error=>setStatus("#message-status",errorText(error),true));});
$("#mailbox-list").addEventListener("click",async event=>{
  const currentAction = beginAdminAction();const button=event.target.closest("[data-copy-message]"); if(button) {try {await navigator.clipboard.writeText(chat.rows.find(row=>row.interaction_id===button.dataset.copyMessage).content);} catch { if (!currentAction()) return;setStatus("#message-status","复制失败",true);}}});
$("#new-conversation").addEventListener("click",async()=>{
  const currentAction = beginAdminAction();const title=prompt("会话名称", "新会话"); if(!title?.trim()) return; try{const row=await request("/api/admin/conversations",{method:"POST",body:JSON.stringify({title:title.trim()})}); if (!currentAction()) return; selectChat(row); await loadMailbox(); if (!currentAction()) return;}catch (error) { if (!currentAction()) return;setStatus("#message-status",errorText(error),true);}});
for(const [id,action] of [["rename-conversation","title"],["pin-conversation","pinned"],["archive-conversation","archived"]]) $("#"+id).addEventListener("click",async()=>{
  const currentAction = beginAdminAction();
  if(!chat.current) return; let value;
  if(action==="title") {value=prompt("会话名称",chat.current.title); if(!value?.trim()) return;}
  else value=action==="pinned"?!chat.current.pinned:chat.current.status!=="archived";
  try {const updated=await request(`/api/admin/conversations/${encodeURIComponent(chat.current.conversation_id)}`,{method:"POST",body:JSON.stringify({[action]:value,expected_revision:chat.current.revision})}); if (!currentAction()) return; chat.current=updated; await loadMailbox(); if (!currentAction()) return;}
  catch (error) { if (!currentAction()) return;setStatus("#message-status",errorText(error),true);}
});

const walletRunStatusLabels = { queued: "排队中", running: "执行中", retry_wait: "等待重试", succeeded: "成功", failed: "失败", unknown: "结果未知", cancelled: "已取消" };
const walletAttemptStatusLabels = { executing: "执行中", succeeded: "成功", failed: "失败", unknown: "结果未知" };

function walletStatusLabel(status, labels = walletRunStatusLabels) { return labels[status] || status || "未知"; }
function walletOrderStatus(status) { return ({open:"开放中",closed:"已关闭",pending_policy:"等待策略",awaiting_confirmation:"等待确认",reserved:"已预留",signing:"签名中",broadcast:"已广播",confirmed:"链上已确认",refunded:"预留已释放",rejected:"已拒绝",expired:"已过期",...walletRunStatusLabels})[status] || status || "未知"; }
function walletAmount(value, assetId) {
  if (value === null || value === undefined) return "无金额记录";
  if (typeof value !== "string" || !/^-?\d+$/.test(value) || value.length > 512) return "金额格式异常";
  const asset = walletAssetLookup().get(assetId);
  if (!asset || !Number.isInteger(asset.decimals) || asset.decimals < 0 || asset.decimals > 255) return `${value} 最小单位（精度未知）`;
  const negative = value.startsWith("-");
  const digits = BigInt(negative ? value.slice(1) : value).toString().padStart(asset.decimals + 1, "0");
  const fraction = asset.decimals ? digits.slice(-asset.decimals).replace(/0+$/, "") : "";
  return `${negative && BigInt(value) !== 0n ? "-" : ""}${asset.decimals ? digits.slice(0, -asset.decimals) : digits}${fraction ? "." + fraction : ""} ${asset.symbol}`;
}
function walletObservationLabel(observedAt) {
  const date = Date.parse(observedAt);
  const reference = Date.parse(walletObservationHealth?.evaluated_at);
  const threshold = walletObservationHealth?.stale_after_seconds;
  if (!Number.isFinite(date)) return "观测时间异常";
  if (!Number.isFinite(reference) || !Number.isFinite(threshold)) return "新鲜度尚未读取";
  const age = (reference - date) / 1000;
  if (age < 0) return "未来时间异常";
  return age > threshold ? "已过期" : age >= threshold * 0.8 ? "临近过期" : "新鲜快照";
}
function walletRPCHost(value) { try { return new URL(value).hostname; } catch { return "RPC 地址异常"; } }
function walletCoverage(rows, label) { return `${label}：已读 ${rows.length} 条${rows.length >= 100 ? "（读取上限 100，可能还有记录）" : ""}`; }
async function walletRefreshAfterWrite(status, message, refresh) {
  const current = beginAdminAction();
  if (!current()) return;
  setStatus(status, message);
  try { await refresh(); if (current()) setStatus(status, message); }
  catch (error) { if (current() && !error?.staleSession) setStatus(status, `${message}；读取更新失败，请刷新核对。${errorText(error)}`, true); }
}
function walletNetworkLookup() { return new Map(walletNetworks.map((item) => [item.network_id, item])); }
function walletAssetLookup() { return new Map(walletAssets.map((item) => [item.asset_id, item])); }
function walletAddressLookup() { return new Map(walletAddresses.map((item) => [item.address_id, item])); }
function walletTargetText(assetId, addressId) {
  const asset = walletAssetLookup().get(assetId);
  const address = walletAddressLookup().get(addressId);
  const network = walletNetworkLookup().get(asset?.network_id || address?.network_id);
  return [network?.label || network?.network_id || "未知网络", asset?.name || assetId || "未知资产", address?.label || address?.address || addressId || "未知地址"].join(" · ");
}
function renderWalletObservationHealth(payload) {
  const list = $("#wallet-observation-health-list");
  const statusNode = $("#wallet-observation-health-status");
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.groups)) {
    list.innerHTML = '<div class="muted">健康分组响应无效，请重试</div>';
    setStatus("#wallet-observation-health-status", "健康分组响应无效，请重试", true);
    return;
  }
  const statusLabels = { ok: "正常", attention: "需要关注", degraded: "不可用" };
  const groupBy = payload.group_by === "source" ? "source" : "network";
  const valueLabel = groupBy === "source" ? "最新来源" : "网络";
  const rows = payload.groups;
  list.innerHTML = rows.length ? rows.map((group) => {
    const value = group.value === null ? "无观测来源" : group.value;
    return '<article class="resource-item wallet-observation-item"><div class="wallet-observation-main"><div class="wallet-observation-title"><h3>' + esc(value) + '</h3><span class="status-chip status-' + esc(group.status) + '">' + esc(statusLabels[group.status] || group.status || "未知") + '</span></div><dl class="wallet-observation-facts"><div><dt>活动组合</dt><dd>' + esc(formatInteger(group.active_pairs)) + '</dd></div><div><dt>已观测</dt><dd>' + esc(formatInteger(group.observed_pairs)) + '</dd></div><div><dt>新鲜 / 临界 / 过期</dt><dd>' + esc(formatInteger(group.fresh_pairs)) + ' / ' + esc(formatInteger(group.near_expiry_pairs)) + ' / ' + esc(formatInteger(group.stale_pairs)) + '</dd></div><div><dt>从未观测 / 未来</dt><dd>' + esc(formatInteger(group.never_pairs)) + ' / ' + esc(formatInteger(group.future_pairs)) + '</dd></div><div><dt>异常组合</dt><dd>' + esc(formatInteger(group.anomalous_pairs)) + '</dd></div><div><dt>历史快照</dt><dd>' + esc(formatInteger(group.snapshot_count)) + '</dd></div><div><dt>最近观测</dt><dd>' + esc(formatTimestamp(group.latest_observed_at)) + '</dd></div><div><dt>最大年龄</dt><dd>' + esc(group.max_age_seconds == null ? "无" : formatInteger(group.max_age_seconds) + " 秒") + '</dd></div></dl></div></article>';
  }).join("") : '<div class="empty-state">暂无健康分组</div>';
  const suffix = payload.has_more ? `，显示前 ${rows.length} / ${payload.total_groups} 组` : `，共 ${payload.total_groups} 组`;
  setStatus("#wallet-observation-health-status", valueLabel + "健康：" + (statusLabels[payload.status] || payload.status || "未知") + suffix);
  statusNode.classList.remove("error");
}
async function loadWalletObservationHealth() {
  const version = ++walletObservationLoadVersion;
  const groupBy = $("#wallet-observation-group").value || "network";
  const refreshButton = $("#refresh-wallet-observation-health");
  const groupControl = $("#wallet-observation-group");
  refreshButton.disabled = true;
  groupControl.disabled = true;
  setStatus("#wallet-observation-health-status", "正在读取");
  try {
    const payload = await request("/api/config/wallet-observation-health?group_by=" + encodeURIComponent(groupBy) + "&limit=100");
    if (version !== walletObservationLoadVersion || !csrfToken) return;
    walletObservationHealth = payload;
    renderWalletObservationHealth(payload);
    renderWalletGraph();
  } catch (error) {
    if (version === walletObservationLoadVersion) {
      walletObservationHealth = null;
      $("#wallet-observation-health-list").innerHTML = '<div class="muted">健康分组加载失败，请重试</div>';
      setStatus("#wallet-observation-health-status", errorText(error), true);
    }
    throw error;
  } finally {
    if (version === walletObservationLoadVersion) {
      refreshButton.disabled = false;
      groupControl.disabled = false;
    }
  }
}

function renderWalletMetrics(health, wallet = {}) {
  const observations=wallet.balance_history || {};
  const freshness=observations.observation_health || {};
  $("#wallet-metrics").innerHTML = [
    ["采集状态", walletStatusLabel(health?.status, { ok: "正常", attention: "需要关注", degraded: "不可用" })],
    ["活动运行", health?.active ?? 0],
    ["需要关注", health?.attention ?? 0],
    ["过期租约", health?.expired_running ?? 0],
    ["RPC 尝试", health?.attempts ?? 0],
    ["最近观测",formatTimestamp(observations.latest_observed_at)],
    ["过期 / 无快照",`${freshness.stale_pairs ?? "未读取"} / ${freshness.never_pairs ?? "未读取"}`],
  ].map(([label, value]) => '<div class="metric"><span>' + esc(label) + "</span><strong>" + esc(value) + "</strong></div>").join("");
}
function updateWalletAddressOptions() {
  const assetId = $("#wallet-enqueue-asset").value;
  const asset = walletAssetLookup().get(assetId);
  const addressSelect = $("#wallet-enqueue-address");
  const current = addressSelect.value;
  const choices = asset ? walletAddresses.filter((item) => item.status === "active" && item.network_id === asset.network_id) : [];
  addressSelect.innerHTML = choices.length
    ? choices.map((item) => '<option value="' + esc(item.address_id) + '">' + esc(item.label) + " · " + esc(item.address) + "</option>").join("")
    : '<option value="">先配置活动地址</option>';
  if (choices.some((item) => item.address_id === current)) addressSelect.value = current;
  addressSelect.disabled = !choices.length;
  $("#wallet-enqueue-form button[type=submit]").disabled = !asset || !choices.length;
}
function renderWalletEnqueueOptions() {
  const assetSelect = $("#wallet-enqueue-asset");
  const current = assetSelect.value;
  const networks = walletNetworkLookup();
  const choices = walletAssets.filter((item) => item.status === "active" && networks.get(item.network_id)?.status === "active");
  assetSelect.innerHTML = choices.length
    ? choices.map((item) => '<option value="' + esc(item.asset_id) + '">' + esc(item.name) + " · " + esc(item.symbol) + " · " + esc(networks.get(item.network_id)?.label || item.network_id) + "</option>").join("")
    : '<option value="">先配置活动资产</option>';
  if (choices.some((item) => item.asset_id === current)) assetSelect.value = current;
  assetSelect.disabled = !choices.length;
  updateWalletAddressOptions();
}
function renderWalletGraph() {
  const balancesByNetwork = new Map();
  walletBalances.forEach((item) => { if (!balancesByNetwork.has(item.network_id)) balancesByNetwork.set(item.network_id, []); balancesByNetwork.get(item.network_id).push(item); });
  const networks = [...balancesByNetwork.keys()].map(id=>walletNetworks.find(row=>row.network_id===id) || {network_id:id,label:id});
  $("#wallet-graph-list").innerHTML = networks.length ? networks.map((network) => {
    const balances = balancesByNetwork.get(network.network_id) || [];
    const balanceMarkup = balances.length
      ? "<ul>" + balances.map((item) => "<li>" + esc(walletTargetText(item.asset_id, item.address_id)) + " · <strong>" + esc(walletAmount(item.balance,item.asset_id)) + "</strong> · " + esc(formatTimestamp(item.observed_at)) + " · " + esc(walletObservationLabel(item.observed_at)) + '<details><summary>最小单位原值</summary><code>' + esc(item.balance) + '</code></details></li>').join("") + "</ul>"
      : '<div class="muted">暂无余额观测</div>';
    return '<article class="resource-item wallet-network-item"><div><h3>' + esc(network.label) + '</h3>' + balanceMarkup + '</div></article>';
  }).join("") : '<div class="empty-state">暂无余额快照</div>';
  $("#wallet-graph-list").insertAdjacentHTML("beforeend",walletPageControls("balances"));
  renderWalletEnqueueOptions();
}
async function readWalletPage(kind, {move = "refresh", status = null} = {}) {
  const epoch = walletListEpoch;
  const before = walletListPages.get(kind);
  const reset = move === "refresh" || before?.status !== status;
  const history = reset ? [null] : [...before.history];
  let index = reset ? 0 : before.index;
  if (move === "next" && !reset) {
    if (!before.next_cursor) return before.items;
    index++;
    history[index] = before.next_cursor;
  } else if (move === "previous" && !reset) index = Math.max(0, index - 1);
  const version = (walletListVersions.get(kind) || 0) + 1;
  walletListVersions.set(kind, version);
  const query = new URLSearchParams({limit: "25"});
  if (status) query.set("status", status);
  if (history[index]) query.set("cursor", history[index]);
  const page = await request(`/api/admin/wallet-lists/${kind}?${query}`);
  if (!csrfToken || epoch !== walletListEpoch || version !== walletListVersions.get(kind)) return null;
  if (!Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < 0 ||
      typeof page.has_more !== "boolean" || (page.has_more && typeof page.next_cursor !== "string")) {
    throw new Error("钱包分页响应无效");
  }
  walletListPages.set(kind, {...page, history, index, status});
  return page.items;
}
function walletPageControls(kind) {
  const page = walletListPages.get(kind);
  if (!page) return "";
  return `<div class="resource-actions wallet-pagination"><span>第 ${page.index + 1} 页 · 本页 ${page.items.length} 条 · 当前筛选 ${page.total} 条</span><button type="button" data-wallet-page="${kind}" data-move="previous" ${page.index ? "" : "disabled"}>上一页</button><button type="button" data-wallet-page="${kind}" data-move="next" ${page.next_cursor ? "" : "disabled"}>下一页</button><small>新记录请刷新；状态按读取时显示</small></div>`;
}
async function loadWalletRegistry({kind = null, move = "refresh"} = {}) {
  const status = $("#wallet-registry-filter").value || null;
  const targets = {networks: "网络", assets: "资产", addresses: "观察地址"};
  const failures = [];
  await Promise.all(Object.entries(targets).map(async ([source, label]) => {
    if (kind && kind !== source) return;
    try {
      const rows = await readWalletPage(source, {move, status});
      if (!rows) return;
      $("#wallet-registry-" + source).innerHTML = rows.map(row => {
        const id = row[({networks:"network_id",assets:"asset_id",addresses:"address_id"})[source]];
        const facts = source === "networks" ? `${row.chain_family} · Chain ID ${row.chain_id} · ${row.rpc_host || "未配置 RPC"}` : source === "assets" ? `${row.symbol} · 小数位 ${row.decimals} · ${row.contract_address || "原生币"}` : row.address;
        return `<article class="resource-item"><div><strong>${esc(row.label || row.name)}</strong><p>${esc(facts)}</p><small>${esc(walletStatusLabel(row.status,{active:"活动",revoked:"已撤销"}))} · ${esc(id)}</small></div>${row.status === "active" ? `<button type="button" data-wallet-revoke="${source}" data-id="${esc(id)}" data-label="${esc(row.label || row.name)}">撤销</button>` : ""}</article>`;
      }).join("") || `<p class="muted">暂无${label}</p>`;
      $("#wallet-registry-" + source).insertAdjacentHTML("beforeend", walletPageControls(source));
    } catch (error) { failures.push(`${label}：${errorText(error)}`); }
  }));
  if (csrfToken) setStatus("#wallet-registry-status", failures.join("；") || "已读取；撤销保留历史记录", Boolean(failures.length));
}
async function loadWalletGraph({move = "refresh"} = {}) {
  const version = ++walletGraphLoadVersion;
  const [networks, assets, addresses, balances] = await Promise.all([
    request("/api/config/wallet-networks?limit=100"),
    request("/api/config/wallet-assets?limit=100"),
    request("/api/config/wallet-addresses?limit=100"),
    ...(walletArea === "balances" ? [readWalletPage("balances",{move})] : [Promise.resolve(walletBalances)]),
  ]);
  if (version !== walletGraphLoadVersion || !csrfToken) return;
  walletNetworks = Array.isArray(networks) ? networks : [];
  walletAssets = Array.isArray(assets) ? assets : [];
  walletAddresses = Array.isArray(addresses) ? addresses : [];
  walletBalances = Array.isArray(balances) ? balances : [];
  renderWalletGraph();
  const select = $("#wallet-budget-network");
  const current = select.value;
  select.innerHTML = walletNetworks.length
    ? walletNetworks.map((item) => '<option value="' + esc(item.network_id) + '">' + esc(item.label) + " · " + esc(item.network_id) + "</option>").join("")
    : '<option value="">尚未登记网络</option>';
  if (walletNetworks.some((item) => item.network_id === current)) select.value = current;
  select.disabled = !walletNetworks.length;
  for(const control of document.querySelectorAll("[data-wallet-network-select]")) {
    const selected=control.value;const choices=walletNetworks.filter(row=>row.status==="active");
    control.innerHTML=choices.map(row=>`<option value="${esc(row.network_id)}">${esc(row.label)}</option>`).join("") || '<option value="">先登记网络</option>';
    if(choices.some(row=>row.network_id===selected))control.value=selected;
    control.disabled=!choices.length;
  }
}
function renderWalletBudget(budget) {
  $("#wallet-budget-summary").innerHTML = summaryRows([
    ["窗口", formatInteger(budget.window_seconds) + " 秒"],
    ["窗口请求上限", formatInteger(budget.request_limit)],
    ["已使用请求", formatInteger(budget.requests_used)],
    ["已预留请求", formatInteger(budget.requests_reserved)],
    ["剩余请求", formatInteger(Math.max(0,budget.request_limit-budget.requests_used-budget.requests_reserved))],
    ["下一次可用", budget.next_allowed_at ? formatTimestamp(budget.next_allowed_at) : "现在"],
  ]);
}
async function loadWalletBudget() {
  const version = ++walletBudgetLoadVersion;
  const networkId = $("#wallet-budget-network").value;
  if (!networkId) {
    $("#wallet-budget-summary").innerHTML = '<div class="muted">尚未登记钱包网络</div>';
    return;
  }
  const button = $("#refresh-wallet-budget");
  button.disabled = true;
  setStatus("#wallet-budget-status", "正在读取");
  try {
    const budget = await request("/api/admin/wallet-acquisition-budget?network_id=" + encodeURIComponent(networkId));
    if (version !== walletBudgetLoadVersion || !csrfToken) return;
    if (!budget || typeof budget !== "object") throw new Error("RPC 预算响应无效");
    renderWalletBudget(budget);
    setStatus("#wallet-budget-status", "");
  } catch (error) {
    if (version === walletBudgetLoadVersion) setStatus("#wallet-budget-status", errorText(error), true);
    throw error;
  } finally {
    if (version === walletBudgetLoadVersion) button.disabled = false;
  }
}
function renderWalletAttempts(target, attempts) {
  target.innerHTML = attempts.length
    ? attempts.map((attempt) => '<div class="wallet-attempt-row"><span>#' + esc(attempt.attempt_number) + " · " + esc(walletStatusLabel(attempt.status, walletAttemptStatusLabels)) + "</span><span>" + esc(attempt.error_code || "无错误") + " · " + esc(attempt.request_count ?? "未结算") + " 请求</span><time>" + esc(formatTimestamp(attempt.started_at)) + "</time></div>").join("")
    : '<div class="muted">暂无尝试记录</div>';
  target.hidden = false;
}
function renderWalletAcquisitions() {
  const list = $("#wallet-acquisition-list");
  if (!walletAcquisitionRows.length) {
    list.innerHTML = '<div class="empty-state">该状态暂无采集运行</div>';
    return;
  }
  list.innerHTML = walletAcquisitionRows.map((row) => {
    const canRetry = row.status === "unknown" && Number(row.attempt_count) < Number(row.max_attempts);
    const canCancel = row.status === "queued" || row.status === "retry_wait";
    const actions = canRetry || canCancel
      ? '<div class="wallet-acquisition-controls"><input data-wallet-acquisition-reason maxlength="2000" placeholder="' + (canRetry ? "填写明确重试理由" : "填写取消理由") + '"><div class="resource-actions">' + (canRetry ? '<button data-wallet-acquisition-action="retry" type="button">重试</button>' : "") + (canCancel ? '<button data-wallet-acquisition-action="cancel" type="button">取消</button>' : "") + "</div></div>"
      : Number(row.attempt_count) >= Number(row.max_attempts) && row.status === "unknown" ? '<p class="muted">已达到最大尝试次数，不能自动重试</p>' : "";
    return '<article class="resource-item wallet-acquisition-item" data-run-id="' + esc(row.run_id) + '"><div class="wallet-acquisition-main"><div class="wallet-acquisition-title"><h3>' + esc(walletTargetText(row.asset_id, row.address_id)) + '</h3><span class="status-chip status-' + esc(row.status) + '">' + esc(walletStatusLabel(row.status)) + '</span></div><dl class="wallet-acquisition-facts"><div><dt>运行 ID</dt><dd><code>' + esc(row.run_id) + "</code></dd></div><div><dt>尝试</dt><dd>" + esc(row.attempt_count) + " / " + esc(row.max_attempts) + "</dd></div><div><dt>最近错误</dt><dd>" + esc(row.last_error_code || "无") + "</dd></div><div><dt>更新时间</dt><dd>" + esc(formatTimestamp(row.updated_at)) + "</dd></div><div><dt>余额快照</dt><dd>" + esc(row.snapshot_id || "无") + "</dd></div><div><dt>租约到期</dt><dd>" + esc(formatTimestamp(row.lease_expires_at)) + "</dd></div></dl>" + actions + '<div class="wallet-attempts" data-wallet-attempts hidden></div></div><div class="resource-actions wallet-acquisition-actions"><button data-wallet-acquisition-action="attempts" type="button">查看尝试</button></div></article>';
  }).join("");
}
async function loadWalletAcquisitions({move = "refresh"} = {}) {
  const version = ++walletQueueLoadVersion;
  const filter = $("#wallet-acquisition-status-filter").value;
  const refreshButton = $("#refresh-wallet-acquisitions");
  const filterControl = $("#wallet-acquisition-status-filter");
  refreshButton.disabled = true;
  filterControl.disabled = true;
  setStatus("#wallet-acquisition-status", "正在读取");
  try {
    const rows = await readWalletPage("acquisitions", {move, status: filter === "all" ? null : filter});
    if (version !== walletQueueLoadVersion || !csrfToken) return;
    if (!Array.isArray(rows)) throw new Error("采集队列响应无效");
    walletAcquisitionRows = rows;
    renderWalletAcquisitions();
    $("#wallet-acquisition-list").insertAdjacentHTML("beforeend", walletPageControls("acquisitions"));
    setStatus("#wallet-acquisition-status", "状态按本次读取时间显示");
  } catch (error) {
    if (version === walletQueueLoadVersion) {
      setStatus("#wallet-acquisition-status", errorText(error), true);
    }
    throw error;
  } finally {
    if (version === walletQueueLoadVersion) {
      refreshButton.disabled = false;
      filterControl.disabled = false;
    }
  }
}
async function loadWalletEconomy({kind = null, move = "refresh"} = {}) {
  const version = ++walletEconomyLoadVersion;
  const [bounties, orders, ledger] = await Promise.all([
    kind && kind !== "bounties" ? Promise.resolve(walletListPages.get("bounties")?.items || []) : readWalletPage("bounties",{move}),
    kind && kind !== "orders" ? Promise.resolve(walletListPages.get("orders")?.items || []) : readWalletPage("orders",{move,status:$("#wallet-order-filter").value || null}),
    request("/api/admin/wallet-ledger?limit=100"),
  ]);
  if (version !== walletEconomyLoadVersion || !csrfToken) return;
  walletOrders = Array.isArray(orders) ? orders : [];
  $("#wallet-bounty-list").innerHTML = (Array.isArray(bounties) && bounties.length) ? bounties.map((item) => `<article class="resource-item"><strong>${esc(item.title)}</strong><span class="muted">${esc(walletOrderStatus(item.status))} · ${esc(walletAmount(item.reward_amount,item.asset_id))}</span></article>`).join("") : '<div class="muted">暂无任务</div>';
  $("#wallet-bounty-list").insertAdjacentHTML("beforeend", walletPageControls("bounties"));
  $("#wallet-order-list").innerHTML = (Array.isArray(orders) && orders.length) ? orders.map((item) => {
    const execute = item.status === "reserved" ? `<button data-wallet-order-action="execute" data-order-id="${esc(item.order_id)}" type="button">签名并广播</button><button data-wallet-order-action="cancel" data-order-id="${esc(item.order_id)}" type="button">取消订单</button>` : item.status==="awaiting_confirmation" ? `<button data-wallet-order-action="confirm" data-order-id="${esc(item.order_id)}" type="button">确认付款额度</button><button data-wallet-order-action="reject" data-order-id="${esc(item.order_id)}" type="button">拒绝订单</button>` : "";
    return `<article class="resource-item"><div><strong>${esc(walletAmount(item.amount,item.asset_id))}</strong><p>${esc(walletOrderStatus(item.status))} · 收款 ${esc(item.recipient_address)}</p><details><summary>订单详情</summary><code>${esc(item.order_id)}</code><p>最小单位 ${esc(item.amount)}</p></details></div><div class="resource-actions">${execute}</div></article>`;
  }).join("") : '<div class="muted">暂无订单</div>';
  $("#wallet-order-list").insertAdjacentHTML("beforeend", walletPageControls("orders"));
  $("#wallet-ledger-list").innerHTML = Array.isArray(ledger) ? ledger.map((item) => `<div class="summary-row"><span>${esc(item.account)} · ${esc(item.network_id)}</span><strong>${esc(walletAmount(item.net,item.asset_id))}</strong></div>`).join("") : "";
}

function renderWalletPolicy(policy) {
  walletPolicy = policy && typeof policy === "object" ? policy : null;
  if (!walletPolicy) return;
  $("#wallet-policy-effective").innerHTML=summaryRows([["模式",({disabled:"付款关闭",conditional_confirmation:"逐笔确认",automatic:"自动付款"})[policy.mode]||policy.mode],["自动付款开关",policy.automation_enabled ? "开启":"关闭"],["紧急暂停",policy.emergency_paused ? "暂停中":"未暂停"],["单笔上限（最小单位）",policy.per_order_limit],["每日上限（最小单位）",policy.daily_limit],["收款白名单",policy.recipient_allowlist_enabled ? `${policy.allowed_recipient_addresses.length} 个地址`:"关闭"],["有效策略版本",policy.policy_version]]);
  if($("#wallet-policy-form").dataset.dirty)return;
  walletPolicyDraftVersion = policy.policy_version;
  $("#wallet-policy-mode").value = walletPolicy.mode || "disabled";
  $("#wallet-policy-per-order").value = walletPolicy.per_order_limit ?? "0";
  $("#wallet-policy-daily").value = walletPolicy.daily_limit ?? "0";
  $("#wallet-policy-automation-enabled").checked = walletPolicy.automation_enabled === true;
  $("#wallet-policy-allowlist-enabled").checked = walletPolicy.recipient_allowlist_enabled === true;
  $("#wallet-policy-allowlist").value = Array.isArray(walletPolicy.allowed_recipient_addresses)
    ? walletPolicy.allowed_recipient_addresses.join("\n")
    : "";
}
const walletFailureReasonLabels = {
  insufficient_balance: "余额不足",
  gas_too_high: "Gas 费用过高",
  nonce_conflict: "Nonce 冲突",
  confirmation_timeout: "确认超时",
  chain_reorg: "链重组",
  rpc_unavailable: "RPC 暂不可用",
  fee_quote_unavailable: "Gas 报价不可用",
  broadcast_unknown: "广播结果未知",
  receipt_failed: "链上执行失败",
  reconcile_required: "需要人工对账",
  unknown: "原因未知",
  none: "无原因码",
};
async function loadWalletPolicy() {
  const version=++walletPolicyLoadVersion;
  const policy = await request("/api/admin/wallet-policy");
  if(version!==walletPolicyLoadVersion || !csrfToken)return;
  renderWalletPolicy(policy);
  try {
    const automation = await request("/api/v1/admin/wallet-automation");
    if(version!==walletPolicyLoadVersion || !csrfToken)return;
    const label = automation.status === "paused" ? "紧急暂停中" : automation.status === "evidence_required" ? "自动付款受验收门禁阻断" : automation.status === "enabled" ? "自动付款已开启" : "自动付款已关闭";
    $("#wallet-automation-status").textContent = `${label} · 待处理 ${Object.values(automation.pending_reason_counts || {}).reduce((a, b) => a + Number(b || 0), 0)} 项`;
    const failure = automation.latest_failure;
    $("#wallet-automation-failure").textContent = failure
      ? `${failure.requires_reconciliation ? "需人工对账" : "最近付款失败"}：${walletFailureReasonLabels[failure.reason_code] || "原因未知"} · ${failure.status} · ${failure.updated_at}`
      : "暂无待处理的付款失败或未知结果。";
    $("#wallet-automation-pause").disabled = automation.emergency_paused === true;
    $("#wallet-automation-resume").disabled = automation.emergency_paused !== true;
  } catch (error) { setStatus("#wallet-automation-status", errorText(error), true); }
  setStatus("#wallet-policy-status", "策略已加载");
}

const providerHealthStateLabels = { healthy: "正常", degraded: "降级", cooldown: "冷却中", half_open: "恢复探测", unknown: "暂无数据" };
function renderProviderHealthList(target, rows) {
  const list = $(target);
  if (!Array.isArray(rows) || !rows.length) {
    list.innerHTML = '<div class="empty-state compact">暂无供应商健康数据</div>';
    return;
  }
  list.innerHTML = rows.map((row) => {
    const state = row.probe_in_progress ? "恢复探测中" : row.state === "half_open" ? "可尝试恢复" : row.state === "no_data" ? "暂无调用数据" : providerHealthStateLabels[row.state] || row.state || "未知";
    const failureRate = `${Math.round((Number(row.failure_rate) || 0) * 100)}%`;
    const latency = row.attempt_count && Number.isFinite(Number(row.average_latency_ms)) ? `${Math.round(Number(row.average_latency_ms))} ms` : "暂无";
    const errors = {timeout:"响应超时：检查供应商延迟与请求时限",rate_limited:"限流：核对供应商额度与并发",auth:"凭据或权限错误：核对密钥和模型授权",schema:"输出格式异常：核对兼容协议",server_error:"供应商服务错误：等待恢复",unknown:"未分类错误：查看活动日志"};
    const advice = Object.entries(row.error_counts || {}).map(([code,count]) => `<li>${esc(errors[code] || code)} · ${esc(formatInteger(count))}</li>`).join("");
    return `<article class="provider-health-item" data-health-pool="${esc(row.pool)}"><div><strong>${esc(row.label || "历史资源")}</strong><span class="status-chip status-${esc(row.state || "unknown")}">${esc(state)}</span><p>${esc(modelPoolLabels[row.pool] || "搜索")} · ${esc(row.model || "历史配置")} ${row.host ? "· " + esc(row.host) : ""} · ${esc(modelResourceStatusLabels[row.resource_status] || row.resource_status)}</p><dl><div><dt>成功 / 失败 / 未知</dt><dd>${esc(formatInteger(row.success_count || 0))} / ${esc(formatInteger(row.failure_count || 0))} / ${esc(formatInteger(row.unknown_count || 0))}</dd></div><div><dt>历史失败率</dt><dd>${row.attempt_count ? esc(failureRate) : "暂无样本"}</dd></div><div><dt>平均尝试耗时</dt><dd>${esc(latency)}</dd></div><div><dt>最近成功</dt><dd>${esc(formatTimestamp(row.last_success_at))}</dd></div><div><dt>最近失败</dt><dd>${esc(formatTimestamp(row.last_failure_at))}</dd></div><div><dt>冷却截止</dt><dd>${esc(formatTimestamp(row.cooldown_until))}</dd></div></dl><details><summary>统计详情</summary><p>保留窗口 ${esc(formatTimestamp(row.window_start))} 至 ${esc(formatTimestamp(row.window_end))}</p><p>P50 ${esc(row.p50_latency_ms || "暂无")} ms · P95 ${esc(row.p95_latency_ms || "暂无")} ms · 保留样本 ${esc(formatInteger(row.latency_sample_count || 0))} / 总尝试 ${esc(formatInteger(row.attempt_count))}（各小时有界采样，含失败耗时）</p><ul>${advice || "<li>暂无错误类别</li>"}</ul><p>${row.state === "half_open" ? "下一次需要该资源时进行一次恢复请求" : row.probe_in_progress ? "恢复请求已占用探测租约" : row.state === "cooldown" ? "冷却期间暂停新路由，结束后等待恢复请求" : "历史统计不代表当前请求一定成功"}</p><code>${esc(row.provider_id)}</code></details></div></article>`;
  }).join("");
}
let healthLoadVersion = 0;
const healthPages = {model: 0, search: 0};
async function loadProviderHealth() {
  const version = ++healthLoadVersion;
  const results = await Promise.allSettled(["model", "search"].map((kind) => request(`/api/admin/provider-health?kind=${kind}&offset=${healthPages[kind]}`)));
  if (version !== healthLoadVersion || !csrfToken) return;
  for (const [index, kind] of ["model", "search"].entries()) {
    const result = results[index]; const target = `#${kind}-provider-health-list`;
    if (result.status === "rejected") { $(target).innerHTML = `<p class="inline-error">${esc(errorText(result.reason))}</p>`; continue; }
    renderProviderHealthList(target, result.value.providers);
    $(target).insertAdjacentHTML("beforeend", `<div class="resource-actions"><button type="button" data-health-page="${kind}" data-step="-50" ${healthPages[kind] ? "" : "disabled"} title="上一页" aria-label="${kind}上一页">←</button><span>第 ${healthPages[kind] / 50 + 1} 页 · 总计 ${result.value.total || 0}</span><button type="button" data-health-page="${kind}" data-step="50" ${healthPages[kind] + 50 < result.value.total ? "" : "disabled"} title="下一页" aria-label="${kind}下一页">→</button></div>`);
  }
  setStatus("#provider-health-status", results.some((result) => result.status === "rejected") ? "部分资源读取失败" : `已更新 · ${formatTimestamp(new Date().toISOString())}`);
}
function renderRetention(payload) {
  const settings = payload?.settings || {};
  const estimate = payload?.estimate || {};
  const latest = payload?.latest;
  const sqliteSpace = payload?.sqlite_space || {};
  const plan = payload?.plan || {};
  const backupPlan = plan.backup || {};
  const backupSummary = backupPlan.backup_summary || {};
  const capacity = payload?.evidence_capacity;
  const latestDeleted = latest?.deleted_by_table ? Object.entries(latest.deleted_by_table).map(([key,value]) => `${key}: ${formatInteger(value)}`).join(" · ") : "暂无清理记录";
  const form = $("#retention-settings-form");
  if (!form.dataset.dirty) { for (const input of form.querySelectorAll("input[name]")) input.value = settings[input.name]; form.dataset.revision = settings.revision; }
  $("#retention-summary").innerHTML = summaryRows([
    ["策略来源与版本", `${settings.source === "saved" ? "已保存" : "环境默认"} · ${settings.revision}`],
    ["健康聚合保留", `${formatInteger(settings.health_days)} 天`],
    ["搜索限速记录保留", `${formatInteger(settings.search_use_hours ?? 2)} 小时`],
    ["每批上限", `${formatInteger(settings.batch_size)} 行`],
    ["清理运行历史", `保留最近 ${formatInteger(settings.run_history ?? 100)} 次`],
    ["待清理健康桶", formatInteger(estimate.provider_health_buckets)],
    ["待清理搜索限速记录", formatInteger(estimate.search_provider_uses)],
    ["待清理运行记录", formatInteger(estimate.retention_runs)],
    ["最近清理", latest ? formatTimestamp(latest.completed_at || latest.started_at) : "暂无"],
    ["最近删除", latestDeleted],
    ["SQLite 文件 / WAL", `${formatInteger(sqliteSpace.database_bytes)} / ${formatInteger(sqliteSpace.wal_bytes)} 字节`],
    ["SQLite 磁盘占用 / 空闲页", `${formatInteger(sqliteSpace.physical_bytes)} 字节 · ${formatInteger(sqliteSpace.freelist_pages)} 页`],
    ["SQLite 可回收空间", `${formatInteger(sqliteSpace.freelist_bytes)} 字节`],
    ["备份计划", backupPlan.status === "planned" ? "已生成（仅预览）" : (backupPlan.reason_code || "未配置")],
    ["已验证恢复点", `${formatInteger(backupSummary.verified_recovery_points_after_plan)} 个 · ${backupSummary.recovery_points_status || "未知"}`],
    ["备份占用 / 水位", `${formatInteger(backupSummary.total_bytes_after_plan)} 字节 · ${backupSummary.space_status || "未知"}`],
    ["未验证备份关注", formatInteger((backupPlan.backup_attention || []).length)],
    ["证据容量", capacity?.blocked ? "已达上限，暂停新增认知工作" : "可用"],
    ["必要证据每表上限", formatInteger(capacity?.limit_per_table)],
    ["模型调用 / 尝试", `${formatInteger(capacity?.counts?.model_calls)} / ${formatInteger(capacity?.counts?.model_attempts)}`],
  ]);
}
async function loadRetention() {
  const current = beginAdminRead("retention");
  const payload = await adminRead(current, "/api/admin/retention");
  if (!current()) return;
  renderRetention(payload);
  setStatus("#retention-status", `读取于 ${formatTimestamp(new Date().toISOString())}`);
}
async function runRetention() {
  const current = beginAdminRead("retention-run");
  const button = $("#run-retention");
  button.disabled = true;
  try {
    const preview = await request("/api/admin/retention");
    if (!current()) return;
    if (!confirm(`预计待处理 ${Object.values(preview.estimate || {}).reduce((sum, value) => sum + Number(value || 0), 0)} 行；实际执行受每批上限限制。继续执行一批？`)) return;
    const result = await request("/api/admin/retention/run", { method: "POST", body: JSON.stringify({ reason: "管理员执行聚合数据保留清理" }) });
    if (!current()) return;
    await loadRetention();
    if (!current()) return;
    const deleted = Object.entries(result.deleted_by_table || {}).filter(([key]) => key !== "retention_runs").reduce((sum,[,value]) => sum + Number(value || 0),0);
    setStatus("#retention-status", result.failed_reason ? `部分失败：${result.failure_stage} / ${result.failed_reason}；已删除 ${formatInteger(deleted)} 行` : `已删除 ${formatInteger(deleted)} 行；压缩 ${formatInteger(result.compacted_by_table?.model_calls || 0)} 条模型内容`, Boolean(result.failed_reason));
  } catch (error) { if (current()) setStatus("#retention-status", errorText(error), true); }
  finally { if (current()) button.disabled = false; }
}

const walletExecutionStatusLabels = { signing: "签名中", broadcast: "已广播", unknown: "结果未知", confirmed: "已确认", failed: "失败" };
function renderWalletExecutions(rows) {
  const list = $("#wallet-execution-list");
  if (!Array.isArray(rows) || !rows.length) { list.innerHTML = '<div class="muted">暂无链上转账执行记录</div>'; return; }
  list.innerHTML = rows.map((row) => {
    const status = walletExecutionStatusLabels[row.status] || row.status || "未知";
    const poll = ["broadcast", "unknown"].includes(row.status) ? `<button data-wallet-execution-action="receipt" data-id="${esc(row.execution_id)}" type="button">查回执</button>` : "";
    const retry = row.status === "unknown" ? `<button data-wallet-execution-action="retry" data-order-id="${esc(row.order_id)}" type="button">明确重试</button>` : "";
    const refund = row.status === "failed" ? `<button data-wallet-execution-action="refund" data-order-id="${esc(row.order_id)}" type="button">释放失败订单预留</button>` : "";
    return `<article class="resource-item"><div><strong>${esc(walletAmount(row.amount,row.asset_id))}</strong><p>${esc(status)} · 尝试 ${esc(row.attempt_count)}</p>${row.status==="unknown" ? '<p class="inline-error">结果未知，资金仍占用；先查回执，不能释放预留。</p>':""}<small>${esc(row.tx_hash || "尚无交易哈希")}</small><details><summary>执行详情</summary><p>订单 ${esc(row.order_id)}</p><p>收款 ${esc(row.recipient_address)} · 最小单位 ${esc(row.amount)}</p></details></div><div class="resource-actions">${poll}${retry}${refund}</div></article>`;
  }).join("");
}
async function loadWalletExecutions({move = "refresh"} = {}) {
  const version = ++walletExecutionLoadVersion;
  try {
    const rows = await readWalletPage("executions",{move,status:$("#wallet-execution-filter").value || null});
    if (version !== walletExecutionLoadVersion || !csrfToken) return;
    renderWalletExecutions(rows);
    $("#wallet-execution-list").insertAdjacentHTML("beforeend", walletPageControls("executions"));
    setStatus("#wallet-execution-status", "已加载 " + (Array.isArray(rows) ? rows.length : 0) + " 条");
  } catch (error) {
    if (version === walletExecutionLoadVersion) setStatus("#wallet-execution-status", errorText(error), true);
  }
}
const walletAuditActionLabels = {
  wallet_address_registered: "登记钱包地址",
  wallet_address_revoked: "撤销钱包地址",
  wallet_asset_registered: "登记钱包资产",
  wallet_asset_revoked: "撤销钱包资产",
  wallet_bounty_created: "创建任务",
  wallet_bounty_published: "发布任务",
  wallet_bounty_closed: "关闭任务",
  wallet_bounty_cancelled: "取消任务",
  wallet_bounty_expired: "任务已过期",
  wallet_bounty_submission_created: "收到任务提交",
  wallet_submission_accepted: "通过任务提交",
  wallet_submission_rejected: "拒绝任务提交",
  wallet_submission_withdrawn: "撤回任务提交",
  wallet_submission_expired: "任务提交已过期",
  wallet_network_registered: "登记钱包网络",
  wallet_network_revoked: "撤销钱包网络",
  wallet_payment_policy_updated: "更新付款策略",
  wallet_payment_order_created: "创建付款订单",
  wallet_payment_order_awaiting_confirmation: "等待付款确认",
  wallet_payment_order_reserved: "预留付款额度",
  wallet_payment_order_rejected: "付款申请被拒绝",
  wallet_payment_order_cancelled: "取消付款订单",
  wallet_payment_order_expired: "付款订单已过期",
  wallet_payment_order_signing: "准备签署付款",
  wallet_payment_order_broadcast: "交易已广播",
  wallet_payment_order_unknown: "交易结果未知",
  wallet_payment_order_confirmed: "交易已确认",
  wallet_payment_order_failed: "付款失败",
  wallet_payment_order_refunded: "失败订单预留已释放",
  wallet_payment_execution_retry: "重试转账执行",
  wallet_payment_failed: "付款失败",
  wallet_payment_execution_signing: "开始签名",
  wallet_payment_broadcast: "交易已广播",
  wallet_payment_confirmed: "交易已确认",
  wallet_payment_unknown: "交易结果未知",
  wallet_balance_acquisition_queued: "加入余额采集队列",
  wallet_balance_acquisition_succeeded: "余额读取成功",
  wallet_balance_acquisition_failed: "余额读取失败",
  wallet_balance_acquisition_retried: "重新排队读取",
  wallet_balance_acquisition_unknown: "余额读取结果未知",
  wallet_balance_acquisition_cancelled: "取消余额读取",
  wallet_reward_workflow_created: "创建奖励流程",
  wallet_reward_workflow_open: "开放奖励流程",
  wallet_reward_workflow_closed: "关闭奖励流程",
  wallet_reward_workflow_cancelled: "取消奖励流程",
  wallet_reward_workflow_manual_intervention: "奖励流程需要人工处理",
  wallet_reward_submission_linked: "关联奖励提交",
  wallet_reward_submission_accepted: "通过奖励验证",
  wallet_reward_submission_rejected: "拒绝奖励验证",
  wallet_reward_incident_opened: "记录奖励异常",
  wallet_reward_incident_resolved: "处理奖励异常",
};
const walletAuditFieldLabels = {
  amount: "金额",
  asset_id: "资产",
  attempt_number: "尝试次数",
  error_code: "错误代码",
  execution_id: "执行编号",
  from: "变更前",
  mode: "付款模式",
  network_id: "网络",
  order_id: "订单编号",
  policy_version: "策略版本",
  reason: "操作理由",
  status: "状态",
  submission_id: "提交编号",
  to: "变更后",
  tx_hash: "交易哈希",
};
async function loadWalletAudits({move = "refresh"} = {}) {
  const version=++walletAuditLoadVersion;
  const list = $("#wallet-audit-list");
  try {
    const rows = await readWalletPage("audits",{move});
    if(version!==walletAuditLoadVersion || !csrfToken)return;
    if (!Array.isArray(rows)) throw new Error("审计记录响应无效");
    list.innerHTML = rows.length ? rows.map((row) => {
      const details = Object.entries(row.details || {}).filter(([key]) => walletAuditFieldLabels[key]);
      const detailMarkup = details.length
        ? `<dl class="wallet-audit-details">${details.map(([key, value]) => `<div><dt>${esc(walletAuditFieldLabels[key])}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>`
        : "";
      return `<article class="resource-item wallet-audit-item"><div class="wallet-audit-heading"><strong>${esc(walletAuditActionLabels[row.action] || "其他钱包操作")}</strong><time>${esc(formatTimestamp(row.occurred_at))}</time></div><p>操作人：${esc(row.actor || "未知")}</p>${detailMarkup}<small>审计编号：${esc(row.audit_id)}</small></article>`;
    }).join("") : '<div class="empty-state">还没有钱包审计记录</div>';
    list.insertAdjacentHTML("beforeend", walletPageControls("audits"));
    setStatus("#wallet-audit-status", "只读钱包审计记录");
  } catch (error) {
    if(version !== walletAuditLoadVersion || !csrfToken) return;
    setStatus("#wallet-audit-status", errorText(error), true);
    throw error;
  }
}
async function loadWallet() {
  const version = ++walletLoadVersion;
  document.querySelectorAll("[data-wallet-area]").forEach(node=>{node.hidden=node.dataset.walletArea!==walletArea;});
  document.querySelectorAll("[data-wallet-tab]").forEach(node=>node.setAttribute("aria-pressed",String(node.dataset.walletTab===walletArea)));
  setStatus("#wallet-read-status", "正在读取已保存数据");
  const failures = [];
  const collect = async (label, load) => { try { await load(); } catch(error) { failures.push({label,error}); } };
  const summary = collect("钱包摘要",async()=>{const data=await request("/api/diagnostics"); if(version===walletLoadVersion && csrfToken) renderWalletMetrics(data.wallet_acquisition,data.wallet);});
  if(walletArea!=="audit") await collect("网络与资产",loadWalletGraph);
  if(version!==walletLoadVersion || !csrfToken)return [];
  const tasks = {balances:[["观测健康",loadWalletObservationHealth]],registry:[["资源登记",loadWalletRegistry]],budget:[["RPC 预算",loadWalletBudget]],acquisition:[["采集队列",loadWalletAcquisitions]],economy:[["赏金与账本",loadWalletEconomy]],payment:[["付款策略",loadWalletPolicy],["转账执行",loadWalletExecutions]],audit:[["审计历史",loadWalletAudits]]}[walletArea];
  await Promise.all([summary,...tasks.map(([label,load])=>collect(label,load))]);
  if(version!==walletLoadVersion || !csrfToken)return [];
  if(failures.some(row=>row.error.status===401)){await logout();return failures;}
  setStatus("#wallet-read-status", failures.length ? `${failures.map(row=>row.label).join("、")}读取失败；保留已有内容` : `读取于 ${formatTimestamp(new Date().toISOString())} · 当前分区`,Boolean(failures.length));
  return failures;
}
$("#wallet-tabs").addEventListener("click",event=>{const button=event.target.closest("[data-wallet-tab]"); if(button){walletArea=button.dataset.walletTab;void loadWallet();}});
$("#wallet-registry-filter").addEventListener("change",()=>void loadWalletRegistry());
$("#wallet-registry-refresh").addEventListener("click",()=>void loadWalletRegistry());
$("#wallet-order-filter").addEventListener("change",()=>void loadWalletEconomy({kind:"orders"}).catch(error=>setStatus("#wallet-read-status",errorText(error),true)));
$("#wallet-execution-filter").addEventListener("change",()=>void loadWalletExecutions());
document.querySelector('[data-section-panel="wallet"]').addEventListener("click", async event => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-wallet-page], [data-wallet-revoke]");
  if (!button || button.disabled) return;
  const epoch = walletListEpoch;
  button.disabled = true;
  try {
    if (button.dataset.walletRevoke) {
      const kind = button.dataset.walletRevoke;
      const reason = prompt(`撤销 ${button.dataset.label}。历史记录保留；新采集或付款将受到限制。请输入理由：`);
      if (!reason?.trim()) return;
      if (!confirm(`${kind==="networks" ? "网络撤销会限制其下全部资产和地址。" : "撤销后不能进行该项的新采集或付款。"}此操作不能撤回已经广播的交易。确认撤销？`)) return;
      await request(`/api/config/wallet-${kind}/${encodeURIComponent(button.dataset.id)}/revoke`, {method:"POST",body:JSON.stringify({reason:reason.trim()})}); if (!currentAction()) return;
      await walletRefreshAfterWrite("#wallet-registry-status","已撤销",()=>Promise.all([loadWalletRegistry(),loadWalletGraph()])); if (!currentAction()) return;
      return;
    }
    const kind = button.dataset.walletPage;
    const options = {kind, move:button.dataset.move};
    if (["networks","assets","addresses"].includes(kind)) await loadWalletRegistry(options);
    else if (kind==="balances") await loadWalletGraph(options);
    else if (["bounties","orders"].includes(kind)) await loadWalletEconomy(options);
    else await ({acquisitions:loadWalletAcquisitions,executions:loadWalletExecutions,audits:loadWalletAudits})[kind](options);
  } catch (error) { if (!currentAction()) return; if (epoch === walletListEpoch && csrfToken) setStatus("#wallet-read-status",`读取或操作未完成，请核对当前状态。${errorText(error)}`,true); }
  finally { if (!currentAction()) return; if (epoch === walletListEpoch) button.disabled = false; }
});
$("#wallet-policy-form").addEventListener("input",event=>{event.currentTarget.dataset.dirty="true";});
function clearWallet() {
  walletListEpoch++;walletListPages.clear();walletListVersions.clear();
  walletLoadVersion++;walletGraphLoadVersion++;walletBudgetLoadVersion++;walletQueueLoadVersion++;walletEconomyLoadVersion++;walletExecutionLoadVersion++;walletObservationLoadVersion++;walletPolicyLoadVersion++;walletAuditLoadVersion++;
  walletNetworks=[];walletAssets=[];walletAddresses=[];walletBalances=[];walletOrders=[];walletPolicy=null;walletPolicyDraftVersion=null;walletObservationHealth=null;walletAcquisitionRows=[];walletHistoryCursor=null;walletHistoryStarted=false;
  for(const id of ["wallet-graph-list","wallet-registry-networks","wallet-registry-assets","wallet-registry-addresses","wallet-history-list","wallet-acquisition-list","wallet-bounty-list","wallet-order-list","wallet-ledger-list","wallet-execution-list","wallet-audit-list","wallet-observation-health-list","wallet-metrics","wallet-policy-effective"])$("#"+id).replaceChildren();
  for(const id of ["wallet-policy-form","wallet-network-form","wallet-asset-form","wallet-address-form","wallet-enqueue-form"]){$("#"+id).reset();delete $("#"+id).dataset.dirty;}
  $("#wallet-history-more").hidden=false;
}
for(const kind of ["network","asset","address"]) $("#wallet-"+kind+"-form").addEventListener("submit",async event=>{
  const currentAction = beginAdminAction();
  event.preventDefault();const form=event.currentTarget;const button=form.querySelector("button[type=submit]");const payload=Object.fromEntries(new FormData(form));
  if(kind==="network") {const id=payload.chain_id;if(!/^[1-9]\d*$/.test(id) || BigInt(id)>BigInt(Number.MAX_SAFE_INTEGER)){setStatus("#wallet-registry-status","Chain ID 超过浏览器精确输入范围",true);return;}payload.chain_id=Number(id);payload.chain_family="evm";payload.rpc_url=payload.rpc_url.trim() || null;}
  if(kind==="asset") {payload.decimals=Number(payload.decimals);payload.contract_address=payload.contract_address.trim() || null;}
  if(kind==="address")payload.purpose="observation";
  button.disabled=true;
  try {await request(`/api/config/wallet-${kind==="address" ? "addresses" : kind+"s"}`,{method:"POST",body:JSON.stringify(payload)}); if (!currentAction()) return;form.reset();delete form.dataset.dirty;await walletRefreshAfterWrite("#wallet-registry-status","登记成功",()=>Promise.all([loadWalletGraph(),loadWalletRegistry()])); if (!currentAction()) return;}
  catch (error) { if (!currentAction()) return;setStatus("#wallet-registry-status",errorText(error),true);}finally { if (!currentAction()) return;button.disabled=false;}
});
$("#wallet-history-more").addEventListener("click",async()=>{
  const currentAction = beginAdminAction();
  const button=$("#wallet-history-more");button.disabled=true;
  try{const params=new URLSearchParams({limit:"25"});if(walletHistoryCursor)params.set("cursor",walletHistoryCursor);
    const page=await request(`/api/config/wallet-balance-history?${params}`); if (!currentAction()) return;
    if(!csrfToken)return;
    if(!walletHistoryStarted)$("#wallet-history-list").innerHTML="";
    walletHistoryStarted=true;walletHistoryCursor=page.next_cursor;
    $("#wallet-history-list").insertAdjacentHTML("beforeend",page.items.map(row=>`<article class="resource-item"><div><strong>${esc(walletAmount(row.balance,row.asset_id))}</strong><p>${esc(walletTargetText(row.asset_id,row.address_id))}</p><time>${esc(formatTimestamp(row.observed_at))}</time><details><summary>最小单位</summary><code>${esc(row.balance)}</code></details></div></article>`).join(""));
    button.hidden=!page.has_more;button.textContent="加载更早观测";setStatus("#wallet-history-status",page.items.length ? "已读取历史快照" : "暂无历史快照");
  }catch (error) { if (!currentAction()) return;setStatus("#wallet-history-status",errorText(error),true);}finally { if (!currentAction()) return;button.disabled=false;}
});
const publicPostStatusLabels = {
  pending_review: "待审核",
  published: "已发布",
  rejected: "已拒绝",
  archived: "已归档",
};
const publicPostProvenanceLabels = { visitor: "访客自填／未验证", subject: "主体", operator: "创建者", verified_channel: "已验证渠道" };

function publicPostActionButtons(row) {
  if (row.status === "pending_review") {
    return `<button class="primary" data-public-post-action="publish" type="button">发布</button><button data-public-post-action="reject" type="button">拒绝</button>`;
  }
  if (row.status === "published" || row.status === "rejected") {
    return '<button data-public-post-action="archive" type="button">归档</button>';
  }
  return "";
}

function renderPublicPosts() {
  const list = $("#public-post-review-list");
  if (!publicPostRows.length) {
    list.innerHTML = '<div class="muted">该状态暂无帖子</div>';
    return;
  }
  list.innerHTML = publicPostRows.map((row) => {
    const actions = publicPostActionButtons(row);
    const status = publicPostStatusLabels[row.status] || row.status || "未知";
    const provenance = publicPostProvenanceLabels[row.author_provenance] || "来源未知";
    return `<article class="message-item" data-public-post-card data-post-id="${esc(row.post_id)}" data-expected-status="${esc(row.status)}"><div class="card-heading"><div><h2>${esc(row.title)}</h2><time>${esc(row.created_at)} · <span class="status-chip">${esc(status)}</span></time></div><span class="muted">${esc(row.kind)} · ${esc(row.author_label)} · ${esc(provenance)}</span></div><p>${esc(row.content)}</p>${actions ? `<div class="model-grid"><label>审核理由<textarea data-public-post-reason maxlength="2000" required aria-required="true" placeholder="必填；说明发布、拒绝或归档原因"></textarea></label><div class="resource-actions">${actions}</div><p class="form-status" data-public-post-item-status role="status"></p></div>` : ""}</article>`;
  }).join("");
}

async function loadPublicPosts({ append = false } = {}) {
  const version = ++publicPostLoadVersion;
  const filter = $("#public-post-status-filter").value;
  if (!append) {
    publicPostCursor = "";
    publicPostRows = [];
    $("#public-post-review-list").innerHTML = '<div class="muted">正在读取审核队列…</div>';
  } else if (!publicPostCursor) {
    return;
  }
  const refreshButton = $("#refresh-public-posts");
  const loadMoreButton = $("#load-more-public-posts");
  const filterControl = $("#public-post-status-filter");
  refreshButton.disabled = true;
  loadMoreButton.disabled = true;
  filterControl.disabled = true;
  setStatus("#public-post-review-status", "正在加载");
  let path = "/api/admin/public-posts?limit=50";
  if (filter !== "all") path += `&status=${encodeURIComponent(filter)}`;
  if (append) path += `&cursor=${encodeURIComponent(publicPostCursor)}`;
  try {
    const batch = await request(path);
    if (version !== publicPostLoadVersion) return;
    if (!Array.isArray(batch)) throw new Error("审核队列响应无效");
    const known = new Set(append ? publicPostRows.map((row) => row.post_id) : []);
    const uniqueBatch = batch.filter((row) => {
      if (!row || typeof row.post_id !== "string" || known.has(row.post_id)) return false;
      known.add(row.post_id);
      return true;
    });
    publicPostRows = append ? [...publicPostRows, ...uniqueBatch] : uniqueBatch;
    const last = batch.at(-1);
    publicPostCursor = batch.length === 50 && typeof last?.created_at === "string" && typeof last?.post_id === "string"
      ? `${last.created_at}|${last.post_id}`
      : "";
    renderPublicPosts();
    loadMoreButton.hidden = !publicPostCursor;
    setStatus("#public-post-review-status", `已加载 ${publicPostRows.length} 条`);
  } catch (error) {
    if (version !== publicPostLoadVersion) return;
    if (!append) $("#public-post-review-list").innerHTML = '<div class="muted">审核队列加载失败，请重试</div>';
    setStatus("#public-post-review-status", errorText(error), true);
    throw error;
  } finally {
    if (version === publicPostLoadVersion) {
      refreshButton.disabled = false;
      loadMoreButton.disabled = false;
      filterControl.disabled = false;
    }
  }
}
let renderedModelQuery = null;
function restoreModelFilters() {
  if (!renderedModelQuery) return;
  $("#model-pool-filter").value = renderedModelQuery.pool;
  $("#model-status-filter").value = renderedModelQuery.status;
  $("#model-search").value = renderedModelQuery.search;
  modelPageOffset = renderedModelQuery.offset;
  document.querySelectorAll("[data-model-pool]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.modelPool === renderedModelQuery.pool)));
}

$("#retention-settings-form").addEventListener("input", (event) => { event.currentTarget.dataset.dirty = "1"; });
$("#retention-settings-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault(); const form = event.currentTarget; const button = submitButton(form,event); button.disabled = true;
  try {
    const settings = Object.fromEntries([...form.querySelectorAll("input[name]")].map((input) => [input.name,Number(input.value)]));
    await request("/api/admin/retention/settings", {method:"POST",body:JSON.stringify({settings,expected_revision:Number(form.dataset.revision)})}); if (!currentAction()) return;
    delete form.dataset.dirty;
    try { await loadRetention(); if (!currentAction()) return; setStatus("#retention-status","设置已保存，尚未执行清理"); }
    catch { if (!currentAction()) return; setStatus("#retention-status","设置已保存；读取失败，请刷新",true); }
  } catch (error) { if (!currentAction()) return; setStatus("#retention-status",error.status === 409 ? "设置已被修改，请刷新后核对版本" : errorText(error),true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
let usageLoadVersion = 0;
let usageResourcePage = 0;
let usageResourceVersion = 0;
async function loadUsageResources() {
  const version = ++usageResourceVersion;
  const query = new URLSearchParams({view:"usage", limit:"50", offset:String(usageResourcePage)});
  if ($("#usage-pool").value) query.set("pool", $("#usage-pool").value);
  try {
    const records = await request(`/api/config/model-resources?${query}`);
    if (version !== usageResourceVersion || !csrfToken) return;
    const selected = $("#usage-resource").value;
    $("#usage-resource").innerHTML = '<option value="">全部资源</option>' + records.map((record) => `<option value="${esc(record.group_id)}">${esc(record.label)} · ${esc(modelPoolLabels[record.pool])}</option>`).join("");
    if (records.some((record) => record.group_id === selected)) $("#usage-resource").value = selected;
    $("#usage-resource-page").textContent = `资源第 ${usageResourcePage / 50 + 1} 页`;
    $("#usage-resource-prev").disabled = usageResourcePage === 0;
    $("#usage-resource-next").disabled = records.length < 50;
  } catch (error) { if (version === usageResourceVersion && csrfToken) setStatus("#usage-status", errorText(error), true); }
}
async function loadUsage() {
  const generation = ++usageLoadVersion;
  const date = new Date();
  const clock = $("#usage-window").value === "today" ? new Date(date.getFullYear(), date.getMonth(), date.getDate()) : date;
  const query = new URLSearchParams({window: $("#usage-window").value, offset_minutes: String(-clock.getTimezoneOffset())});
  if ($("#usage-pool").value) query.set("pool", $("#usage-pool").value);
  if ($("#usage-resource").value) query.set("resource", $("#usage-resource").value);
  try {
    const payload = await request(`/api/admin/usage?${query}`);
    if (generation === usageLoadVersion && csrfToken) { renderModelObservability(payload); setStatus("#usage-status", ""); }
  } catch (error) {
    if (generation === usageLoadVersion) setStatus("#usage-status", error.code === "usage_query_limit" ? "历史查询超过读取限额，请缩小时间范围" : errorText(error), true);
  }
}
function confirmUnlimitedLimits(enabled) {
  return Object.values(enabled).some(Boolean) || window.confirm("四项资源级每日限制均已关闭。该资源不再受这些每日上限约束，仍可能产生供应商费用。确认保存吗？");
}
function readModelLimits(form, attribute) {
  const payload = { limit_enabled: {} };
  for (const [dimension, field] of Object.entries(limitFields)) {
    const enabled = form.querySelector(`[${attribute}="${dimension}"]`).checked;
    payload.limit_enabled[dimension] = enabled;
    if (!enabled) continue;
    const raw = form.elements[field].value;
    const value = field === "daily_cost_limit_usd" ? normalizeBudgetCost(raw) : normalizeBudgetInteger(field, raw);
    if (value === null) throw budgetError(field, `${parameterHelp[field][0]}已启用，请填写数值（零代表零额度）`);
    payload[field] = value;
  }
  return payload;
}
function validateCostProtection(payload) {
  const limits = payload.limit_enabled;
  const hasPrice = [payload.input_usd_per_million, payload.output_usd_per_million].some(value => value != null && !/^0*(?:\.0*)?$/.test(String(value)));
  if (limits.cost && !limits.attempts && !limits.input_tokens && !limits.output_tokens && payload.pricing_status !== "free" && !hasPrice) {
    throw new Error("只启用成本上限时必须填写供应商单价，或明确确认免费；价格未知时请同时启用次数或Token上限");
  }
}
function modelBudgetRemaining(maximum, accounted) {
  // The ledger aggregate already includes executing and unknown reservations.
  const difference = BigInt(maximum) - BigInt(accounted || "0");
  return difference > 0n ? difference.toString() : "0";
}
function modelDeletionMessage(item) {
  const pool = modelPoolLabels[item.pool] || "未知池";
  let warning = "";
  if (item.status === "active" && item.pool_summary?.active_resources === 1) {
    warning = "按最近一次刷新，这是此池最后一个启用资源，删除后可能无法继续认知。";
  } else if (item.status === "active" && item.available_key_count > 0 && item.pool_summary?.available_resources === 1) {
    warning = "按最近一次刷新，此池没有其他具有可用密钥的资源，删除后可能无法继续认知。";
  }
  return `删除“${item.label}” · ${pool} · 共 ${formatInteger(item.key_count)} 个密钥？\n所有密钥永久撤销，历史用量和必要审计保留；在途供应商请求无法撤回。\n${warning}`;
}
let modelKeyActive = 0;
const modelKeyWaiters = [];
function acquireModelKeySlot() {
  return new Promise((resolve) => {
    const start = () => {
      modelKeyActive++;
      resolve(() => { modelKeyActive--; modelKeyWaiters.shift()?.(); });
    };
    if (modelKeyActive < 4) start();
    else modelKeyWaiters.push(start);
  });
}
async function loadModelKeys(details) {
  const session = csrfToken;
  const item = renderedModels.get(details.dataset.id);
  if (!item) return;
  const current = () => details.isConnected && renderedModels.get(details.dataset.id) === item && session && session === csrfToken;
  if (!current() || details.dataset.loading || details.dataset.loaded) return;
  details.dataset.loading = "true";
  const content = details.querySelector("[data-model-key-content]");
  content.innerHTML = '<p class="muted" role="status">正在读取密钥状态…</p>';
  const release = await acquireModelKeySlot();
  try {
    if (!current()) return;
    const keys = await request(`/api/config/model-resources/${encodeURIComponent(item.group_id)}/keys`);
    if (!current()) return;
    if (!Array.isArray(keys)) throw new Error("密钥状态响应不完整");
    content.innerHTML = keys.length ? keys.map((key) => renderModelKey(item, key)).join("") : '<p class="muted">没有密钥记录</p>';
    if (keys.length < item.key_count) content.insertAdjacentHTML("beforeend", `<p class="muted">显示 ${keys.length} / ${esc(formatInteger(item.key_count))} 条密钥记录</p>`);
    details.dataset.loaded = "true";
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) await logout();
    else content.innerHTML = '<p class="form-status inline-error" role="status">密钥详情加载失败，已有资源状态不受影响。</p><button type="button" data-model-keys-retry>重试</button>';
  } finally { release(); delete details.dataset.loading; }
}
async function loadModels() {
  if ($("#model-list").querySelector("form[data-dirty]") && !window.confirm("资源有未保存的修改。放弃这些修改并刷新吗？")) { restoreModelFilters(); return; }
  const generation = ++modelLoadVersion;
  const selectedQuery = { pool: $("#model-pool-filter").value, status: $("#model-status-filter").value, search: $("#model-search").value.trim(), offset: modelPageOffset };
  const query = new URLSearchParams({ view: "management", limit: "50", offset: String(modelPageOffset), pool: $("#model-pool-filter").value, search: $("#model-search").value.trim() });
  if ($("#model-status-filter").value !== "all") query.set("status", $("#model-status-filter").value);
  const rows = await request(`/api/config/model-resources?${query}`);
  if (generation !== modelLoadVersion || !csrfToken) return;
  if (JSON.stringify(renderedModelQuery) !== JSON.stringify(selectedQuery)) $("#model-list").scrollTop = 0;
  renderedModelQuery = selectedQuery;
  renderedModels.clear();
  rows.forEach((item) => renderedModels.set(item.group_id, item));
  $("#model-list").innerHTML = rows.length ? rows.map((item) => {
    const revoked = item.status === "revoked";
    const status = modelResourceStatusLabels[item.status] || item.status || "未知";
    const pool = modelPoolLabels[item.pool] || item.pool || "未知池";
    const exactBudget = item.exact_budget || item;
    const limits = revoked ? "" : `<form class="model-limit-form" data-model-budget-form data-id="${esc(item.group_id)}">
      <div class="model-limit-heading"><strong>调度与每日限制</strong></div>
      <label for="budget-${esc(item.group_id)}-priority">调度优先级<input id="budget-${esc(item.group_id)}-priority" data-budget-field name="priority" type="text" inputmode="numeric" pattern="[0-9]*" min="0" max="1000" maxlength="4" value="${esc(exactBudget.priority)}" aria-describedby="budget-${esc(item.group_id)}-priority-error"><small class="field-hint">0 到 1000</small><small class="field-error" data-budget-error="priority" id="budget-${esc(item.group_id)}-priority-error" role="alert"></small></label>
      <label for="budget-${esc(item.group_id)}-weight">调度权重<input id="budget-${esc(item.group_id)}-weight" data-budget-field name="weight" type="text" inputmode="numeric" pattern="[0-9]*" min="1" max="1000" maxlength="4" value="${esc(exactBudget.weight)}" aria-describedby="budget-${esc(item.group_id)}-weight-error"><small class="field-hint">1 到 1000</small><small class="field-error" data-budget-error="weight" id="budget-${esc(item.group_id)}-weight-error" role="alert"></small></label>
      <label for="budget-${esc(item.group_id)}-daily-attempts">每日尝试上限<input id="budget-${esc(item.group_id)}-daily-attempts" data-budget-field name="daily_attempts" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="19" value="${esc(exactBudget.daily_attempts)}" aria-describedby="budget-${esc(item.group_id)}-daily-attempts-error"><small class="field-hint">非负整数；零为零额度</small><small class="field-error" data-budget-error="daily_attempts" id="budget-${esc(item.group_id)}-daily-attempts-error" role="alert"></small></label>
      <label for="budget-${esc(item.group_id)}-daily-input-tokens">每日输入 Token 上限<input id="budget-${esc(item.group_id)}-daily-input-tokens" data-budget-field name="daily_input_tokens" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="19" value="${esc(exactBudget.daily_input_tokens)}" aria-describedby="budget-${esc(item.group_id)}-daily-input-tokens-error"><small class="field-hint">非负整数；零为零额度</small><small class="field-error" data-budget-error="daily_input_tokens" id="budget-${esc(item.group_id)}-daily-input-tokens-error" role="alert"></small></label>
      <label for="budget-${esc(item.group_id)}-daily-output-tokens">每日输出 Token 上限<input id="budget-${esc(item.group_id)}-daily-output-tokens" data-budget-field name="daily_output_tokens" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="19" value="${esc(exactBudget.daily_output_tokens)}" aria-describedby="budget-${esc(item.group_id)}-daily-output-tokens-error"><small class="field-hint">非负整数；零为零额度</small><small class="field-error" data-budget-error="daily_output_tokens" id="budget-${esc(item.group_id)}-daily-output-tokens-error" role="alert"></small></label>
      <label for="budget-${esc(item.group_id)}-daily-cost">每日成本上限（USD）<input id="budget-${esc(item.group_id)}-daily-cost" data-budget-field name="daily_cost_limit_usd" type="text" inputmode="decimal" pattern="[0-9]+(?:\\.[0-9]{1,6})?" maxlength="26" value="${esc(formatMicroUsd(exactBudget.daily_cost_microusd))}" aria-describedby="budget-${esc(item.group_id)}-daily-cost-error"><small class="field-hint">非负数字，最多 6 位小数</small><small class="field-error" data-budget-error="daily_cost_limit_usd" id="budget-${esc(item.group_id)}-daily-cost-error" role="alert"></small></label>
      <button type="submit">保存限制</button>
    </form>`;
    return `<article class="resource-item model-resource-item" data-pool="${esc(item.pool)}" data-resource-id="${esc(item.group_id)}"><div class="model-resource-main"><div class="model-resource-title"><h3>${esc(item.label)}</h3><span class="status-chip status-${esc(item.status)}">${esc(status)}</span></div><p class="model-resource-identity">${esc(pool)} · ${esc(item.model)}</p><dl class="model-resource-facts"><div><dt>可用密钥</dt><dd>${esc(formatInteger(item.available_key_count))} / ${esc(formatInteger(item.key_count))}</dd></div><div><dt>最大尝试</dt><dd>${esc(formatInteger(exactBudget.max_attempts))}</dd></div><div><dt>优先级</dt><dd>${esc(formatInteger(exactBudget.priority))}</dd></div><div><dt>权重</dt><dd>${esc(formatInteger(exactBudget.weight))}</dd></div></dl><details class="model-keys" data-model-key-details data-id="${esc(item.group_id)}"><summary>密钥管理</summary><div data-model-key-content></div>${revoked ? "" : `<form data-add-model-keys data-id="${esc(item.group_id)}"><label>添加密钥<textarea name="api_keys" autocomplete="new-password" required maxlength="20000"></textarea></label><button type="submit">添加密钥</button></form>`}</details>${revoked ? "" : `<details><summary>额度与路由设置</summary>${limits}</details>`}</div><div class="resource-actions model-resource-actions">${item.status === "active" ? `<button class="primary" data-model-action="test" data-id="${esc(item.group_id)}" type="button">测试连接</button><button data-model-action="disable" data-id="${esc(item.group_id)}" type="button">停用</button>` : item.status === "disabled" ? `<button data-model-action="enable" data-id="${esc(item.group_id)}" type="button">启用</button>` : ""}${revoked ? "" : `<button data-model-action="revoke" data-id="${esc(item.group_id)}" type="button">删除资源</button>`}</div></article>`;
  }).join("") : '<div class="empty-state">尚未配置认知资源</div>';
  for (const item of rows) {
    const exactBudget = item.exact_budget || item;
    const card = Array.from($("#model-list").children).find((node) => node.dataset.resourceId === item.group_id);
    const form = card?.querySelector("[data-model-budget-form]");
    if (form) {
    for (const [dimension, field] of Object.entries(limitFields)) {
      const input = form.elements[field];
      const toggle = document.createElement("input");
      toggle.type = "checkbox"; toggle.dataset.limitDimension = dimension;
      toggle.checked = item.limit_enabled?.[dimension] !== false;
      toggle.setAttribute("aria-label", `启用${parameterHelp[field][0]}`);
      input.before(toggle); input.disabled = !toggle.checked;
      toggle.addEventListener("change", () => { input.disabled = !toggle.checked; });
    }
    for (const [field, value] of [["input_usd_per_million", formatMicroUsd(exactBudget.input_microusd_per_million)], ["output_usd_per_million", formatMicroUsd(exactBudget.output_microusd_per_million)], ["max_attempts", exactBudget.max_attempts]]) {
      form.querySelector("button[type=submit]").insertAdjacentHTML("beforebegin", `<label>${parameterHelp[field][0]}<input name="${field}" value="${esc(value)}" inputmode="${field === "max_attempts" ? "numeric" : "decimal"}" required></label>`);
    }
    form.dataset.updatedAt = item.updated_at;
    form.dataset.limitRevision = item.limit_revision;
    form.querySelector("button[type=submit]").insertAdjacentHTML("beforebegin", `<label>价格依据<select name="pricing_status"><option value="unknown">尚未确认价格</option><option value="priced">已填写单价（估算）</option><option value="free">确认免费</option></select></label>`);
    form.elements.pricing_status.value = item.pricing_status;
    }
    const usage = item.today_usage || {}; const reserved = item.reserved || {};
    const facts = card.querySelector(".model-resource-facts");
    for (const [dimension, field] of Object.entries(limitFields)) {
      const amount = dimension === "cost" ? usage.cost_microusd : usage[dimension];
      const reserve = dimension === "cost" ? reserved.cost_microusd : reserved[dimension];
      const maximum = dimension === "cost" ? exactBudget.daily_cost_microusd : exactBudget[field];
      const displayAmount = (value) => dimension === "cost" ? `$${formatMicroUsd(value || "0")}` : formatInteger(value);
      const remaining = item.limit_enabled?.[dimension] === false ? "未设资源级上限" : displayAmount(modelBudgetRemaining(maximum, amount));
      facts.insertAdjacentHTML("beforeend", `<div><dt>${parameterHelp[field][0]}</dt><dd>已计（含预留）${esc(displayAmount(amount))}<br>其中预留 ${esc(displayAmount(reserve))}<br>剩余 ${esc(remaining)}</dd></div>`);
    }
    facts.insertAdjacentHTML("beforeend", `<div><dt>价格依据</dt><dd>${esc(({unknown:"价格未知",priced:"填写单价估算",free:"已确认免费"})[item.pricing_status] || "价格未知")}</dd></div>`);
    if (!item.cost_limit_effective && item.limit_enabled?.cost) facts.insertAdjacentHTML("beforeend", `<div><dt>成本保护</dt><dd>${item.cost_only_blocked ? "调用已阻止：仅启用成本上限但价格未知" : "实际费用无法限制；仅次数/Token上限提供准入保护"}</dd></div>`);
    card.querySelector(".model-resource-identity").insertAdjacentHTML("afterend", `<small>UTC 重置 ${esc(formatTimestamp(item.reset_at_utc))}${item.cleanup_pending ? ` · 密钥清理待完成 ${item.cleanup_pending}` : ""}</small>`);
  }
  const cards = Array.from($("#model-list").querySelectorAll("[data-pool]"));
  $("#model-list").replaceChildren();
  for (const pool of [selectedQuery.pool]) {
    const section = document.createElement("section"); section.className = "model-pool-section";
    section.innerHTML = `<h3>${modelPoolLabels[pool]} · ${cards.filter((card) => card.dataset.pool === pool).length}</h3>`;
    section.append(...cards.filter((card) => card.dataset.pool === pool));
    if (!section.querySelector("article")) section.insertAdjacentHTML("beforeend", '<p class="muted">此筛选下暂无资源</p>');
    $("#model-list").append(section);
  }
  addParameterHelp($("#model-list"));
  $("#model-page-label").textContent = `第 ${Math.floor(modelPageOffset / 50) + 1} 页 · ${rows.length} 条 · 最多 50 条/页`;
  $("#model-page-prev").disabled = modelPageOffset === 0;
  $("#model-page-next").disabled = rows.length < 50;
}
function draftModelPayload(includeModel = true) {
  const keys = $("#admin-model-keys").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const payload = { base_url: $("#admin-model-url").value.trim(), api_keys: keys };
  if (includeModel) payload.model = $("#admin-model-name").value.trim();
  const reasoning = $("#admin-model-reasoning")?.value;
  if (includeModel && reasoning) payload.reasoning_effort = reasoning;
  return payload;
}
async function loadSearchProviders() {
  const current = beginAdminRead("search");
  const [providers, routing] = await Promise.all([adminRead(current, "/api/config/search-providers"), adminRead(current, "/api/config/search-routing")]);
  if (!current()) return;
  if (!$("#admin-search-routing-form").dataset.dirty) $("#search-routing-mode").value = routing.mode || "auto";
  if ($("#search-provider-list").querySelector("form[data-dirty]")) return;
  const statusLabels = { active: "可用", revoked: "已撤销" };
  $("#search-provider-list").innerHTML = providers.length ? providers.map((item) => {
    const enabled = item.status === "active" && item.enabled !== false;
    const controls = item.status === "active" ? `<button data-search-provider-action="${enabled ? "disable" : "enable"}" data-id="${esc(item.config_id)}" type="button">${enabled ? "停用" : "启用"}</button><button data-search-provider-action="revoke" data-id="${esc(item.config_id)}" type="button">撤销</button>` : "";
    const routing = item.status === "active" ? `<form class="provider-routing-form" data-search-routing-form data-id="${esc(item.config_id)}" aria-label="设置 ${esc(item.label)} 的搜索优先级"><label>优先级<input name="priority" type="number" min="0" max="1000" step="1" value="${esc(item.priority ?? 100)}" required></label><label>权重<input name="weight" type="number" min="1" max="1000" step="1" value="${esc(item.weight ?? 1)}" required></label><label class="provider-routing-reason">调整原因<input name="reason" type="text" maxlength="500" value="管理员调整搜索供应商优先级" required></label><button type="submit">保存优先级</button><span data-routing-status role="status"></span></form>` : "";
    return `<article class="resource-item"><div><h3>${esc(item.label)} · ${esc(item.provider_type)}</h3><p>密钥指纹：${esc(item.key_fingerprint)} · 状态：${esc(item.status === "active" ? (enabled ? "可用" : "已停用") : statusLabels[item.status] || "未知")} · 优先级 ${esc(formatInteger(item.priority ?? 100))} · 权重 ${esc(formatInteger(item.weight ?? 1))}</p></div><div class="resource-actions">${controls}</div>${routing}</article>`;
  }).join("") : '<div class="empty-state">尚未配置搜索 API</div>';
}
async function loadCapabilities() {
  const current = beginAdminRead("capabilities");
  const rows = await adminRead(current, "/api/config/capabilities");
  if (!current()) return;
  $("#capability-list").innerHTML = rows.length ? rows.map((item) => { const scope = item.scope?.public_https ? "所有经过安全边界的公网 HTTPS 来源" : JSON.stringify(item.scope || {}); const canRevoke = item.status === "active"; return `<article class="resource-item"><div><h3>${esc(item.capability_type)} · ${esc(item.effective_status || item.status)}</h3><p>${esc(scope)}</p><small>签发者：${esc(item.issuer)} · 每小时：${esc(item.rate_limit_per_hour)} · ${esc(item.expires_at || "长期")}</small></div>${canRevoke ? `<button data-capability-action="revoke" data-id="${esc(item.grant_id)}" type="button">撤销</button>` : ""}</article>`; }).join("") : '<div class="muted">暂无能力授权</div>';
}
async function loadChannels() {
  const current = beginAdminRead("channels");
  const [transports, bindings] = await Promise.all([adminRead(current, "/api/config/transports"), adminRead(current, "/api/config/inbound-bindings")]);
  if (!current()) return;
  const transportMap = new Map(transports.map((item) => [item.transport_id, item]));
  $("#admin-binding-transport").innerHTML = transports.filter((item) => item.status === "active").map((item) => `<option value="${esc(item.transport_id)}">${esc(item.channel)} · ${esc(item.label)}</option>`).join("") || '<option value="">先添加渠道</option>';
  const selected = $("#qq-identity-transport").value;
  $("#qq-identity-transport").innerHTML = transports.filter(item => item.channel === "qq" && item.status === "active").map(item => `<option value="${esc(item.transport_id)}">${esc(item.label)}</option>`).join("") || '<option value="">尚无启用的 QQ 机器人</option>';
  if ([...$("#qq-identity-transport").options].some(option => option.value === selected)) $("#qq-identity-transport").value = selected;
  $("#channel-list").innerHTML = transports.length ? transports.map((item) => `<article class="resource-item"><div><h3>${esc(item.label)}</h3><p>${esc(item.channel)} · ${esc(item.status)}</p><small>${esc(item.endpoint)}</small></div><div class="resource-actions">${item.status === "active" ? `<button data-channel-action="disable" data-id="${esc(item.transport_id)}" type="button">停用</button>` : item.status === "disabled" ? `<button data-channel-action="enable" data-id="${esc(item.transport_id)}" type="button">启用</button>` : ""}</div></article>`).join("") : '<div class="muted">尚未配置通讯渠道</div>';
  channelBindings = bindings;
  $("#binding-list").innerHTML = bindings.length ? bindings.map((item) => `<article class="resource-item"><div><h3>${esc(item.label || item.external_sender_id)}</h3><p>${esc(item.channel)} · ${{creator:"创建者",participant:"参与者"}[item.role] || "未知角色"} · ${{active:"已准入",disabled:"已停用",revoked:"已解绑"}[item.status] || "未知状态"}</p><p>${item.identity_verified ? "身份已验证" : "手动准入"} · ${item.share_web_history ? "网页历史已关联" : "网页历史未共享"} · ${item.push_web_replies ? "新网页回复推送已开启" : "网页回复推送关闭"}</p><details><summary>平台标识</summary><small>${esc(item.external_account_id)} / ${esc(item.external_sender_id)}</small></details></div><div class="resource-actions">${item.status === "active" ? `<button data-binding-action="disable" data-id="${esc(item.binding_id)}" type="button">停用</button>${item.identity_verified ? `<button data-binding-route="${esc(item.binding_id)}" type="button">设置聊天关联</button>` : ""}${item.share_web_history ? `<button data-binding-action="unlink" data-id="${esc(item.binding_id)}" type="button">取消历史共享</button>` : ""}` : item.status === "disabled" ? `<button data-binding-action="enable" data-id="${esc(item.binding_id)}" type="button">启用</button>` : ""}${item.status !== "revoked" ? `<button data-binding-action="revoke" data-id="${esc(item.binding_id)}" type="button">解绑账号</button>` : ""}</div></article>`).join("") : '<div class="muted">暂无允许聊天的账号</div>';
  void transportMap;
}
let qqIdentity = null;
let channelBindings = [];
function clearQQIdentity() {
  qqIdentity = null;
  $("#qq-identity-summary").replaceChildren();
  $("#qq-identity-challenge").hidden = true;
  $("#qq-chat-route-form").hidden = true;
  $("#qq-share-history").checked = false;
  $("#qq-push-web-replies").checked = false;
  $("#qq-identity-status").textContent="";
}
async function checkQQIdentity() {
  if (!qqIdentity) return;
  const challenge = qqIdentity;
  const state = await request(`/api/config/identity-challenges/${encodeURIComponent(challenge.challenge_id)}`);
  if (qqIdentity !== challenge || !csrfToken) return;
  const labels = {pending:"等待 QQ 私聊验证码",observed:"已收到验证，请核对账号",confirmed:"已确认身份",expired:"验证码已过期",revoked:"验证已取消"};
  $("#qq-identity-confirm").disabled = state.status !== "observed";
  setStatus("#qq-identity-status", `${labels[state.status] || "未知状态"}${state.external_sender_id ? " · 账号 " + state.external_sender_id : ""}`);
  if (state.binding_id) qqIdentity.binding_id = state.binding_id;
}
async function loadRuntime() {
  const current = beginAdminRead("runtime");
  const results = await Promise.allSettled([request("/api/diagnostics"), request("/api/admin/public-post-controls")]);
  if (!current()) return;
  const unauthorized = results.find(result => result.status === "rejected" && result.reason?.status === 401);
  if (unauthorized) throw unauthorized.reason;
  if (results[0].status === "fulfilled") {
    const diagnostics = results[0].value; const cognition = diagnostics.cognition || {};
    $("#runtime-summary").innerHTML = summaryRows([["认知状态", cognition.enabled ? "已启用" : "未启用"], ["待处理消息", cognition.pending_interactions?.length || 0], ["认知等待任务", cognition.waiting_interaction_tasks?.length || 0], ["入站去重", JSON.stringify(diagnostics.inbound || {})], ["未知模型调用", diagnostics.unknown?.model_calls || 0], ["外部投递", JSON.stringify(diagnostics.deliveries || {})], ["完整性", diagnostics.integrity?.status || "未配置"]]).replaceAll("summary-row", "runtime-item");
  }
  if (results[1].status === "rejected") { setStatus("#public-post-controls-status", errorText(results[1].reason), true); return; }
  const controls = results[1].value; const form = $("#public-post-controls-form");
  if (!form.dataset.dirty) {
    for (const [id,field] of [["rate-limit","rate_limit_per_hour"],["queue-cap","queue_cap"],["captcha-ttl","captcha_ttl_seconds"],["captcha-attempts","captcha_max_attempts"],["captcha-mode","captcha_mode"],["captcha-issue-limit","captcha_issue_limit_per_hour"],["captcha-global-rate","captcha_global_rate_per_minute"],["max-chars","max_content_chars"]]) $("#public-post-" + id).value = controls[field];
    $("#public-post-storage-unit").value = "1"; $("#public-post-storage-cap").value = controls.storage_cap_bytes;
    form.dataset.lengthRevision = controls.length_revision;
  }
  const usage = controls.usage || {};
  $("#public-post-usage").innerHTML = summaryRows([["待审核", usage.pending_count ?? 0], ["帖子总数", usage.post_count ?? 0], ["帖子内容", `${usage.byte_size ?? 0} / ${usage.storage_cap_bytes ?? controls.storage_cap_bytes} 字节`], ["正文上限", `${controls.max_content_chars} 字符`]]);
}
async function loadAll() {
  if(!csrfToken)return;
  const current = beginAdminRead("all");
  const section=activeSection;
  const tasks = {overview:[["总览",loadOverview]],logs:[["活动日志",loadRuntimeLogs]],conversation:[["私密交流",loadMailbox]],"public-posts":[["内容审核",loadPublicPosts]],publications:[["公开摘要",loadPublicPublications]],models:[["认知资源",loadModels]],search:[["搜索配置",loadSearchProviders]],capabilities:[["能力授权",loadCapabilities]],channels:[["通讯渠道",loadChannels]],wallet:[["钱包与付款",loadWallet]],migration:[["迁移与庇护所",loadMigration]],runtime:[["运行防护",loadRuntime]]}[section] || [];
  if(section==="overview")for(const [id,label,load] of [["overview-setup","配置检查",loadSetupGuide],["overview-version","本地版本",loadUpgradePanel],["overview-usage","用量",async()=>{await Promise.all([loadUsage(),loadUsageResources()]);}],["overview-providers","供应商健康",loadProviderHealth],["overview-retention","数据保留",loadRetention]])if($("#"+id).open)tasks.push([label,load]);
  const results = await Promise.allSettled(tasks.map(([, load]) => load()));
  if(section!==activeSection || !current())return;
  const failures = [];
  results.forEach((result, index) => {
    if (result.status === "rejected") failures.push({ label: tasks[index][0], error: result.reason });
    else if (Array.isArray(result.value)) failures.push(...result.value);
  });
  if (failures.some(({ error }) => error?.status === 401)) {
    setStatus("#global-status", "登录会话已失效，请重新登录", true);
    await logout();
    return;
  }
  if (failures.length) {
    const unique = [...new Set(failures.map(({ label }) => label))];
    setStatus("#global-status", `${unique.join("、")}暂时无法读取；其他内容仍可使用。${errorText(failures[0].error)}`, true);
  } else {
    setStatus("#global-status", "");
  }
}
for(const [id,load] of [["overview-setup",loadSetupGuide],["overview-version",loadUpgradePanel],["overview-usage",async()=>{await Promise.all([loadUsage(),loadUsageResources()]);}],["overview-providers",loadProviderHealth],["overview-retention",loadRetention]])$("#"+id).addEventListener("toggle",()=>{if($("#"+id).open && csrfToken)void load().catch(error=>setStatus("#global-status",errorText(error),true));});
async function restoreSession() { const session = adminSessionGeneration; try { const result = await request("/admin/session"); if (!result.authenticated || session !== adminSessionGeneration) return; adminSessionGeneration++; csrfToken = result.csrf_token; $("#login-shell").hidden = true; $("#admin-shell").hidden = false; await loadAll(); } catch (error) { if (session === adminSessionGeneration) setStatus("#login-status", errorText(error), true); } }

$("#refresh-runtime-logs").addEventListener("click", () => { runtimeLogCursor = null; void loadRuntimeLogs(); });
$("#runtime-log-more").addEventListener("click", () => { void loadRuntimeLogs({ append: true }); });
for (const selector of ["#runtime-log-category", "#runtime-log-status", "#runtime-log-since", "#runtime-log-until"]) $(selector).addEventListener("change", () => { runtimeLogCursor = null; void loadRuntimeLogs(); });
$("#runtime-log-auto").addEventListener("change", () => { clearTimeout(runtimeLogTimer); scheduleRuntimeLogs(); });
$("#logout").addEventListener("click", stopRuntimeLogPolling);
document.addEventListener("visibilitychange", () => { if (document.hidden) stopRuntimeLogPolling(); else if (activeSection === "logs") { runtimeLogCursor = null; void loadRuntimeLogs(); } });

$("#login-form").addEventListener("submit", login); $("#logout").addEventListener("click", logout); $("#refresh-admin").addEventListener("click", loadAll); $("#check-upgrade-version").addEventListener("click", checkUpgradeVersion); $("#start-upgrade").addEventListener("click", requestUpgradeConfirmation); $("#confirm-upgrade").addEventListener("click", confirmUpgrade); $("#refresh-observability").addEventListener("click", () => { void loadOverview().catch((error) => setStatus("#global-status", errorText(error), true)); }); $("#refresh-provider-health").addEventListener("click", () => { void loadProviderHealth().catch((error) => setStatus("#provider-health-status", errorText(error), true)); }); $("#refresh-retention").addEventListener("click", () => { void loadRetention().catch((error) => setStatus("#retention-status", errorText(error), true)); }); $("#run-retention").addEventListener("click", () => { void runRetention(); }); $("#refresh-mailbox").addEventListener("click", loadMailbox); $("#refresh-models").addEventListener("click", loadModels); $("#refresh-search-providers").addEventListener("click", loadSearchProviders); $("#refresh-capabilities").addEventListener("click", loadCapabilities); $("#refresh-runtime").addEventListener("click", loadRuntime); document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showSection(button.dataset.section)));
$("[data-section-panel=overview]").addEventListener("click", (event) => { const button = event.target.closest("[data-setup-go]"); if (button) showSection(button.dataset.setupGo); });
$("#refresh-wallet-audits").addEventListener("click", () => { void loadWalletAudits().catch(() => {}); });
$("#refresh-migration").addEventListener("click", () => {
  const form = $("#migration-policy-form");
  if (form.dataset.dirty && !window.confirm("重新读取将丢弃尚未保存的迁移策略草稿。继续吗？")) return;
  delete form.dataset.dirty;
  void loadMigration().catch(error => setStatus("#migration-status", errorText(error), true));
});
$("#refresh-migration-tasks").addEventListener("click", () => { void loadMigration().catch((error) => setStatus("#migration-status", errorText(error), true)); });
$("#migration-target-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  try {
    await request("/api/v1/admin/migration/targets", { method: "POST", body: JSON.stringify({
      target_id: $("#migration-target-id").value.trim(), endpoint: $("#migration-target-endpoint").value.trim(), public_key: $("#migration-target-key").value.trim(), recipient_public_key: $("#migration-target-recipient").value.trim(), channel_token: $("#migration-target-token").value.trim(), release_sha: $("#migration-target-release").value.trim(), os_arch: $("#migration-target-arch").value.trim(), region: $("#migration-target-region").value.trim() || null, provider: $("#migration-target-provider").value.trim() || null, capabilities: {}, encrypted_volume: $("#migration-target-encrypted").checked,
    }) }); if (!currentAction()) return;
    event.currentTarget.reset(); $("#migration-target-arch").value = "linux-amd64"; setStatus("#migration-target-status", "迁移目标已注册"); await loadMigration(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return; setStatus("#migration-target-status", errorText(error), true); }
});
$("#migration-target-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const challengeButton = event.target.closest("[data-migration-challenge]");
  const revokeButton = event.target.closest("[data-migration-revoke]");
  try {
    if (challengeButton) {
      const targetId = challengeButton.dataset.migrationChallenge;
      await request(`/api/v1/admin/migration/targets/${encodeURIComponent(targetId)}/challenge`, { method: "POST", body: JSON.stringify({ automatic: true }) }); if (!currentAction()) return;
      setStatus("#migration-target-status", "目标验证成功"); await loadMigration(); if (!currentAction()) return;
    } else if (revokeButton) {
      const targetId = revokeButton.dataset.migrationRevoke;
      if (!window.confirm(`确定撤销目标 ${targetId} 吗？`)) return;
      await request(`/api/v1/admin/migration/targets/${encodeURIComponent(targetId)}/revoke`, { method: "POST", body: JSON.stringify({ reason: "管理员在管理台撤销" }) }); if (!currentAction()) return;
      setStatus("#migration-target-status", "目标已撤销"); await loadMigration(); if (!currentAction()) return;
    }
  } catch (error) { if (!currentAction()) return; setStatus("#migration-target-status", errorText(error), true); }
});
$("#migration-proposal-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const approve = event.target.closest("[data-migration-approve]");
  const reject = event.target.closest("[data-migration-reject]");
  try {
    if (approve) {
      const result = await request(`/api/v1/admin/migration/proposals/${encodeURIComponent(approve.dataset.migrationApprove)}/approve`, { method: "POST", body: JSON.stringify({ idempotency_key: `admin-approve-${approve.dataset.migrationApprove}` }) }); if (!currentAction()) return;
      setStatus("#migration-status", result.approval_status === "awaiting_second_approval" ? `已记录复核 ${result.approval_count}/${result.required_approvals}；需要另一种服务端登录身份继续` : "迁移提案已批准"); await loadMigration(); if (!currentAction()) return;
    } else if (reject) {
      const reason = window.prompt("请输入拒绝原因：");
      if (!reason?.trim()) return;
      await request(`/api/v1/admin/migration/proposals/${encodeURIComponent(reject.dataset.migrationReject)}/reject`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }); if (!currentAction()) return;
      setStatus("#migration-status", "迁移提案已拒绝"); await loadMigration(); if (!currentAction()) return;
    }
  } catch (error) { if (!currentAction()) return; setStatus("#migration-status", errorText(error), true); }
});
$("#migration-task-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const cutover = event.target.closest("[data-migration-cutover]");
  const attestation = event.target.closest("[data-migration-attestation]");
  const rollback = event.target.closest("[data-migration-rollback]");
  const cancel = event.target.closest("[data-migration-cancel]");
  try {
    if (attestation) {
      const report = await request(`/api/v1/admin/migration/tasks/${encodeURIComponent(attestation.dataset.migrationAttestation)}/self-attestation`); if (!currentAction()) return;
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = url; link.download = `noyra-migration-self-attestation-${attestation.dataset.migrationAttestation}.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      setStatus("#migration-status", "自核验报告已导出");
    } else if (cutover) {
      const policy = await request("/api/v1/admin/migration/policy"); if (!currentAction()) return;
      if (policy.wallet_mode === "local_wallet_transfer") {
        if (!window.confirm("本次迁移将通过接收方加密包转移本地钱包及解锁文件。目标必须是您信任的设备。确认授权本次任务吗？")) return;
        await request(`/api/v1/admin/migration/tasks/${encodeURIComponent(cutover.dataset.migrationCutover)}/wallet-approval`, { method: "POST", body: JSON.stringify({ authorize_local_wallet: true }) }); if (!currentAction()) return;
      }
      await request(`/api/v1/admin/migration/tasks/${encodeURIComponent(cutover.dataset.migrationCutover)}/cutover`, { method: "POST", body: JSON.stringify({}) }); if (!currentAction()) return;
      setStatus("#migration-status", "迁移执行已启动；传输和验证在服务器后台继续，请更新任务状态");
      await loadMigration(); if (!currentAction()) return;
    } else if (cancel) {
      const reason = window.prompt("请输入取消迁移的原因：");
      if (!reason?.trim()) return;
      await request(`/api/v1/admin/migration/tasks/${encodeURIComponent(cancel.dataset.migrationCancel)}/cancel`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }); if (!currentAction()) return;
      setStatus("#migration-status", "迁移任务已取消"); await loadMigration(); if (!currentAction()) return;
    } else if (rollback) {
      const reason = window.prompt("请输入回滚原因：");
      if (!reason?.trim()) return;
      await request(`/api/v1/admin/migration/tasks/${encodeURIComponent(rollback.dataset.migrationRollback)}/rollback`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }); if (!currentAction()) return;
      setStatus("#migration-status", "迁移任务已回滚"); await loadMigration(); if (!currentAction()) return;
    }
  } catch (error) { if (!currentAction()) return; setStatus("#migration-status", errorText(error), true); }
});
$("#migration-recovery-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  try {
    await request("/api/v1/admin/migration/recovery", { method: "POST", body: JSON.stringify({
      task_id: $("#migration-recovery-task-id").value.trim(),
      standby_target_id: $("#migration-recovery-standby-target-id").value.trim(),
      verified_backup_id: $("#migration-recovery-backup-id").value.trim(),
      manifest_digest: $("#migration-recovery-manifest-digest").value.trim(),
      restore_report_digest: $("#migration-recovery-restore-report-digest").value.trim(),
      health_report_digest: $("#migration-recovery-health-report-digest").value.trim(),
      target_signature: $("#migration-recovery-target-signature").value.trim(),
      source_failure_evidence: $("#migration-recovery-source-failure-evidence").value.trim(),
    }) }); if (!currentAction()) return;
    setStatus("#migration-recovery-status", "紧急恢复已提交，服务器正在准备验证任务");
    await loadMigration(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#migration-recovery-status", errorText(error), true);
  } finally { if (!currentAction()) return;
    if (button) button.disabled = false;
  }
});
$("#migration-policy-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const controls = [...form.elements];
  const disabled = controls.map(input => input.disabled);
  const revision = form.dataset.revision;
  if (!revision || !/^[0-9]+$/.test(revision) || !Number.isSafeInteger(Number(revision))) {
    setStatus("#migration-status", "尚未读取编辑版本，请重新读取策略后再保存", true); return;
  }
  const draft = {
    expected_revision: Number(revision), enabled: $("#migration-enabled").checked,
    approval_mode: $("#migration-approval-mode").value,
    rejection_cooldown_seconds: Number($("#migration-cooldown").value) * 86400,
    allowed_target_ids: $("#migration-allowed-targets").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    allowed_regions: $("#migration-allowed-regions").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    max_cost_microusd: Number($("#migration-max-cost").value), min_free_bytes: Number($("#migration-min-free").value),
    trust_level: Number($("#migration-trust").value), max_downtime_seconds: Number($("#migration-downtime").value),
    wallet_mode: $("#migration-wallet-mode").value, local_wallet_transfer_enabled: $("#migration-local-wallet").checked,
  };
  controls.forEach(input => { input.disabled = true; });
  try {
    const current = await request("/api/v1/admin/migration/policy"); if (!currentAction()) return;
    if (current.revision !== draft.expected_revision) {
      setStatus("#migration-status", "策略已被其他窗口修改，草稿已保留。请比较当前策略并重新读取后再保存", true); return;
    }
    const approvalMode = draft.approval_mode;
    const walletMode = draft.wallet_mode;
    const localWalletEnabled = draft.local_wallet_transfer_enabled;
    const enteringPolicyAuto = approvalMode === "policy_auto" && (current.approval_mode !== "policy_auto" || !current.enabled);
    const enteringLocalWallet = walletMode === "local_wallet_transfer" && localWalletEnabled && (current.wallet_mode !== "local_wallet_transfer" || !current.local_wallet_transfer_enabled);
    const confirmPolicyAuto = !enteringPolicyAuto || window.confirm("策略自动模式会允许无需逐次人工批准的迁移，必须确认目标、资源和回滚边界均已验证。继续吗？");
    if (!confirmPolicyAuto) return;
    const confirmLocalWallet = !enteringLocalWallet || window.confirm("本地钱包迁移会在受保护通道中转移密钥，必须确认已准备二次批准和回滚方案。继续吗？");
    if (!confirmLocalWallet) return;
    await request("/api/v1/admin/migration/policy", { method: "PUT", body: JSON.stringify({
      ...draft,
      ...(enteringPolicyAuto ? { confirm_policy_auto: true } : {}),
      ...(enteringLocalWallet ? { confirm_local_wallet_transfer: true } : {}),
    }) }); if (!currentAction()) return;
    setStatus("#migration-status", "迁移策略已保存");
    delete $("#migration-policy-form").dataset.dirty;
    await loadMigration(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return; setStatus("#migration-status", error.status === 409 ? "策略版本冲突，草稿已保留。请核对当前策略后重新读取" : errorText(error), true); }
  finally { if (currentAction()) controls.forEach((input, index) => { input.disabled = disabled[index]; }); }
});
$("#admin-message-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault(); if(event.isComposing || !chat.current || chat.current.status!=="active") return;
  const button=submitButton(event.currentTarget,event); button.disabled=true;
  const windowId=chat.current.conversation_id; const content=$("#admin-message").value.trim();
  const keyId=windowId+":"+content; const key=chat.keys.get(keyId)||crypto.randomUUID(); chat.keys.set(keyId,key);
  try {
    await request("/api/interactions", {method:"POST",body:JSON.stringify({channel:"web",counterparty:"web-user",content,idempotency_key:key,conversation_id:windowId})}); if (!currentAction()) return;
    chat.keys.delete(keyId);
    if(chat.drafts.get(windowId)?.trim()===content) chat.drafts.delete(windowId);
    if(chat.current?.conversation_id===windowId && $("#admin-message").value.trim()===content) $("#admin-message").value="";
    setStatus("#message-status","消息已记录，等待处理");
    try {await loadChatMessages(); if (!currentAction()) return;} catch { if (!currentAction()) return;setStatus("#message-status","消息已记录；读取失败，请刷新",true);}
  } catch (error) { if (!currentAction()) return;setStatus("#message-status",errorText(error),true);}
  finally { if (!currentAction()) return;button.disabled=false;}
});

$("#wallet-enqueue-asset").addEventListener("change", updateWalletAddressOptions);
$("#wallet-budget-network").addEventListener("change", () => { void loadWalletBudget().catch(() => {}); });
$("#refresh-wallet-graph").addEventListener("click", () => { void loadWalletGraph().catch((error) => setStatus("#global-status", errorText(error), true)); });
$("#refresh-wallet-budget").addEventListener("click", () => { void loadWalletBudget().catch(() => {}); });
$("#wallet-acquisition-status-filter").addEventListener("change", () => { void loadWalletAcquisitions().catch(() => {}); });
$("#refresh-wallet-acquisitions").addEventListener("click", () => { void loadWalletAcquisitions().catch(() => {}); });
$("#wallet-observation-group").addEventListener("change", () => { void loadWalletObservationHealth().catch(() => {}); });
$("#refresh-wallet-observation-health").addEventListener("click", () => { void loadWalletObservationHealth().catch(() => {}); });
$("#refresh-wallet-policy").addEventListener("click", () => { void loadWalletPolicy().catch((error) => setStatus("#wallet-policy-status", errorText(error), true)); });
$("#refresh-wallet-economy").addEventListener("click", () => { void loadWalletEconomy().catch((error) => setStatus("#global-status", errorText(error), true)); });
$("#run-wallet-acquisitions").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.currentTarget;
  const limit = Number($("#wallet-run-limit").value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 32) {
    setStatus("#wallet-acquisition-status", "处理上限必须是 1 到 32 的整数", true);
    return;
  }
  button.disabled = true;
  if(!confirm(`处理最多 ${limit} 个到期采集；会向链上读取并消耗 RPC 预算。继续？`)){button.disabled=false;return;}
  setStatus("#wallet-acquisition-status", "正在处理到期读取");
  try {
    const result = await request("/api/admin/wallet-acquisitions/run", { method: "POST", body: JSON.stringify({ limit }) }); if (!currentAction()) return;
    await walletRefreshAfterWrite("#wallet-acquisition-status","本次处理 " + (result?.processed ?? 0) + " 条",loadWalletAcquisitions); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#wallet-acquisition-status", errorText(error), true);
    if (error.status === 401) await logout();
  } finally { if (!currentAction()) return;
    button.disabled = false;
  }
});
$("#wallet-enqueue-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  const assetId = $("#wallet-enqueue-asset").value;
  const addressId = $("#wallet-enqueue-address").value;
  const maxAttempts = Number($("#wallet-enqueue-attempts").value);
  const idempotencyKey = $("#wallet-enqueue-idempotency").value.trim();
  if (!assetId || !addressId || !Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 5) {
    setStatus("#wallet-acquisition-form-status", "请选择有效的资产、地址和最大尝试次数", true);
    return;
  }
  if (button) button.disabled = true;
  try {
    const payload = { asset_id: assetId, address_id: addressId, max_attempts: maxAttempts };
    if (idempotencyKey) payload.idempotency_key = idempotencyKey;
    const result = await request("/api/admin/wallet-acquisitions", { method: "POST", body: JSON.stringify(payload) }); if (!currentAction()) return;
    form.reset();
    $("#wallet-enqueue-attempts").value = "5";
    renderWalletEnqueueOptions();
    setStatus("#wallet-acquisition-form-status", result?.status === "queued" ? "采集已加入队列" : "采集队列已更新");
    try { await loadWalletAcquisitions(); if (!currentAction()) return; } catch (error) { if (!currentAction()) return; setStatus("#wallet-acquisition-form-status",`采集请求已处理；刷新失败，请核对队列。${errorText(error)}`,true); }
  } catch (error) { if (!currentAction()) return;
    setStatus("#wallet-acquisition-form-status", errorText(error), true);
    if (error.status === 401) await logout();
  } finally { if (!currentAction()) return;
    if (button) button.disabled = false;
  }
});
$("#wallet-acquisition-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-wallet-acquisition-action]");
  if (!button) return;
  const card = button.closest("[data-run-id]");
  const runId = card?.dataset.runId;
  const operation = button.dataset.walletAcquisitionAction;
  if (!card || !runId || !operation) return;
  if (operation === "attempts") {
    const target = card.querySelector("[data-wallet-attempts]");
    if (!target) return;
    if (!target.hidden) {
      target.hidden = true;
      return;
    }
    button.disabled = true;
    try {
      const attempts = await request("/api/admin/wallet-acquisitions/" + encodeURIComponent(runId) + "/attempts"); if (!currentAction()) return;
      if (!Array.isArray(attempts)) throw new Error("尝试记录响应无效");
      renderWalletAttempts(target, attempts);
    } catch (error) { if (!currentAction()) return;
      setStatus("#wallet-acquisition-status", errorText(error), true);
      if (error.status === 401) await logout();
    } finally { if (!currentAction()) return;
      button.disabled = false;
    }
    return;
  }
  const reasonInput = card.querySelector("[data-wallet-acquisition-reason]");
  const reason = reasonInput?.value.trim() || "";
  if (!reason) {
    setStatus("#wallet-acquisition-status", "请先填写操作理由", true);
    reasonInput?.focus();
    return;
  }
  if (operation === "cancel" && !window.confirm("取消后该采集不会自动执行，继续吗？")) return;
  if (operation === "retry" && !window.confirm("将未知采集重新排队；后续读取会再次消耗 RPC 预算。继续吗？")) return;
  const controls = card.querySelectorAll("button, input");
  controls.forEach((control) => { control.disabled = true; });
  button.textContent = operation === "retry" ? "重试中…" : "取消中…";
  try {
    await request("/api/admin/wallet-acquisitions/" + encodeURIComponent(runId) + "/" + operation, { method: "POST", body: JSON.stringify({ reason }) }); if (!currentAction()) return;
    await walletRefreshAfterWrite("#wallet-acquisition-status",operation === "retry" ? "采集已重新排队" : "采集已取消",loadWalletAcquisitions); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#wallet-acquisition-status", errorText(error), true);
    if (error.status === 401) await logout();
  } finally { if (!currentAction()) return;
    controls.forEach((control) => { control.disabled = false; });
  }
});

$("#public-post-controls-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault(); const form = event.currentTarget; const button = submitButton(form, event); if (button) button.disabled = true;
  try {
    const raw = $("#public-post-storage-cap").value.trim();
    if (!/^\d+(\.\d{1,9})?$/.test(raw)) throw new Error("容量应为非负数字，最多九位小数");
    const [whole,fraction=""] = raw.split("."); const scale = BigInt($("#public-post-storage-unit").value);
    const numerator = BigInt(whole + fraction) * scale; const divisor = 10n ** BigInt(fraction.length);
    if (numerator % divisor || numerator / divisor < 1000n || numerator / divisor > 2000000000n) throw new Error("容量须为整数个字节，范围 1 KB 至 2 GB（十进制）");
    const payload = {rate_limit_per_hour:Number($("#public-post-rate-limit").value),queue_cap:Number($("#public-post-queue-cap").value),captcha_ttl_seconds:Number($("#public-post-captcha-ttl").value),captcha_max_attempts:Number($("#public-post-captcha-attempts").value),captcha_mode:$("#public-post-captcha-mode").value,storage_cap_bytes:Number(numerator/divisor),captcha_issue_limit_per_hour:Number($("#public-post-captcha-issue-limit").value),captcha_global_rate_per_minute:Number($("#public-post-captcha-global-rate").value),max_content_chars:Number($("#public-post-max-chars").value),expected_length_revision:Number(form.dataset.lengthRevision)};
    await request("/api/config/public-post-controls", {method:"POST",body:JSON.stringify(payload)}); if (!currentAction()) return;
    delete form.dataset.dirty; setStatus("#public-post-controls-status", "公共发帖防护设置已保存");
    try { await loadRuntime(); if (!currentAction()) return; } catch { if (!currentAction()) return; setStatus("#public-post-controls-status", "设置已保存；读取失败，请刷新",true); }
  } catch (error) { if (!currentAction()) return; setStatus("#public-post-controls-status",errorText(error),true); }
  finally { if (!currentAction()) return; if (button) button.disabled = false; }
});

$("#model-list").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  const form = event.target.closest("[data-model-budget-form]");
  if (!form) return;
  event.preventDefault();
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  clearBudgetErrors(form);
  try {
    const payload = { reason: "admin budget update", expected_updated_at: form.dataset.updatedAt, expected_limit_revision: Number(form.dataset.limitRevision), ...readModelLimits(form, "data-limit-dimension") };
    if (!confirmUnlimitedLimits(payload.limit_enabled)) return;
    payload.pricing_status = form.elements.pricing_status.value;
    for (const name of ["priority", "weight"]) payload[name] = normalizeBudgetInteger(name, form.elements[name].value);
    payload.input_usd_per_million = normalizeBudgetCost(form.elements.input_usd_per_million.value);
    payload.output_usd_per_million = normalizeBudgetCost(form.elements.output_usd_per_million.value);
    validateCostProtection(payload);
    const attempts = form.elements.max_attempts.value.trim();
    if (!/^[1-9][0-9]*$/.test(attempts) || BigInt(attempts) > 10n) throw new Error("最大尝试应为 1 至 10");
    payload.max_attempts = attempts;
    Object.keys(payload).forEach((name) => { if (payload[name] === null) delete payload[name]; });
    await request(`/api/config/model-resources/${encodeURIComponent(form.dataset.id)}/update`, { method: "POST", body: JSON.stringify(payload) }); if (!currentAction()) return;
    delete form.dataset.dirty;
    setStatus("#model-status", "模型资源限制已更新");
    try { await loadModels(); if (!currentAction()) return; } catch { if (!currentAction()) return; setStatus("#model-status", "模型资源限制已保存；列表刷新失败，请点击更新", true); }
  } catch (error) { if (!currentAction()) return;
    showBudgetError(form, error);
    setStatus("#model-status", errorText(error), true);
  } finally { if (!currentAction()) return;
    if (button) button.disabled = false;
  }
});

$("#capability-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-capability-action]");
  if (!button) return;
  if (!window.confirm("撤销后该能力立即失效，并保留审计记录。继续吗？")) return;
  button.disabled = true;
  try {
    await request(`/api/config/capabilities/${encodeURIComponent(button.dataset.id)}/revoke`, { method: "POST", body: JSON.stringify({ reason: "admin capability revoke" }) }); if (!currentAction()) return;
    setStatus("#capability-status", "能力已撤销");
    await loadCapabilities(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#capability-status", errorText(error), true);
  } finally { if (!currentAction()) return;
    button.disabled = false;
  }
});

$("#public-post-status-filter").addEventListener("change", () => { void loadPublicPosts().catch(() => {}); });
$("#refresh-public-posts").addEventListener("click", () => { void loadPublicPosts().catch(() => {}); });
$("#load-more-public-posts").addEventListener("click", () => { void loadPublicPosts({ append: true }).catch(() => {}); });
$("#public-post-review-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-public-post-action]");
  if (!button) return;
  const card = button.closest("[data-public-post-card]");
  const reasonInput = card?.querySelector("[data-public-post-reason]");
  const itemStatus = card?.querySelector("[data-public-post-item-status]");
  const postId = card?.dataset.postId;
  const expectedStatus = card?.dataset.expectedStatus;
  const operation = button.dataset.publicPostAction;
  const reason = reasonInput?.value.trim() || "";
  if (!card || !itemStatus || !postId || !expectedStatus || !reason) {
    if (itemStatus) {
      itemStatus.textContent = "请先填写审核理由";
      itemStatus.classList.add("error");
    }
    reasonInput?.focus();
    return;
  }
  // Keep retries of the same decision idempotent, while allowing an operator
  // to correct the reason after a failed attempt without reusing its key.
  const actionKey = `${postId}:${expectedStatus}:${operation}:${reason}`;
  const idempotencyKey = publicPostActionKeys.get(actionKey) || crypto.randomUUID();
  publicPostActionKeys.set(actionKey, idempotencyKey);
  const controls = card.querySelectorAll("button, input, textarea");
  const originalLabel = button.textContent;
  controls.forEach((control) => { control.disabled = true; });
  button.textContent = "处理中…";
  itemStatus.textContent = "正在提交审核决定";
  itemStatus.classList.remove("error");
  try {
    await request(`/api/admin/public-posts/${encodeURIComponent(postId)}/${operation}`, {
      method: "POST",
      body: JSON.stringify({ reason, expected_status: expectedStatus, idempotency_key: idempotencyKey }),
    }); if (!currentAction()) return;
    publicPostActionKeys.delete(actionKey);
    setStatus("#public-post-review-status", "审核决定已保存");
    try {
      await loadPublicPosts(); if (!currentAction()) return;
    } catch (error) { if (!currentAction()) return;
      setStatus("#public-post-review-status", `审核已保存，但列表刷新失败：${errorText(error)}`, true);
    }
  } catch (error) { if (!currentAction()) return;
    itemStatus.textContent = errorText(error);
    itemStatus.classList.add("error");
    if (error.status === 401) await logout();
  } finally { if (!currentAction()) return;
    button.textContent = originalLabel;
    controls.forEach((control) => { control.disabled = false; });
  }
});

function updateChannelForm() {
  const channel = $("#admin-channel-type").value;
  const feishuMode = $("#admin-channel-feishu-mode").value;
  const feishuApp = channel === "feishu" && feishuMode === "app";
  const feishuCustom = channel === "feishu" && feishuMode === "custom";
  const placeholders = {
    telegram: "https://api.telegram.org",
    qq: "https://api.sgroup.qq.com",
    feishu: feishuCustom ? "飞书群自定义机器人完整 Webhook 地址" : "https://open.feishu.cn",
    wechat: "https://api.weixin.qq.com",
    email: "smtps://smtp.example.com:465 或 smtp://smtp.example.com:587",
    webhook: "https://example.com/webhook",
  };
  $("#admin-channel-endpoint").placeholder = placeholders[channel] || "HTTPS API 地址";
  const endpoint = $("#admin-channel-endpoint");
  if (endpoint.dataset.default === endpoint.value || !endpoint.value) {
    endpoint.value = channel === "qq" ? placeholders.qq : "";
    endpoint.dataset.default = endpoint.value;
  }
  const accountPlaceholders = {
    telegram: "默认 Chat ID（可选）",
    qq: "默认用户 / 群 / 频道 OpenID（可选）",
    feishu: "默认飞书收件人 ID（可选）",
    wechat: "默认关注者 OpenID（可选）",
    email: "发件邮箱及默认求助收件邮箱",
    webhook: "默认会话 ID（可选）",
  };
  $("#admin-channel-account").placeholder = accountPlaceholders[channel] || "默认收件人（可选）";
  $("#admin-channel-app-id").placeholder = channel === "wechat" ? "微信公众号 AppID" : `${channel === "qq" ? "QQ" : "飞书"} App ID`;
  $("#admin-channel-app-secret").placeholder = `${channel === "wechat" ? "微信公众号" : channel === "qq" ? "QQ" : "飞书"} App Secret（仅写入密钥文件）`;
  $("#admin-channel-secret").placeholder = channel === "qq"
    ? "旧版 Noyra HMAC 回调密钥（可选）"
    : feishuApp
      ? "飞书 Verification Token（事件回调）"
      : feishuCustom
        ? "飞书群机器人签名密钥（可选）"
        : channel === "wechat"
          ? "微信公众号 Token（事件回调验签）"
          : channel === "telegram"
            ? "Telegram Webhook Secret Token"
            : "Webhook HMAC 密钥";
  $("#admin-channel-token").placeholder = channel === "telegram"
    ? "Telegram Bot Token"
    : channel === "feishu"
      ? "Tenant Access Token（可选；默认自动获取）"
      : "Access Token（可选；默认通过 App 凭证自动获取）";
  $("#admin-channel-encryption-key").placeholder = channel === "feishu"
    ? "飞书 Encrypt Key（入站验签，可选）"
    : "微信 EncodingAESKey（安全模式，可选）";
  const setChannelField = (id, visible) => {
    const field = $(id);
    const container = field.closest("[data-channel-field]");
    field.hidden = !visible;
    if (container) container.hidden = !visible;
  };
  setChannelField("#admin-channel-feishu-mode", channel === "feishu");
  const appCredentialsVisible = ["qq", "wechat"].includes(channel) || feishuApp;
  setChannelField("#admin-channel-app-id", appCredentialsVisible);
  setChannelField("#admin-channel-app-secret", appCredentialsVisible);
  setChannelField("#admin-channel-secret", channel !== "email");
  setChannelField("#admin-channel-token", channel !== "email" && channel !== "webhook" && !feishuCustom);
  setChannelField("#admin-channel-account", !feishuCustom);
  setChannelField("#admin-channel-target-type", channel === "qq");
  setChannelField("#admin-channel-receive-id-type", feishuApp);
  setChannelField("#admin-channel-encryption-key", channel === "wechat" || feishuApp);
  setChannelField("#admin-channel-smtp-user", channel === "email");
  setChannelField("#admin-channel-smtp-password", channel === "email");
  $("#admin-channel-app-id").required = appCredentialsVisible;
  $("#admin-channel-app-secret").required = appCredentialsVisible;
  $("#admin-channel-token").required = channel === "telegram";
  $("#admin-channel-secret").required = ["telegram", "wechat"].includes(channel) || feishuApp;
}
$("#admin-channel-type").addEventListener("change", updateChannelForm);
$("#admin-channel-feishu-mode").addEventListener("change", updateChannelForm);
updateChannelForm();
$("#admin-channel-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  try {
    const channel = $("#admin-channel-type").value;
    const feishuMode = $("#admin-channel-feishu-mode").value;
    const usesAppCredentials = ["qq", "wechat"].includes(channel) || (channel === "feishu" && feishuMode === "app");
    const account = channel === "feishu" && feishuMode === "custom" ? "" : $("#admin-channel-account").value.trim();
    const appId = usesAppCredentials ? $("#admin-channel-app-id").value.trim() : "";
    const appSecret = usesAppCredentials ? $("#admin-channel-app-secret").value.trim() : "";
    const secret = $("#admin-channel-secret").value.trim();
    const acceptsToken = ["telegram", "qq", "wechat"].includes(channel) || (channel === "feishu" && feishuMode === "app");
    const token = acceptsToken ? $("#admin-channel-token").value.trim() : "";
    const credentials = {};
    const settings = {};
    if (appId) credentials.app_id = appId;
    if (appSecret) credentials.app_secret = appSecret;
    if (secret && ["telegram", "qq", "wechat", "webhook"].includes(channel)) credentials.webhook_secret = secret;
    if (token) {
      credentials[channel === "telegram" ? "bot_token" : channel === "feishu" ? "tenant_access_token" : channel === "wechat" || channel === "qq" ? "access_token" : "token"] = token;
    }
    if (channel === "feishu") {
      settings.delivery_mode = feishuMode;
      if (feishuMode === "app") {
        settings.receive_id_type = $("#admin-channel-receive-id-type").value;
        if (secret) credentials.verification_token = secret;
      } else if (secret) {
        credentials.signing_secret = secret;
      }
    }
    if (channel === "qq") settings.target_type = $("#admin-channel-target-type").value;
    if (channel === "wechat" || (channel === "feishu" && feishuMode === "app")) {
      const encryptionKey = $("#admin-channel-encryption-key").value.trim();
      if (encryptionKey) {
        if (channel === "wechat") credentials.encoding_aes_key = encryptionKey;
        else credentials.encrypt_key = encryptionKey;
      }
    }
    if (channel === "email") {
      credentials.from_address = account;
      const username = $("#admin-channel-smtp-user").value.trim();
      const password = $("#admin-channel-smtp-password").value.trim();
      if (username) credentials.username = username;
      if (password) credentials.password = password;
      settings.recipient = account;
    } else if (account) {
      settings.recipient = account;
    }
    await request("/api/config/transports", { method: "POST", body: JSON.stringify({ channel, label: $("#admin-channel-label").value.trim(), endpoint: $("#admin-channel-endpoint").value.trim(), settings, credentials }) }); if (!currentAction()) return;
    form.reset();
    updateChannelForm();
    setStatus("#channel-status", "通讯渠道已保存");
    await loadChannels(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#channel-status", errorText(error), true);
  } finally { if (!currentAction()) return;
    if (button) button.disabled = false;
  }
});
$("#qq-identity-form").addEventListener("submit", async event => {
  const currentAction = beginAdminAction();
  event.preventDefault(); const button = submitButton(event.currentTarget, event); button.disabled = true;
  const session = csrfToken;
  try {
    const result = await request("/api/config/identity-challenges", {method:"POST",body:JSON.stringify({transport_id:$("#qq-identity-transport").value})}); if (!currentAction()) return;
    if (!session || csrfToken !== session) return;
    clearQQIdentity(); qqIdentity = result;
    $("#qq-identity-challenge").hidden = false;
    $("#qq-identity-summary").innerHTML = summaryRows([["QQ 私聊验证码",result.code],["有效至",formatTimestamp(result.expires_at)],["验证范围","当前机器人私聊；不触发认知调用"]]);
    setStatus("#qq-identity-status","等待收到验证码");
  } catch (error) { if (!currentAction()) return;setStatus("#qq-identity-status",errorText(error),true);} finally { if (!currentAction()) return;button.disabled=false;}
});
$("#qq-identity-transport").addEventListener("change",clearQQIdentity);
$("#logout").addEventListener("click",clearQQIdentity);
$("#qq-identity-check").addEventListener("click",async event => {
  const currentAction = beginAdminAction();
  const button=event.currentTarget; button.disabled=true;
  try {await checkQQIdentity();} catch (error) { if (!currentAction()) return;setStatus("#qq-identity-status",errorText(error),true);} finally { if (!currentAction()) return;button.disabled=false;}
});
$("#qq-identity-confirm").addEventListener("click",async event => {
  const currentAction = beginAdminAction();
  const button=event.currentTarget;button.disabled=true;const identity=qqIdentity;
  if(!identity)return;
  try {
    const result=await request(`/api/config/identity-challenges/${encodeURIComponent(identity.challenge_id)}/confirm`,{method:"POST",body:JSON.stringify({label:"我的 QQ"})}); if (!currentAction()) return;
    if(qqIdentity!==identity || !csrfToken)return;
    identity.binding_id=result.binding_id;identity.revision=0;$("#qq-identity-summary").replaceChildren();
    setStatus("#qq-identity-status","身份已确认；网页历史共享尚未开启");
    const page=await request("/api/admin/conversations?limit=100"); if (!currentAction()) return;
    if(qqIdentity!==identity || !csrfToken)return;
    $("#qq-chat-window").innerHTML=page.items.filter(item=>item.principal_id==="operator:web-user").map(item=>`<option value="${esc(item.conversation_id)}">${esc(item.title)}</option>`).join("");
    $("#qq-chat-route-form").hidden=false;await loadChannels(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;setStatus("#qq-identity-status",errorText(error),true);button.disabled=false;}
});
$("#qq-chat-route-form").addEventListener("submit",async event => {
  const currentAction = beginAdminAction();
  event.preventDefault();if(!qqIdentity?.binding_id)return;const button=submitButton(event.currentTarget,event);button.disabled=true;
  try {
    const push = $("#qq-push-web-replies").checked;
    if(push && !window.confirm("将此窗口今后产生的 Noyra 网页回复发送到已验证 QQ 私聊；不补发历史。平台可能拒绝主动消息，已发送的第三方副本无法撤回。继续吗？")) return;
    await request(`/api/config/inbound-bindings/${encodeURIComponent(qqIdentity.binding_id)}/route`,{method:"POST",body:JSON.stringify({conversation_id:$("#qq-chat-window").value,share_web_history:$("#qq-share-history").checked,push_web_replies:push,expected_revision:qqIdentity.revision ?? 0,reason:"operator verified private history association"})}); if (!currentAction()) return;
    if(!qqIdentity || !csrfToken)return;
    qqIdentity.revision=(qqIdentity.revision ?? 0)+1;
    setStatus("#qq-identity-status",push ? "聊天关联已保存，新网页回复推送已开启" : "聊天关联已保存，网页回复推送关闭");
    try {await loadChannels(); if (!currentAction()) return;} catch { if (!currentAction()) return;setStatus("#qq-identity-status","关联已保存；联系人刷新失败",true);}
  } catch (error) { if (!currentAction()) return;setStatus("#qq-identity-status",errorText(error),true);} finally { if (!currentAction()) return;button.disabled=false;}
});
$("#admin-binding-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction(); event.preventDefault(); const form = event.currentTarget; const button = submitButton(form, event); if (button) button.disabled = true; try { await request("/api/config/inbound-bindings", { method: "POST", body: JSON.stringify({ transport_id: $("#admin-binding-transport").value, external_account_id: $("#admin-binding-account").value.trim(), external_sender_id: $("#admin-binding-sender").value.trim(), role: $("#admin-binding-role").value, label: $("#admin-binding-label").value.trim() }) }); if (!currentAction()) return; form.reset(); setStatus("#binding-status", "入站账号已绑定"); await loadChannels(); if (!currentAction()) return; } catch (error) { if (!currentAction()) return; setStatus("#binding-status", errorText(error), true); } finally { if (!currentAction()) return; if (button) button.disabled = false; } });
$("#channel-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction(); const button = event.target.closest("[data-channel-action]"); if (!button) return; button.disabled = true; try { const operation = button.dataset.channelAction; await request(`/api/config/transports/${button.dataset.id}/${operation}`, { method: "POST", body: JSON.stringify({ reason: `admin ${operation} transport` }) }); if (!currentAction()) return; await loadChannels(); if (!currentAction()) return; } catch (error) { if (!currentAction()) return; setStatus("#channel-status", errorText(error), true); } finally { if (!currentAction()) return; button.disabled = false; } });
$("#binding-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-binding-action], [data-binding-route]"); if (!button) return; button.disabled = true;
  try {
    if(button.dataset.bindingRoute) {
      const binding = channelBindings.find(item=>item.binding_id===button.dataset.bindingRoute);
      if(!binding)return;
      clearQQIdentity(); const identity = {binding_id:binding.binding_id,revision:binding.revision ?? 0}; qqIdentity=identity;
      const page = await request("/api/admin/conversations?limit=100"); if (!currentAction()) return;
      if(qqIdentity!==identity || !csrfToken)return;
      $("#qq-chat-window").innerHTML=page.items.filter(item=>item.principal_id==="operator:web-user").map(item=>`<option value="${esc(item.conversation_id)}">${esc(item.title)}</option>`).join("");
      if(binding.conversation_id)$("#qq-chat-window").value=binding.conversation_id;
      $("#qq-share-history").checked=Boolean(binding.share_web_history);$("#qq-push-web-replies").checked=Boolean(binding.push_web_replies);
      $("#qq-chat-route-form").hidden=false; $("#qq-chat-route-form").scrollIntoView({block:"nearest"});
      return;
    }
    const operation=button.dataset.bindingAction;
    if(["revoke","unlink"].includes(operation) && !window.confirm(operation==="revoke" ? "解绑后此账号无法发送新消息，尚未外发的回复将停止投递。重新接入需要再次验证。继续吗？" : "取消关联会停止历史共享和网页回复推送，已有消息保留。继续吗？"))return;
    await request(`/api/config/inbound-bindings/${encodeURIComponent(button.dataset.id)}/${operation}`,{method:"POST",body:JSON.stringify({reason:`admin ${operation} inbound binding`})}); if (!currentAction()) return;
    clearQQIdentity();setStatus("#binding-status","联系人设置已更新");
    try {await loadChannels(); if (!currentAction()) return;} catch { if (!currentAction()) return;setStatus("#binding-status","设置已更新；列表刷新失败",true);}
  } catch (error) { if (!currentAction()) return;setStatus("#binding-status",errorText(error),true);} finally { if (!currentAction()) return;button.disabled=false;}
});
$("#refresh-channels").addEventListener("click", loadChannels);
$("#model-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-model-action]"); if (!button) return;
  const action = button.dataset.modelAction;
  if (action === "test" && !window.confirm("测试会调用供应商并消耗模型额度，继续吗？")) return;
  if (action === "revoke") {
    const item = renderedModels.get(button.dataset.id);
    if (!item || !window.confirm(modelDeletionMessage(item))) return;
  }
  button.disabled = true;
  try {
    await request(`/api/config/model-resources/${encodeURIComponent(button.dataset.id)}/${action}`, { method: "POST", body: JSON.stringify({ reason: `admin ${action} model resource` }) }); if (!currentAction()) return;
    setStatus("#model-status", action === "test" ? "模型测试请求已完成" : "资源状态已更新");
    try { await loadModels(); if (!currentAction()) return; } catch { if (!currentAction()) return; setStatus("#model-status", "操作已完成；列表刷新失败，请点击更新", true); }
  } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#model-list").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  const form = event.target.closest("[data-add-model-keys]"); if (!form) return;
  event.preventDefault(); const button = form.querySelector("button[type=submit]"); button.disabled = true;
  try {
    await request(`/api/config/model-resources/${encodeURIComponent(form.dataset.id)}/keys`, { method: "POST", body: JSON.stringify({ api_keys: form.elements.api_keys.value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean) }) }); if (!currentAction()) return;
    form.reset(); delete form.dataset.dirty; setStatus("#model-status", "密钥已添加");
    try { await loadModels(); if (!currentAction()) return; } catch { if (!currentAction()) return; setStatus("#model-status", "密钥已添加；列表刷新失败", true); }
  } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#model-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction(); const button = event.target.closest("[data-model-key-action]"); if (!button) return; if (!window.confirm("撤销后该密钥立即失效，且不可恢复。继续吗？")) return; button.disabled = true; try { await request(`/api/config/model-resources/${encodeURIComponent(button.dataset.groupId)}/keys/${encodeURIComponent(button.dataset.keyId)}/revoke`, { method: "POST", body: JSON.stringify({ reason: "admin revoked model key" }) }); if (!currentAction()) return; setStatus("#model-status", "模型密钥已撤销"); await loadModels(); if (!currentAction()) return; } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); } finally { if (!currentAction()) return; button.disabled = false; } });
$("#wallet-order-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest('[data-wallet-order-action]');
  if (!button) return;
  const action = button.dataset.walletOrderAction;
  if (!["execute","confirm","reject","cancel"].includes(action)) return;
  button.disabled = true;
  try {
    await loadWalletEconomy({kind:"orders",move:"current"}); if (!currentAction()) return;
    const order=walletOrders.find(row=>row.order_id===button.dataset.orderId);
    const expected = ["execute","cancel"].includes(action) ? "reserved":"awaiting_confirmation";
    if(!order || order.status!==expected)throw new Error("订单已变化，请核对当前状态");
    const payload={};
    if(["reject","cancel"].includes(action)){
      const reason=prompt("请输入操作理由：");if(!reason?.trim())return;payload.reason=reason.trim();
    }
    const labels={execute:"签名并广播",confirm:"确认付款额度",reject:"拒绝订单",cancel:"取消订单"};
    const effect=action==="execute" ? "此操作会消耗 RPC，链上付款不可撤回。" : action==="confirm" ? "确认会预留额度；已开启的自动付款流程可能随后执行此订单。" : "此操作不追回链上资金。";
    if(!confirm(`${labels[action]} ${walletAmount(order.amount,order.asset_id)}\n网络：${order.network_id}\n收款：${order.recipient_address}\n${effect}确认继续？`))return;
    await request(`/api/admin/wallet-orders/${encodeURIComponent(button.dataset.orderId)}/${action}`, { method: "POST", body: JSON.stringify(payload) }); if (!currentAction()) return;
    const messages={execute:"转账请求已处理，请核对订单与回执",confirm:"付款额度已确认并预留",reject:"订单已拒绝",cancel:"订单已取消"};
    await walletRefreshAfterWrite("#wallet-read-status",messages[action],()=>loadWalletEconomy()); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return; setStatus("#wallet-read-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});

$("#admin-model-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  try {
    const payload = { pool: $("#admin-model-pool").value, label: $("#admin-model-label").value.trim(), base_url: $("#admin-model-url").value.trim(), model: $("#admin-model-name").value.trim(), api_keys: $("#admin-model-keys").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean), ...readModelLimits(form, "data-draft-limit") };
    if ($("#admin-model-reasoning")?.value) payload.reasoning_effort = $("#admin-model-reasoning").value;
    if (!confirmUnlimitedLimits(payload.limit_enabled)) return;
    payload.input_usd_per_million = normalizeBudgetCost(form.elements.input_usd_per_million.value);
    payload.output_usd_per_million = normalizeBudgetCost(form.elements.output_usd_per_million.value);
    payload.pricing_status = form.elements.pricing_status.value;
    validateCostProtection(payload);
    const maxAttempts = form.elements.max_attempts.value.trim();
    if (!/^[1-9][0-9]*$/.test(maxAttempts) || BigInt(maxAttempts) > 10n) throw new Error("最大尝试应为 1 至 10");
    payload.max_attempts = maxAttempts;
    await request("/api/config/model-resources", { method: "POST", body: JSON.stringify(payload) }); if (!currentAction()) return;
    form.reset();
    delete form.dataset.dirty;
    for (const toggle of form.querySelectorAll("[data-draft-limit]")) form.elements[limitFields[toggle.dataset.draftLimit]].disabled = !toggle.checked;
    $("#admin-model-options").hidden = true;
    setStatus("#model-status", "认知资源已保存");
    try { await loadModels(); if (!currentAction()) return; } catch { if (!currentAction()) return; setStatus("#model-status", "认知资源已保存；列表刷新失败，请点击更新", true); }
  } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); }
  finally { if (!currentAction()) return; if (button) button.disabled = false; }
});
$("#admin-model-test").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.currentTarget;
  if (!window.confirm("测试当前配置会向供应商发送请求，可能产生费用；配置尚未保存，因此不会计入已保存资源的预算。确认测试吗？")) return;
  button.disabled = true;
  try {
    const result = await request("/api/config/model-resources/test", { method: "POST", body: JSON.stringify(draftModelPayload(true)) }); if (!currentAction()) return;
    setStatus("#model-status", `连接测试成功：${result.model}`);
  } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#admin-model-discover").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await request("/api/config/model-resources/models", { method: "POST", body: JSON.stringify(draftModelPayload(false)) }); if (!currentAction()) return;
    const select = $("#admin-model-options");
    select.innerHTML = '<option value="">请选择模型</option>' + (result.models || []).map((model) => `<option value="${esc(model)}">${esc(model)}</option>`).join("");
    select.hidden = !(result.models || []).length;
    setStatus("#model-status", result.models?.length ? `已获取 ${result.models.length} 个模型` : "接口返回的模型列表为空");
  } catch (error) { if (!currentAction()) return; setStatus("#model-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#admin-model-options").addEventListener("change", (event) => { if (event.currentTarget.value) $("#admin-model-name").value = event.currentTarget.value; });

$("#wallet-policy-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  if (!walletPolicy) {
    setStatus("#wallet-policy-status", "策略尚未加载，请先更新", true);
    return;
  }
  const form = event.currentTarget;
  const button = submitButton(form, event);
  const perOrder = $("#wallet-policy-per-order").value.trim();
  const daily = $("#wallet-policy-daily").value.trim();
  const mode = $("#wallet-policy-mode").value;
  if (!/^\d+$/.test(perOrder) || !/^\d+$/.test(daily)) {
    setStatus("#wallet-policy-status", "额度必须是非负整数", true);
    return;
  }
  if (mode === "automatic" && (BigInt(perOrder) <= 0n || BigInt(daily) <= 0n)) {
    setStatus("#wallet-policy-status", "自动付款需要填写大于 0 的单笔上限和每日总额上限", true);
    return;
  }
  const allowedRecipients = $("#wallet-policy-allowlist").value
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
  if(!confirm(`保存付款策略？\n模式：${$("#wallet-policy-mode option:checked").textContent}\n自动付款：${$("#wallet-policy-automation-enabled").checked ? "开启":"关闭"}\n单笔 / 每日上限：${perOrder} / ${daily} 最小单位\n白名单：${$("#wallet-policy-allowlist-enabled").checked ? allowedRecipients.length + " 个地址" : "关闭"}\n其他有效限制保持现有值。`))return;
  if (button) button.disabled = true;
  try {
    await request("/api/admin/wallet-policy", {
      method: "POST",
      body: JSON.stringify({
        expected_version: walletPolicyDraftVersion,
        mode,
        allowed_network_ids: walletPolicy.allowed_network_ids || [],
        allowed_asset_ids: walletPolicy.allowed_asset_ids || [],
        per_order_limit: perOrder,
        daily_limit: daily,
        monthly_limit: walletPolicy.monthly_limit || "0",
        daily_order_limit: walletPolicy.daily_order_limit || 0,
        monthly_order_limit: walletPolicy.monthly_order_limit || 0,
        min_balance: walletPolicy.min_balance || "0",
        max_observation_age_seconds: walletPolicy.max_observation_age_seconds || 0,
        // Legacy field retained for schema compatibility; automatic payments
        // use only the visible per-order and daily amount caps.
        automatic_max_amount: "0",
        anomaly_block: walletPolicy.anomaly_block !== false,
        emergency_paused: walletPolicy.emergency_paused === true,
        automation_enabled: $("#wallet-policy-automation-enabled").checked,
        recipient_allowlist_enabled: $("#wallet-policy-allowlist-enabled").checked,
        allowed_recipient_addresses: allowedRecipients,
      }),
    }); if (!currentAction()) return;
    delete form.dataset.dirty;
    await walletRefreshAfterWrite("#wallet-policy-status","付款策略已保存",loadWalletPolicy); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    setStatus("#wallet-policy-status", errorText(error), true);
    if (error.status === 401) await logout();
  } finally { if (!currentAction()) return;
    if (button) button.disabled = false;
  }
});

async function setWalletAutomationPause(paused) {
  const currentAction = beginAdminAction();
  if (!walletPolicy) return;
  if(!paused && !confirm("解除紧急暂停后，已开启的自动付款可恢复执行。确认继续？"))return;
  const button = paused ? $("#wallet-automation-pause") : $("#wallet-automation-resume");
  button.disabled = true;
  try {
    await request(`/api/v1/admin/wallet-automation/${paused ? "pause" : "resume"}`, {
      method: "POST",
      body: JSON.stringify({ expected_version: walletPolicy.policy_version, reason: paused ? "管理员紧急暂停自动付款" : "管理员解除自动付款暂停" }),
    });
    if (!currentAction()) return;
    await walletRefreshAfterWrite("#wallet-policy-status",paused ? "已紧急暂停":"已解除暂停",loadWalletPolicy);
  } catch (error) { if (!currentAction()) return; setStatus("#wallet-policy-status", errorText(error), true); button.disabled = false; }
}
$("#wallet-automation-pause").addEventListener("click", () => { void setWalletAutomationPause(true); });
$("#wallet-automation-resume").addEventListener("click", () => { void setWalletAutomationPause(false); });

$("#admin-search-provider-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  try {
    await request("/api/config/search-providers", { method: "POST", body: JSON.stringify({ provider_type: $("#admin-search-provider-type").value, label: $("#admin-search-provider-label").value.trim(), api_key: $("#admin-search-provider-key").value }) }); if (!currentAction()) return;
    form.reset();
    setStatus("#search-provider-status", "搜索 API 已保存");
    await loadSearchProviders(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return; setStatus("#search-provider-status", errorText(error), true); }
  finally { if (!currentAction()) return; if (button) button.disabled = false; }
});
$("#admin-search-provider-test").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await request("/api/config/search-providers/test", { method: "POST", body: JSON.stringify({ provider_type: $("#admin-search-provider-type").value, api_key: $("#admin-search-provider-key").value }) }); if (!currentAction()) return;
    setStatus("#search-provider-status", `连接测试成功，返回 ${result.result_count} 条结果`);
  } catch (error) { if (!currentAction()) return; setStatus("#search-provider-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
for (const selector of ["#migration-policy-form", "#admin-search-routing-form"]) $(selector).addEventListener("input", event => { event.currentTarget.dataset.dirty="1"; });
$("#search-provider-list").addEventListener("input", event => { const form=event.target.closest("form"); if(form)form.dataset.dirty="1"; });
$("#admin-search-routing-form").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  event.preventDefault();
  const form = event.currentTarget;
  const button = submitButton(form, event);
  if (button) button.disabled = true;
  try { await request("/api/config/search-routing", { method: "POST", body: JSON.stringify({ mode: $("#search-routing-mode").value }) }); if (!currentAction()) return; delete form.dataset.dirty; setStatus("#search-routing-status", "搜索方式已保存"); }
  catch (error) { if (!currentAction()) return; setStatus("#search-routing-status", errorText(error), true); }
  finally { if (!currentAction()) return; if (button) button.disabled = false; }
});
$("#search-provider-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-search-provider-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const action = button.dataset.searchProviderAction;
    const path = action === "revoke" ? "revoke" : action;
    const reason = action === "revoke" ? "管理员撤销搜索 API" : (action === "enable" ? "管理员启用搜索 API" : "管理员停用搜索 API");
    await request(`/api/config/search-providers/${encodeURIComponent(button.dataset.id)}/${path}`, { method: "POST", body: JSON.stringify({ reason }) }); if (!currentAction()) return;
    await loadSearchProviders(); if (!currentAction()) return;
  }
  catch (error) { if (!currentAction()) return; setStatus("#search-provider-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#search-provider-list").addEventListener("submit", async (event) => {
  const currentAction = beginAdminAction();
  const form = event.target.closest("[data-search-routing-form]");
  if (!form) return;
  event.preventDefault();
  if (!form.reportValidity()) return;
  const button = submitButton(form, event);
  const status = form.querySelector("[data-routing-status]");
  if (button) button.disabled = true;
  try {
    const priority = Number(form.elements.priority.value);
    const weight = Number(form.elements.weight.value);
    if (!Number.isInteger(priority) || !Number.isInteger(weight)) throw new Error("优先级和权重必须是整数");
    await request(`/api/config/search-providers/${encodeURIComponent(form.dataset.id)}/routing`, {
      method: "POST",
      body: JSON.stringify({ priority, weight, reason: form.elements.reason.value.trim() }),
    }); if (!currentAction()) return;
    status.textContent = "优先级已保存";
    delete form.dataset.dirty;
    status.classList.remove("error");
    await loadSearchProviders(); if (!currentAction()) return;
  } catch (error) { if (!currentAction()) return;
    status.textContent = error?.code === "invalid_search_provider_routing" ? "搜索优先级或权重不符合要求" : errorText(error);
    status.classList.add("error");
  }
  finally { if (!currentAction()) return; if (button) button.disabled = false; }
});
$("#wallet-execution-list").addEventListener("click", async (event) => {
  const currentAction = beginAdminAction();
  const button = event.target.closest("[data-wallet-execution-action]");
  if (!button) return;
  const action = button.dataset.walletExecutionAction;
  const id = button.dataset.id;
  const orderId = button.dataset.orderId;
  let path;
  let payload = {};
  if (action === "receipt") {if(!confirm("查询链上回执会消耗 RPC 并更新执行状态。继续？"))return;path = `/api/admin/wallet-executions/${encodeURIComponent(id)}/receipt`;}
  else {
    if(action==="retry" && !confirm("上次广播结果未知，重试可能造成重复付款。请先核对链上回执，确认仍需重试？"))return;
    if(action==="refund" && !confirm("仅释放失败订单的内部预留，不追回已发送的链上资金。继续？"))return;
    const reason = window.prompt(action === "retry" ? "请输入已核对链上结果后的重试理由" : "请输入释放失败订单预留的理由", "");
    if (!reason || !reason.trim()) return;
    path = `/api/admin/wallet-orders/${encodeURIComponent(orderId)}/${action}`;
    payload = { reason: reason.trim() };
  }
  button.disabled = true;
  try { await request(path, { method: "POST", body: JSON.stringify(payload) }); if (!currentAction()) return; await walletRefreshAfterWrite("#wallet-execution-status","操作已处理，请核对当前状态",loadWalletExecutions); if (!currentAction()) return; }
  catch (error) { if (!currentAction()) return; setStatus("#wallet-execution-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
$("#refresh-wallet-executions").addEventListener("click", loadWalletExecutions);
$("#recover-wallet-executions").addEventListener("click", async () => {
  const currentAction = beginAdminAction();
  if(!confirm("仅在确认无签名请求仍在进行时使用：所有签名中记录会被标记为结果未知，不会重复广播。继续？"))return;
  const button = $("#recover-wallet-executions"); button.disabled = true;
  try { await request("/api/admin/wallet-executions/recover", { method: "POST", body: JSON.stringify({ limit: 100 }) }); if (!currentAction()) return; await walletRefreshAfterWrite("#wallet-execution-status","恢复操作已处理",loadWalletExecutions); if (!currentAction()) return; }
  catch (error) { if (!currentAction()) return; setStatus("#wallet-execution-status", errorText(error), true); }
  finally { if (!currentAction()) return; button.disabled = false; }
});
const modelFilter = document.createElement("select"); modelFilter.id = "model-status-filter"; modelFilter.setAttribute("aria-label", "资源状态筛选");
modelFilter.innerHTML = '<option value="active">启用的资源</option><option value="disabled">停用的资源</option><option value="revoked">已删除的记录</option><option value="all">全部记录</option>';
$("#refresh-models").before(modelFilter);
modelFilter.addEventListener("change", () => { modelPageOffset = 0; void loadModels().catch((error) => { if (error?.staleSession) return; restoreModelFilters(); setStatus("#model-status", errorText(error), true); }); });
const poolFilter = document.createElement("select"); poolFilter.id = "model-pool-filter"; poolFilter.setAttribute("aria-label", "认知池筛选"); poolFilter.innerHTML = '<option value="economy">经济池</option><option value="deep">深度池</option>';
const modelSearch = document.createElement("input"); modelSearch.id = "model-search"; modelSearch.type = "search"; modelSearch.placeholder = "名称或模型"; modelSearch.maxLength = 128; modelSearch.setAttribute("aria-label", "搜索认知资源");
modelFilter.before(poolFilter, modelSearch);
for (const control of [poolFilter, modelSearch]) control.addEventListener("change", () => { modelPageOffset = 0; void loadModels().catch((error) => { if (error?.staleSession) return; restoreModelFilters(); setStatus("#model-status", errorText(error), true); }); });
const poolTabs = document.createElement("div"); poolTabs.className = "pool-tabs"; poolTabs.setAttribute("role", "tablist"); poolTabs.setAttribute("aria-label", "认知池");
poolTabs.innerHTML = '<button type="button" role="tab" data-model-pool="economy" aria-selected="true">经济池</button><button type="button" role="tab" data-model-pool="deep" aria-selected="false">深度池</button>';
poolFilter.before(poolTabs); poolFilter.hidden = true;
poolTabs.addEventListener("click", (event) => { const button = event.target.closest("[data-model-pool]"); if (!button) return; poolFilter.value = button.dataset.modelPool; poolTabs.querySelectorAll("button").forEach((item) => item.setAttribute("aria-selected", String(item === button))); poolFilter.dispatchEvent(new Event("change")); });
const modelPaging = document.createElement("div"); modelPaging.className = "resource-actions";
modelPaging.innerHTML = '<button type="button" id="model-page-prev" aria-label="上一页" title="上一页">←</button><span id="model-page-label"></span><button type="button" id="model-page-next" aria-label="下一页" title="下一页">→</button>';
$("#model-list").after(modelPaging);
for (const [id, step] of [["model-page-prev", -50], ["model-page-next", 50]]) $("#" + id).addEventListener("click", () => { modelPageOffset = Math.max(0, modelPageOffset + step); void loadModels().catch((error) => { if (error?.staleSession) return; restoreModelFilters(); setStatus("#model-status", errorText(error), true); }); });
$("#model-list").addEventListener("input", (event) => { const form = event.target.closest("form"); if (form) form.dataset.dirty = "true"; });
$("#model-list").addEventListener("toggle", (event) => {
  const details = event.target;
  if (details.matches("[data-model-key-details]") && details.open) void loadModelKeys(details);
}, true);
$("#model-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-model-keys-retry]");
  if (button) void loadModelKeys(button.closest("[data-model-key-details]"));
});
const draftLimits = document.createElement("details"); draftLimits.className = "draft-limits";
draftLimits.innerHTML = '<summary>每日限制</summary>' + Object.entries(limitFields).map(([dimension, field]) => `<label><input type="checkbox" data-draft-limit="${dimension}" checked>${parameterHelp[field][0]}<input name="${field}" value="${({ attempts: "100", input_tokens: "100000", output_tokens: "20000", cost: "5" })[dimension]}" inputmode="${dimension === "cost" ? "decimal" : "numeric"}"></label>`).join("");
$("#admin-model-form .resource-actions").before(draftLimits);
draftLimits.addEventListener("change", (event) => { const toggle = event.target.closest("[data-draft-limit]"); if (toggle) $("#admin-model-form").elements[limitFields[toggle.dataset.draftLimit]].disabled = !toggle.checked; });
const draftPricing = document.createElement("details"); draftPricing.className = "draft-limits";
draftPricing.innerHTML = '<summary>价格与重试</summary><label>输入 USD／百万 Token<input name="input_usd_per_million" value="0" inputmode="decimal" required></label><label>输出 USD／百万 Token<input name="output_usd_per_million" value="0" inputmode="decimal" required></label><label>价格依据<select name="pricing_status"><option value="unknown">尚未确认价格</option><option value="priced">已填写单价（估算）</option><option value="free">确认免费</option></select></label><label>最大尝试<input name="max_attempts" value="3" inputmode="numeric" required></label>';
$("#admin-model-form .resource-actions").before(draftPricing);
const reasoningField = document.createElement("label"); reasoningField.id = "admin-model-reasoning-field"; reasoningField.hidden = true;
reasoningField.innerHTML = '<span>思考强度</span><select id="admin-model-reasoning"><option value="">不设置</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select>';
$("#admin-model-form .resource-actions").before(reasoningField);
let reasoningCapabilityGeneration = 0;
let reasoningCapabilityTimer = null;
const applyReasoningProfile = (profile) => {
  const select = $("#admin-model-reasoning");
  const selected = select.value;
  const efforts = Array.isArray(profile?.efforts) ? profile.efforts.filter((item) => typeof item === "string") : [];
  select.replaceChildren(new Option("不设置", ""), ...efforts.map((effort) => new Option({low:"低",medium:"中",high:"高",xhigh:"极高"}[effort] || effort, effort)));
  reasoningField.hidden = profile?.supported !== true || efforts.length === 0;
  select.value = efforts.includes(selected) ? selected : "";
  if (profile?.revision) reasoningField.dataset.profileRevision = String(profile.revision);
  else delete reasoningField.dataset.profileRevision;
};
const updateReasoningField = async () => {
  const generation = ++reasoningCapabilityGeneration;
  const model = $("#admin-model-name").value.trim();
  const baseUrl = $("#admin-model-url").value.trim();
  reasoningField.hidden = true;
  $("#admin-model-reasoning").value = "";
  delete reasoningField.dataset.profileRevision;
  if (!csrfToken || !model) return;
  const query = new URLSearchParams({model, provider_type: "openai_compatible"});
  if (baseUrl) query.set("base_url", baseUrl);
  try {
    const profile = await request(`/api/config/model-capabilities?${query}`);
    if (generation !== reasoningCapabilityGeneration || !csrfToken) return;
    applyReasoningProfile(profile);
  } catch {
    if (generation !== reasoningCapabilityGeneration) return;
    reasoningField.hidden = true;
    $("#admin-model-reasoning").value = "";
  }
};
const scheduleReasoningFieldUpdate = () => {
  reasoningField.hidden = true;
  $("#admin-model-reasoning").value = "";
  clearTimeout(reasoningCapabilityTimer);
  reasoningCapabilityTimer = setTimeout(() => void updateReasoningField(), 250);
};
$("#admin-model-name").addEventListener("input", scheduleReasoningFieldUpdate);
$("#admin-model-url").addEventListener("input", scheduleReasoningFieldUpdate);
$("#admin-model-form").addEventListener("input", () => { $("#admin-model-form").dataset.dirty = "true"; });
window.addEventListener("beforeunload", (event) => { if (document.querySelector("form[data-dirty]")) { event.preventDefault(); event.returnValue = ""; } });
addParameterHelp($("#admin-model-form"));
addParameterHelp($("#retention-settings-form"));
restoreSession();
$("#refresh-observability").replaceWith($("#refresh-observability").cloneNode(true));
$("#refresh-observability").addEventListener("click", () => { void loadUsage(); });
$("#usage-window").addEventListener("change", () => { void loadUsage(); });
$("#usage-resource").addEventListener("change", () => { void loadUsage(); });
$("#usage-pool").addEventListener("change", () => { usageResourcePage = 0; $("#usage-resource").value = ""; void loadUsageResources().then(loadUsage); });
for (const [id, step] of [["usage-resource-prev",-50],["usage-resource-next",50]]) $("#" + id).addEventListener("click", () => { usageResourcePage = Math.max(0, usageResourcePage + step); void loadUsageResources().then(loadUsage); });
$("#provider-health").addEventListener("click", (event) => { const button = event.target.closest("[data-health-page]"); if (button) { healthPages[button.dataset.healthPage] = Math.max(0, healthPages[button.dataset.healthPage] + Number(button.dataset.step)); void loadProviderHealth(); } });

// Explicit public snapshots never fetch or copy private source text.
let publicationRows = new Map(),
    publicationCursor = null,
    publicationEditor = null;
let publicationWriting = false;
let publicationPending = null;
const publicationKindLabels = {
    goal: "目标",
    project: "项目",
    prediction: "预测",
    research: "研究",
    sleep: "睡眠",
    update: "公开更新",
};
function clearPublicPublications() {
    publicationRows.clear();
    publicationCursor = null;
    publicationEditor = null;
    publicationWriting = false;
    publicationPending = null;
    $("#publication-list").replaceChildren();
    $("#publication-form").reset();
    for (const key of ["phases", "sources"]) $("#publication-" + key).replaceChildren();
    for (const control of $("#publication-form").querySelectorAll(
        "input,textarea,select,button",
    ))
        control.disabled = false;
    publicationFields();
    $("#publication-kind").disabled = false;
    $("#publication-source").disabled = false;
    $("#publication-editor-heading").textContent = "新建公开摘要";
    $("#publication-more").hidden = true;
}
function publicationFields() {
    const kind = $("#publication-kind").value;
    const blocked = publicationWriting || Boolean(publicationPending);
    $("#publication-form").querySelectorAll("input,textarea,select,button").forEach(node => node.disabled = blocked);
    document.querySelectorAll("[data-publication-field]").forEach((node) => {
        node.hidden = !node.dataset.publicationField.split(" ").includes(kind);
        node.querySelectorAll("input,textarea,select,button").forEach(
            (input) => (input.disabled = node.hidden || blocked),
        );
    });
    $("#publication-kind").disabled = blocked || Boolean(publicationEditor?.revision);
    $("#publication-source").disabled = blocked || Boolean(publicationEditor?.revision) || kind === "update";
    $("#publication-new").disabled = blocked;
    $("#publication-reconcile").hidden = !publicationPending;
    $("#publication-reconcile").disabled = publicationWriting;
    const submit = $("#publication-form").querySelector('button[type="submit"]');
    if (submit) submit.disabled = publicationWriting || Boolean(publicationEditor?.invalidBase);
}
function addPublicationEntry(key, item = {}) {
    const container = $("#publication-" + key), field = key === "sources" ? "url" : "status";
    if (container.children.length >= 10) throw new Error("最多填写10个公开阶段或参考链接");
    const row = document.createElement("div");
    row.className = "publication-entry";
    row.innerHTML = `<label>名称<textarea data-public-entry="title" maxlength="500" rows="2" required></textarea></label><label>${key === "sources" ? "HTTPS链接" : "状态"}<textarea data-public-entry="${field}" maxlength="500" rows="2" required></textarea></label><button type="button" data-public-entry-remove>移除此项</button>`;
    row.querySelector('[data-public-entry="title"]').value = item.title || "";
    row.querySelector(`[data-public-entry="${field}"]`).value = item[field] || "";
    container.append(row);
    publicationFields();
}
function publicListEntries(key) {
    const field = key === "sources" ? "url" : "status";
    return [...$("#publication-" + key).querySelectorAll(".publication-entry")].map(row => {
        const title = publicationTrim(row.querySelector('[data-public-entry="title"]').value), value = publicationTrim(row.querySelector(`[data-public-entry="${field}"]`).value);
        if (!title || !value) throw new Error("请完整填写名称和状态或HTTPS链接，或移除空项");
        return {title, [field]: value};
    });
}
function collectPublicationDetails() {
    const kind = $("#publication-kind").value,
        details = {};
    if ($("#publication-status").value)
        details.status = publicationTrim($("#publication-status").value);
    const text = (field, key) => {
        const value = publicationTrim($("#publication-" + field).value);
        if (value) details[key] = value;
    };
    const percent = (field, key) => {
        const value = $("#publication-" + field).value;
        if (value !== "") {
            const number = Number(value);
            if (!Number.isFinite(number) || number < 0 || number > 100)
                throw new Error("公开进度或概率须为0至100");
            const original = publicationEditor?.details?.[key];
            details[key] = original != null && String(original * 100) === String(value) ? original : number / 100;
        }
    };
    const timestamp = (field, key) => {
        const value = $("#publication-" + field).value;
        if (value) {
            details[key] = publicationTimestamp(value);
        }
    };
    if (["goal", "project"].includes(kind)) {
        percent("progress", "progress");
        text("next", "next_step");
    }
    if (kind === "project") {
        text("resources", "resource_state");
        const phases = publicListEntries("phases");
        if (phases.length || publicationEditor?.details?.phases) details.phases = phases;
    }
    if (kind === "prediction") {
        percent("probability", "probability");
        timestamp("target", "target_at");
        text("outcome", "outcome");
    }
    if (kind === "research") {
        text("uncertainty", "uncertainty");
        const sources = publicListEntries("sources");
        for (const source of sources) {
            const url = new URL(source.url);
            if (url.protocol !== "https:" || url.username || url.password)
                throw new Error("参考资料必须为不含凭证的HTTPS链接");
        }
        if (sources.length || publicationEditor?.details?.sources) details.sources = sources;
    }
    if (kind === "sleep") {
        timestamp("start", "started_at");
        timestamp("end", "completed_at");
        text("reason", "reason_category");
        if (
            details.started_at &&
            details.completed_at &&
            details.started_at > details.completed_at
        )
            throw new Error("结束时间不能早于开始时间");
    }
    return details;
}
function publicationError(error) {
    return (
        {
            public_publication_conflict:
                "修订版本已变化。草稿已保留，请刷新列表后重新打开最新摘要并核对，不能覆盖他人修订。",
            invalid_public_publication:
                "公开字段未通过校验。请检查字符长度、关联归属、参考资料与时间。",
            public_publication_not_found:
                "摘要或关联记录不存在，或不属于当前主体。",
            public_publication_capacity_full:
                "公开摘要或修订容量已满。已有摘要仍可撤回；请先由管理员审查容量，勿反复提交。",
            public_content_unavailable:
                "公开内容完整性检查暂未通过，请稍后重试。",
        }[error.code] || errorText(error)
    );
}
async function loadPublicPublications({ append = false } = {}) {
    if (!csrfToken) return;
    const current = beginAdminRead("publications");
    const cursor = append ? publicationCursor : null;
    const path =
        "/api/admin/public-publications?view=management&limit=20" +
        (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
    try {
        const page = await adminRead(current, path);
        if (!current()) return;
        if (!append) publicationRows.clear();
        for (const row of page.items) publicationRows.set(row.id, row);
        publicationCursor = page.next_cursor;
        $("#publication-list").innerHTML = publicationRows.size
            ? [...publicationRows.values()]
                  .map(
                      (row) =>
                          `<article class="resource-card"><div class="card-heading"><h3>${esc(row.title)}</h3><span>${esc(publicationKindLabels[row.kind])} · ${row.status === "published" ? "已公开" : "已撤回"} · 修订 ${esc(row.revision)}</span></div><p class="publication-summary">${esc(row.summary)}</p><p class="muted">首次公开 ${esc(formatTimestamp(row.occurred_at))} · 更新 ${esc(formatTimestamp(row.updated_at))}</p><div class="resource-actions"><button type="button" data-publication-edit="${esc(row.id)}">${row.status === "withdrawn" ? "编辑并重新审核" : "编辑摘要"}</button>${row.status === "published" ? `<button type="button" data-publication-withdraw="${esc(row.id)}">撤回公开</button>` : ""}</div></article>`,
                  )
                  .join("")
            : '<p class="muted">尚无公开摘要。内部记录保持私密。</p>';
        $("#publication-more").hidden = !publicationCursor;
        setStatus(
            "#publication-list-status",
            `已显示 ${publicationRows.size} 条`,
        );
    } catch (error) {
        if (!current()) return;
        setStatus("#publication-list-status", publicationError(error), true);
        if (error.status === 401) await logout();
        throw error;
    }
}
function publicationLocalTime(value) {
    return value || "";
}
function publicationTrim(value) {
    // Python str.strip whitespace, excluding controls rejected by _text.
    return value.replace(/^[\t\n\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, "");
}
function publicationTimestamp(value) {
    value = publicationTrim(value);
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
    if (!match || match[1] === "0000") throw new Error("公开时间须包含时区，例如 2026-10-12T10:30:00+08:00 或 2026-10-12T02:30:00Z");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) throw new Error("公开时间无效");
    const zone = match[8], offset = zone === "Z" ? 0 : (zone[0] === "-" ? -1 : 1) * (Number(zone.slice(1,3)) * 60 + Number(zone.slice(4)));
    const local = new Date(date.getTime() + offset * 60000).toISOString().slice(0,19);
    if (local !== `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6] || "00"}`) throw new Error("公开日期或时间无效");
    const iso = date.toISOString();
    if (!/^\d{4}-/.test(iso) || iso.startsWith("0000")) throw new Error("公开时间超出支持范围");
    return iso.replace(/Z$/, "+00:00");
}
function canonicalPublicationDetails(details) {
    const result = {};
    for (const [key,value] of Object.entries(details)) {
        if (["target_at","started_at","completed_at"].includes(key)) result[key] = publicationTimestamp(value);
        else if (["sources","phases"].includes(key)) result[key] = value.map(entry => Object.fromEntries(Object.entries(entry).map(([name,text]) => [name,publicationTrim(text)])));
        else if (["status","next_step","uncertainty"].includes(key)) result[key] = publicationTrim(value);
        else result[key] = value;
    }
    return result;
}
function invalidatePublicationBase(id) {
    if (publicationEditor?.id !== id) return;
    publicationEditor.invalidBase = true;
    $("#publication-editor-heading").textContent = "草稿保留 · 基准修订已失效";
    setStatus("#publication-editor-status", "此摘要已撤回或修订已变化。当前文字保留；请先复制需要保留的修改，再从列表重新打开最新摘要并逐项核对后审核发布。", true);
}
function editPublicPublication(row) {
    if (publicationWriting || publicationPending) return;
    if (
        $("#publication-form").dataset.dirty &&
        !window.confirm("放弃当前未保存的摘要草稿并打开此记录？")
    )
        return;
    const form = $("#publication-form");
    form.reset();
    delete form.dataset.dirty;
    publicationEditor = {
        id: row.id,
        revision: row.revision,
        source: row.source_id,
        kind: row.kind,
        details: row.details,
    };
    $("#publication-kind").value = row.kind;
    $("#publication-source").value = row.source_id || "";
    $("#publication-kind").disabled = true;
    $("#publication-source").disabled = true;
    for (const name of ["title", "summary", "body"])
        $("#publication-" + name).value = row[name] || "";
    const d = row.details || {};
    for (const [field, key] of [
        ["status", "status"],
        ["next", "next_step"],
        ["resources", "resource_state"],
        ["outcome", "outcome"],
        ["uncertainty", "uncertainty"],
        ["reason", "reason_category"],
    ])
        $("#publication-" + field).value = d[key] || "";
    for (const key of ["progress", "probability"])
        $("#publication-" + key).value = d[key] == null ? "" : d[key] * 100;
    for (const [field, key] of [
        ["target", "target_at"],
        ["start", "started_at"],
        ["end", "completed_at"],
    ])
        $("#publication-" + field).value = publicationLocalTime(d[key]);
    for (const key of ["phases", "sources"]) {
        $("#publication-" + key).replaceChildren();
        for (const item of d[key] || []) addPublicationEntry(key, item);
    }
    $("#publication-editor-heading").textContent =
        `编辑公开摘要 · 修订 ${row.revision}`;
    publicationFields();
    $("#publication-title").focus();
}
async function savePublicPublication(event) {
    event.preventDefault();
    if (publicationWriting || !csrfToken || publicationEditor?.invalidBase) return;
    const form = event.currentTarget,
        current = beginAdminAction();
    if (!publicationPending && (!form.reportValidity() || !$("#publication-confirm").checked)) return;
    let details;
    try {
        details = publicationPending?.details || canonicalPublicationDetails(collectPublicationDetails());
    } catch (error) {
        setStatus("#publication-editor-status", publicationError(error), true);
        return;
    }
    if (
        !window.confirm(
            "发布后任何访客都可阅读这些文字和字段。确认已审核且允许匿名公开？",
        )
    )
        return;
    if (!publicationEditor)
        publicationEditor = {
            id: crypto.randomUUID(),
            revision: 0,
            source: $("#publication-source").disabled
                ? null
                : $("#publication-source").value.trim() || null,
            kind: $("#publication-kind").value,
        };
    const editor = publicationEditor,
        payload = publicationPending || {
            publication_id: editor.id,
            kind: editor.kind,
            source_id: editor.source,
            title: publicationTrim($("#publication-title").value),
            summary: publicationTrim($("#publication-summary").value),
            body: publicationTrim($("#publication-body").value),
            details,
            expected_revision: editor.revision,
            confirm_public: true,
        };
    publicationWriting = true;
    publicationFields();
    try {
        const row = await request("/api/admin/public-publications", {
            method: "POST",
            body: JSON.stringify(payload),
        });
        if (!current()) return;
        publicationPending = null;
        publicationEditor = { ...editor, revision: row.revision, details: row.details };
        delete form.dataset.dirty;
        $("#publication-confirm").checked = false;
        $("#publication-editor-heading").textContent =
            `编辑公开摘要 · 修订 ${row.revision}`;
        setStatus(
            "#publication-editor-status",
            "摘要已公开。仅影响公开副本，原始内部记录保持原样。",
        );
        try {
            await loadPublicPublications();
        } catch (error) {
            if (current())
                setStatus(
                    "#publication-editor-status",
                    "发布已成功，但列表刷新失败；不要重复新建。" +
                        publicationError(error),
                    true,
                );
        }
    } catch (error) {
        if (!current()) return;
        const rejected = [400,401,403,404,409,411,413,415,429].includes(error.status);
        if (rejected && editor.revision === 0 && !publicationPending) publicationEditor = null;
        if (error.status === 409 && !publicationPending && editor.revision > 0) invalidatePublicationBase(editor.id);
        if (!rejected || publicationPending) publicationPending = payload;
        setStatus("#publication-editor-status", publicationPending ? "发布结果尚未确认。请核对结果，或保持原内容使用同一编号重试；当前草稿已保留。" : publicationError(error), true);
        if (error.status === 401) await logout();
    } finally {
        if (current()) {
            publicationWriting = false;
            publicationFields();
        }
    }
}
async function withdrawPublicPublication(id) {
    const row = publicationRows.get(id);
    if (!row || publicationWriting || publicationPending || !csrfToken) return;
    if (
        !window.confirm(
            `撤回“${row.title}”的公开展示？不删除原始记录，也不能撤回访客已复制的内容。`,
        )
    )
        return;
    const current = beginAdminAction();
    publicationWriting = true;
    publicationFields();
    try {
        await request(
            `/api/admin/public-publications/${encodeURIComponent(id)}/withdraw`,
            {
                method: "POST",
                body: JSON.stringify({ expected_revision: row.revision }),
            },
        );
        if (!current()) return;
        invalidatePublicationBase(id);
        setStatus("#publication-list-status", "已撤回，正在刷新公开状态。");
        await loadPublicPublications();
    } catch (error) {
        if (current()) {
            if (error.status === 409 || ![400,401,403,404,411,413,415,429].includes(error.status)) invalidatePublicationBase(id);
            setStatus(
                "#publication-list-status",
                publicationError(error),
                true,
            );
            if (error.status === 401) await logout();
        }
    } finally {
        if (current()) { publicationWriting = false; publicationFields(); }
    }
}
async function reconcilePublicPublication() {
    if (!publicationPending || publicationWriting || !csrfToken) return;
    const current = beginAdminAction(), pending = publicationPending;
    publicationWriting = true;
    publicationFields();
    try {
        const result = await request("/api/admin/public-publications?view=management&id=" + encodeURIComponent(pending.publication_id));
        if (!current()) return;
        if (result.item && (result.item.kind !== pending.kind || (result.item.source_id || null) !== pending.source_id)) throw new Error("服务器记录的类型或来源不同，请联系管理员核对，不可覆盖。");
        const normalize = value => Array.isArray(value) ? value.map(normalize) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key,normalize(value[key])])) : value;
        const same = result.item && result.item.id === pending.publication_id && result.item.status === "published" && ["title","summary","body"].every(key => publicationTrim(result.item[key]) === publicationTrim(pending[key])) && JSON.stringify(normalize(canonicalPublicationDetails(result.item.details))) === JSON.stringify(normalize(canonicalPublicationDetails(pending.details)));
        publicationPending = null;
        if (same) publicationEditor = { ...publicationEditor, revision:result.item.revision, details:result.item.details, invalidBase:false };
        else if (!result.item) publicationEditor = null;
        else invalidatePublicationBase(pending.publication_id);
        setStatus("#publication-editor-status", same ? `已确认发布成功，修订 ${result.item.revision}。` : result.item ? "服务器记录与草稿不同。保留原修订，请刷新列表、重新打开最新摘要并核对，不能覆盖他人修订。" : "服务器确认尚无此摘要。草稿已保留，可修改后重新审核发布。");
    } catch (error) {
        if (current()) {setStatus("#publication-editor-status", "仍无法确认结果，保留原编号与草稿。" + publicationError(error), true); if (error.status === 401) await logout();}
    } finally {
        if (current()) {publicationWriting = false; publicationFields();}
    }
}
$("#publication-kind").addEventListener("change", publicationFields);
$("#publication-reconcile").addEventListener("click", () => void reconcilePublicPublication());
$("#publication-form").addEventListener("click", event => {
    if (publicationWriting || publicationPending) return;
    const add = event.target.closest("[data-publication-add]"), remove = event.target.closest("[data-public-entry-remove]");
    try {
        if (add) addPublicationEntry(add.dataset.publicationAdd);
        if (remove) remove.closest(".publication-entry").remove();
        if (add || remove) $("#publication-form").dataset.dirty = "true";
    } catch (error) {setStatus("#publication-editor-status", publicationError(error), true);}
});
$("#publication-form").addEventListener("input", () => {
    $("#publication-form").dataset.dirty = "true";
});
$("#publication-form").addEventListener("submit", (event) => {
    const current = beginAdminAction();
    void savePublicPublication(event).catch((error) => {
        if (current())
            setStatus(
                "#publication-editor-status",
                publicationError(error),
                true,
            );
    });
});
$("#publication-new").addEventListener("click", () => {
    if (publicationWriting || publicationPending) return;
    if (
        $("#publication-form").dataset.dirty &&
        !window.confirm("放弃当前未保存的摘要并新建？")
    )
        return;
    $("#publication-form").reset();
    for (const key of ["phases", "sources"]) $("#publication-" + key).replaceChildren();
    delete $("#publication-form").dataset.dirty;
    publicationEditor = null;
    $("#publication-kind").disabled = false;
    $("#publication-source").disabled = false;
    $("#publication-editor-heading").textContent = "新建公开摘要";
    publicationFields();
});
$("#publication-refresh").addEventListener("click", () => {
    void loadPublicPublications().catch(() => {});
});
$("#publication-more").addEventListener("click", () => {
    if (publicationCursor)
        void loadPublicPublications({ append: true }).catch(() => {});
});
$("#publication-list").addEventListener("click", (event) => {
    const edit = event.target.closest("[data-publication-edit]");
    if (edit) {
        const row = publicationRows.get(edit.dataset.publicationEdit);
        if (row) editPublicPublication(row);
    }
    const withdraw = event.target.closest("[data-publication-withdraw]");
    if (withdraw)
        void withdrawPublicPublication(withdraw.dataset.publicationWithdraw);
});
publicationFields();
