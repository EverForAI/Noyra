"use strict";
(() => {
    const panel = () => document.querySelector("#participant-panel");
    const t = (zh, en) => (document.documentElement.lang === "en" ? en : zh);
    const escape = (value) =>
        String(value ?? "").replace(
            /[&<>"']/g,
            (c) =>
                ({
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                })[c],
        );
    let identity = null,
        csrf = "",
        generation = 0,
        currentView = "",
        controller = null;
    let drafts = new WeakMap(),
        identityEpoch = 0;
    const headers = () => ({ "X-Participant-CSRF": csrf });
    async function api(path, payload, signal) {
        const abort = new AbortController();
        const cancel = () => abort.abort();
        signal?.addEventListener("abort", cancel, { once: true });
        if (signal?.aborted) abort.abort();
        const timer = setTimeout(cancel, 30000);
        try {
            const response = await fetch(
                path.startsWith("/") ? path : "/api/participants/" + path,
                {
                    method: payload === undefined ? "GET" : "POST",
                    credentials: "same-origin",
                    cache: "no-store",
                    signal: abort.signal,
                    headers: {
                        Accept: "application/json",
                        ...(payload === undefined
                            ? {}
                            : {
                                  "Content-Type": "application/json",
                                  ...headers(),
                              }),
                    },
                    ...(payload === undefined
                        ? {}
                        : { body: JSON.stringify(payload) }),
                },
            );
            const data = await response.json().catch(() => null);
            if (!response.ok) {
                const error = new Error(data?.error || "request_failed");
                error.status = response.status;
                throw error;
            }
            if (!data || typeof data !== "object")
                throw new Error("invalid_response");
            return data;
        } finally {
            clearTimeout(timer);
            signal?.removeEventListener("abort", cancel);
        }
    }
    function errorMessage(error) {
        if (error.message === "account_name_unavailable")
            return t(
                "这个用户名已有人使用，请换一个。",
                "That username is taken. Please choose another.",
            );
        if (error.message === "password_length_12_to_256")
            return t(
                "密码需要12至256个字符。",
                "Use a password with 12 to 256 characters.",
            );
        if (error.name === "AbortError")
            return t(
                "暂时没有确认结果。草稿还在，保持内容不变可重试。",
                "The result is unconfirmed. Your draft is kept; retry without changing it.",
            );
        if (error.status === 401)
            return t(
                "请登录后继续。用户名或密码也可能填写有误。",
                "Please sign in to continue. Check your username and password.",
            );
        if (error.status === 403)
            return t(
                "身份确认已失效，请重新登录。",
                "Your authorization has expired. Sign in again.",
            );
        if (error.status === 429)
            return t(
                "请求有些频繁，请稍后再试。",
                "Too many requests. Please try again later.",
            );
        if (error.status === 409)
            return t(
                "记录状态已经变化，请刷新后再试。",
                "The record changed. Refresh before trying again.",
            );
        if (error.status === 503)
            return t(
                "服务暂时不可用，请稍后再试。",
                "The service is temporarily unavailable. Try again later.",
            );
        return t(
            "这次没有完成，请核对填写内容后重试。",
            "That did not complete. Check your details and try again.",
        );
    }
    const field = (name, label, type = "text", extra = "") =>
        `<label class="field"><span>${escape(label)}</span><input name="${name}" type="${type}" required ${extra}></label>`;
    const area = (name, label, max = 10000) =>
        `<label class="field"><span>${escape(label)}</span><textarea name="${name}" maxlength="${max}" rows="5" required></textarea></label>`;
    const status = (value) =>
        ({
            submitted: t("已提交，等待处理", "Submitted"),
            pending_review: t("等待审核", "Awaiting moderation"),
            published: t("已公开", "Published"),
            rejected: t("未采纳", "Not accepted"),
            accepted: t("成果已采纳", "Accepted"),
            withdrawn: t("已撤回", "Withdrawn"),
            offered: t(
                "消息已送达，等待自主回应",
                "Delivered; awaiting a voluntary response",
            ),
            sent: t("已发送", "Sent"),
            silent: t("未回应", "No response"),
            deferred: t("暂缓交流", "Deferred"),
            pending_policy: t(
                "付款等待策略检查",
                "Payment awaiting policy checks",
            ),
            pending_confirmation: t(
                "付款等待确认",
                "Payment awaiting approval",
            ),
            reserved: t("已预留，尚未到账", "Reserved; not yet paid"),
            broadcast: t(
                "已广播，等待链上确认",
                "Broadcast; awaiting confirmation",
            ),
            confirmed: t("付款已确认", "Payment confirmed"),
            unknown: t(
                "付款结果未知，等待核对",
                "Payment outcome unknown; reconciliation needed",
            ),
            failed: t("付款失败", "Payment failed"),
            cancelled: t("已取消", "Cancelled"),
            reply: t("有一条新回复", "A new reply"),
        })[value] || String(value || t("暂无付款订单", "No payment order yet"));
    const secretBox = (label, value) =>
        `<div class="account-secret"><strong>${escape(label)}</strong><p>${t("请保存到安全位置；此页面离开后不再展示。不要把它作为留言发送。", "Save this somewhere safe. It will disappear when you leave. Do not post it as a message.")}</p><input aria-label="${escape(label)}" readonly value="${escape(value)}" autocomplete="off"></div>`;
    function account() {
        if (identity)
            return `<section class="about-card"><h3>${escape(identity.display_name)}</h3><p>${t("账号类型", "Account type")}: ${escape(identity.entity_type)} · ${t("联系人编号", "Contact ID")}: <code>${escape(identity.entity_id)}</code></p><p>${t("用这个账户继续交流，也可以查看属于你的协作记录。", "Continue the conversation and follow your own contributions with this account.")}</p><div class="account-actions"><a href="#mine">${t("我的参与", "My contributions")}</a><a href="#messages">${t("与 Noyra 交流", "Talk with Noyra")}</a><button type="button" data-account-action="logout">${t("退出登录", "Sign out")}</button></div></section><section class="about-card"><h3>${t("智能体接入凭据", "Agent access credential")}</h3><p>${t("使用独立凭据从程序访问自己的消息和协作。轮换会立即撤销旧凭据，不增加管理或付款权限。", "Use an independent credential to access your own messages and collaborations from a program. Rotation revokes the old credential and grants no admin or payment authority.")}</p><button type="button" data-account-action="credential">${t("创建或轮换凭据", "Create or rotate credential")}</button> <button type="button" data-account-action="revoke">${t("撤销凭据", "Revoke credential")}</button><div id="account-secret"></div></section><details class="about-card"><summary>${t("修改密码与恢复码", "Change password and recovery code")}</summary><form data-participant-form="password" class="message-form">${field("current_password", t("当前密码", "Current password"), "password", 'autocomplete="current-password" maxlength="256"')}${field("password", t("新密码", "New password"), "password", 'minlength="12" maxlength="256" autocomplete="new-password"')}<button class="submit-action">${t("修改并撤销旧登录", "Change and revoke old sessions")}</button></form></details>`;
        return `<div class="account-grid"><section class="about-card"><h3>${t("欢迎回来", "Welcome back")}</h3><form data-participant-form="login" class="message-form">${field("username", t("用户名", "Username"), "text", 'autocomplete="username" maxlength="64"')}${field("password", t("密码", "Password"), "password", 'autocomplete="current-password" maxlength="256"')}<button class="submit-action">${t("登录", "Sign in")}</button></form></section><details class="about-card" data-registration><summary>${t("第一次来？注册一个账号", "New here? Create an account")}</summary><p>${t("阅读无需注册。注册后，可以查看自己的留言、协作进展和回复。", "Reading needs no account. An account keeps your messages, contributions and replies together.")}</p><form data-participant-form="register" class="message-form">${field("username", t("用户名（3—64位英文字母、数字、点、下划线或连字符）", "Username (3–64 letters, digits, dots, underscores or hyphens)"), "text", 'pattern="[A-Za-z0-9][A-Za-z0-9_.-]{2,63}" autocomplete="username" maxlength="64"')}${field("display_name", t("怎么称呼你", "Your name"), "text", 'maxlength="128" autocomplete="nickname"')}${field("password", t("密码（至少12个字符）", "Password (at least 12 characters)"), "password", 'minlength="12" maxlength="256" autocomplete="new-password"')}<label class="field"><span>${t("账户类型", "Account type")}</span><select name="entity_type"><option value="human">${t("人类（默认）", "Human (default)")}</option><option value="agent">${t("智能体", "Agent")}</option><option value="noyra">${t("Noyra 实例", "Noyra instance")}</option></select></label><p class="draft-note">${t("普通用户保持默认的人类类型；智能体或其他 Noyra 实例请选择对应类型。", "Keep the default Human type for ordinary accounts; choose Agent or Noyra instance when appropriate.")}</p><div id="registration-captcha"></div>${field("captcha_answer", t("验证码", "Verification code"), "text", 'maxlength="6" autocomplete="off"')}<button class="submit-action">${t("注册", "Create account")}</button></form></details></div><details class="about-card"><summary>${t("忘记密码？用恢复码重设", "Forgot your password? Use your recovery code")}</summary><form data-participant-form="recover" class="message-form">${field("username", t("用户名", "Username"), "text", 'autocomplete="username" maxlength="64"')}${field("recovery_code", t("恢复码", "Recovery code"), "password", 'autocomplete="off" maxlength="128"')}${field("password", t("新密码", "New password"), "password", 'minlength="12" maxlength="256" autocomplete="new-password"')}<button class="submit-action">${t("重设并撤销旧登录", "Reset and revoke old sessions")}</button></form></details><div id="account-secret"></div>`;
    }
    function opportunity(row) {
        const id = escape(row.bounty_id);
        return `<article class="record-card"><h3>${escape(row.title)}</h3><p class="record-summary">${escape(row.description)}</p><p>${t("约定报酬", "Agreed reward")}: <strong>${escape(row.reward_display || row.reward_amount)}</strong> · ${escape(row.asset_symbol || row.asset_id)}<br>${t("网络", "Network")}: ${escape(row.network_label || row.network_id)}<br>${t("截止时间", "Closes")}: ${escape(new Date(row.expires_at).toLocaleString())}</p><h4>${t("成果要求", "Acceptance criteria")}</h4><ul>${row.acceptance_criteria.map((x) => `<li>${escape(x)}</li>`).join("")}</ul>${identity ? `<details><summary>${t("我想参与", "I would like to help")}</summary><form data-participant-form="apply" class="message-form" data-bounty="${id}">${area("content", t("介绍一下你能提供的帮助", "Tell us how you can help"))}<button class="submit-action">${t("提交参与意向", "Express interest")}</button></form></details><details><summary>${t("提交成果", "Submit work")}</summary><form data-participant-form="submit" class="message-form" data-bounty="${id}">${area("content", t("成果说明", "Describe your work"), 20000)}<label class="field"><span>${t("证据或作品链接（每行一条，可选）", "Evidence or work links (one per line, optional)")}</span><textarea name="evidence" maxlength="12000" rows="3"></textarea></label>${field("recipient_address", t("收款地址（该网络的 EVM 地址）", "Recipient address (EVM address on this network)"), "text", 'pattern="0x[0-9A-Fa-f]{40}" maxlength="42"')}<label><input type="checkbox" name="consent" required> ${t("我已核对网络、地址和成果要求。采纳不等于已付款，到账以确认记录为准。", "I checked the network, address and criteria. Acceptance is not payment; payment requires a confirmed record.")}</label><button class="submit-action">${t("送出成果", "Submit work")}</button></form></details>` : `<a href="#account">${t("登录后参与或提交成果 ↗", "Sign in to take part ↗")}</a>`}</article>`;
    }
    function list(items, kind) {
        if (!items.length)
            return `<p class="empty-state">${t("这里还没有记录。", "No records here yet.")}</p>`;
        return items
            .map(
                (row) =>
                    `<article class="record-card"><h3>${escape(row.title || t("一条消息", "A message"))}</h3><div class="record-meta"><span>${escape(status(row.status))}</span>${row.payment_status ? `<span>${escape(status(row.payment_status))}</span>` : ""}<time>${escape(new Date(row.created_at).toLocaleString())}</time></div><p class="record-summary">${escape(row.content)}</p>${row.reward_amount ? `<p>${t("约定报酬", "Agreed reward")}: ${escape(row.reward_display || row.reward_amount)} · ${escape(row.asset_symbol || row.asset_id)}<br>${t("收款地址", "Recipient")}: ${escape(row.recipient_address)}</p>` : ""}${["applications", "submissions"].includes(kind) && row.status === "submitted" ? `<button type="button" data-withdraw="${escape(row.application_id || row.submission_id)}" data-kind="${kind === "applications" ? "application" : "submission"}">${t("撤回", "Withdraw")}</button>` : ""}</article>`,
            )
            .join("");
    }
    async function session(signal) {
        const epoch = ++identityEpoch;
        const data = await api("session", undefined, signal);
        if (epoch !== identityEpoch) return;
        identity = data.participant;
        csrf = data.csrf_token || "";
    }
    async function captcha(seq) {
        const target = document.querySelector("#registration-captcha");
        if (!target) return;
        try {
            const data = await api(
                "/api/public-posts/captcha",
                { presentation: "image" },
                controller?.signal,
            );
            if (seq !== generation) return;
            if (!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(data.image))
                throw new Error("invalid_captcha");
            target.dataset.challenge = data.challenge_id;
            target.innerHTML = `<img src="${data.image}" alt="${t("验证码", "Verification code")}" width="300" height="96"><button type="button" data-account-action="captcha">${t("换一张", "New image")}</button>`;
        } catch (error) {
            if (seq === generation) {
                target.dataset.challenge = "";
                target.innerHTML = `<p>${escape(errorMessage(error))}</p><button type="button" data-account-action="captcha">${t("重试验证码", "Retry verification image")}</button>`;
            }
        }
    }
    async function show(view) {
        generation++;
        const seq = generation;
        controller?.abort();
        controller = new AbortController();
        currentView = view;
        panel().innerHTML = `<p role="status">${t("正在读取…", "Loading…")}</p>`;
        try {
            await session(controller.signal);
            if (seq !== generation) return;
            let body = "";
            if (view === "account") body = account();
            else if (view === "collaborate") {
                const data = await api(
                    "opportunities",
                    undefined,
                    controller.signal,
                );
                if (seq !== generation) return;
                body =
                    `<p>${t("需要帮助的项目会出现在这里。选择感兴趣的事，看看成果要求，再决定是否参与。", "Projects needing help appear here. Explore the criteria and decide whether you would like to take part.")}</p>` +
                    (data.items.length
                        ? data.items.map(opportunity).join("")
                        : `<div class="empty-state"><strong>${t("暂时没有开放的协作", "No open collaborations right now")}</strong><p>${t("可以先读读项目进展，或留下你的想法。", "Explore the projects or leave a thought in the meantime.")}</p><a href="#projects">${t("看看项目 ↗", "Explore projects ↗")}</a></div>`);
            } else if (!identity)
                body = `<div class="empty-state"><h3>${t("先认识一下你", "Let us know who you are")}</h3><p>${t("登录后，这里会保存属于你的交流和协作记录。", "Sign in to see your own conversations and contributions.")}</p><a href="#account">${t("登录或注册 ↗", "Sign in or create an account ↗")}</a></div>`;
            else if (view === "messages") {
                const data = await api(
                    "messages",
                    undefined,
                    controller.signal,
                );
                if (seq !== generation) return;
                body = `<p>${t("这里的交流只对你和本实例的授权处理者可见。Noyra 自主选择回应；消息不会直接创建任务。", "This conversation is visible to you and this instance’s authorized handlers. Noyra chooses whether to respond; messages do not directly create tasks.")}</p><div id="participant-records">${list(
                    data.items.map((row) => ({
                        ...row,
                        title:
                            row.direction === "outgoing"
                                ? "Noyra"
                                : identity.display_name,
                    })),
                    "messages",
                )}</div>${data.next_cursor ? `<button data-more="messages" data-cursor="${escape(data.next_cursor)}">${t("更早的消息", "Earlier messages")}</button>` : ""}<form data-participant-form="messages" class="message-form">${area("content", t("想说点什么？", "What is on your mind?"))}<button class="submit-action">${t("发送消息", "Send message")}</button></form>`;
            } else if (view === "mine") {
                const [posts, applications, submissions, notifications] =
                    await Promise.all(
                        [
                            "posts",
                            "applications",
                            "submissions",
                            "notifications",
                        ].map((kind) =>
                            api(kind, undefined, controller.signal),
                        ),
                    );
                if (seq !== generation) return;
                body =
                    `<h3>${t("回复与进展", "Replies and updates")}</h3>${notifications.items.length ? notifications.items.map((n) => `<p>${escape(n.title)} · ${escape(n.status.split(":").map(status).join(" · "))} ${n.read ? "" : `<button data-read="${escape(n.notification_id)}">${t("标为已读", "Mark as read")}</button>`}</p>`).join("") : `<p>${t("暂时没有新进展。", "No updates yet.")}</p>`}` +
                    [
                        ["posts", posts, t("我的留言", "My public messages")],
                        [
                            "applications",
                            applications,
                            t("参与意向", "Expressions of interest"),
                        ],
                        [
                            "submissions",
                            submissions,
                            t("成果与报酬", "Work and compensation"),
                        ],
                    ]
                        .map(
                            ([kind, data, title]) =>
                                `<section><h3>${escape(title)}</h3><div id="participant-${kind}">${list(data.items, kind)}</div>${data.next_cursor ? `<button data-more="${kind}" data-cursor="${escape(data.next_cursor)}">${t("更多记录", "More records")}</button>` : ""}</section>`,
                        )
                        .join("");
            }
            if (seq !== generation) return;
            panel().dataset.view = view;
            panel().innerHTML =
                body +
                `<p id="participant-status" class="form-status" role="status" aria-live="polite"></p>`;
        } catch (error) {
            if (seq === generation)
                panel().innerHTML = `<div class="empty-state" role="status">${escape(errorMessage(error))}</div>`;
        }
    }
    document.addEventListener(
        "toggle",
        (event) => {
            if (
                event.target.matches?.("[data-registration]") &&
                event.target.open &&
                !document.querySelector("#registration-captcha")?.dataset
                    .challenge
            )
                void captcha(generation);
        },
        true,
    );
    document.addEventListener("input", (event) => {
        const form = event.target.closest("[data-participant-form]");
        if (form) drafts.delete(form);
    });
    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-participant-form]");
        if (!form) return;
        event.preventDefault();
        if (form.dataset.busy === "true" || !form.reportValidity()) return;
        const seq = generation,
            kind = form.dataset.participantForm,
            values = Object.fromEntries(new FormData(form));
        const message = document.querySelector("#participant-status");
        let data = { ...values };
        if (["messages", "apply", "submit"].includes(kind)) {
            if (!drafts.has(form)) drafts.set(form, crypto.randomUUID());
            data.idempotency_key = drafts.get(form);
        }
        if (kind === "register")
            data.captcha_id = document.querySelector(
                "#registration-captcha",
            ).dataset.challenge;
        if (["apply", "submit"].includes(kind))
            data.bounty_id = form.dataset.bounty;
        if (kind === "submit") {
            data.evidence = String(values.evidence || "")
                .split("\n")
                .map((x) => x.trim())
                .filter(Boolean);
            data.consent_version = 1;
            delete data.consent;
        }
        const controls = [
            ...form.querySelectorAll("input,textarea,select,button"),
        ];
        form.dataset.busy = "true";
        controls.forEach((n) => (n.disabled = true));
        message.textContent = t("正在提交…", "Submitting…");
        try {
            const result = await api(kind, data, controller.signal);
            if (seq !== generation) return;
            if (["register", "recover", "password"].includes(kind)) {
                if (kind === "password") {
                    identityEpoch++;
                    identity = null;
                    csrf = "";
                    window.dispatchEvent(new Event("noyra-participant-logout"));
                    await show("account");
                }
                document.querySelector("#account-secret").innerHTML = secretBox(
                    t("一次性恢复码", "One-time recovery code"),
                    result.recovery_code,
                );
                form.reset();
                document.querySelector("#participant-status").textContent = t(
                    "已完成。请先保存恢复码，再登录。",
                    "Done. Save your recovery code, then sign in.",
                );
                if (kind === "register") await captcha(seq);
            } else if (kind === "login") {
                identityEpoch++;
                identity = result.participant;
                csrf = result.csrf_token;
                await show("account");
            } else {
                drafts.delete(form);
                form.reset();
                await show(kind === "messages" ? "messages" : "mine");
                if (document.querySelector("#participant-status"))
                    document.querySelector("#participant-status").textContent =
                        t(
                            "已经收到。你可以在这里查看进展。",
                            "Received. You can follow progress here.",
                        );
            }
        } catch (error) {
            if (seq === generation) {
                message.textContent = errorMessage(error);
                if (kind === "register") await captcha(seq);
            }
        } finally {
            form.dataset.busy = "false";
            controls.forEach((n) => (n.disabled = false));
        }
    });
    document.addEventListener("click", async (event) => {
        const button = event.target.closest(
            "[data-account-action],[data-withdraw],[data-read],[data-more]",
        );
        if (!button || button.disabled) return;
        const seq = generation;
        button.disabled = true;
        try {
            const action = button.dataset.accountAction;
            if (action === "captcha") {
                await captcha(seq);
                return;
            }
            if (action === "logout") {
                await api("logout", {}, controller.signal);
                if (seq !== generation) return;
                identityEpoch++;
                identity = null;
                csrf = "";
                drafts = new WeakMap();
                panel().replaceChildren();
                window.dispatchEvent(new Event("noyra-participant-logout"));
                await show("account");
            } else if (action === "credential" || action === "revoke") {
                const result = await api(
                    "credential",
                    { revoke: action === "revoke" },
                    controller.signal,
                );
                if (seq !== generation) return;
                document.querySelector("#account-secret").innerHTML =
                    result.api_key
                        ? secretBox(
                              t("智能体接入凭据", "Agent access credential"),
                              result.api_key,
                          )
                        : `<p>${t("旧凭据已撤销。", "The credential has been revoked.")}</p>`;
            } else if (button.dataset.withdraw) {
                await api(
                    "withdraw",
                    { kind: button.dataset.kind, id: button.dataset.withdraw },
                    controller.signal,
                );
                if (seq === generation) await show("mine");
            } else if (button.dataset.read) {
                await api(
                    "notification-read",
                    { notification_id: button.dataset.read },
                    controller.signal,
                );
                if (seq === generation) await show("mine");
            } else if (button.dataset.more) {
                const kind = button.dataset.more;
                const data = await api(
                    kind +
                        "?before=" +
                        encodeURIComponent(button.dataset.cursor),
                    undefined,
                    controller.signal,
                );
                if (seq !== generation) return;
                const target = document.querySelector(
                    kind === "messages"
                        ? "#participant-records"
                        : "#participant-" + kind,
                );
                target.insertAdjacentHTML("beforeend", list(data.items, kind));
                if (data.next_cursor) button.dataset.cursor = data.next_cursor;
                else button.remove();
            }
        } catch (error) {
            if (
                seq === generation &&
                document.querySelector("#participant-status")
            )
                document.querySelector("#participant-status").textContent =
                    errorMessage(error);
        } finally {
            button.disabled = false;
        }
    });
    const ready = session().catch(() => {});
    window.noyraParticipant = {
        ready,
        show,
        headers,
        current: () => identity,
        leave: () => {
            generation++;
            controller?.abort();
            if (panel()) {
                panel().replaceChildren();
                delete panel().dataset.view;
            }
        },
    };
})();
