const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const html = readFileSync(join(__dirname, "../../src/noyra/web/admin.html"), "utf8");
const source = readFileSync(join(__dirname, "../../src/noyra/web/admin.js"), "utf8");

test("login starts disabled with visible initialization and no-script guidance", () => {
  assert.match(html, /<noscript>[\s\S]*JavaScript[\s\S]*<\/noscript>/);
  assert.match(html, /<p[^>]*id="login-initialization-status"[^>]*role="status"[^>]*>[^<]*初始化/);
  assert.doesNotMatch(html.match(/<p[^>]*id="login-initialization-status"[^>]*>/)[0], /hidden/);
  assert.match(html.match(/<form id="login-form">[\s\S]*?<\/form>/)[0], /<button[^>]*type="submit"[^>]*disabled/);
  assert.doesNotMatch(html.match(/<input[^>]*id="login-token"[^>]*>/)[0], /\bname=/);
  assert.match(html, /<script src="\/admin.js" defer><\/script>/);
});

function startup(failBinding = false) {
  const button = { disabled: true };
  const notice = { hidden: false };
  const events = [];
  const handler = () => {};
  let restores = 0;
  const form = {
    addEventListener(name, listener) {
      if (failBinding) throw new Error("synthetic binding failure");
      assert.equal(button.disabled, true);
      assert.equal(notice.hidden, false);
      events.push([name, listener]);
    },
    querySelector(selector) {
      assert.equal(selector, 'button[type="submit"]');
      return button;
    },
  };
  const context = vm.createContext({
    $: selector => ({ "#login-form": form, "#login-initialization-status": notice })[selector],
    login: handler,
    restoreSession: () => {
      assert.equal(events.length, 1);
      assert.equal(button.disabled, false);
      assert.equal(notice.hidden, true);
      restores += 1;
    },
  });
  const start = source.indexOf("function startAdminLogin()");
  assert.ok(start >= 0, "startup must be explicit and follow all synchronous bindings");
  return { button, notice, events, handler, restores: () => restores,
    run: () => vm.runInContext(source.slice(start), context) };
}

test("login enables and clears initialization only after submit binding", () => {
  const page = startup();
  page.run();
  assert.equal(page.button.disabled, false);
  assert.equal(page.notice.hidden, true);
  assert.deepEqual(page.events, [["submit", page.handler]]);
  assert.equal(page.restores(), 1);
  assert.equal((source.match(/restoreSession\(\);/g) || []).length, 1);
  assert.equal((source.match(/addEventListener\("submit", login\)/g) || []).length, 1);
});

test("binding failure retains disabled login and visible initialization guidance", () => {
  const page = startup(true);
  assert.throws(page.run, /synthetic binding failure/);
  assert.equal(page.button.disabled, true);
  assert.equal(page.notice.hidden, false);
  assert.equal(page.restores(), 0);
});
