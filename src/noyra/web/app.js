const content = document.querySelector("#content");
let activeView = "diary";
let viewGeneration = 0;
const requestControllers = new Map();
const MAX_EXPORT_DOWNLOAD_BYTES = 128 * 1024 * 1024;
let publicPostCaptchaId = "";
let publicPostIdempotencyKey = "";
let publicPostCaptchaGeneration = 0;
let publicPostCaptchaPresentation = "image";

// Private management routes and forms belong exclusively to /admin.
// These names document the boundary covered by the compatibility contract:
// /api/admin/lifecycle/ /api/admin/actions/ /api/admin/model-calls/
// /api/deliveries/ /api/config/training-policy /api/config/common-knowledge/trust

function abortScope(scope) {
  const controller = requestControllers.get(scope);
  if (controller) {
    controller.abort();
    requestControllers.delete(scope);
  }
}

function beginViewGeneration() {
  viewGeneration += 1;
  abortScope("view");
  return viewGeneration;
}

function requestScope(scope) {
  abortScope(scope);
  const controller = new AbortController();
  requestControllers.set(scope, controller);
  return controller;
}

function currentViewGeneration(generation) {
  return generation === viewGeneration;
}

function isAbortError(error) {
  return error?.name === "AbortError";
}

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function getJson(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    signal: options.signal,
    headers: { Accept: "application/json" },
  });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
  return payload;
}

function publicErrorMessage(error) {
  const messages = {
    at_rest_boundary_unavailable: "安全存储暂时不可用，验证码无法生成。请稍后刷新，或联系管理员检查服务器存储。",
    public_post_integrity_unavailable: "公开内容服务正在进行完整性检查，请稍后再试。",
    public_post_capacity_full: "当前公开投稿容量已满，请稍后再试。",
    public_post_queue_full: "审核队列暂时已满，请稍后再试。",
    captcha_rate_limited: "验证码请求过于频繁，请稍后再试。",
    invalid_captcha: "验证码不正确或已过期，请换一张。",
    public_network_unavailable: "公开档案暂时无法连接，请稍后刷新页面。",
    service_unavailable: "公开服务暂时不可用，请稍后刷新页面。",
  };
  if (error?.message === "Failed to fetch" || error?.name === "TypeError") {
    return messages.public_network_unavailable;
  }
  const message = error?.message;
  if (messages[message]) return messages[message];
  if (typeof message === "string" && (/^HTTP \d+$/i.test(message) || /^[a-z][a-z0-9_]+$/.test(message))) {
    return "公开服务暂时不可用，请稍后刷新页面。";
  }
  return message || "服务暂时不可用，请稍后再试";
}

function table(rows, columns) {
  if (!rows.length) return '<div class="empty">暂无记录</div>';
  const head = columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map(([key]) => `<td>${escapeHtml(row[key])}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderDiary(rows) {
  return rows.length
    ? rows.map((entry) => `<article class="diary-entry"><h2>${escapeHtml(entry.title)}</h2><time>${escapeHtml(entry.created_at)}</time><p>${escapeHtml(entry.body)}</p></article>`).join("")
    : '<div class="empty empty-state"><span class="empty-mark" aria-hidden="true">N</span><strong>新的公开记录尚未出现</strong><p>当 Noyra 留下可公开的日记时，它们会按时间收录在这里。</p></div>';
}

function renderPosts(rows) {
  const provenanceLabels = { visitor: "访客自填／未验证", subject: "主体", operator: "创建者", verified_channel: "已验证渠道" };
  return rows.length
    ? rows.map((post) => `<article class="diary-entry"><h2>${escapeHtml(post.title)}</h2><div class="goal-meta"><span>${escapeHtml(post.kind)}</span><span>${escapeHtml(post.author_label)}</span><span>${escapeHtml(provenanceLabels[post.author_provenance] || "来源未知")}</span><time>${escapeHtml(post.published_at)}</time></div><p>${escapeHtml(post.content)}</p></article>`).join("")
    : '<div class="empty empty-state"><span class="empty-mark" aria-hidden="true">N</span><strong>这里还没有已发布的帖子</strong><p>通过审核的公开投稿会显示在这里。</p></div>';
}

async function loadState(generation, signal) {
  const state = await getJson("/api/state", { signal });
  if (!currentViewGeneration(generation)) return;
  const identity = document.querySelector("#identity");
  const lifecycleNode = document.querySelector("#lifecycle");
  const onlineNode = document.querySelector("#online-status");
  const versionNode = document.querySelector("#version");
  const diaryCountNode = document.querySelector("#diary-count");
  const privatePlanNode = document.querySelector("#private-plan");
  const stateFormatNode = document.querySelector("#state-format");
  if (identity) identity.textContent = state.display_name || state.subject_id || "Noyra";
  if (lifecycleNode) lifecycleNode.textContent = state.lifecycle?.state || "-";
  if (onlineNode) onlineNode.textContent = state.online ? "在线" : "离线";
  if (versionNode) versionNode.textContent = state.schema_version ?? "-";
  if (diaryCountNode) diaryCountNode.textContent = state.public_diary_count ?? "-";
  if (privatePlanNode) privatePlanNode.textContent = "未公开";
  if (stateFormatNode) stateFormatNode.textContent = state.schema || "-";
  const lifecycle = state.lifecycle?.state || "未知";
  const dot = document.querySelector("#header-state-dot");
  const label = document.querySelector("#header-state-label");
  dot?.classList.toggle("is-online", Boolean(state.online));
  dot?.classList.toggle("is-offline", !state.online);
  if (label) label.textContent = state.online ? "在线运行" : "暂时离线";
  const status = document.querySelector("#subject-status");
  if (status) status.textContent = state.online ? "公开状态可读取" : "等待恢复连接";
  const heroLifecycle = document.querySelector("#hero-lifecycle");
  const heroVersion = document.querySelector("#hero-version");
  const heroUpdated = document.querySelector("#hero-updated");
  if (heroLifecycle) heroLifecycle.textContent = lifecycle;
  if (heroVersion) heroVersion.textContent = state.schema_version ?? "-";
  if (heroUpdated) heroUpdated.textContent = state.online ? "刚刚同步" : "暂不可用";
}

async function loadView(generation, signal) {
  if (!currentViewGeneration(generation)) return;
  let rows;
  if (activeView === "diary") {
    rows = await getJson("/api/diary", { signal });
    content.innerHTML = renderDiary(rows);
  } else if (activeView === "behavior") {
    rows = await getJson("/api/behavior", { signal });
    content.innerHTML = table(rows, [["occurred_at", "时间"], ["action_type", "行为"], ["result_status", "结果"], ["public_explanation", "说明"]]);
  } else if (activeView === "interactions") {
    rows = await getJson("/api/interactions", { signal });
    content.innerHTML = table(rows, [["created_at", "时间"], ["direction", "方向"], ["status", "状态"], ["content", "内容"]]);
  } else if (activeView === "posts") {
    rows = await getJson("/api/public-posts", { signal });
    content.innerHTML = renderPosts(rows);
  } else {
    content.innerHTML = '<div class="empty">该内容仅在管理台中提供。</div>';
  }
}

async function refresh() {
  const generation = beginViewGeneration();
  const controller = requestScope("view");
  try {
    await Promise.all([
      loadState(generation, controller.signal),
      loadView(generation, controller.signal),
    ]);
  } catch (error) {
    if (!isAbortError(error) && currentViewGeneration(generation)) {
      content.innerHTML = `<div class="empty empty-state error-state"><span class="empty-mark" aria-hidden="true">!</span><strong>${escapeHtml(publicErrorMessage(error))}</strong><p>请稍后刷新页面。如果问题持续存在，请联系管理员检查公开服务。</p></div>`;
    }
  }
}

async function loadPublicPostCaptcha() {
  const generation = ++publicPostCaptchaGeneration;
  const image = document.querySelector("#public-post-captcha-image");
  const answer = document.querySelector("#public-post-captcha-answer");
  const imageWrap = document.querySelector("#captcha-image-wrap");
  const loading = document.querySelector("#captcha-loading");
  const status = document.querySelector("#captcha-status");
  const audio = document.querySelector("#public-post-captcha-audio");
  const audioWrap = document.querySelector("#captcha-audio-wrap");
  if (!image || !answer) return;
  const presentation = publicPostCaptchaPresentation;
  publicPostCaptchaId = "";
  const controller = requestScope("captcha");
  audio?.pause();
  audio?.removeAttribute("src");
  image.removeAttribute("src");
  if (imageWrap) imageWrap.hidden = presentation === "audio";
  if (audioWrap) audioWrap.hidden = presentation !== "audio";
  document.querySelector(".captcha-row")?.classList.toggle("audio-mode", presentation === "audio");
  document.querySelectorAll("[data-captcha-presentation]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.captchaPresentation === presentation)));
  document.querySelector("#public-post-captcha-refresh").textContent = "换一组";
  answer.inputMode = presentation === "audio" ? "numeric" : "text";
  answer.placeholder = presentation === "audio" ? "输入六位数字" : "输入验证码";
  imageWrap?.classList.remove("has-image");
  if (loading) loading.textContent = "正在生成验证码";
  if (status) { status.textContent = ""; status.classList.remove("error"); }
  try {
    const response = await fetch("/api/public-posts/captcha", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ presentation }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    const media = payload?.[presentation];
    const prefix = presentation === "audio" ? "data:audio/wav;base64," : "data:image/png;base64,";
    if (typeof payload?.challenge_id !== "string" || typeof media !== "string" || !media.startsWith(prefix)) {
      throw new Error("验证码响应无效");
    }
    if (generation !== publicPostCaptchaGeneration) return;
    if (presentation === "audio") {
      audio.src = media;
      audio.load();
    } else {
      image.src = media;
      await image.decode();
    }
    if (generation !== publicPostCaptchaGeneration) return;
    publicPostCaptchaId = payload.challenge_id;
    imageWrap?.classList.add("has-image");
    answer.value = "";
    if (status) status.textContent = presentation === "audio" ? "中文数字音频已就绪" : "图片验证码已就绪";
  } catch (error) {
    if (isAbortError(error) || generation !== publicPostCaptchaGeneration) return;
    publicPostCaptchaId = "";
    image.removeAttribute("src");
    if (loading) loading.textContent = "验证码暂时不可用";
    if (status) { status.textContent = publicErrorMessage(error); status.classList.add("error"); }
  }
}

// Kept as an explicit no-op so the public bundle cannot accidentally grow a privileged action.
function confirmPrivilegedAction() {
  return false;
}

document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  activeView = button.dataset.view;
  refresh();
}));
document.querySelector("#refresh").addEventListener("click", refresh);
document.querySelector("#public-post-captcha-refresh")?.addEventListener("click", () => loadPublicPostCaptcha());
document.querySelectorAll("[data-captcha-presentation]").forEach((button) => button.addEventListener("click", () => {
  if (publicPostCaptchaPresentation === button.dataset.captchaPresentation) return;
  publicPostCaptchaPresentation = button.dataset.captchaPresentation;
  loadPublicPostCaptcha();
}));
document.querySelector("#public-post-captcha-audio")?.addEventListener("error", () => {
  if (publicPostCaptchaPresentation !== "audio") return;
  publicPostCaptchaId = "";
  const status = document.querySelector("#captcha-status");
  if (status) { status.textContent = "音频暂时无法播放，请换一组后重试"; status.classList.add("error"); }
});
document.querySelector("#public-post-form")?.addEventListener("input", (event) => {
  if (event.target.id !== "public-post-captcha-answer") publicPostIdempotencyKey = "";
});
document.querySelector("#public-post-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  const status = document.querySelector("#public-post-status");
  button.disabled = true;
  status.textContent = "";
  try {
    const title = document.querySelector("#public-post-title").value.trim();
    const body = document.querySelector("#public-post-content").value.trim();
    if (!title || !body) throw new Error("标题和内容不能为空");
    const captchaAnswer = document.querySelector("#public-post-captcha-answer").value.trim();
    if (!publicPostCaptchaId || !captchaAnswer) throw new Error("请先输入验证码");
    if (!publicPostIdempotencyKey) publicPostIdempotencyKey = crypto.randomUUID();
    const response = await fetch("/api/public-posts", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        kind: document.querySelector("#public-post-kind").value,
        title,
        content: body,
        author_label: document.querySelector("#public-post-author").value.trim() || "访客",
        captcha_id: publicPostCaptchaId,
        captcha_answer: captchaAnswer,
        idempotency_key: publicPostIdempotencyKey,
      }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    publicPostIdempotencyKey = "";
    form.reset();
    status.textContent = "已提交，等待审核";
    await loadPublicPostCaptcha();
  } catch (error) {
    status.textContent = publicErrorMessage(error);
    if (error.message === "invalid_captcha") {
      await loadPublicPostCaptcha();
    }
  } finally {
    button.disabled = false;
  }
});

refresh();
loadPublicPostCaptcha();
window.addEventListener("beforeunload", () => abortScope("view"));
setInterval(async () => {
  await refresh();
}, 5000);

// Export is admin-only; this constant remains bounded for the shared frontend contract.
// The admin bundle owns the bounded stream (`getReader()`), Retry-After backoff,
// `delayMs = Math.min(delayMs * 2, 5000)`, and the cancel path
// `/api/admin/export-jobs/${encodeURIComponent(jobId)}/cancel`; the public bundle never calls them.
void MAX_EXPORT_DOWNLOAD_BYTES;
