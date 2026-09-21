import {
  App,
  PostMessageTransport,
} from "@modelcontextprotocol/ext-apps";

import { createViewerController } from "./viewer-controller.js";


// The official transport emits full JSON-RPC messages at debug level.
// This view suppresses debug logging so image bytes never reach the console.
console.debug = () => {};

const app = new App(
  { name: "Remember-Me asset viewer", version: "1.1.0" },
  {},
  { autoResize: true, strict: true },
);
const viewer = createViewerController(app);
const transport = new PostMessageTransport(window.parent, window.parent);

void viewer.connect(transport);