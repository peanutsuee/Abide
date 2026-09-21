"use strict";

const assert = require("node:assert/strict");
global.window = {};
require("../dashboard_assets.js");

const message = global.window.RememberMeAssetBrowser.uploadErrorMessage;
assert.equal(message("csrf_required", 403), "\u767b\u5f55\u9a8c\u8bc1\u5df2\u8fc7\u671f\uff0c\u8bf7\u5237\u65b0\u9875\u9762\u540e\u91cd\u8bd5\u3002");
assert.equal(message("same_origin_required", 403), "\u4e0a\u4f20\u8bf7\u6c42\u672a\u901a\u8fc7\u540c\u6e90\u5b89\u5168\u6821\u9a8c\uff0c\u8bf7\u5237\u65b0\u9875\u9762\u540e\u91cd\u8bd5\u3002");
assert.equal(message("file_too_large", 413), "\u56fe\u7247\u8d85\u8fc7 10 MiB\u3002");
assert.equal(message("image_pixel_limit", 422), "\u56fe\u7247\u50cf\u7d20\u8d85\u8fc7 20,000,000\u3002");
assert.equal(message("unsupported_image", 415), "\u4ec5\u652f\u6301 PNG \u548c JPEG\u3002");
assert.equal(message("unsupported_image_format", 415), "\u4ec5\u652f\u6301 PNG \u548c JPEG\u3002");
assert.equal(message("image_mime_mismatch", 422), "\u56fe\u7247\u683c\u5f0f\u4e0e\u6587\u4ef6\u58f0\u660e\u4e0d\u4e00\u81f4\u3002");
assert.equal(message("invalid_image", 422), "\u56fe\u7247\u5df2\u635f\u574f\u6216\u65e0\u6cd5\u8bfb\u53d6\u3002");
assert.equal(message("invalid_multipart", 400), "\u4e0a\u4f20\u8bf7\u6c42\u89e3\u6790\u5931\u8d25\u3002");
const unknown = message("private internal exception text", 500);
assert.equal(unknown, "\u670d\u52a1\u5668\u5904\u7406\u4e0a\u4f20\u65f6\u51fa\u9519\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002");
assert.ok(!unknown.includes("private internal exception text"));
