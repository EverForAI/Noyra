const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = readFileSync(
    join(__dirname, "../../src/noyra/web/participant.js"),
    "utf8",
);
const deferred = () => {
    let resolve;
    const promise = new Promise((r) => (resolve = r));
    return { promise, resolve };
};
const person = {
    entity_id: "entity-alice",
    display_name: "Alice",
    entity_type: "human",
};
function harness(fetch, language = "zh-CN") {
    const nodes = new Map();
    const node = (key) => {
        if (!nodes.has(key))
            nodes.set(key, {
                innerHTML: "",
                dataset: {},
                textContent: "",
                insertAdjacentHTML(position, html) { this.innerHTML += html; },
                replaceChildren() {
                    this.innerHTML = "";
                },
            });
        return nodes.get(key);
    };
    const events = {};
    const sandbox = {
        document: {
            documentElement: { lang: language },
            querySelector: node,
            addEventListener: (name, fn) => (events[name] = fn),
        },
        window: { dispatchEvent() {} },
        fetch,
        AbortController,
        setTimeout,
        clearTimeout,
        console,
        Event,
        crypto: { randomUUID: () => "fixed-request-id" },
        FormData: class {
            constructor(form) {
                return Object.entries(form.values);
            }
        },
    };
    vm.runInNewContext(source, sandbox);
    return { api: sandbox.window.noyraParticipant, node, events };
}
const response = (data) => ({ ok: true, json: async () => data });
test("late anonymous bootstrap cannot replace an established authenticated identity", async () => {
    const pending = deferred();
    let requests = 0;
    const h = harness(() =>
        ++requests === 1
            ? pending.promise
            : Promise.resolve(
                  response({ participant: person, csrf_token: "csrf" }),
              ),
    );
    await h.api.show("account");
    pending.resolve(response({ participant: null }));
    await h.api.ready;
    assert.equal(h.api.current().entity_id, "entity-alice");
    assert.match(h.node("#participant-panel").innerHTML, /Alice/);
});
test("leaving an account view clears private content and refuses late responses", async () => {
    const messages = deferred();
    const h = harness((path) =>
        path.endsWith("messages")
            ? messages.promise
            : Promise.resolve(
                  response({ participant: person, csrf_token: "csrf" }),
              ),
    );
    await h.api.ready;
    const reading = h.api.show("messages");
    await new Promise((r) => setImmediate(r));
    h.api.leave();
    messages.resolve(
        response({
            items: [{ content: "PRIVATE-OTHER-RESPONSE", title: "secret" }],
        }),
    );
    await reading;
    assert.equal(h.node("#participant-panel").innerHTML, "");
});
test("profile text is escaped and browser requests use independent CSRF and cookie scope", async () => {
    const options = [];
    const h = harness(async (path, config) => {
        options.push(config);
        return response({
            participant: {
                ...person,
                display_name: "<img src=x onerror=bad()>",
            },
            csrf_token: "visitor-csrf",
        });
    });
    await h.api.ready;
    await h.api.show("account");
    assert.match(h.node("#participant-panel").innerHTML, /&lt;img/);
    assert.doesNotMatch(h.node("#participant-panel").innerHTML, /<img src=x/);
    assert.equal(h.api.headers()["X-Participant-CSRF"], "visitor-csrf");
    assert.ok(
        options.every(
            (o) => o.credentials === "same-origin" && o.cache === "no-store",
        ),
    );
});
test("uncertain message submission keeps the same identity and draft on retry", async () => {
    const submitted = [];
    const h = harness(async (path, config) => {
        if (config.method === "POST") {
            submitted.push(JSON.parse(config.body));
            throw new TypeError("network unavailable");
        }
        return response(
            path.endsWith("messages")
                ? { items: [] }
                : { participant: person, csrf_token: "csrf" },
        );
    });
    await h.api.ready;
    await h.api.show("messages");
    const form = {
        values: { content: "a private draft" },
        dataset: { participantForm: "messages" },
        reportValidity: () => true,
        querySelectorAll: () => [],
        reset() {
            throw new Error("draft must be preserved");
        },
    };
    for (let n = 0; n < 2; n++)
        await h.events.submit({
            target: { closest: () => form },
            preventDefault() {},
        });
    assert.equal(submitted.length, 2);
    assert.equal(submitted[0].idempotency_key, submitted[1].idempotency_key);
    assert.equal(form.values.content, "a private draft");
    assert.equal(form.dataset.busy, "false");
});

test("anonymous opportunity pagination appends the next bounded page", async () => {
    const paths = [];
    const opportunity = (id) => ({ bounty_id: id, title: id, description: "Detail", acceptance_criteria: ["Criterion"], reward_amount: "10", asset_id: "asset", network_id: "network" });
    const h = harness(async (path) => {
        paths.push(path);
        if (path.includes("opportunities")) return response({ items: [opportunity(path.includes("?") ? "older" : "newer")], next_cursor: path.includes("?") ? null : "next-page" });
        return response({ participant: null });
    });
    await h.api.ready;
    await h.api.show("collaborate");
    assert.match(h.node("#participant-panel").innerHTML, /data-more="opportunities"/);
    const button = { dataset: { more: "opportunities", cursor: "next-page" }, disabled: false, remove() { this.removed = true; } };
    await h.events.click({ target: { closest: () => button } });
    assert.ok(paths.includes("/api/participants/opportunities?before=next-page"));
    assert.match(h.node("#participant-opportunities").innerHTML, /older/);
    assert.equal(button.removed, true);
});
test("notification pages render owner references and mark old updates without scanning", async () => {
    const sent = [];
    const notice = { notification_id: "n-1", kind: "messages", related_id: "reply-1", title: "<unsafe>", status: "reply", read: false };
    const h = harness(async (path, config) => {
        if (config.method === "POST") { sent.push(JSON.parse(config.body)); return response({ ok: true }); }
        if (path.endsWith("session")) return response({ participant: person, csrf_token: "csrf" });
        if (path.includes("notifications")) return response({ items: [notice], next_cursor: path.includes("?") ? null : "older-page" });
        return response({ items: [], next_cursor: null });
    });
    await h.api.ready;
    await h.api.show("mine");
    assert.match(h.node("#participant-panel").innerHTML, /data-more="notifications"/);
    assert.doesNotMatch(h.node("#participant-panel").innerHTML, /<unsafe>/);
    const more = { dataset: { more: "notifications", cursor: "older-page" }, disabled: false, remove() {} };
    await h.events.click({ target: { closest: () => more } });
    assert.match(h.node("#participant-notifications").innerHTML, /data-related="reply-1"/);
    const read = { dataset: { read: "n-1", noticeKind: "messages", related: "reply-1" }, disabled: false };
    await h.events.click({ target: { closest: () => read } });
    assert.deepEqual(sent[0], { notification_id: "n-1", kind: "messages", related_id: "reply-1" });
});

test("all backend payment states have readable Chinese and English explanations", async () => {
    const contract = readFileSync(join(__dirname, "../../src/noyra/wallet/economy_types.py"), "utf8");
    const literal = contract.match(/PaymentOrderStatus = Literal\[([\s\S]*?)\]/)[1];
    const states = [...literal.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    assert.equal(states.length, 12);
    for (const language of ["zh-CN", "en"]) {
        const h = harness(async (path) => {
            if (path.endsWith("session")) return response({ participant: person, csrf_token: "csrf" });
            if (path.endsWith("submissions")) return response({ items: states.map((state, index) => ({
                submission_id: "s-" + index, title: "Work", content: "Evidence", status: "accepted", payment_status: state, created_at: "2026-10-24T00:00:00+00:00",
            })) });
            if (path.endsWith("notifications")) return response({ items: states.map((state, index) => ({
                notification_id: "n-" + index, title: "Work", status: "accepted:" + state, read: true,
            })) });
            return response({ items: [] });
        }, language);
        await h.api.ready;
        await h.api.show("mine");
        const html = h.node("#participant-panel").innerHTML;
        for (const state of states) assert.doesNotMatch(html, new RegExp(">" + state + "<"));
        if (language === "zh-CN") {
            assert.match(html, /付款等待确认/);
            assert.match(html, /付款签名中，尚未广播/);
            assert.match(html, /付款被拒绝/);
            assert.match(html, /已退款/);
        } else {
            assert.match(html, /Payment awaiting approval/);
            assert.match(html, /Signing payment; not yet broadcast/);
            assert.match(html, /Payment rejected/);
            assert.match(html, /Refunded/);
        }
    }
});
