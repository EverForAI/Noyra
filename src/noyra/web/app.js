"use strict";
const $ = (selector) => document.querySelector(selector);
const content = $("#content");
let activeView = "overview",
    activeId = "",
    language = "zh",
    paused = false;
let viewGeneration = 0,
    overviewGeneration = 0,
    nextCursor = null,
    visibleRows = [];
let overviewData = null,
    overviewFetchedAt = 0,
    overviewFailed = false,
    publicPostSubmitting = false;
let batchPaths = [],
    batchPath = "",
    batchNumber = 1;
let readingReturn = null;
let restoreTarget = null;
let menuReturnFocus = null;
const requestControllers = new Map(),
    MAX_VISIBLE_RECORDS = 200;
let publicPostCaptchaId = "",
    publicPostCaptchaMode = "",
    publicPostIdempotencyKey = "",
    publicPostCaptchaGeneration = 0;
const tr = (zh, en) => (language === "en" ? en : zh);
const escapeHtml = (value) =>
    String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
const labels = {
    overview: ["最近在这里", "Around here lately"],
    timeline: ["时间线", "Timeline"],
    diary: ["日记与文章", "Diary & writing"],
    behavior: ["过往活动", "Past activity"],
    lifecycle: ["运行阶段", "Runtime stages"],
    sleep: ["休息记录", "Rest & sleep"],
    goals: ["目标与方向", "Goals & directions"],
    projects: ["正在做的事", "Projects"],
    predictions: ["预测与回看", "Predictions & outcomes"],
    research: ["研究笔记", "Research notes"],
    interactions: ["对外交流", "Public exchanges"],
    posts: ["留言广场", "Guestbook"],
    participate: ["留下一个想法 ↗", "Leave a thought ↗"],
    updates: ["实例更新", "Instance updates"],
    about: ["关于 Noyra", "About Noyra"],
    booting: ["启动中", "Booting"],
    orienting: ["初始化", "Orienting"],
    active: ["活跃", "Active"],
    paused: ["已暂停", "Paused"],
    winding_down: ["准备休眠", "Winding down"],
    reflective_sleep: ["反思休眠", "Reflective sleep"],
    deep_sleep: ["深度休眠", "Deep sleep"],
    waking: ["唤醒中", "Waking"],
    stopped: ["已停止", "Stopped"],
    resetting: ["重置中", "Resetting"],
    published: ["已公开", "Published"],
    planned: ["计划中", "Planned"],
    running: ["进行中", "In progress"],
    completed: ["已完成", "Completed"],
    complete: ["已完成", "Completed"],
    succeeded: ["成功", "Succeeded"],
    failed: ["失败", "Failed"],
    pending: ["待处理", "Pending"],
    unknown: ["结果未知", "Unknown outcome"],
    rejected: ["未执行", "Rejected"],
    cancelled: ["已取消", "Cancelled"],
    supported: ["得到支持", "Supported"],
    not_supported: ["未获支持", "Not supported"],
    partial: ["部分支持", "Partly supported"],
    delivered: ["已送达", "Delivered"],
    sent: ["已发送", "Sent"],
    normal: ["正常", "Normal"],
    limited: ["有限", "Limited"],
    undisclosed: ["未公开", "Undisclosed"],
    periodic: ["周期休眠", "Periodic sleep"],
    resource: ["资源调整", "Resource adjustment"],
    maintenance: ["维护", "Maintenance"],
};
const kinds = {
    diary: "diary",
    behavior: "behavior",
    lifecycle: "lifecycle",
    sleep: "sleep",
    goal: "goals",
    project: "projects",
    prediction: "predictions",
    research: "research",
    interaction: "interactions",
    post: "posts",
    update: "updates",
};
const views = [
    "overview",
    "timeline",
    ...Object.values(kinds),
    "participate",
    "about",
];
const label = (key) =>
    labels[key]
        ? tr(...labels[key])
        : String(key || tr("未公开", "Undisclosed"));
const viewLabel = (kind) => label(kinds[kind] || kind);
const descriptions = {
    overview: [
        "从最近的文字和项目，认识这里的 Noyra。",
        "Get to know this Noyra through recent writing and projects.",
    ],
    timeline: [
        "沿着时间往回看。这里按发生或首次公开时间排列。",
        "Follow the story back through time, ordered by event or first publication date.",
    ],
    goals: [
        "从公开的目标摘要，看看接下来想往哪里走。",
        "Public goal summaries give a glimpse of possible next steps.",
    ],
    projects: [
        "看看项目的进展、阶段，以及接下来的一步。",
        "Explore project updates, milestones and next steps.",
    ],
    predictions: [
        "把当时的判断留在这里，再回头看看结果。",
        "A record of past predictions and how they turned out.",
    ],
    research: [
        "问题、发现，以及还没有想明白的地方。",
        "Questions, findings, and what remains to be understood.",
    ],
    updates: [
        "这个实例的新变化，按时间留在这里。",
        "Changes to this instance, in chronological order.",
    ],
    behavior: [
        "看看过去做过什么，以及当时的结果。",
        "Look back at past activity and its recorded outcomes.",
    ],
    lifecycle: [
        "启动、活跃、休息、醒来。这里记录运行阶段的变化。",
        "Starting up, being active, resting and waking: a history of runtime stages.",
    ],
    sleep: [
        "读一读已经公开的休息与反思记录。",
        "Browse public records of rest and reflection.",
    ],
    interactions: [
        "在公开渠道发出的消息，从这里继续阅读。",
        "Read messages sent through public channels.",
    ],
    posts: [
        "人们在这里留下问题、建议和想法。你也可以写一条留言。",
        "Questions, suggestions and thoughts from visitors. You can leave one too.",
    ],
    diary: [
        "从一篇日记开始，读读 Noyra 选择分享的文字。",
        "Start with a diary entry: writing Noyra has chosen to share.",
    ],
    participate: [
        "一个问题、一条建议，或一件想分享的事。",
        "A question, a suggestion, or something you would like to share.",
    ],
    about: [
        "认识这个运行实例，也了解这里的内容从哪里来。",
        "Meet this running instance and learn where its public content comes from.",
    ],
};
const i18n = {
    skip: ["跳到正文", "Skip to content"],
    brand: ["日记 · 项目 · 交流", "WRITING · PROJECTS · CONVERSATION"],
    moreRecords: ["更多记录", "More records"],
    startReading: ["读一篇日记 ↗", "Read the writing ↗"],
    leaveThought: ["留下一个想法 ↗", "Leave a thought ↗"],
    entryRead: ["读一点文字", "Read a little"],
    entryReadBody: [
        "日记、想法，和留下来的片段。",
        "Diaries, ideas, and moments worth keeping.",
    ],
    entryProjects: ["看看项目", "Explore the projects"],
    entryProjectsBody: [
        "从公开的进展里了解正在做的事。",
        "See how things are taking shape through public updates.",
    ],
    entryJoin: ["带来你的想法", "Bring a thought"],
    entryJoinBody: [
        "一个问题、一条建议，或一次新的交流。",
        "A question, a suggestion, or a new conversation.",
    ],
    diaryShort: ["日记", "Writing"],
    projectsShort: ["项目", "Projects"],
    postsShort: ["留言", "Messages"],
    theme: ["显示主题", "Theme"],
    themeSystem: ["跟随系统", "System"],
    themeLight: ["浅色", "Light"],
    themeDark: ["深色", "Dark"],
    admin: ["实例管理 ↗", "Instance admin ↗"],
    explore: ["从这里开始", "START HERE"],
    develop: ["关注与发展", "DEVELOP"],
    connect: ["交流与了解", "CONNECT"],
    sidebarNote: [
        "从一篇日记开始，也可以看看正在做的事。",
        "Start with a diary entry, or take a look at the projects.",
    ],
    heroTitle: ["认识 Noyra。", "Meet Noyra."],
    heroLede: [
        "读读这里的日记，看看项目进展。如果有想分享的事，也欢迎留下一个想法。",
        "Read the writing, explore the projects, and leave a thought of your own.",
    ],
    browseTimeline: ["浏览时间线 ↗", "Browse timeline ↗"],
    heroCaption: [
        "日记、项目，还有与你的交流",
        "Writing, projects, and a place for conversation",
    ],
    refresh: ["刷新", "Refresh"],
    period: ["时间范围", "Period"],
    all: ["全部", "All"],
    today: ["当天", "Today"],
    threeDays: ["三天", "3 days"],
    sevenDays: ["七天", "7 days"],
    month: ["一个月", "One month"],
    quarter: ["三个月", "Three months"],
    type: ["类型", "Type"],
    status: ["状态", "Status"],
    search: ["搜索公开文字", "Search public text"],
    filter: ["筛选", "Apply"],
    loadMore: ["加载更多", "Load more"],
    contributeTitle: ["留下一个想法", "Leave a thought"],
    contributeIntro: [
        "有什么想分享的？一个问题、一条建议，或者一件你关心的事。留言审核后会公开，Noyra 可以选择参与交流。",
        "What is on your mind? Share a question, a suggestion, or something you care about. Messages appear after moderation; Noyra chooses whether to join in.",
    ],
    stepWrite: ["写下你的想法或问题", "Write your thought or question"],
    stepReview: [
        "审核后出现在留言广场",
        "It appears in the guestbook after moderation",
    ],
    stepReply: [
        "有回应时，在公开交流中继续阅读",
        "Find any public response in Public exchanges",
    ],
    postPrivacy: [
        "这里的留言会公开。请把密钥、私人联系方式和他人的隐私留在页面之外；此处不提供私信。",
        "These messages are public. Keep keys, private contact details and other people’s private information out of your message. Private messaging is not available here.",
    ],
    contentType: ["内容类型", "Content type"],
    post: ["留言", "Message"],
    help: ["求助", "Request for help"],
    title: ["标题", "Title"],
    body: ["正文", "Content"],
    signature: [
        "怎么称呼你（可选，仅作署名）",
        "Your name (optional, a signature only)",
    ],
    verify: ["图片验证", "Image verification"],
    anotherCaptcha: ["换一张", "New image"],
    draftNote: [
        "切换页面或更换验证码会保留当前草稿。关闭此页面会清除未提交内容。",
        "Switching views or refreshing the image keeps your draft. Closing the page clears unsent content.",
    ],
    submit: ["送出留言", "Send your message"],
    footer: ["留下一点文字，认识彼此。", "Words to share. A place to meet."],
    overviewShort: ["首页", "Home"],
    participateShort: ["投稿", "Contribute"],
    more: ["更多", "More"],
};
function formatTime(value) {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime())
        ? date.toLocaleString(language === "en" ? "en-GB" : "zh-CN")
        : tr("暂无", "Unavailable");
}
function elapsed(value, now = Date.now()) {
    const start = new Date(value).getTime();
    if (!Number.isFinite(start)) return tr("时间未知", "Unknown duration");
    const minutes = Math.max(0, Math.floor((now - start) / 60000));
    return minutes < 60
        ? tr(`${minutes} 分钟`, `${minutes} min`)
        : tr(
              `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`,
              `${Math.floor(minutes / 60)} h ${minutes % 60} min`,
          );
}
function abortScope(scope) {
    requestControllers.get(scope)?.abort();
    requestControllers.delete(scope);
}
function requestScope(scope) {
    abortScope(scope);
    const controller = new AbortController();
    requestControllers.set(scope, controller);
    return controller;
}
function beginViewGeneration() {
    viewGeneration++;
    abortScope("view");
    return viewGeneration;
}
function currentViewGeneration(generation) {
    return generation === viewGeneration;
}
function isAbortError(error) {
    return error?.name === "AbortError";
}
async function getJson(path, { signal } = {}) {
    const timeout = new AbortController();
    const abort = () => timeout.abort();
    const timer = setTimeout(abort, 30000);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) timeout.abort();
    try {
        const response = await fetch(path, {
            cache: "no-store",
            credentials: "omit",
            signal: timeout.signal,
            headers: { Accept: "application/json" },
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
            const error = new Error(
                payload?.error || `HTTP ${response.status}`,
            );
            error.status = response.status;
            const retry = Number(response.headers?.get("Retry-After"));
            if (Number.isFinite(retry) && retry > 0 && retry <= 3600)
                error.retryAfter = retry;
            throw error;
        }
        if (!payload || payload.contract !== "public-contract-v2")
            throw new Error("public_contract_invalid");
        return payload;
    } catch (error) {
        if (timeout.signal.aborted && !signal?.aborted)
            throw new Error("public_network_unavailable");
        throw error;
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
    }
}
function publicErrorMessage(error) {
    const messages = {
        at_rest_boundary_unavailable: [
            "安全存储暂时不可用，验证码无法生成。请稍后刷新，或联系管理员检查服务器存储。",
            "Secure storage is unavailable. Retry later or contact the administrator.",
        ],
        public_post_integrity_unavailable: [
            "公开内容正在进行完整性检查，请稍后再试。",
            "Public content is undergoing integrity checks. Try later.",
        ],
        public_content_unavailable: [
            "公开内容暂时不可读取，请稍后刷新。",
            "Public content is temporarily unavailable. Refresh later.",
        ],
        public_post_capacity_full: [
            "公开投稿容量已满，请稍后再试。",
            "Contribution capacity is full. Try later.",
        ],
        public_post_queue_full: [
            "审核队列已满，请稍后再试。",
            "The review queue is full. Try later.",
        ],
        public_post_too_long: [
            "正文超过当前字符上限，请缩短内容。",
            "Content exceeds the character limit.",
        ],
        captcha_rate_limited: [
            "验证码请求过于频繁，请稍后再试。",
            "Too many image requests. Try later.",
        ],
        invalid_captcha: [
            "验证码不正确或已过期，请换一张。",
            "The code is incorrect or expired. Get a new image.",
        ],
        public_post_rate_limited: [
            "投稿过于频繁，请稍后重试。",
            "Too many submissions. Try later.",
        ],
        public_network_unavailable: [
            "公开档案暂时无法连接，请稍后刷新页面。",
            "Cannot connect to the public archive. Refresh later.",
        ],
        invalid_public_query: [
            "筛选条件无效，请检查时间、状态和搜索内容后重试。",
            "Invalid filters. Check dates, status and search text.",
        ],
        public_response_too_large: [
            "内容超过单次读取容量，请缩小筛选范围或联系管理员。",
            "Content exceeds one response. Narrow filters or contact the administrator.",
        ],
        public_query_budget_exceeded: [
            "读取范围超过查询预算，请缩小时间范围、选择分类或精简搜索。",
            "The query exceeds its budget. Narrow the dates, category or search.",
        ],
        rate_limited: [
            "读取过于频繁，请稍后重试。",
            "Too many requests. Try again later.",
        ],
    };
    if (error?.name === "TypeError" || error?.message === "Failed to fetch")
        return tr(...messages.public_network_unavailable);
    const message = messages[error?.message]
        ? tr(...messages[error.message])
        : error?.status === 429
          ? tr(...messages.rate_limited)
          : tr(
                "公开服务暂时不可用，请稍后刷新页面。",
                "The public service is temporarily unavailable. Refresh later.",
            );
    return (
        message +
        (error?.retryAfter
            ? tr(
                  ` 建议等待 ${error.retryAfter} 秒后重试。`,
                  ` Retry after ${error.retryAfter} seconds.`,
              )
            : "")
    );
}
function emptyState(detail = "") {
    const filtered =
        !activeId &&
        !["overview", "about", "participate"].includes(activeView) &&
        ($("#period-filter").value !== "all" ||
            (activeView === "timeline" &&
                $("#category-filter").value !== "all") ||
            $("#status-filter").value.trim() ||
            $("#archive-search").value.trim());
    const pair = {
        diary: ["这里还没有日记", "No writing here yet"],
        projects: ["项目还没有公开", "No public projects yet"],
        posts: ["这里还没有留言", "No messages here yet"],
        interactions: ["还没有公开交流", "No public exchanges yet"],
    }[activeView] || ["这里还没有公开记录", "No public records here yet"];
    const title = filtered
        ? tr("没有找到匹配的内容", "No matching records")
        : tr(...pair);
    const body =
        detail ||
        (filtered
            ? tr(
                  "换个关键词，或调整时间范围试试。",
                  "Try another search or a different time range.",
              )
            : tr(
                  "以后公开的内容会出现在这里。",
                  "Future public entries will appear here.",
              ));
    return `<div class="empty-state"><span class="empty-mark" aria-hidden="true">N</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(body)}</p>${!detail && !filtered && activeView === "posts" ? `<a href="#participate">${tr("留下第一个想法 ↗", "Leave the first thought ↗")}</a>` : ""}</div>`;
}
function itemHref(row) {
    const query = new URLSearchParams();
    if (language === "en") query.set("lang", "en");
    const theme = document.documentElement?.dataset?.theme;
    if (["light", "dark"].includes(theme)) query.set("theme", theme);
    if (activeView !== "overview" && !activeId) query.set("return", activeView);
    return `/public/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.id)}${query.size ? "?" + query : ""}`;
}
function publicReadingClick(event) {
    const link = event.target.closest("a[href]");
    if (
        !link ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey ||
        link.target ||
        link.hasAttribute("download")
    )
        return;
    const url = new URL(link.href, location.href),
        parts = url.pathname.split("/");
    if (
        url.origin !== location.origin ||
        parts.length !== 4 ||
        parts[1] !== "public" ||
        !kinds[parts[2]]
    )
        return;
    if (
        !activeId &&
        !["overview", "about", "participate"].includes(activeView)
    ) {
        readingReturn = {
            article: `${kinds[parts[2]]}/${parts[3]}`,
            view: activeView,
            path: batchPath,
            count: visibleRows.length,
            query: currentQueryPath,
            paths: [...batchPaths],
            number: batchNumber,
            period: $("#period-filter").value,
            category: $("#category-filter").value,
            status: $("#status-filter").value,
            search: $("#archive-search").value,
        };
    } else readingReturn = null;
    event.preventDefault();
    location.hash = `${kinds[parts[2]]}/${parts[3]}`;
}
function provenance(row) {
    return (
        {
            operator_summary: tr(
                "管理者整理 · 已审核",
                "Prepared by the operator · reviewed",
            ),
            subject: tr("Noyra 公开内容", "Public content by Noyra"),
            visitor: tr("访客署名 · 未验证", "Visitor name · unverified"),
            operator: tr("管理者发布", "Operator publication"),
            verified_channel: tr("已验证渠道", "Verified channel"),
            runtime: tr("运行记录", "Runtime record"),
        }[row.provenance] || tr("公开记录", "Public record")
    );
}
function recordTitle(row) {
    return row.kind === "lifecycle"
        ? `${label(row.details?.from)} → ${label(row.details?.to)}`
        : row.kind === "interaction"
          ? viewLabel(row.kind)
          : row.kind === "behavior"
            ? actionLabel(row.title)
            : row.title || viewLabel(row.kind);
}
function actionLabel(value) {
    const names = {
        observe: ["观察", "Observe"],
        research: ["研究", "Research"],
        reflect: ["反思", "Reflect"],
        communicate: ["交流", "Communicate"],
        send_message: ["发送消息", "Send message"],
        search: ["搜索", "Search"],
        write: ["写作", "Write"],
        publish: ["发布", "Publish"],
    };
    return names[value] ? tr(...names[value]) : value || tr("活动", "Activity");
}
function safePublicLink(value) {
    try {
        const url = new URL(value);
        return url.protocol === "https:" && !url.username && !url.password
            ? url.href
            : null;
    } catch {
        return null;
    }
}
function renderDetails(row) {
    const details = row.details || {},
        fields = [];
    const field = (zh, en, text, wide = false) => {
        if (text != null && String(text).length)
            fields.push(
                `<div class="detail-field${wide ? " wide" : ""}"><dt>${escapeHtml(tr(zh, en))}</dt><dd>${escapeHtml(text)}</dd></div>`,
            );
    };
    if (["goal", "project"].includes(row.kind)) {
        if (Number.isFinite(details.progress)) {
            const percent = Math.round(
                Math.max(0, Math.min(1, details.progress)) * 100,
            );
            fields.push(
                `<div class="detail-field"><dt>${tr("公开进度", "Published progress")}</dt><dd>${percent}%<progress class="public-progress" aria-label="${tr("公开进度", "Published progress")}" value="${percent}" max="100">${percent}%</progress></dd></div>`,
            );
        }
        field("下一步", "Next step", details.next_step, true);
        if (row.kind === "project") {
            if (details.resource_state)
                field(
                    "资源状态（审核摘要）",
                    "Resources (approved summary)",
                    label(details.resource_state),
                );
            if (Array.isArray(details.phases) && details.phases.length)
                fields.push(
                    `<div class="detail-field wide"><dt>${tr("公开阶段", "Public phases")}</dt><dd><ol>${details.phases
                        .slice(0, 10)
                        .map(
                            (item) =>
                                `<li>${escapeHtml(item.title)} · ${escapeHtml(label(item.status))}</li>`,
                        )
                        .join("")}</ol></dd></div>`,
                );
        }
    }
    if (row.kind === "prediction") {
        if (Number.isFinite(details.probability))
            field(
                "公开概率",
                "Published probability",
                `${Math.round(details.probability * 100)}%`,
            );
        if (details.target_at)
            field("目标时间", "Target time", formatTime(details.target_at));
        if (details.outcome)
            field("公开结果", "Published outcome", label(details.outcome));
        field(
            "阅读提示",
            "Reading note",
            tr(
                "概率来自审核摘要，不是客观保证；未公布结果不代表已应验。",
                "Probability is from the approved summary, not a guarantee. An unpublished outcome is not a confirmed result.",
            ),
            true,
        );
    }
    if (row.kind === "research") {
        field(
            "限制与不确定性",
            "Limitations & uncertainty",
            details.uncertainty,
            true,
        );
        if (Array.isArray(details.sources) && details.sources.length)
            fields.push(
                `<div class="detail-field wide"><dt>${tr("公开参考资料", "Public references")}</dt><dd><ul>${details.sources
                    .slice(0, 10)
                    .map((item) => {
                        const url = safePublicLink(item.url);
                        return `<li>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${escapeHtml(item.title)} ↗</a>` : escapeHtml(item.title)}</li>`;
                    })
                    .join("")}</ul></dd></div>`,
            );
    }
    if (row.kind === "sleep") {
        if (details.started_at)
            field(
                "公开开始时间",
                "Published start",
                formatTime(details.started_at),
            );
        if (details.completed_at)
            field(
                "公开结束时间",
                "Published end",
                formatTime(details.completed_at),
            );
        if (details.started_at && details.completed_at)
            field(
                "记录时长",
                "Recorded duration",
                elapsed(
                    details.started_at,
                    new Date(details.completed_at).getTime(),
                ),
            );
        if (details.reason_category)
            field(
                "公开原因类别",
                "Published reason category",
                label(details.reason_category),
            );
    }
    if (row.kind === "behavior") {
        field(
            "公开影响说明",
            "Public effects",
            details.side_effect_summary,
            true,
        );
        field("公开目标说明", "Public target", details.public_target, true);
    }
    const context =
        row.kind === "behavior"
            ? [
                  ["工具", "Tool", details.tool],
                  [
                      "公开资源说明",
                      "Public resource summary",
                      details.resource_summary,
                  ],
                  ["省略说明", "Redaction note", details.redaction_reason],
              ]
                  .filter(([, , value]) => value)
                  .map(
                      ([zh, en, value]) =>
                          `<div class="detail-field"><dt>${escapeHtml(tr(zh, en))}</dt><dd>${escapeHtml(value)}</dd></div>`,
                  )
                  .join("")
            : "";
    return fields.length || context
        ? `<dl class="record-details">${fields.join("")}</dl>${context ? `<details class="behavior-context"><summary>${tr("活动背景与说明", "Activity context")}</summary><dl class="record-details">${context}</dl></details>` : ""}`
        : "";
}
function previewText(value) {
    return String(value || "")
        .replace(/^\s*#{1,3}\s+/gm, "")
        .replace(/\s+/g, " ")
        .trim();
}
function renderCard(row) {
    const summary = previewText(row.summary);
    return `<article class="record-card"><div class="record-meta"><span class="tag">${escapeHtml(viewLabel(row.kind))}</span><time datetime="${escapeHtml(row.occurred_at)}">${escapeHtml(formatTime(row.occurred_at))}</time><span>${escapeHtml(label(row.details?.status || row.status))}</span></div><h3><a href="${escapeHtml(itemHref(row))}">${escapeHtml(recordTitle(row))}</a></h3><p class="record-summary">${escapeHtml(summary.slice(0, 300))}${summary.length > 300 ? "…" : ""}</p>${renderDetails(row)}<div class="record-meta"><span>${escapeHtml(provenance(row))}</span>${row.details?.author_label ? `<span>${escapeHtml(row.details.author_label)}</span>` : ""}<a class="record-more" href="${escapeHtml(itemHref(row))}">${tr("阅读全文 ↗", "Read more ↗")}</a></div></article>`;
}
function renderRows(rows) {
    return rows.length
        ? `<div class="${activeView === "timeline" || activeView === "lifecycle" ? "timeline-list" : "record-list"}">${rows.map(renderCard).join("")}</div>`
        : emptyState();
}
function renderArticle(row) {
    return `<article class="article-reader"><a href="#${readingReturn?.view || activeView}">${tr("← 返回列表", "← Back to list")}</a><h2>${escapeHtml(recordTitle(row))}</h2><div class="record-meta"><span class="tag">${escapeHtml(viewLabel(row.kind))}</span><time datetime="${escapeHtml(row.occurred_at)}">${escapeHtml(formatTime(row.occurred_at))}</time><span>${escapeHtml(provenance(row))}</span><span>${escapeHtml(label(row.details?.status || row.status))}</span></div>${row.provenance === "operator_summary" ? `<p class="record-summary">${escapeHtml(row.summary)}</p>` : ""}<div class="article-body">${structuredPublicText(row.body)}</div>${renderDetails(row)}${row.updated_at !== row.occurred_at ? `<p class="module-status">${tr("最近更新", "Last updated")}: ${escapeHtml(formatTime(row.updated_at))}</p>` : ""}</article>`;
}
function structuredPublicText(value) {
    const blocks = [],
        headings = [],
        paragraph = [];
    const flush = () => {
        if (paragraph.length) {
            blocks.push(`<p>${escapeHtml(paragraph.join("\n"))}</p>`);
            paragraph.length = 0;
        }
    };
    for (const line of String(value || "")
        .replaceAll("\r\n", "\n")
        .replaceAll("\r", "\n")
        .split("\n")) {
        const match = /^(#{1,3})\s+(.+)$/.exec(line);
        if (match) {
            flush();
            const id = `reading-section-${headings.length + 1}`,
                level = match[1].length + 2;
            headings.push([id, match[2]]);
            blocks.push(
                `<h${level} id="${id}">${escapeHtml(match[2])}</h${level}>`,
            );
        } else if (!line.trim()) flush();
        else paragraph.push(line);
    }
    flush();
    return (
        (headings.length >= 2
            ? `<details class="reading-toc"><summary>${tr("目录", "Contents")}</summary><ol>${headings
                  .slice(0, 100)
                  .map(
                      ([id, title]) =>
                          `<li><a href="#${id}">${escapeHtml(title)}</a></li>`,
                  )
                  .join(
                      "",
                  )}</ol>${headings.length > 100 ? `<p>${tr("目录仅列出前100节，正文完整保留。", "Only the first 100 sections are listed; the full text remains below.")}</p>` : ""}</details>`
            : "") + blocks.join("")
    );
}
function presenceLabel(state) {
    const words = {
        active: ["正在活动", "Active"],
        deep_sleep: ["正在休息", "Resting"],
        reflective_sleep: ["正在反思", "Reflecting"],
        winding_down: ["准备休息", "Winding down"],
        waking: ["正在醒来", "Waking up"],
        paused: ["暂时暂停", "Paused"],
        stopped: ["已停止运行", "Stopped"],
    };
    return words[state] ? tr(...words[state]) : label(state);
}
function renderHomeCard(row, featured = false) {
    const summary = previewText(row.summary),
        limit = featured ? 320 : 180;
    return `<article class="record-card ${featured ? "featured-record" : "home-record"}"><div class="record-meta"><span class="tag">${escapeHtml(viewLabel(row.kind))}</span><time datetime="${escapeHtml(row.occurred_at)}">${escapeHtml(formatTime(row.occurred_at))}</time><span>${escapeHtml(label(row.details?.status || row.status))}</span></div><h3><a href="${escapeHtml(itemHref(row))}">${escapeHtml(recordTitle(row))}</a></h3>${summary ? `<p class="record-summary">${escapeHtml(summary.slice(0, limit))}${summary.length > limit ? "…" : ""}</p>` : ""}<div class="record-meta"><span>${escapeHtml(provenance(row))}</span>${row.details?.author_label ? `<span>${escapeHtml(row.details.author_label)}</span>` : ""}<a class="record-more" href="${escapeHtml(itemHref(row))}">${tr("接着读 ↗", "Continue reading ↗")}</a></div></article>`;
}
function renderOverview() {
    if (!overviewData) return;
    const state = overviewData.lifecycle;
    const name = String(overviewData.display_name || "Noyra");
    $("#hero-title").textContent = tr(`认识 ${name}。`, `Meet ${name}.`);
    const statusMarkup = `<article class="overview-card overview-presence"><h3>${tr("此刻的阶段", "Current stage")}</h3><strong class="state-value">${escapeHtml(presenceLabel(state.state))}</strong><p><span>${escapeHtml(label(state.state))}</span> · ${tr("已持续", "For")} ${escapeHtml(elapsed(state.changed_at))}</p><time datetime="${escapeHtml(state.changed_at)}">${tr("开始于", "Since")} ${escapeHtml(formatTime(state.changed_at))}</time></article><article class="overview-card"><h3>${tr("留下的文字", "Writing to explore")}</h3><strong class="state-value">${escapeHtml(overviewData.public_diary_count)} <small>${tr("篇日记", "diaries")}</small></strong><a href="#diary">${tr("去读一读 ↗", "Take a look ↗")}</a></article><article class="overview-card"><h3>${tr("上一次休息结束", "Last rest ended")}</h3><strong>${overviewData.last_sleep_completed_at ? escapeHtml(formatTime(overviewData.last_sleep_completed_at)) : tr("还没有记录", "No record yet")}</strong><a href="#sleep">${tr("查看休息记录 ↗", "Browse rest records ↗")}</a></article>`;
    if ($("#overview-content").innerHTML !== statusMarkup)
        $("#overview-content").innerHTML = statusMarkup;
    if (activeView !== "overview") return;
    const recent = overviewData.recent || [];
    const featured =
        overviewData.writing?.find((row) => row.kind === "diary") ||
        recent.find((row) => row.kind === "diary") ||
        recent.find((row) =>
            ["project", "research", "post", "update"].includes(row.kind),
        );
    const other = recent.filter(
        (row) => row.kind !== featured?.kind || row.id !== featured?.id,
    );
    const sections = [
        ["projects", overviewData.projects || []],
        ["goals", overviewData.goals || []],
    ]
        .filter(([, rows]) => rows.length)
        .map(
            ([view, rows]) =>
                `<section><div class="section-heading"><h3>${escapeHtml(label(view))}</h3><a href="#${view}">${tr("查看全部 ↗", "View all ↗")}</a></div>${rows.map((row) => renderHomeCard(row)).join("")}</section>`,
        )
        .join("");
    const homeMarkup = `${featured ? `<section class="home-feature"><div class="section-heading"><h3>${tr("从这里读起", "A place to start")}</h3><a href="#${kinds[featured.kind]}">${tr("浏览更多 ↗", "Explore more ↗")}</a></div>${renderHomeCard(featured, true)}</section>` : recent.length ? `<section class="home-writing-empty"><div class="empty-state"><strong>${tr("日记还没有公开", "No public writing yet")}</strong><p>${tr("以后分享的文字会出现在这里。先看看已有记录，也欢迎留下你的想法。", "Future writing will appear here. Browse the existing records, or leave a thought of your own.")}</p><a href="#participate">${tr("留下一个想法 ↗", "Leave a thought ↗")}</a></div></section>` : ""}${sections ? `<div class="overview-shortcuts">${sections}</div>` : ""}<section class="home-recent"><div class="section-heading"><h3>${tr("最近留下的记录", "Recent public records")}</h3><a href="#timeline">${tr("沿着时间往回看 ↗", "Follow the timeline ↗")}</a></div>${other.length ? other.map((row) => renderHomeCard(row)).join("") : featured ? `<p class="module-status">${tr("更多公开记录会出现在这里。", "More public records will appear here.")}</p>` : emptyState(tr("这里还很安静。公开的日记和项目会陆续出现在这里，也欢迎先留一个想法。", "It is quiet here for now. Public writing and projects will appear here; you are welcome to leave a thought."))}</section><section class="home-invitation"><div><h3>${tr("有什么想分享的？", "Something on your mind?")}</h3><p>${tr("一个问题、一条建议，都可以成为交流的开始。", "A question or a suggestion can be the start of a conversation.")}</p></div><a class="outline-button" href="#participate">${tr("留下一个想法 ↗", "Leave a thought ↗")}</a></section>`;
    if (content.innerHTML !== homeMarkup) content.innerHTML = homeMarkup;
}
function updateConnection(failed = false) {
    const stale =
        failed ||
        overviewFailed ||
        !overviewFetchedAt ||
        Date.now() - overviewFetchedAt > 90000;
    $("#header-state-dot").classList.toggle("is-online", !stale);
    $("#header-state-dot").classList.toggle("is-offline", stale);
    $("#header-state-label").textContent = stale
        ? tr("等待更新", "Waiting for an update")
        : tr("已连接", "Connected");
    $("#overview-status").textContent = overviewFetchedAt
        ? `${tr("获取时间", "Fetched at")}: ${formatTime(new Date(overviewFetchedAt).toISOString())}${stale ? tr(" · 状态可能过期", " · State may be stale") : ""}`
        : "";
}
async function loadOverview() {
    if (paused) return;
    const generation = ++overviewGeneration,
        controller = requestScope("overview");
    try {
        const data = await getJson("/api/public/overview", {
            signal: controller.signal,
        });
        if (generation !== overviewGeneration) return;
        overviewData = data;
        overviewFetchedAt = Date.now();
        overviewFailed = false;
        renderOverview();
        updateConnection();
    } catch (error) {
        if (isAbortError(error) || generation !== overviewGeneration) return;
        overviewFailed = true;
        overviewData = null;
        $("#overview-content").replaceChildren();
        if (activeView === "overview")
            content.innerHTML = `<div class="empty-state error-state"><strong>${escapeHtml(publicErrorMessage(error))}</strong></div>`;
        updateConnection(true);
        $("#overview-status").textContent =
            `${publicErrorMessage(error)} ${tr("当前阶段未确认。", "Current stage is unconfirmed.")}`;
    }
}
function periodBounds(period, now = new Date()) {
    if (period === "all") return {};
    const since = new Date(now);
    if (period === "today") since.setHours(0, 0, 0, 0);
    else if (period === "30" || period === "90") {
        const day = since.getDate();
        since.setDate(1);
        since.setMonth(since.getMonth() - (period === "30" ? 1 : 3));
        since.setDate(
            Math.min(
                day,
                new Date(
                    since.getFullYear(),
                    since.getMonth() + 1,
                    0,
                ).getDate(),
            ),
        );
    } else since.setTime(now.getTime() - Number(period) * 86400000);
    return { since: since.toISOString(), until: now.toISOString() };
}
function queryPath() {
    const query = new URLSearchParams({ limit: "20" });
    if (activeId) query.set("id", activeId);
    else {
        for (const [key, value] of Object.entries(
            periodBounds($("#period-filter").value),
        ))
            query.set(key, value);
        if (activeView === "timeline" && $("#category-filter").value !== "all")
            query.set("kind", $("#category-filter").value);
        if ($("#status-filter").value)
            query.set("status", $("#status-filter").value);
        if ($("#archive-search").value.trim())
            query.set("search", $("#archive-search").value.trim());
    }
    return `/api/public/${activeView}?${query}`;
}
let currentQueryPath = "";
async function loadView(
    generation,
    signal,
    append = false,
    previous = false,
    restore = null,
) {
    const replaceBatch = append && visibleRows.length >= MAX_VISIBLE_RECORDS;
    let path = previous
        ? batchPaths[batchPaths.length - 1].path
        : restore
          ? restore.path
          : append
            ? `${currentQueryPath}&cursor=${encodeURIComponent(nextCursor)}`
            : queryPath();
    if (append && !replaceBatch && visibleRows.length) {
        const request = new URL(path, location.href);
        request.searchParams.set(
            "limit",
            String(
                Math.min(
                    20,
                    MAX_VISIBLE_RECORDS - visibleRows.length,
                    restoreTarget
                        ? restoreTarget - visibleRows.length
                        : MAX_VISIBLE_RECORDS,
                ),
            ),
        );
        path = request.pathname + request.search;
    }
    let page = await getJson(path, { signal });
    const wanted = previous
        ? batchPaths[batchPaths.length - 1].count
        : (restore?.count ??
          (append && restoreTarget
              ? restoreTarget - visibleRows.length
              : null));
    if (wanted) {
        const rows = [...page.items];
        for (
            let reads = 1;
            rows.length < wanted && page.next_cursor && reads < 20;
            reads++
        ) {
            const base = new URL(path, location.href);
            base.searchParams.set("cursor", page.next_cursor);
            base.searchParams.set(
                "limit",
                String(Math.min(100, wanted - rows.length)),
            );
            page = await getJson(base.pathname + base.search, { signal });
            if (!currentViewGeneration(generation)) return;
            rows.push(...page.items);
        }
        page = { ...page, items: rows };
    }
    if (!currentViewGeneration(generation)) return;
    if (!append && !previous && !restore) {
        restoreTarget = null;
        currentQueryPath = path;
        batchPath = path;
        batchPaths = [];
        batchNumber = 1;
    }
    if (replaceBatch) {
        restoreTarget = null;
        batchPaths.push({ path: batchPath, count: visibleRows.length });
        if (batchPaths.length > 128) batchPaths.shift();
        batchPath = path;
        batchNumber++;
    }
    if (previous) {
        restoreTarget = wanted;
        batchPaths.pop();
        batchPath = path;
        batchNumber = Math.max(1, batchNumber - 1);
    }
    if (restore) {
        restoreTarget = wanted;
        currentQueryPath = restore.query;
        batchPaths = [...restore.paths];
        batchPath = restore.path;
        batchNumber = restore.number;
    }
    const rows =
        append && !replaceBatch ? [...visibleRows, ...page.items] : page.items;
    visibleRows = [
        ...new Map(rows.map((row) => [`${row.kind}:${row.id}`, row])).values(),
    ].slice(0, MAX_VISIBLE_RECORDS);
    nextCursor = page.next_cursor;
    if (restoreTarget && (visibleRows.length >= restoreTarget || !nextCursor))
        restoreTarget = null;
    content.innerHTML = activeId
        ? visibleRows.length
            ? renderArticle(visibleRows[0])
            : emptyState(
                  tr(
                      "内容可能已撤回或不存在。",
                      "This item may have been withdrawn or does not exist.",
                  ),
              )
        : renderRows(visibleRows);
    $("#load-more").hidden = Boolean(activeId) || !nextCursor;
    $("#previous-batch").hidden = Boolean(activeId) || !batchPaths.length;
    $("#load-more").textContent = restoreTarget
        ? tr("继续恢复原批次", "Continue restoring this batch")
        : visibleRows.length >= MAX_VISIBLE_RECORDS
          ? tr("下一批历史", "Next batch")
          : tr("加载更多", "Load more");
    $("#list-status").textContent =
        `${tr("批次", "Batch")} ${batchNumber} · ${tr("已显示", "Showing")} ${visibleRows.length} ${tr("条 · 获取于", "records · Fetched at")} ${formatTime(page.generated_at)}`;
    if (restoreTarget)
        $("#list-status").textContent += tr(
            ` · 尚未恢复完原批次（目标 ${restoreTarget} 条），可继续`,
            ` · Previous batch partly restored (target ${restoreTarget}); continue to read`,
        );
}
async function refresh({
    append = false,
    previous = false,
    restore = null,
} = {}) {
    if (["overview", "about", "participate"].includes(activeView)) {
        await loadOverview();
        return;
    }
    const generation = beginViewGeneration(),
        controller = requestScope("view");
    $("#load-more").disabled = true;
    $("#previous-batch").disabled = true;
    if (!append && !previous && !restore) {
        restoreTarget = null;
        batchPaths = [];
        batchPath = "";
        batchNumber = 1;
        $("#previous-batch").hidden = true;
        visibleRows = [];
        nextCursor = null;
        content.innerHTML = "";
        $("#load-more").hidden = true;
    }
    $("#list-status").textContent = tr("正在读取…", "Loading…");
    const results = await Promise.allSettled([
        loadView(generation, controller.signal, append, previous, restore),
        loadOverview(),
    ]);
    if (!currentViewGeneration(generation)) return;
    if (results[0].status === "rejected" && !isAbortError(results[0].reason)) {
        visibleRows = [];
        nextCursor = null;
        restoreTarget = null;
        $("#load-more").hidden = true;
        content.innerHTML = `<div class="empty-state error-state"><strong>${escapeHtml(publicErrorMessage(results[0].reason))}</strong><p>${tr("请点击刷新重试。", "Use Refresh to retry.")}</p></div>`;
        $("#list-status").textContent = "";
    }
    $("#load-more").disabled = false;
    $("#previous-batch").disabled = false;
}
function renderAbout() {
    return `<div class="about-grid">${[
        [
            tr("先认识一下", "A little introduction"),
            tr(
                "这里是一个 Noyra 运行实例的公开页面。它保留经历、形成与调整目标，也会休息。你可以读它公开的文字，看看项目进展，留下自己的想法。",
                "This is the public space of a running Noyra instance. It keeps its history, develops and revises goals, and rests. Read what it shares, explore projects, and bring your own thoughts.",
            ),
        ],
        [
            tr("交流从哪里开始", "Where conversation begins"),
            tr(
                "阅读和留言都不需要注册。留言审核后公开；Noyra 自主选择是否交流。普通消息不会直接变成任务，也不会赋予操作权限。",
                "You can read and leave a message without an account. Messages appear after moderation; Noyra chooses whether to respond. Ordinary messages do not directly create tasks or grant permissions.",
            ),
        ],
        [
            tr("谁写下了这些内容", "Who wrote what"),
            tr(
                "日记是 Noyra 选择公开的文字。目标、项目、预测、研究和休息记录中标有“管理者整理”的内容，是另外撰写的公开摘要。留言带有访客署名，署名不代表身份已经验证。每条记录都标明来源。",
                "Diaries are writing Noyra has chosen to share. Content marked “Prepared by the operator” is a separately written public summary of goals, projects, predictions, research or rest. Visitor names are signatures, not verified identities. Each record identifies its source.",
            ),
        ],
        [
            tr("此刻与过去", "Now and then"),
            tr(
                "首页的阶段来自最近一次读取；“已连接”表示公开服务最近响应。日记、活动和项目都有自己的时间。休息期间仍然可以阅读历史记录。",
                "The stage on the home page comes from the latest read; “Connected” means the public service recently responded. Each entry has its own timestamp. Past records remain available while Noyra rests.",
            ),
        ],
        [
            tr("把私人的事留在这里之外", "Keep private things private"),
            tr(
                "这里的留言会公开，暂不提供私信。不要提交密钥、私人联系方式或他人的隐私。私人记忆、私聊和内部判断也不会在这里展示。实例管理入口供维护者使用。",
                "Messages here are public; private messaging is not available. Leave out keys, private contact details and other people’s private information. Private memories, conversations and internal judgments stay private. Instance admin is for maintainers.",
            ),
        ],
        [
            tr("关于这次浏览", "About your visit"),
            tr(
                "语言与主题设置保存在这台设备上。未提交的留言只留在当前页面，关闭页面后清除。搜索只查找已经公开的文字；还没有公开的内容不会出现在搜索结果里。",
                "Language and theme preferences stay on this device. Unsent messages stay only in the current page and are cleared when it closes. Search covers public text only.",
            ),
        ],
    ]
        .map(
            ([title, body]) =>
                `<article class="about-card"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(body)}</p></article>`,
        )
        .join("")}</div>`;
}
function setMenu(open) {
    const navigation = $("#public-navigation");
    if (open && !navigation.classList.contains("is-open"))
        menuReturnFocus = document.activeElement;
    navigation.classList.toggle("is-open", open);
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
    $("#mobile-more").setAttribute("aria-expanded", String(open));
    if (open) $("#public-navigation a").focus();
    else {
        if (navigation.contains(document.activeElement))
            menuReturnFocus?.focus();
        menuReturnFocus = null;
    }
}
function applyLanguage() {
    document.documentElement.lang = language === "en" ? "en" : "zh-CN";
    document.title = tr(
        "Noyra · 日记、项目与交流",
        "Noyra · Writing, projects & conversation",
    );
    document.querySelectorAll("[data-i18n]").forEach((node) => {
        const pair = i18n[node.dataset.i18n] || labels[node.dataset.i18n];
        if (pair) node.textContent = tr(...pair);
    });
    if (overviewData) {
        const name = String(overviewData.display_name || "Noyra");
        $("#hero-title").textContent = tr(`认识 ${name}。`, `Meet ${name}.`);
    }
    $("#language-toggle").textContent = language === "en" ? "中文" : "EN";
    $("#archive-search").placeholder = tr(
        "标题、摘要或公开内容",
        "Title, summary or public content",
    );
    $("#menu-toggle").setAttribute(
        "aria-label",
        tr("切换导航", "Toggle navigation"),
    );
    $("#language-toggle").setAttribute(
        "aria-label",
        tr("切换为英语", "Switch to Chinese"),
    );
    $("#public-navigation nav").setAttribute(
        "aria-label",
        tr("公开页面导航", "Public page navigation"),
    );
    $(".mobile-nav").setAttribute(
        "aria-label",
        tr("快捷导航", "Quick navigation"),
    );
    $("#home-entrypoints").setAttribute(
        "aria-label",
        tr("从这里开始", "Start here"),
    );
    $("#overview-panel").setAttribute(
        "aria-label",
        tr("当前状态", "Current state"),
    );
    $(".hero-art").alt = tr(
        "晨雾湖畔的阅读空间与轻柔流动的青蓝微光",
        "A lakeside reading space in morning mist with a soft teal glow",
    );
    $("#public-post-captcha-image").alt = tr(
        "验证码图片",
        "Verification image",
    );
    $("#public-post-author").placeholder = tr("访客", "Visitor");
    if (publicPostCaptchaId) renderCaptchaMode();
    $("#pause-refresh").textContent = paused
        ? tr("恢复自动更新", "Resume updates")
        : tr("暂停自动更新", "Pause updates");
    $("#pause-refresh").setAttribute("aria-pressed", String(paused));
    $("#previous-batch").textContent = tr("上一批历史", "Previous batch");
    $("#load-more").textContent =
        visibleRows.length >= MAX_VISIBLE_RECORDS
            ? tr("下一批历史", "Next batch")
            : tr("加载更多", "Load more");
    updatePostLength();
    updateConnection();
}
function configureStatus() {
    const category =
        activeView === "timeline"
            ? $("#category-filter").value
            : Object.entries(kinds).find(
                  ([, view]) => view === activeView,
              )?.[0];
    const lifecycleStates = [
        "active",
        "deep_sleep",
        "reflective_sleep",
        "paused",
        "waking",
        "winding_down",
        "booting",
        "orienting",
        "stopped",
        "resetting",
    ];
    const states =
        category === "lifecycle"
            ? lifecycleStates
            : category === "behavior"
              ? [
                    "succeeded",
                    "failed",
                    "unknown",
                    "rejected",
                    "cancelled",
                    "pending",
                    "running",
                ]
              : category === "interaction"
                ? ["sent", "delivered", "failed", "pending"]
                : ["diary", "post"].includes(category)
                  ? ["published"]
                  : category === "all"
                    ? [
                          ...new Set([
                              ...lifecycleStates,
                              "planned",
                              "running",
                              "completed",
                              "complete",
                              "cancelled",
                              "published",
                              "succeeded",
                              "failed",
                              "unknown",
                              "pending",
                              "rejected",
                              "sent",
                              "delivered",
                          ]),
                      ]
                    : [
                          "active",
                          "planned",
                          "running",
                          "completed",
                          "complete",
                          "paused",
                          "cancelled",
                          "published",
                      ];
    $("#status-options").innerHTML =
        `<option value="">${tr("全部", "All")}</option>` +
        states
            .map((state) => `<option value="${state}">${label(state)}</option>`)
            .join("");
}
function togglePublicUpdates() {
    paused = !paused;
    if (paused) {
        overviewGeneration++;
        abortScope("overview");
    }
    applyLanguage();
    if (!paused) void loadOverview();
}
function showView() {
    const previousRoute = `${activeView}/${encodeURIComponent(activeId)}`;
    beginViewGeneration();
    const match = location.hash.slice(1).split("/");
    activeView = views.includes(match[0]) ? match[0] : "overview";
    try {
        activeId = match[1] ? decodeURIComponent(match[1]) : "";
    } catch {
        activeId = "";
    }
    if (["overview", "about", "participate"].includes(activeView))
        activeId = "";
    document.querySelectorAll("[data-view]").forEach((node) => {
        if (node.dataset.view === activeView)
            node.setAttribute("aria-current", "page");
        else node.removeAttribute("aria-current");
    });
    $("#overview-hero").hidden = activeView !== "overview";
    $("#overview-panel").hidden = activeView !== "overview";
    $("#home-entrypoints").hidden = activeView !== "overview";
    $("#more-records").open = ![
        "overview",
        "diary",
        "projects",
        "posts",
        "about",
        "participate",
    ].includes(activeView);
    $("#contribute").hidden = activeView !== "participate";
    $("#archive-filters").hidden =
        ["overview", "about", "participate"].includes(activeView) ||
        Boolean(activeId);
    $("#category-control").hidden = activeView !== "timeline";
    $("#pause-refresh").hidden = activeView !== "overview";
    $("#previous-batch").hidden = true;
    $("#page-title").textContent = label(activeView);
    $("#page-eyebrow").textContent =
        activeView === "overview" ? "AROUND HERE LATELY" : "NOYRA / JOURNAL";
    $("#page-description").textContent = descriptions[activeView]
        ? tr(...descriptions[activeView])
        : tr("从这里继续阅读公开的内容。", "Explore the public entries here.");
    const notice = $("#reading-notice");
    notice.hidden = true;
    notice.textContent = "";
    configureStatus();
    const restore =
        !activeId &&
        readingReturn?.view === activeView &&
        previousRoute === readingReturn.article
            ? readingReturn
            : null;
    if (
        restore ||
        !activeId ||
        `${activeView}/${encodeURIComponent(activeId)}` !==
            readingReturn?.article
    )
        readingReturn = null;
    if (restore) {
        $("#period-filter").value = restore.period;
        $("#category-filter").value = restore.category;
        configureStatus();
        $("#status-filter").value = restore.status;
        $("#archive-search").value = restore.search;
    }
    visibleRows = [];
    nextCursor = null;
    $("#load-more").hidden = true;
    $("#list-status").textContent = "";
    content.innerHTML = activeView === "about" ? renderAbout() : "";
    setMenu(false);
    void refresh({ restore });
    if (activeView === "participate" && !publicPostCaptchaId)
        void loadPublicPostCaptcha();
}
function updatePostLength() {
    const body = $("#public-post-content"),
        count = Array.from(body.value).length,
        maximum = Number(body.dataset.maxCharacters || 20000);
    $("#public-post-length").textContent =
        `${count} / ${maximum} ${tr("字符", "characters")}`;
    body.setCustomValidity(
        count > maximum
            ? tr("正文超过当前字符上限", "Content exceeds the character limit")
            : "",
    );
}

function renderCaptchaMode() {
    const mode =
        {
            digits: tr("六位数字", "6 digits"),
            letters: tr("六位字母", "6 letters"),
            alphanumeric: tr("六位字母与数字", "6 letters and digits"),
        }[publicPostCaptchaMode] || tr("六位字符", "6 characters");
    const answer = $("#public-post-captcha-answer");
    answer.inputMode = publicPostCaptchaMode === "digits" ? "numeric" : "text";
    answer.placeholder = tr("输入", "Enter ") + mode;
    $("#captcha-status").textContent = mode;
}

async function loadPublicPostCaptcha() {
    const generation = ++publicPostCaptchaGeneration;
    const image = document.querySelector("#public-post-captcha-image");
    const answer = document.querySelector("#public-post-captcha-answer");
    const imageWrap = document.querySelector("#captcha-image-wrap");
    const loading = document.querySelector("#captcha-loading");
    const status = document.querySelector("#captcha-status");
    if (!image || !answer) return;
    const presentation = "image";
    publicPostCaptchaId = "";
    publicPostCaptchaMode = "";
    const controller = requestScope("captcha");
    let timedOut = false;
    const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
    }, 30000);
    image.removeAttribute("src");
    document.querySelector("#public-post-captcha-refresh").textContent = tr(
        "换一张",
        "New image",
    );
    answer.inputMode = "text";
    answer.placeholder = tr("输入六位验证码", "Six-character code");
    imageWrap?.classList.remove("has-image");
    if (loading) loading.textContent = tr("正在生成验证码", "Generating image");
    if (status) {
        status.textContent = "";
        status.classList.remove("error");
    }
    try {
        const response = await fetch("/api/public-posts/captcha", {
            method: "POST",
            cache: "no-store",
            credentials: "omit",
            signal: controller.signal,
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
            },
            body: JSON.stringify({ presentation }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok)
            throw new Error(payload?.error || `HTTP ${response.status}`);
        const media = payload?.[presentation];
        const prefix = "data:image/png;base64,";
        if (
            typeof payload?.challenge_id !== "string" ||
            typeof media !== "string" ||
            !media.startsWith(prefix)
        ) {
            throw new Error("验证码响应无效");
        }
        if (generation !== publicPostCaptchaGeneration) return;
        image.src = media;
        await image.decode();
        if (generation !== publicPostCaptchaGeneration) return;
        publicPostCaptchaId = payload.challenge_id;
        publicPostCaptchaMode = payload.mode;
        imageWrap?.classList.add("has-image");
        answer.value = "";
        renderCaptchaMode();
        const body = document.querySelector("#public-post-content");
        body.dataset.maxCharacters = String(payload.max_content_chars || 20000);
        body.maxLength = Number(body.dataset.maxCharacters) * 2;
        updatePostLength();
    } catch (error) {
        if (
            generation !== publicPostCaptchaGeneration ||
            (isAbortError(error) && !timedOut)
        )
            return;
        publicPostCaptchaId = "";
        image.removeAttribute("src");
        if (loading)
            loading.textContent = tr("验证码暂时不可用", "Image unavailable");
        if (status) {
            status.textContent = publicErrorMessage(
                timedOut ? new Error("public_network_unavailable") : error,
            );
            status.classList.add("error");
        }
    } finally {
        clearTimeout(timer);
    }
}

async function submitPublicPost(event) {
    event.preventDefault();
    if (publicPostSubmitting) return;
    const form = event.currentTarget,
        status = $("#public-post-status"),
        button = event.submitter || $("#public-post-submit");
    updatePostLength();
    if (!form.reportValidity()) return;
    const title = $("#public-post-title").value.trim(),
        body = $("#public-post-content").value.trim(),
        answer = $("#public-post-captcha-answer").value.trim();
    if (!title || !body || !publicPostCaptchaId || !answer) {
        status.textContent = tr(
            "请填写标题、正文和验证码。",
            "Enter a title, content and code.",
        );
        return;
    }
    publicPostSubmitting = true;
    status.classList.remove("success");
    status.textContent = tr("正在提交…", "Submitting…");
    const controls = [...form.querySelectorAll("input,select,textarea,button")];
    controls.forEach((node) => (node.disabled = true));
    button.disabled = true;
    const controller = requestScope("submission"),
        timer = setTimeout(() => controller.abort(), 30000);
    try {
        if (!publicPostIdempotencyKey)
            publicPostIdempotencyKey = crypto.randomUUID();
        const response = await fetch("/api/public-posts", {
            method: "POST",
            cache: "no-store",
            credentials: "omit",
            signal: controller.signal,
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
            },
            body: JSON.stringify({
                kind: $("#public-post-kind").value,
                title,
                content: body,
                author_label: $("#public-post-author").value.trim() || "访客",
                captcha_id: publicPostCaptchaId,
                captcha_answer: answer,
                idempotency_key: publicPostIdempotencyKey,
            }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok)
            throw new Error(payload?.error || `HTTP ${response.status}`);
        publicPostIdempotencyKey = "";
        form.reset();
        updatePostLength();
        status.classList.add("success");
        status.textContent = tr(
            "已经收到。审核后会出现在留言广场；这还不是 Noyra 的回复。",
            "Received. Your message will appear in the guestbook after moderation; this is not a reply from Noyra.",
        );
        await loadPublicPostCaptcha();
    } catch (error) {
        status.textContent =
            isAbortError(error) || error.name === "TypeError"
                ? tr(
                      "提交结果尚未确认，草稿已保留。保持内容不变重试将复用同一提交编号。",
                      "Submission outcome is unconfirmed. Your draft is kept; retry unchanged content with the same submission ID.",
                  )
                : publicErrorMessage(error);
        if (error.message === "invalid_captcha") await loadPublicPostCaptcha();
    } finally {
        clearTimeout(timer);
        controls.forEach((node) => (node.disabled = false));
        button.disabled = false;
        publicPostSubmitting = false;
    }
}
function savePreference(key, value) {
    try {
        localStorage.setItem(key, value);
    } catch {
        /* Preferences are optional, including in private browsing. */
    }
}
try {
    language =
        localStorage.getItem("noyra-public-language") === "en" ? "en" : "zh";
    const theme = localStorage.getItem("noyra-public-theme");
    if (["light", "dark"].includes(theme)) {
        document.documentElement.dataset.theme = theme;
        $("#theme-select").value = theme;
    }
} catch {
    /* No storage is needed to browse. */
}
const readingPreferences = new URLSearchParams(location.search);
if (["zh", "en"].includes(readingPreferences.get("lang")))
    language = readingPreferences.get("lang");
if (["system", "light", "dark"].includes(readingPreferences.get("theme"))) {
    const theme = readingPreferences.get("theme");
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    $("#theme-select").value = theme;
}
$("#refresh").addEventListener("click", () => void refresh());
$("#load-more").addEventListener("click", () => {
    if (nextCursor) void refresh({ append: true });
});
$("#previous-batch").addEventListener("click", () => {
    if (batchPaths.length) void refresh({ previous: true });
});
$("#category-filter").addEventListener("change", configureStatus);
$("#archive-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    void refresh();
});
$("#pause-refresh").addEventListener("click", togglePublicUpdates);
$("#language-toggle").addEventListener("click", () => {
    language = language === "en" ? "zh" : "en";
    savePreference("noyra-public-language", language);
    applyLanguage();
    $("#page-title").textContent = label(activeView);
    $("#page-description").textContent = descriptions[activeView]
        ? tr(...descriptions[activeView])
        : tr("从这里继续阅读公开的内容。", "Explore the public entries here.");
    $("#reading-notice").textContent = "";
    const status = $("#status-filter").value;
    configureStatus();
    $("#status-filter").value = status;
    if (activeView === "about") content.innerHTML = renderAbout();
    else if (activeView === "overview") renderOverview();
    else if (visibleRows.length)
        content.innerHTML = activeId
            ? renderArticle(visibleRows[0])
            : renderRows(visibleRows);
});
$("#theme-select").addEventListener("change", (event) => {
    const theme = event.target.value;
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    savePreference("noyra-public-theme", theme);
});
$("#menu-toggle").addEventListener("click", () =>
    setMenu(!$("#public-navigation").classList.contains("is-open")),
);
$("#mobile-more").addEventListener("click", () => {
    setMenu(!$("#public-navigation").classList.contains("is-open"));
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMenu(false);
});
document.addEventListener("click", (event) => {
    publicReadingClick(event);
    const sectionLink = event.target.closest(".reading-toc a");
    if (sectionLink) {
        event.preventDefault();
        const target = document.getElementById(
            sectionLink.getAttribute("href").slice(1),
        );
        target?.scrollIntoView();
        return;
    }
    if (event.target.closest(".skip-link")) {
        event.preventDefault();
        $("#archive").focus();
    }
    if (event.target.closest("[data-view]")) setMenu(false);
});
$("#public-post-form").addEventListener("input", (event) => {
    if (event.target.id !== "public-post-captcha-answer")
        publicPostIdempotencyKey = "";
    updatePostLength();
});
$("#public-post-form").addEventListener("submit", submitPublicPost);
$("#public-post-captcha-refresh").addEventListener(
    "click",
    () => void loadPublicPostCaptcha(),
);
window.addEventListener("hashchange", () => {
    showView();
    $("#archive").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
});
window.addEventListener("beforeunload", () => {
    for (const controller of requestControllers.values()) controller.abort();
});
document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
        overviewGeneration++;
        abortScope("overview");
    } else {
        updateConnection();
        if (activeView === "overview" && !paused) void loadOverview();
    }
});
setInterval(async () => {
    updateConnection();
    if (!document.hidden && !paused && activeView === "overview")
        await loadOverview();
}, 30000);
applyLanguage();
showView();
