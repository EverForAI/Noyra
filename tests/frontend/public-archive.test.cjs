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
        location: {href:"https://example.test/",origin:"https://example.test",hash:""},
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
    assert.match(h.node("#public-post-status").textContent, /等待管理者审核/);
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
    const h=harness();
    h.set('activeView="diary";batchPath="/api/public/diary?limit=20&cursor=200";currentQueryPath="/api/public/diary?limit=20";visibleRows=Array.from({length:200},(_,i)=>({id:String(i)}));batchNumber=2');
    h.node("#archive-search").value="draft search";
    const link={href:"https://example.test/public/diary/example",target:"",hasAttribute:()=>false};
    let prevented=0;
    const event={target:{closest:()=>link},button:0,preventDefault(){prevented++}};
    h.sandbox.publicReadingClick({...event,ctrlKey:true});
    assert.equal(prevented,0);
    h.sandbox.publicReadingClick(event);
    assert.equal(prevented,1);
    assert.equal(h.sandbox.location.hash,"diary/example");
    assert.equal(h.get("readingReturn.count"),200);
    assert.equal(h.get("readingReturn.search"),"draft search");
    h.set('readingReturn.view="timeline"');
    assert.ok(h.sandbox.renderArticle(record()).includes('href="#timeline"'));
});

test("restoring a reading context rereads content and preserves batch/query metadata",async()=>{
    const h=harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    let reads=0;
    h.sandbox.getJson=async path=>{reads++;const q=new URL(path,"https://example.test").searchParams,start=Number(q.get("cursor")||0),limit=Number(q.get("limit"));return {...page(Array.from({length:limit},(_,i)=>record(String(start+i)))),next_cursor:String(start+limit)};};
    await h.sandbox.refresh({restore:{path:"/api/public/diary?limit=20&cursor=200",count:200,query:"/api/public/diary?limit=20&search=x",paths:[{path:"/api/public/diary?limit=20",count:200}],number:2}});
    assert.equal(h.get("visibleRows.length"),200);
    assert.equal(h.get("visibleRows[0].id"),"200");
    assert.equal(h.get("nextCursor"),"400");
    assert.equal(h.get("batchNumber"),2);
    assert.ok(h.get("currentQueryPath").includes("search=x"));
    assert.equal(reads,3);
});

test("irregular bounded pages cannot skip rows at the 200 record batch boundary",async()=>{
    const h=harness();
    h.set('activeView="diary";loadOverview=async()=>{}');
    h.sandbox.getJson=async path=>{const q=new URL(path,"https://example.test").searchParams,start=Number(q.get("cursor")||0),count=Math.min(Number(q.get("limit")),13,450-start);return {...page(Array.from({length:count},(_,i)=>record(String(start+i)))),next_cursor:start+count<450?String(start+count):null};};
    const ids=new Set();
    await h.sandbox.refresh();
    for(let reads=0;reads<100;reads++){
        h.get("visibleRows").forEach(row=>ids.add(row.id));
        assert.ok(h.get("visibleRows.length")<=200);
        if(!h.get("nextCursor"))break;
        await h.sandbox.refresh({append:true});
    }
    assert.equal(ids.size,450);
});
