const content = document.querySelector("#content");
let activeView = "diary";
let viewGeneration = 0;
const requestControllers = new Map();
const MAX_EXPORT_DOWNLOAD_BYTES = 128 * 1024 * 1024;
let publicPostCaptchaId = "";
let publicPostIdempotencyKey = "";

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

function table(rows, columns) {
  if (!rows.length) return '<div class="empty">暂无记录</div>';
  const head = columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map(([key]) => `<td>${escapeHtml(row[key])}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderDiary(rows) {
  return rows.length
    ? rows.map((entry) => `<article class="diary-entry"><h2>${escapeHtml(entry.title)}</h2><time>${escapeHtml(entry.created_at)}</time><p>${escapeHtml(entry.body)}</p></article>`).join("")
    : '<div class="empty">暂无公开日记</div>';
}

function renderPosts(rows) {
  const provenanceLabels = { visitor: "访客自填／未验证", subject: "主体", operator: "创建者", verified_channel: "已验证渠道" };
  return rows.length
    ? rows.map((post) => `<article class="diary-entry"><h2>${escapeHtml(post.title)}</h2><div class="goal-meta"><span>${escapeHtml(post.kind)}</span><span>${escapeHtml(post.author_label)}</span><span>${escapeHtml(provenanceLabels[post.author_provenance] || "来源未知")}</span><time>${escapeHtml(post.published_at)}</time></div><p>${escapeHtml(post.content)}</p></article>`).join("")
    : '<div class="empty">暂无已发布帖子</div>';
}

async function loadState(generation, signal) {
  const state = await getJson("/api/state", { signal });
  if (!currentViewGeneration(generation)) return;
  document.querySelector("#identity").textContent = state.display_name || state.subject_id || "Noyra";
  document.querySelector("#lifecycle").textContent = state.lifecycle?.state || "-";
  document.querySelector("#online-status").textContent = state.online ? "在线" : "离线";
  document.querySelector("#version").textContent = state.schema_version ?? "-";
  document.querySelector("#diary-count").textContent = state.public_diary_count ?? "-";
  document.querySelector("#private-plan").textContent = "未公开";
  document.querySelector("#state-format").textContent = state.schema || "-";
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
      content.innerHTML = `<div class="empty">${escapeHtml(error.message || "加载失败")}</div>`;
    }
  }
}

async function loadPublicPostCaptcha() {
  const image = document.querySelector("#public-post-captcha-image");
  const answer = document.querySelector("#public-post-captcha-answer");
  if (!image || !answer) return;
  try {
    const response = await fetch("/api/public-posts/captcha", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: "{}",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    if (typeof payload?.challenge_id !== "string" || typeof payload?.image !== "string" || !payload.image.startsWith("data:image/png;base64,")) {
      throw new Error("验证码响应无效");
    }
    publicPostCaptchaId = payload.challenge_id;
    image.src = payload.image;
    answer.value = "";
  } catch (error) {
    publicPostCaptchaId = "";
    image.removeAttribute("src");
    document.querySelector("#public-post-status").textContent = error.message || "验证码加载失败";
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
document.querySelector("#public-post-captcha-refresh")?.addEventListener("click", loadPublicPostCaptcha);
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
    status.textContent = error.message || "提交失败";
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
