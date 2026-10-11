const { readFileSync } = require("node:fs"),
    { join } = require("node:path"),
    vm = require("node:vm"),
    assert = require("node:assert/strict"),
    { test } = require("node:test");
const source = readFileSync(
    join(__dirname, "../../src/noyra/web/admin.js"),
    "utf8",
);
const block = source.slice(
    source.indexOf("// Explicit public snapshots"),
    source.indexOf('$("#publication-kind").addEventListener'),
);
function context(extra = {}) {
    const nodes = new Map();
    const $ = (selector) => {
        if (!nodes.has(selector))
            nodes.set(selector, {
                value: "",
                innerHTML: "",
                textContent: "",
                disabled: false,
                hidden: false,
                checked: true,
                dataset: {},
                querySelectorAll: () => [],
                focus() {},
                reset() {},
                reportValidity: () => true,
                replaceChildren() {
                    this.innerHTML = "";
                },
            });
        return nodes.get(selector);
    };
    const sandbox = vm.createContext({
        $,
        csrfToken: "session",
        adminSessionGeneration: 0,
        adminReadVersions: new Map(),
        URL,
        window: { confirm: () => true },
        crypto: { randomUUID: () => "public-test-identifier" },
        esc: (value) => String(value ?? "").replaceAll("<", "&lt;"),
        formatTimestamp: String,
        setStatus: (sel, val) => {
            $(sel).textContent = val;
        },
        errorText: (error) => error.code || error.message,
        document: { querySelectorAll: () => [] },
        ...extra,
    });
    vm.runInContext(
        source.slice(
            source.indexOf("function beginAdminRead("),
            source.indexOf("let activeSection ="),
        ),
        sandbox,
    );
    vm.runInContext(block, sandbox);
    $("#publication-kind").value = "goal";
    return {
        sandbox,
        $,
        set: (code) => vm.runInContext(code, sandbox),
        get: (code) => vm.runInContext(code, sandbox),
    };
}
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => {
        resolve = yes;
        reject = no;
    });
    return { promise, resolve, reject };
};
const row = {
    id: "public-row-1",
    kind: "goal",
    title: "Title",
    summary: "Summary",
    body: "Body",
    source_id: null,
    status: "published",
    revision: 2,
    details: { status: "active", progress: 0 },
    occurred_at: "2026-10-12T00:00:00Z",
    updated_at: "2026-10-12T01:00:00Z",
};
test("public summary reads ignore old session and older refreshes", async () => {
    const old = deferred(),
        h = context();
    h.sandbox.request = () => old.promise;
    const pending = h.sandbox.loadPublicPublications();
    h.set('adminSessionGeneration++;csrfToken="new"');
    h.sandbox.request = async () => ({
        items: [{ ...row, title: "Current" }],
        next_cursor: null,
    });
    await h.sandbox.loadPublicPublications();
    old.resolve({ items: [{ ...row, title: "Old" }], next_cursor: null });
    await pending;
    assert.ok(h.$("#publication-list").innerHTML.includes("Current"));
    assert.ok(!h.$("#publication-list").innerHTML.includes("Old"));
});
test("public summary revisions remain attached to draft when list refreshes", async () => {
    const h = context();
    h.sandbox.editPublicPublication(row);
    assert.equal(h.$("#publication-progress").value, 0);
    h.$("#publication-title").value = "Local draft";
    h.sandbox.request = async () => ({
        items: [{ ...row, revision: 3 }],
        next_cursor: null,
    });
    await h.sandbox.loadPublicPublications();
    assert.equal(h.$("#publication-title").value, "Local draft");
    assert.equal(h.get("publicationEditor.revision"), 2);
});
test("unconfirmed publication creates no request", async () => {
    let calls = 0;
    const h = context({
        request: async () => {
            calls++;
        },
        window: { confirm: () => false },
    });
    await h.sandbox.savePublicPublication({
        preventDefault() {},
        currentTarget: h.$("#publication-form"),
    });
    assert.equal(calls, 0);
    assert.equal(h.get("publicationEditor"), null);
});
test("publication network retry keeps identifier and does not duplicate snapshots", async () => {
    const ids = [],
        h = context();
    h.$("#publication-title").value = "Title";
    h.$("#publication-summary").value = "Summary";
    h.sandbox.request = async (path, options) => {
        if (options) {
            ids.push(JSON.parse(options.body));
            throw new Error("network");
        }
        return { items: [], next_cursor: null };
    };
    const event = {
        preventDefault() {},
        currentTarget: h.$("#publication-form"),
    };
    await h.sandbox.savePublicPublication(event);
    await h.sandbox.savePublicPublication(event);
    assert.equal(ids.length, 2);
    assert.equal(ids[0].publication_id, ids[1].publication_id);
    assert.equal(ids[1].expected_revision, 0);
});
for (const outcome of ["success", "error"]) {
    test(`publication ${outcome} cannot write into a newer session`, async () => {
        const pending = deferred(),
            h = context({ request: () => pending.promise });
        const promise = h.sandbox.savePublicPublication({
            preventDefault() {},
            currentTarget: h.$("#publication-form"),
        });
        h.set(
            'adminSessionGeneration++;csrfToken="new";publicationEditor=null',
        );
        h.$("#publication-editor-status").textContent = "new state";
        if (outcome === "success") pending.resolve(row);
        else pending.reject(new Error("old error"));
        await promise;
        assert.equal(
            h.$("#publication-editor-status").textContent,
            "new state",
        );
        assert.equal(h.get("publicationEditor"), null);
    });
}
test("unsupported optional fields do not cross category boundary", () => {
    const h = context();
    h.$("#publication-progress").value = "0";
    h.$("#publication-probability").value = "90";
    assert.equal(h.sandbox.collectPublicationDetails().progress, 0);
    assert.equal(h.sandbox.collectPublicationDetails().probability, undefined);
    h.$("#publication-kind").value = "research";
    h.$("#publication-sources").value = "Source | javascript:alert(1)";
    assert.throws(() => h.sandbox.collectPublicationDetails(), /HTTPS/);
});
test("publication logout clears summaries, drafts and revision identity", () => {
    const h = context();
    h.set(
        'publicationRows.set("id",{});publicationEditor={id:"old",revision:4}',
    );
    h.sandbox.clearPublicPublications();
    assert.equal(h.get("publicationRows.size"), 0);
    assert.equal(h.get("publicationEditor"), null);
    assert.equal(h.$("#publication-list").innerHTML, "");
});
