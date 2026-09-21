import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { App } from "@modelcontextprotocol/ext-apps";
import { AppBridge } from "@modelcontextprotocol/ext-apps/app-bridge";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

import { createViewerController } from "../src/viewer-controller.js";


class FakeElement {
  constructor({ failImage = false } = {}) {
    this.alt = "";
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.failImage = failImage;
    this.hidden = false;
    this.onload = null;
    this.onerror = null;
    this.textContent = "";
    this._src = "";
  }

  appendChild(child) {
    this.children.push(child);
  }

  removeAttribute(name) {
    if (name === "src") {
      this._src = "";
    }
  }

  replaceChildren(...children) {
    this.children = children;
  }

  set src(value) {
    this._src = value;
    queueMicrotask(() => {
      if (this.failImage) {
        this.onerror?.();
      } else {
        this.onload?.();
      }
    });
  }

  get src() {
    return this._src;
  }
}


function createFakeDocument({ failImage = false } = {}) {
  const nodes = new Map();
  for (const id of [
    "status",
    "viewer",
    "image",
    "title",
    "filename",
    "dimensions",
    "mime",
    "tags",
  ]) {
    nodes.set(
      id,
      new FakeElement({ failImage: id === "image" && failImage }),
    );
  }
  nodes.get("status").textContent = "Loading Remember-Me image...";
  nodes.get("viewer").hidden = true;
  return {
    documentElement: new FakeElement(),
    createElement: () => new FakeElement(),
    getElementById: (id) => nodes.get(id),
    nodes,
  };
}


function createResult({ includeMeta = true } = {}) {
  const result = {
    content: [{ type: "text", text: "fallback" }],
    structuredContent: {
      asset_id: "a".repeat(32),
      title: "<script>safe text</script>",
      filename: "viewer.jpg",
      mime_type: "image/jpeg",
      width: 48,
      height: 32,
      tags: ["<b>tag</b>", "safe"],
      stored_bytes: 4,
    },
  };
  if (includeMeta) {
    result._meta = {
      rememberMe: {
        schemaVersion: 1,
        imageBase64: "AQIDBA==",
        mimeType: "image/jpeg",
      },
    };
  }
  return result;
}


async function runProtocolResult(result, options = {}) {
  const [appTransport, bridgeTransport] =
    InMemoryTransport.createLinkedPair();
  const app = new App(
    { name: "Viewer test", version: "1.0.0" },
    {},
    { autoResize: false, strict: true },
  );
  const documentRef = createFakeDocument(options);
  const viewer = createViewerController(app, documentRef, {
    connectTimeoutMs: 250,
  });
  const bridge = new AppBridge(
    null,
    { name: "Viewer host test", version: "1.0.0" },
    {},
  );

  assert.equal(typeof app.ontoolinput, "function");
  assert.equal(typeof app.ontoolresult, "function");
  assert.equal(typeof app.onteardown, "function");

  bridge.oninitialized = async () => {
    await bridge.sendToolInput({
      arguments: { asset_id: "a".repeat(32) },
    });
    await bridge.sendToolResult(result);
  };

  await bridge.connect(bridgeTransport);
  await viewer.connect(appTransport);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await appTransport.close();
  await bridgeTransport.close();
  return documentRef;
}


test("official AppBridge handshake renders a valid tool result", async () => {
  const documentRef = await runProtocolResult(createResult());
  const { nodes } = documentRef;

  assert.equal(nodes.get("status").hidden, true);
  assert.equal(nodes.get("status").textContent, "Image loaded.");
  assert.equal(nodes.get("viewer").hidden, false);
  assert.equal(
    nodes.get("image").src,
    "data:image/jpeg;base64,AQIDBA==",
  );
  assert.equal(
    nodes.get("title").textContent,
    "<script>safe text</script>",
  );
  assert.equal(nodes.get("dimensions").textContent, "48 x 32");
  assert.deepEqual(
    nodes.get("tags").children.map((node) => node.textContent),
    ["<b>tag</b>", "safe"],
  );
});


test("metadata without image bytes shows a safe error", async () => {
  const documentRef = await runProtocolResult(
    createResult({ includeMeta: false }),
  );
  const status = documentRef.nodes.get("status");
  assert.equal(status.hidden, false);
  assert.equal(status.dataset.state, "error");
  assert.equal(
    status.textContent,
    "Image metadata received, but image bytes were unavailable.",
  );
});


test("image decode failure shows a safe error", async () => {
  const documentRef = await runProtocolResult(
    createResult(),
    { failImage: true },
  );
  const status = documentRef.nodes.get("status");
  assert.equal(status.hidden, false);
  assert.equal(status.dataset.state, "error");
  assert.equal(status.textContent, "The image could not be decoded safely.");
});


test("initialize timeout cannot leave the page blank", async () => {
  const [appTransport, bridgeTransport] =
    InMemoryTransport.createLinkedPair();
  const app = new App(
    { name: "Viewer timeout test", version: "1.0.0" },
    {},
    { autoResize: false, strict: true },
  );
  const documentRef = createFakeDocument();
  const viewer = createViewerController(app, documentRef, {
    connectTimeoutMs: 20,
  });

  await viewer.connect(appTransport);
  const status = documentRef.nodes.get("status");
  assert.equal(status.hidden, false);
  assert.equal(status.dataset.state, "error");
  assert.equal(status.textContent, "The viewer connection timed out.");
  await appTransport.close();
  await bridgeTransport.close();
});


test("production bundle is self-contained and starts visibly", async () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const html = await readFile(`${root}/asset_viewer.html`, "utf8");
  const mainSource = await readFile(
    fileURLToPath(new URL("../src/main.js", import.meta.url)),
    "utf8",
  );

  assert.match(html, /Loading Remember-Me image&hellip;/);
  assert.match(mainSource, /new App\(/);
  assert.match(
    mainSource,
    /new PostMessageTransport\(window\.parent, window\.parent\)/,
  );
  assert.doesNotMatch(html, /unpkg\.com|jsdelivr\.net|cdn\.jsdelivr\.net/);
  assert.doesNotMatch(html, /sourceMappingURL/);
  assert.doesNotMatch(html, /localhost|127\.0\.0\.1|@vite\/client/);
  assert.doesNotMatch(html, /<script[^>]+src=/);
});