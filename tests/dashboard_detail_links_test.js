const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const dashboard = fs.readFileSync(
  path.join(__dirname, "..", "dashboard.html"), "utf8"
);

function sourceBetween(start, end) {
  const startAt = dashboard.indexOf(start);
  const endAt = dashboard.indexOf(end, startAt + start.length);
  assert.notEqual(startAt, -1, `missing ${start}`);
  assert.notEqual(endAt, -1, `missing ${end}`);
  return dashboard.slice(startAt, endAt);
}

test("Dashboard detail renders bucket IDs with DOM text nodes and explicit states", () => {
  const renderer = sourceBetween(
    "function renderContentWithBucketLinks", "function renderReferencedBy"
  );
  assert.match(renderer, /document\.createTextNode/);
  assert.match(renderer, /\\b\[0-9a-f\]\{12\}\\b/);
  assert.match(renderer, /target\.exists/);
  assert.match(renderer, /bucket-id-link/);
  assert.match(renderer, /bucket-id-missing/);
  assert.match(renderer, /目标不存在或已删除/);
  assert.match(renderer, /已封存/);
  assert.match(renderer, /openDetail\(targetId\)/);
  assert.doesNotMatch(renderer, /innerHTML/);
});

test("Dashboard detail renders referenced_by once per source with safe DOM buttons", () => {
  const renderer = sourceBetween(
    "function renderReferencedBy", "async function showDetail"
  );
  assert.match(renderer, /被引用于/);
  assert.match(renderer, /暂无引用/);
  assert.match(renderer, /reference_kinds/);
  assert.match(renderer, /'正文'/);
  assert.match(renderer, /'关联'/);
  assert.match(renderer, /openDetail\(item\.id\)/);
  assert.doesNotMatch(renderer, /innerHTML/);
});

test("Dashboard detail history separates navigation from loading and handles back", () => {
  const loader = sourceBetween("async function showDetail", "function beginDetailEdit");
  assert.doesNotMatch(loader, /history\.pushState/);
  assert.match(loader, /if \(!res\.ok\)/);
  assert.match(loader, /res\.status === 404/);
  assert.match(dashboard, /function openDetail\(id\)/);
  assert.match(dashboard, /history\.pushState\(detailHistoryState\(id\), ''\)/);
  assert.match(dashboard, /history\.replaceState\(detailHistoryState\(null\), ''\)/);
  assert.match(dashboard, /window\.addEventListener\('popstate'/);
  assert.match(dashboard, /showDetail\(state\.detailId\)/);
  assert.match(dashboard, /history\.back\(\)/);
});

test("Dashboard detail history restores A to B to C and back to the list", () => {
  const historySource = sourceBetween(
    "function detailHistoryState", "function renderContentWithBucketLinks"
  );
  const listenerSource = sourceBetween(
    "window.addEventListener('popstate'", "var networkData"
  );
  const entries = [{preserved: "state"}];
  let index = 0;
  let popstate;
  const history = {
    state: entries[index],
    replaceState(state) {
      entries[index] = state;
      this.state = state;
    },
    pushState(state) {
      entries.splice(index + 1);
      entries.push(state);
      index += 1;
      this.state = state;
    },
    back() {
      if (!index) return;
      index -= 1;
      this.state = entries[index];
      popstate({state: this.state});
    },
  };
  const shown = [];
  let closed = 0;
  const api = new Function("history", "showDetail", "document", "window", `
    ${historySource}
    ${listenerSource}
    return { initializeDetailHistory, openDetail };
  `)(
    history,
    (id) => shown.push(id),
    {getElementById: () => ({classList: {remove: () => { closed += 1; }}})},
    {addEventListener: (_name, handler) => { popstate = handler; }},
  );

  api.initializeDetailHistory();
  assert.equal(history.state.preserved, "state");
  assert.equal(history.state.detailId, null);
  api.openDetail("aaaaaaaaaaaa");
  api.openDetail("bbbbbbbbbbbb");
  api.openDetail("cccccccccccc");
  history.back();
  history.back();
  history.back();

  assert.deepEqual(shown, [
    "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc",
    "bbbbbbbbbbbb", "aaaaaaaaaaaa",
  ]);
  assert.equal(history.state.detailId, null);
  assert.equal(closed, 1);
});

test("Every Dashboard detail entry uses the history wrapper", () => {
  assert.match(dashboard, /openDetail\(row\.dataset\.bucketId\)/);
  assert.match(dashboard, /openDetail\(idMatches\[0\]\.id\)/);
  assert.match(dashboard, /archive-row.*openDetail/);
  assert.match(dashboard, /openDetail\(targetId\)/);
  assert.match(dashboard, /openDetail\(item\.id\)/);
  assert.match(dashboard, /navigator\.clipboard\.writeText\(id\)/);
  assert.match(dashboard, /event\.stopPropagation\(\)/);
});
