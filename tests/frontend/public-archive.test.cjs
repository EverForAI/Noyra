const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = readFileSync(
    join(__dirname, "../../src/noyra/web/app.js"),
    "utf8",
);
function harness(extra = {}) {
    const nodes = new Map();
    const node = (selector) => {
        if (!nodes.has(selector))
            nodes.set(selector, {
                value: "",
                innerHTML: "",
                textContent: "",
                disabled: false,
                hidden: false,
                dataset: {},
                classList: { toggle() {}, add() {}, remove() {} },
                replaceChildren() {
                    this.innerHTML = "";
                },
                setCustomValidity() {},
            });
        return nodes.get(selector);
    };
    node("#period-filter").value = "all";
    node("#category-filter").value = "all";
    const sandbox = vm.createContext({
        document: { querySelector: node },
        URLSearchParams,
        AbortController,
        Date,
        console,
        ...extra,
    });
    vm.runInContext(
        source.slice(
            0,
            source.indexOf("async function loadPublicPostCaptcha("),
        ),
        sandbox,
    );
    return {
        sandbox,
        nodes,
        node,
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
const record = (id = "example") => ({
    id,
    kind: "diary",
    title: "Public title",
    body: "Text",
    summary: "Text",
    details: {},
    status: "published",
    occurred_at: "2026-10-12T01:00:00+00:00",
    updated_at: "2026-10-12T01:00:00+00:00",
    provenance: "subject",
});
const page = (items) => ({
    contract: "public-contract-v2",
    generated_at: "2026-10-12T01:01:00+00:00",
    items,
    next_cursor: null,
});
for (const view of [
    "diary",
    "behavior",
    "interactions",
    "posts",
    "goals",
    "lifecycle",
]) {
    for (const outcome of ["success", "error"]) {
        test(`${view} late ${outcome} cannot overwrite new navigation`, async () => {
            const pending = deferred();
            const h = harness();
            h.set(
                `activeView=${JSON.stringify(view)};loadOverview=async()=>{};`,
            );
            h.sandbox.getJson = () => pending.promise;
            const old = h.sandbox.refresh();
            h.set('activeView="posts"');
            h.sandbox.getJson = async () => page([record("new-record")]);
            await h.sandbox.refresh();
            const before = h.node("#content").innerHTML;
            assert.ok(before.includes("new-record"));
            if (outcome === "success")
                pending.resolve(page([record("old-record")]));
            else pending.reject(new Error("old error"));
            await old;
            assert.equal(h.node("#content").innerHTML, before);
        });
    }
}
test("overview failure cannot hide independently successful archive", async () => {
    const h = harness();
    h.set('activeView="diary"');
    h.sandbox.getJson = async (path) => {
        if (path.includes("overview")) throw new Error("unavailable");
        return page([record()]);
    };
    await h.sandbox.refresh();
    assert.ok(h.node("#content").innerHTML.includes("Public title"));
    assert.ok(h.node("#header-state-label").textContent.includes("连接未更新"));
});
test("overview late response cannot overwrite a later overview", async () => {
    const old = deferred(),
        h = harness();
    let count = 0;
    const overview = (state) => ({
        contract: "public-contract-v2",
        lifecycle: { state, changed_at: "2026-10-12T01:00:00Z" },
        public_diary_count: 0,
        goals: [],
        projects: [],
        recent: [],
    });
    h.sandbox.getJson = () =>
        ++count === 1 ? old.promise : Promise.resolve(overview("deep_sleep"));
    const pending = h.sandbox.loadOverview();
    await h.sandbox.loadOverview();
    old.resolve(overview("active"));
    await pending;
    assert.ok(h.node("#overview-content").innerHTML.includes("深度休眠"));
    assert.ok(!h.node("#overview-content").innerHTML.includes("活跃"));
});
test("pagination retains original interval and query when time advances", async () => {
    const h = harness(),
        paths = [];
    h.set('activeView="diary";loadOverview=async()=>{}');
    h.node("#period-filter").value = "7";
    h.sandbox.getJson = async (path) => {
        paths.push(path);
        return {
            ...page([record(String(paths.length))]),
            next_cursor: "cursor+value",
        };
    };
    await h.sandbox.refresh();
    h.node("#period-filter").value = "3";
    await h.sandbox.refresh({ append: true });
    assert.equal(paths[1], paths[0] + "&cursor=cursor%2Bvalue");
});
test("re-reading withdrawn item removes previous content", async () => {
    const h = harness();
    h.set('activeView="diary";activeId="example";loadOverview=async()=>{}');
    h.sandbox.getJson = async () => page([record()]);
    await h.sandbox.refresh();
    assert.ok(h.node("#content").innerHTML.includes("Public title"));
    h.sandbox.getJson = async () => page([]);
    await h.sandbox.refresh();
    assert.ok(!h.node("#content").innerHTML.includes("Public title"));
    assert.ok(h.node("#content").innerHTML.includes("撤回"));
});
test("failed re-read does not present previous public content as current", async () => {
    const h = harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    h.sandbox.getJson = async () => page([record()]);
    await h.sandbox.refresh();
    h.sandbox.getJson = async () => {
        throw new Error("unavailable");
    };
    await h.sandbox.refresh();
    assert.ok(!h.node("#content").innerHTML.includes("Public title"));
});
test("public text is escaped in cards and article reader", () => {
    const h = harness(),
        row = {
            ...record(),
            title: "<img src=x onerror=alert(1)>",
            body: "<script>private()</script>",
            summary: "<svg onload=x>",
            details: { author_label: '"<iframe>' },
        };
    for (const fn of [h.sandbox.renderCard, h.sandbox.renderArticle]) {
        const html = fn(row);
        assert.ok(!html.includes("<script>"));
        assert.ok(!html.includes("<img src=x"));
        assert.ok(html.includes("&lt;"));
    }
});
test("calendar month bounds handle month end without rolling into next month", () => {
    const h = harness();
    const bounds = h.sandbox.periodBounds(
        "30",
        new Date("2026-03-31T12:00:00Z"),
    );
    assert.ok(bounds.since.startsWith("2026-02-28"));
    assert.deepEqual(Object.keys(h.sandbox.periodBounds("all")), []);
});
test("public requests omit management cookies and reject wrong contracts", async () => {
    let options;
    const h = harness({
        fetch: async (_path, opts) => {
            options = opts;
            return { ok: true, json: async () => ({ contract: "private" }) };
        },
    });
    await assert.rejects(
        h.sandbox.getJson("/api/public/diary"),
        /public_contract_invalid/,
    );
    assert.equal(options.credentials, "omit");
    assert.equal(options.cache, "no-store");
});
