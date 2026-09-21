const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const dashboard = fs.readFileSync(path.join(__dirname, "..", "dashboard.html"), "utf8");

test("Dashboard search renders grouped ID results and empty states", () => {
  for (const label of ["此 ID 的桶", "提到此 ID 的 ", "关键词 / 相关记忆"]) {
    assert.match(dashboard, new RegExp(label));
  }
  assert.match(dashboard, /没有找到匹配的 bucket ID/);
  assert.match(dashboard, /没有桶提到此 ID/);
  assert.match(dashboard, /payload\.mode === 'id'/);
});

test("Dashboard search exposes sealed state, direct full-ID detail, and copy isolation", () => {
  assert.match(dashboard, /已封存/);
  assert.match(dashboard, /payload\.normalized_query\.length === 12 && idMatches\.length === 1 && idMatches\[0\]\.match_reason === 'id_exact'/);
  assert.match(dashboard, /openDetail\(idMatches\[0\]\.id\)/);
  assert.match(dashboard, /bucket-copy-id/);
  assert.match(dashboard, /event\.stopPropagation\(\)/);
  assert.match(dashboard, /navigator\.clipboard\.writeText\(id\)/);
});
