"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "dashboard.html"), "utf8");
const bootstrapMatch = html.match(
  /<script id="dashboard-theme-bootstrap">([\s\S]*?)<\/script>/,
);

assert.ok(bootstrapMatch, "the early dashboard theme bootstrap must exist");

function runBootstrap(savedTheme, controls = []) {
  const root = { dataset: {} };
  const storage = new Map();
  if (savedTheme !== undefined) storage.set("ombre-brain-dashboard-theme", savedTheme);
  const context = {
    document: {
      documentElement: root,
      querySelectorAll: (selector) => selector === '[name="dashboard-theme"]' ? controls : [],
    },
    localStorage: {
      getItem: (key) => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, value),
    },
    window: {},
  };
  vm.runInNewContext(bootstrapMatch[1], context);
  return { root, storage, theme: context.window.OmbreDashboardTheme };
}

test("default theme is applied before styles when storage is empty", () => {
  const { root, theme } = runBootstrap();
  assert.equal(root.dataset.dashboardTheme, "default");
  assert.equal(theme.storageKey, "ombre-brain-dashboard-theme");
  assert.ok(html.indexOf('id="dashboard-theme-bootstrap"') < html.indexOf("<style>"));
});

test("each selectable theme applies and persists", () => {
  for (const value of ["iris", "blush", "blue-gray", "matcha"]) {
    const controls = [
      { value: "default", checked: false },
      { value, checked: false },
    ];
    const { root, storage, theme } = runBootstrap(undefined, controls);
    assert.equal(theme.apply(value, true), value);
    assert.equal(root.dataset.dashboardTheme, value);
    assert.equal(storage.get(theme.storageKey), value);
    assert.equal(controls[1].checked, true);
  }
});

test("a valid saved theme restores while an invalid value safely defaults", () => {
  assert.equal(runBootstrap("matcha").root.dataset.dashboardTheme, "matcha");
  assert.equal(runBootstrap("not-a-dashboard-theme").root.dataset.dashboardTheme, "default");
});

test("theme selector exposes every requested Chinese name", () => {
  for (const name of ["默认", "灰紫鸢尾", "烟粉豆沙", "奶雾蓝灰", "抹茶雾绿"]) {
    assert.match(html, new RegExp(`>\\s*${name}\\s*</label>`));
  }
  assert.match(html, /id="dashboard-theme-selector"/);
});

test("theme palettes share the readable body color and do not override semantic danger colors", () => {
  for (const selector of ["iris", "blush", "blue-gray", "matcha"]) {
    const block = html.match(new RegExp(`html\\[data-dashboard-theme="${selector}"\\] \\{([\\s\\S]*?)\\n  \\}`));
    assert.ok(block, `${selector} palette block exists`);
    assert.match(block[1], /--body-text: #403A3D/);
    assert.doesNotMatch(block[1], /--(?:negative|warning|positive):/);
  }
  assert.match(html, /--negative: #8B4A4A/);
  assert.match(html, /--warning: #9A7B4F/);
});

test("existing Dashboard asset wiring remains present", () => {
  assert.match(html, /<script src="\/dashboard-assets\.js"><\/script>/);
  assert.match(html, /data-tab="settings">设置</);
  assert.match(html, /id="network-canvas"/);
});
