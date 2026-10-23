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
                removeAttribute(key) {
                    delete this[key];
                },
                async decode() {},
            });
        return nodes.get(selector);
    };
    node("#period-filter").value = "all";
    node("#category-filter").value = "all";
    const sandbox = vm.createContext({
        document: { querySelector: node },
        URLSearchParams,
        URL,
        AbortController,
        setTimeout,
        clearTimeout,
        Date,
        location: {
            href: "https://example.test/",
            origin: "https://example.test",
            hash: "",
        },
        console,
        ...extra,
    });
    vm.runInContext(
        source.slice(0, source.indexOf("function savePreference(")),
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
test("450 records remain reachable in bounded batches with a previous path", async () => {
    const h = harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    h.sandbox.getJson = async (path) => {
        const start = Number(
            new URL(path, "https://example.test").searchParams.get("cursor") ||
                0,
        );
        return {
            ...page(
                Array.from({ length: Math.min(20, 450 - start) }, (_, i) =>
                    record(String(start + i)),
                ),
            ),
            next_cursor: start + 20 < 450 ? String(start + 20) : null,
        };
    };
    const seen = new Set();
    await h.sandbox.refresh();
    do {
        for (const item of h.get("visibleRows")) seen.add(item.id);
        assert.ok(h.get("visibleRows.length") <= 200);
        if (!h.get("nextCursor")) break;
        await h.sandbox.refresh({ append: true });
    } while (true);
    assert.equal(seen.size, 450);
    assert.equal(h.get("batchNumber"), 3);
    await h.sandbox.refresh({ previous: true });
    assert.equal(h.get("visibleRows[0].id"), "200");
    assert.equal(h.get("visibleRows.length"), 200);
    assert.equal(h.get("visibleRows[199].id"), "399");
    await h.sandbox.refresh();
    assert.equal(h.get("batchPaths.length"), 0);
});
test("pause invalidates pending overview and resume performs one read", async () => {
    const h = harness(),
        pending = deferred();
    h.sandbox.applyLanguage = () => {};
    h.sandbox.getJson = () => pending.promise;
    const reading = h.sandbox.loadOverview();
    h.sandbox.togglePublicUpdates();
    pending.resolve({});
    await reading;
    assert.equal(h.get("overviewData"), null);
    let reads = 0;
    h.sandbox.loadOverview = async () => reads++;
    h.sandbox.togglePublicUpdates();
    assert.equal(reads, 1);
});
test("HTTP error status and safe retry guidance survive public reads", async () => {
    const h = harness({
        fetch: async () => ({
            ok: false,
            status: 429,
            headers: { get: () => "60" },
            json: async () => ({ error: "rate_limited" }),
        }),
    });
    await assert.rejects(
        h.sandbox.getJson("/api/public/timeline"),
        (error) =>
            error.status === 429 &&
            error.retryAfter === 60 &&
            h.sandbox.publicErrorMessage(error).includes("60"),
    );
    assert.ok(
        h.sandbox
            .publicErrorMessage(new Error("public_query_budget_exceeded"))
            .includes("缩小"),
    );
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
    assert.ok(h.node("#header-state-label").textContent.includes("等待更新"));
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
test("request timeout reports a local network error and releases its timer", async () => {
    let timer,
        cleared = false;
    const h = harness({
        setTimeout: (fn) => {
            timer = fn;
            return 1;
        },
        clearTimeout: () => {
            cleared = true;
        },
        fetch: async (_path, options) =>
            new Promise((_yes, no) =>
                options.signal.addEventListener("abort", () => {
                    const error = new Error("aborted");
                    error.name = "AbortError";
                    no(error);
                }),
            ),
    });
    const pending = h.sandbox.getJson("/api/public/timeline");
    timer();
    await assert.rejects(pending, /public_network_unavailable/);
    assert.ok(cleared);
});
test("timeline category exposes matching lifecycle status choices", () => {
    const h = harness();
    h.set('activeView="timeline"');
    h.node("#category-filter").value = "lifecycle";
    h.sandbox.configureStatus();
    assert.ok(
        h.node("#status-options").innerHTML.includes('value="deep_sleep"'),
    );
    h.node("#category-filter").value = "post";
    h.sandbox.configureStatus();
    assert.ok(!h.node("#status-options").innerHTML.includes('value="active"'));
    h.node("#category-filter").value = "update";
    h.sandbox.configureStatus();
    assert.ok(h.node("#status-options").innerHTML.includes('value="active"'));
});

test("mobile navigation returns keyboard focus to its opener on close", () => {
    const h = harness();
    const nav = h.node("#public-navigation"),
        firstLink = h.node("#public-navigation a"),
        opener = h.node("#mobile-more");
    let open = false;
    nav.classList = {
        contains: () => open,
        toggle: (_name, value) => {
            open = value;
        },
    };
    nav.contains = (node) => node === firstLink;
    for (const node of [firstLink, opener])
        node.focus = () => {
            h.sandbox.document.activeElement = node;
        };
    for (const node of [h.node("#menu-toggle"), opener])
        node.setAttribute = (name, value) => {
            node[name] = value;
        };
    h.sandbox.document.activeElement = opener;
    h.sandbox.setMenu(true);
    assert.equal(h.sandbox.document.activeElement, firstLink);
    assert.equal(opener["aria-expanded"], "true");
    h.sandbox.setMenu(false);
    assert.equal(h.sandbox.document.activeElement, opener);
    assert.equal(opener["aria-expanded"], "false");
    assert.equal(open, false);
});

test("captcha timeout shows a retryable error without clearing the draft", async () => {
    let timer,
        cleared = false;
    const h = harness({
        setTimeout: (fn) => {
            timer = fn;
            return 1;
        },
        clearTimeout: () => {
            cleared = true;
        },
        fetch: async (_path, options) =>
            new Promise((_yes, no) =>
                options.signal.addEventListener("abort", () => {
                    const error = new Error("aborted");
                    error.name = "AbortError";
                    no(error);
                }),
            ),
    });
    h.node("#public-post-content").value = "unsent draft";
    const pending = h.sandbox.loadPublicPostCaptcha();
    timer();
    await pending;
    assert.ok(cleared);
    assert.equal(h.get("publicPostCaptchaId"), "");
    assert.match(h.node("#captcha-status").textContent, /无法连接/);
    assert.equal(h.node("#public-post-content").value, "unsent draft");
});

test("older captcha response cannot replace the current challenge", async () => {
    const old = deferred();
    const response = (id, mode = "digits") => ({
        ok: true,
        json: async () => ({
            challenge_id: id,
            mode,
            image: "data:image/png;base64,synthetic",
            max_content_chars: 7,
        }),
    });
    const h = harness({ fetch: () => old.promise });
    const first = h.sandbox.loadPublicPostCaptcha();
    h.sandbox.fetch = async () => response("new", "letters");
    await h.sandbox.loadPublicPostCaptcha();
    old.resolve(response("old"));
    await first;
    assert.equal(h.get("publicPostCaptchaId"), "new");
    assert.equal(h.node("#public-post-captcha-answer").inputMode, "text");
    assert.equal(h.node("#public-post-content").dataset.maxCharacters, "7");
    h.set('language="en"');
    h.sandbox.renderCaptchaMode();
    assert.equal(
        h.node("#public-post-captcha-answer").placeholder,
        "Enter 6 letters",
    );
    assert.equal(h.get("publicPostCaptchaId"), "new");
});

function submissionHarness(fetch) {
    const h = harness({
        fetch,
        crypto: { randomUUID: () => "same-submission-id" },
    });
    h.set('publicPostCaptchaId="captcha"');
    h.node("#public-post-title").value = "A title";
    h.node("#public-post-content").value = "An unsent thought";
    h.node("#public-post-captcha-answer").value = "ABCDEF";
    h.node("#public-post-kind").value = "post";
    const controls = [
        h.node("#public-post-title"),
        h.node("#public-post-content"),
    ];
    const form = {
        reportValidity: () => true,
        querySelectorAll: () => controls,
        reset: () => {
            for (const node of controls) node.value = "";
        },
    };
    return { h, event: { preventDefault() {}, currentTarget: form } };
}

test("unconfirmed post retry retains the draft and same payload across language changes", async () => {
    const writes = [];
    const { h, event } = submissionHarness(async (_path, options) => {
        writes.push(JSON.parse(options.body));
        throw new TypeError("network failed after send");
    });
    await h.sandbox.submitPublicPost(event);
    assert.equal(h.node("#public-post-content").value, "An unsent thought");
    assert.equal(h.node("#public-post-submit").disabled, false);
    assert.match(h.node("#public-post-status").textContent, /尚未确认/);
    h.set('language="en"');
    await h.sandbox.submitPublicPost(event);
    assert.deepEqual(writes[0], writes[1]);
    assert.equal(writes[1].idempotency_key, "same-submission-id");
});

test("duplicate post clicks send only once and successful submission clears the draft", async () => {
    const pending = deferred();
    let writes = 0;
    const { h, event } = submissionHarness(() => {
        writes++;
        return pending.promise;
    });
    h.sandbox.loadPublicPostCaptcha = async () => {};
    const first = h.sandbox.submitPublicPost(event);
    await h.sandbox.submitPublicPost(event);
    assert.equal(writes, 1);
    pending.resolve({ ok: true, json: async () => ({ status: "pending" }) });
    await first;
    assert.equal(h.node("#public-post-content").value, "");
    assert.equal(h.node("#public-post-submit").disabled, false);
    assert.equal(h.get("publicPostIdempotencyKey"), "");
    assert.match(
        h.node("#public-post-status").textContent,
        /审核后会出现在留言广场/,
    );
});

test("structured summaries render zero progress and probability without private fields", () => {
    const h = harness();
    assert.ok(
        h.sandbox
            .renderDetails({
                kind: "goal",
                details: { progress: 0, next_step: "<private>" },
            })
            .includes("0%"),
    );
    const prediction = h.sandbox.renderDetails({
        kind: "prediction",
        details: {
            probability: 0,
            outcome: "pending",
            private_reason: "sentinel",
        },
    });
    assert.ok(prediction.includes("0%"));
    assert.ok(!prediction.includes("sentinel"));
    const sleep = h.sandbox.renderDetails({
        kind: "sleep",
        details: {
            started_at: "2026-10-12T00:00:00Z",
            completed_at: "2026-10-12T04:00:00Z",
        },
    });
    assert.ok(sleep.includes("4 小时"));
});
test("research references reject script, HTTP and credential-bearing URLs", () => {
    const h = harness();
    for (const url of [
        "javascript:alert(1)",
        "http://example.com",
        "https://user:secret@example.com",
    ]) {
        assert.equal(h.sandbox.safePublicLink(url), null);
        const html = h.sandbox.renderDetails({
            kind: "research",
            details: { sources: [{ title: "<img>", url }] },
        });
        assert.ok(!html.includes('href="'));
        assert.ok(!html.includes("<img>"));
    }
    const html = h.sandbox.renderDetails({
        kind: "research",
        details: {
            sources: [{ title: "Reference", url: "https://example.com/a" }],
        },
    });
    assert.ok(html.includes('rel="noopener noreferrer"'));
    assert.ok(html.includes('referrerpolicy="no-referrer"'));
});

test("article links identify independent pages and structured text stays inert", () => {
    const h = harness();
    h.set('language="en"');
    assert.equal(
        h.sandbox.itemHref({ ...record("id/?x"), kind: "research" }),
        "/public/research/id%2F%3Fx?lang=en",
    );
    const markup = h.sandbox.structuredPublicText(
        "# First\ntext\n\n## <script>bad()</script>\njavascript:bad()",
    );
    assert.ok(markup.includes('id="reading-section-1"'));
    assert.ok(markup.includes('href="#reading-section-2"'));
    assert.ok(markup.includes("Contents"));
    assert.ok(markup.includes("&lt;script&gt;"));
    assert.ok(!markup.includes("<script>"));
    assert.ok(!markup.includes('href="javascript:'));
});

test("ordinary article clicks retain SPA context while modified clicks keep canonical navigation", () => {
    const h = harness();
    h.set(
        'activeView="diary";batchPath="/api/public/diary?limit=20&cursor=200";currentQueryPath="/api/public/diary?limit=20";visibleRows=Array.from({length:200},(_,i)=>({id:String(i)}));batchNumber=2',
    );
    h.node("#archive-search").value = "draft search";
    const link = {
        href: "https://example.test/public/diary/example",
        target: "",
        hasAttribute: () => false,
    };
    let prevented = 0;
    const event = {
        target: { closest: () => link },
        button: 0,
        preventDefault() {
            prevented++;
        },
    };
    h.sandbox.publicReadingClick({ ...event, ctrlKey: true });
    assert.equal(prevented, 0);
    h.sandbox.publicReadingClick(event);
    assert.equal(prevented, 1);
    assert.equal(h.sandbox.location.hash, "diary/example");
    assert.equal(h.get("readingReturn.count"), 200);
    assert.equal(h.get("readingReturn.search"), "draft search");
    h.set('readingReturn.view="timeline"');
    assert.ok(h.sandbox.renderArticle(record()).includes('href="#timeline"'));
});

test("restoring a reading context rereads content and preserves batch/query metadata", async () => {
    const h = harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    let reads = 0;
    h.sandbox.getJson = async (path) => {
        reads++;
        const q = new URL(path, "https://example.test").searchParams,
            start = Number(q.get("cursor") || 0),
            limit = Number(q.get("limit"));
        return {
            ...page(
                Array.from({ length: limit }, (_, i) =>
                    record(String(start + i)),
                ),
            ),
            next_cursor: String(start + limit),
        };
    };
    await h.sandbox.refresh({
        restore: {
            path: "/api/public/diary?limit=20&cursor=200",
            count: 200,
            query: "/api/public/diary?limit=20&search=x",
            paths: [{ path: "/api/public/diary?limit=20", count: 200 }],
            number: 2,
        },
    });
    assert.equal(h.get("visibleRows.length"), 200);
    assert.equal(h.get("visibleRows[0].id"), "200");
    assert.equal(h.get("nextCursor"), "400");
    assert.equal(h.get("batchNumber"), 2);
    assert.ok(h.get("currentQueryPath").includes("search=x"));
    assert.equal(reads, 3);
});

test("irregular bounded pages cannot skip rows at the 200 record batch boundary", async () => {
    const h = harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    h.sandbox.getJson = async (path) => {
        const q = new URL(path, "https://example.test").searchParams,
            start = Number(q.get("cursor") || 0),
            count = Math.min(Number(q.get("limit")), 13, 450 - start);
        return {
            ...page(
                Array.from({ length: count }, (_, i) =>
                    record(String(start + i)),
                ),
            ),
            next_cursor: start + count < 450 ? String(start + count) : null,
        };
    };
    const ids = new Set();
    await h.sandbox.refresh();
    for (let reads = 0; reads < 100; reads++) {
        h.get("visibleRows").forEach((row) => ids.add(row.id));
        assert.ok(h.get("visibleRows.length") <= 200);
        if (!h.get("nextCursor")) break;
        await h.sandbox.refresh({ append: true });
    }
    assert.equal(ids.size, 450);
});

test("reading return is confined to its article navigation chain", () => {
    const h = harness();
    h.sandbox.document.querySelectorAll = () => [];
    vm.runInContext(
        source.slice(
            source.indexOf("function showView()"),
            source.indexOf("function updatePostLength()"),
        ),
        h.sandbox,
    );
    h.sandbox.setMenu = () => {};
    let options;
    h.sandbox.refresh = (input) => {
        options = input;
    };
    h.set(
        'activeView="diary";activeId="example";readingReturn={article:"diary/example",view:"diary",search:"old",period:"all",category:"all",status:""}',
    );
    h.sandbox.location.hash = "#about";
    h.sandbox.showView();
    assert.equal(h.get("readingReturn"), null);
    h.node("#archive-search").value = "new";
    h.sandbox.location.hash = "#diary";
    h.sandbox.showView();
    assert.equal(options.restore, null);
    assert.equal(h.node("#archive-search").value, "new");
    h.set(
        'activeView="diary";activeId="example";readingReturn={article:"diary/example",view:"diary",search:"old",period:"all",category:"all",status:""}',
    );
    h.sandbox.showView();
    assert.equal(options.restore.search, "old");
    assert.equal(h.get("readingReturn"), null);
    assert.equal(h.node("#archive-search").value, "old");
});

test("extremely short and empty pages restore only with bounded user continuations", async () => {
    const h = harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    let reads = 0;
    h.sandbox.getJson = async (path) => {
        reads++;
        const q = new URL(path, "https://example.test").searchParams;
        const position = Number(q.get("cursor") || 0);
        const empty = position % 7 === 0;
        return {
            ...page(empty ? [] : [record(String(position))]),
            next_cursor: String(position + 1),
        };
    };
    await h.sandbox.refresh({
        restore: {
            path: "/api/public/diary?limit=20",
            count: 200,
            query: "/api/public/diary?limit=20",
            paths: [],
            number: 1,
        },
    });
    assert.equal(reads, 20);
    assert.ok(h.get("visibleRows.length") < 200);
    assert.equal(h.get("restoreTarget"), 200);
    assert.match(h.node("#list-status").textContent, /尚未恢复完/);
    while (h.get("restoreTarget")) {
        const before = reads;
        await h.sandbox.refresh({ append: true });
        assert.ok(reads - before <= 20);
    }
    assert.equal(h.get("visibleRows.length"), 200);
    assert.equal(new Set(h.get("visibleRows").map((row) => row.id)).size, 200);
    assert.doesNotMatch(h.node("#list-status").textContent, /尚未恢复完/);
});

test("home uses one existing public request and only published previews", async () => {
    const h = harness();
    const paths = [];
    const diary = {
        ...record("writing"),
        body: "DETAIL-ONLY-NOT-A-PREVIEW",
        summary: "A shared moment",
    };
    const project = {
        ...record("project"),
        kind: "project",
        provenance: "operator_summary",
        details: { private_reason: "DO-NOT-DISPLAY" },
    };
    h.sandbox.getJson = async (path) => {
        paths.push(path);
        return {
            display_name: "Aster <img src=x onerror=alert(1)>",
            lifecycle: {
                state: "deep_sleep",
                changed_at: "2026-10-12T01:00:00Z",
            },
            public_diary_count: 1,
            last_sleep_completed_at: null,
            recent: [diary],
            projects: [project],
            goals: [],
        };
    };
    await h.sandbox.loadOverview();
    assert.deepEqual(paths, ["/api/public/overview"]);
    assert.equal(
        h.node("#hero-title").textContent,
        "认识 Aster <img src=x onerror=alert(1)>。",
    );
    const html = h.node("#content").innerHTML;
    assert.match(html, /A shared moment/);
    assert.match(html, /管理者整理/);
    assert.doesNotMatch(html, /DETAIL-ONLY-NOT-A-PREVIEW|DO-NOT-DISPLAY|<img/);
    assert.equal((html.match(/A shared moment/g) || []).length, 1);
    assert.match(h.node("#overview-content").innerHTML, /正在休息/);
    assert.match(h.node("#overview-content").innerHTML, /深度休眠/);
    assert.doesNotMatch(
        h.node("#overview-content").innerHTML,
        /梦|整理今天|预计醒来/,
    );
});

test("home empty state offers real participation without inventing projects or accounts", () => {
    const h = harness();
    h.set(
        'overviewData={lifecycle:{state:"active",changed_at:"2026-10-12T01:00:00Z"},public_diary_count:0,recent:[],projects:[],goals:[]}',
    );
    h.node("#period-filter").value = "7";
    h.node("#archive-search").value = "old list filter";
    h.sandbox.renderOverview();
    const html = h.node("#content").innerHTML;
    assert.match(html, /还很安静/);
    assert.match(html, /href="#participate"/);
    assert.doesNotMatch(
        html,
        /没有找到匹配|featured-record|home-record|login|register|我的投稿|私信/,
    );
    assert.equal(h.node("#hero-title").textContent, "认识 Noyra。");
});

test("home and compact cards keep author names and hostile text escaped in both languages", () => {
    const h = harness();
    const hostile = {
        ...record('a/b"'),
        kind: "post",
        provenance: "visitor",
        title: "<script>evil()</script>",
        summary: '<svg onload="evil()">',
        details: { author_label: "<img onerror=evil()>" },
    };
    for (const language of ["zh", "en"]) {
        h.set(`language="${language}"`);
        const html = h.sandbox.renderHomeCard(hostile, true);
        assert.doesNotMatch(html, /<script>|<svg|<img/);
        assert.match(html, /&lt;script&gt;/);
        assert.match(html, /%2F/);
        assert.match(
            html,
            language === "en" ? /Visitor name.*unverified/ : /访客署名.*未验证/,
        );
    }
});

test("empty results distinguish filters from a genuinely empty guestbook", () => {
    const h = harness();
    h.set('activeView="posts"');
    assert.match(h.sandbox.emptyState(), /留下第一个想法/);
    h.node("#archive-search").value = "not found";
    assert.match(h.sandbox.emptyState(), /没有找到匹配/);
    assert.doesNotMatch(h.sandbox.emptyState(), /第一个想法/);
    h.set('activeView="diary";activeId="withdrawn"');
    assert.match(
        h.sandbox.emptyState("This entry was withdrawn"),
        /This entry was withdrawn/,
    );
    assert.doesNotMatch(
        h.sandbox.emptyState("This entry was withdrawn"),
        /没有找到匹配/,
    );
});

test("old detailed routes remain reachable and reveal the secondary navigation", () => {
    const h = harness();
    h.sandbox.document.querySelectorAll = () => [];
    h.sandbox.setMenu = () => {};
    h.sandbox.refresh = () => {};
    h.sandbox.loadPublicPostCaptcha = async () => {};
    for (const view of [
        "timeline",
        "goals",
        "interactions",
        "behavior",
        "research",
        "predictions",
        "sleep",
        "lifecycle",
        "updates",
    ]) {
        h.sandbox.location.hash = `#${view}`;
        h.sandbox.showView();
        assert.equal(h.get("activeView"), view);
        assert.equal(h.node("#more-records").open, true);
        assert.equal(h.node("#home-entrypoints").hidden, true);
    }
    for (const view of [
        "overview",
        "diary",
        "projects",
        "posts",
        "about",
        "participate",
    ]) {
        h.sandbox.location.hash = `#${view}`;
        h.sandbox.showView();
        assert.equal(h.node("#more-records").open, false);
        assert.equal(h.node("#home-entrypoints").hidden, view !== "overview");
    }
});

test("visitor guidance never sends people to operator login or invents private messaging", () => {
    const h = harness();
    for (const language of ["zh", "en"]) {
        h.set(`language="${language}"`);
        const about = h.sandbox.renderAbout();
        assert.doesNotMatch(
            about,
            /href="\/admin|For private conversations use|私密交流请使用管理台/,
        );
        assert.match(
            about,
            language === "en" ? /without an account/ : /无需注册/,
        );
        assert.match(
            about,
            language === "en"
                ? /Signed-in private messages/
                : /登录后的独立消息/,
        );
        const note = h.get("tr(...i18n.postPrivacy)");
        assert.doesNotMatch(note, /admin console|管理台/);
    }
});

test("dedicated writing stays visible after stage changes without duplicating timeline entries", () => {
    const h = harness();
    const writing = record("diary-one");
    const lifecycle = {
        ...record("stage"),
        kind: "lifecycle",
        title: "",
        details: { from: "active", to: "deep_sleep" },
    };
    h.sandbox.overviewFixture = {
        lifecycle: { state: "deep_sleep", changed_at: "2026-10-12T01:00:00Z" },
        public_diary_count: 1,
        writing: [writing],
        recent: [lifecycle],
        projects: [],
        goals: [],
    };
    h.set("overviewData=overviewFixture");
    h.sandbox.renderOverview();
    assert.match(h.node("#content").innerHTML, /featured-record/);
    assert.match(h.node("#content").innerHTML, /diary-one/);
    assert.match(h.node("#content").innerHTML, /stage/);
    h.set("overviewData.recent=[{...overviewData.writing[0]}]");
    h.sandbox.renderOverview();
    // The same writing returned as separate objects in both projections is
    // one home card, with a heading link and a continuation link.
    assert.equal(
        (h.node("#content").innerHTML.match(/diary-one/g) || []).length,
        2,
    );
});

test("unchanged overview refresh preserves content nodes instead of rebuilding the reader", () => {
    const h = harness();
    h.set(
        'overviewData={display_name:"Aster",lifecycle:{state:"active",changed_at:"2026-10-12T01:00:00Z"},public_diary_count:1,recent:[],writing:[],projects:[],goals:[]}',
    );
    h.sandbox.renderOverview();
    let writes = 0;
    const node = h.node("#content");
    let markup = node.innerHTML;
    Object.defineProperty(node, "innerHTML", {
        get: () => markup,
        set: (value) => {
            writes++;
            markup = value;
        },
    });
    h.sandbox.renderOverview();
    assert.equal(writes, 0);
    h.set(
        'overviewData.recent=[{id:"new-entry",kind:"diary",title:"New writing",summary:"A new paragraph",body:"",details:{},provenance:"subject",status:"published"}]',
    );
    h.sandbox.renderOverview();
    assert.equal(writes, 1);
    assert.match(markup, /New writing/);
});

test("preview formatting keeps quoted article bodies out of summary cards", () => {
    const h = harness();
    const row = {
        ...record(),
        summary: "# A heading\n\nA short paragraph.\nAnother line.",
        body: "Do not use the full body as a fallback",
    };
    assert.equal(
        h.sandbox.previewText(row.summary),
        "A heading A short paragraph. Another line.",
    );
    assert.doesNotMatch(
        h.sandbox.renderCard({ ...row, summary: "" }),
        /Do not use the full body/,
    );
    assert.match(h.sandbox.renderArticle(row), /Do not use the full body/);
});

test("a stage-only instance explains missing writing without claiming there is no activity", () => {
    const h = harness();
    h.set(
        'overviewData={lifecycle:{state:"active",changed_at:"2026-10-12T01:00:00Z"},public_diary_count:0,recent:[{id:"stage",kind:"lifecycle",details:{from:"orienting",to:"active"},status:"active",provenance:"runtime"}],writing:[],projects:[],goals:[]}',
    );
    h.sandbox.renderOverview();
    assert.match(h.node("#content").innerHTML, /日记还没有公开/);
    assert.match(h.node("#content").innerHTML, /orienting|初始化/);
    assert.doesNotMatch(h.node("#content").innerHTML, /这里还没有公开记录/);
});

test("private page refresh uses its authenticated view rather than the public overview", async () => {
    const h = harness();
    let shown = "";
    let publicReads = 0;
    h.sandbox.noyraParticipant = { show: async (view) => (shown = view) };
    h.sandbox.loadOverview = async () => publicReads++;
    for (const view of ["account", "collaborate", "mine", "messages"]) {
        h.set(`activeView="${view}"`);
        await h.sandbox.refresh();
        assert.equal(shown, view);
    }
    assert.equal(publicReads, 0);
});
