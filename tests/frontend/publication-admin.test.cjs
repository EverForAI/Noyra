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
                querySelector: () => null,
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
    h.$("#publication-sources").querySelectorAll = () => [{ querySelector: sel => ({value: sel.includes('title') ? "Source" : "javascript:alert(1)"}) }];
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

test("explicit rejection unlocks a new draft while ambiguous outcome keeps identity", async () => {
    const h = context();
    h.sandbox.request = async () => { throw Object.assign(new Error("rejected"), {status:404}); };
    const event = {preventDefault(){},currentTarget:h.$("#publication-form")};
    await h.sandbox.savePublicPublication(event);
    assert.equal(h.get("publicationEditor"), null);
    assert.equal(h.$("#publication-kind").disabled, false);
    h.sandbox.request = async () => { throw new Error("network"); };
    await h.sandbox.savePublicPublication(event);
    assert.ok(h.get("publicationPending"));
    assert.equal(h.$("#publication-kind").disabled, true);
    assert.equal(h.$("#publication-new").disabled, true);
    h.sandbox.request = async () => ({item:null});
    await h.sandbox.reconcilePublicPublication();
    assert.equal(h.get("publicationPending"), null);
    assert.equal(h.$("#publication-kind").disabled, false);
});

test("precise percentages and custom status survive an unrelated title edit", () => {
    const h = context();
    h.sandbox.editPublicPublication({...row,details:{status:"Custom Status 1",progress:0.123456}});
    h.$("#publication-progress").value = String(h.$("#publication-progress").value);
    const details = h.sandbox.collectPublicationDetails();
    assert.equal(details.status,"Custom Status 1");
    assert.equal(details.progress,0.123456);
});


test("invalid structured draft reports validation without starting a write", async () => {
    const h = context();
    let writes=0,confirmations=0;
    h.sandbox.request=async()=>{writes++;throw Error("must not send")};
    h.sandbox.window.confirm=()=>{confirmations++;return true};
    h.sandbox.collectPublicationDetails=()=>{throw Error("参考资料必须为不含凭证的HTTPS链接")};
    await h.sandbox.savePublicPublication({preventDefault(){},currentTarget:h.$("#publication-form")});
    assert.equal(writes,0);
    assert.equal(confirmations,0);
    assert.equal(h.get("publicationEditor"),null);
    assert.equal(h.get("publicationPending"),null);
    assert.equal(h.get("publicationWriting"),false);
    assert.ok(h.$("#publication-editor-status").textContent.includes("HTTPS"));
    assert.equal(h.$("#publication-kind").disabled,false);
});


test("reconciliation adopts only matching content and preserves CAS after a different edit", async () => {
    const h = context();
    h.sandbox.editPublicPublication(row);
    h.sandbox.request=async()=>{throw Error("network")};
    await h.sandbox.savePublicPublication({preventDefault(){},currentTarget:h.$("#publication-form")});
    const pending=h.get("publicationPending");
    h.sandbox.request=async()=>({item:{...row,...pending, id:pending.publication_id, revision:3, status:"published"}});
    await h.sandbox.reconcilePublicPublication();
    assert.equal(h.get("publicationPending"),null);
    assert.equal(h.get("publicationEditor.revision"),3);
    h.sandbox.request=async()=>{throw Error("network")};
    await h.sandbox.savePublicPublication({preventDefault(){},currentTarget:h.$("#publication-form")});
    h.sandbox.request=async()=>({item:{...row,revision:4,title:"Other operator edit"}});
    await h.sandbox.reconcilePublicPublication();
    assert.equal(h.get("publicationPending"),null);
    assert.equal(h.get("publicationEditor.revision"),3);
    assert.ok(h.$("#publication-editor-status").textContent.includes("不能覆盖"));
});

test("withdrawal invalidates only the current editor and preserves unsaved text", async () => {
    const h=context();
    h.sandbox.editPublicPublication(row);
    h.$("#publication-title").value="Unsaved text";
    h.set('publicationRows.set("public-row-1",' + JSON.stringify(row) + ')');
    h.sandbox.request=async(path,options)=>options ? {status:"withdrawn",revision:3} : {items:[],next_cursor:null};
    await h.sandbox.withdrawPublicPublication(row.id);
    assert.equal(h.$("#publication-title").value,"Unsaved text");
    assert.equal(h.get("publicationEditor.invalidBase"),true);
    let writes=0;
    h.sandbox.request=async()=>{writes++};
    await h.sandbox.savePublicPublication({preventDefault(){},currentTarget:h.$("#publication-form")});
    assert.equal(writes,0);
    h.sandbox.editPublicPublication(row);
    h.set('publicationRows.set("other",' + JSON.stringify({...row,id:"other"}) + ')');
    h.sandbox.request=async(path,options)=>options ? {} : {items:[],next_cursor:null};
    await h.sandbox.withdrawPublicPublication("other");
    assert.equal(h.get("publicationEditor.invalidBase"),undefined);
});

test("fresh 409 is a rejection; 409 after unknown outcome retains reconciliation", async () => {
    const h=context();
    h.sandbox.editPublicPublication(row);
    const event={preventDefault(){},currentTarget:h.$("#publication-form")};
    h.sandbox.request=async()=>{throw Object.assign(Error("conflict"),{status:409,code:"public_publication_conflict"})};
    await h.sandbox.savePublicPublication(event);
    assert.equal(h.get("publicationPending"),null);
    assert.equal(h.get("publicationEditor.invalidBase"),true);
    h.sandbox.editPublicPublication(row);
    h.sandbox.request=async()=>{throw Error("network")};
    await h.sandbox.savePublicPublication(event);
    const pending=h.get("publicationPending");
    h.sandbox.request=async()=>{throw Object.assign(Error("conflict"),{status:409})};
    await h.sandbox.savePublicPublication(event);
    assert.equal(h.get("publicationPending"),pending);
    assert.equal(h.get("publicationEditor.invalidBase"),undefined);
});

test("reconciliation canonicalizes status, offsets, milliseconds and ordered entries", async () => {
    const h=context();
    h.sandbox.editPublicPublication({...row,kind:"prediction",details:{status:" active ",target_at:"2026-11-01T01:30:00.123-05:00"}});
    h.sandbox.request=async()=>{throw Error("network")};
    await h.sandbox.savePublicPublication({preventDefault(){},currentTarget:h.$("#publication-form")});
    const pending=h.get("publicationPending");
    assert.equal(pending.details.status,"active");
    assert.equal(pending.details.target_at,"2026-11-01T06:30:00.123+00:00");
    h.sandbox.request=async()=>({item:{...row,...pending,id:pending.publication_id,status:"published",revision:3}});
    await h.sandbox.reconcilePublicPublication();
    assert.equal(h.get("publicationEditor.revision"),3);
    const a=h.sandbox.canonicalPublicationDetails({phases:[{title:" A ",status:" ready "},{title:"B",status:"x"}]});
    const b=h.sandbox.canonicalPublicationDetails({phases:[{title:"B",status:"x"},{title:"A",status:"ready"}]});
    assert.notEqual(JSON.stringify(a),JSON.stringify(b));
});

test("timestamp editing preserves instants across DST folds, gaps and zones", () => {
    const originalZone=process.env.TZ;
    try {
        for(const zone of ["UTC","Asia/Shanghai","America/New_York"]){
            process.env.TZ=zone;
            const h=context();
            for(const value of ["2026-11-01T06:30:00.123Z","2026-03-08T02:30:00.456-05:00","2026-10-12T10:30:00.123456+08:00"]){
                h.sandbox.editPublicPublication({...row,kind:"prediction",details:{target_at:value}});
                assert.equal(h.sandbox.collectPublicationDetails().target_at,new Date(value).toISOString().replace("Z","+00:00"));
            }
            assert.throws(()=>h.sandbox.publicationTimestamp("2026-11-01T01:30:00"),/时区/);
            assert.throws(()=>h.sandbox.publicationTimestamp("2026-02-30T01:30:00Z"),/无效/);
        }
    } finally {if(originalZone===undefined) delete process.env.TZ; else process.env.TZ=originalZone;}
});
