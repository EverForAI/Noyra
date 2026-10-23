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
function harness(fetch) {
    const nodes = new Map();
    const node = (key) => {
        if (!nodes.has(key))
            nodes.set(key, {
                innerHTML: "",
                dataset: {},
                textContent: "",
                replaceChildren() {
                    this.innerHTML = "";
                },
            });
        return nodes.get(key);
    };
    const events = {};
    const sandbox = {
        document: {
            documentElement: { lang: "zh-CN" },
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
