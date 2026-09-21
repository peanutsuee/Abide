(function () {
  "use strict";

  const DEFAULT_LIMIT = 12;
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20000000;
  const ALLOWED_TYPES = new Set(["image/png", "image/jpeg"]);
  const UPLOAD_ERROR_MESSAGES = Object.freeze({
    csrf_required: "登录验证已过期，请刷新页面后重试。",
    same_origin_required: "上传请求未通过同源安全校验，请刷新页面后重试。",
    file_too_large: "图片超过 10 MiB。",
    image_pixel_limit: "图片像素超过 20,000,000。",
    unsupported_image: "仅支持 PNG 和 JPEG。",
    unsupported_image_format: "仅支持 PNG 和 JPEG。",
    image_mime_mismatch: "图片格式与文件声明不一致。",
    invalid_image: "图片已损坏或无法读取。",
    invalid_multipart: "上传请求解析失败。",
  });

  function uploadErrorMessage(code, status) {
    if (typeof code === "string" && UPLOAD_ERROR_MESSAGES[code]) {
      return UPLOAD_ERROR_MESSAGES[code];
    }
    if (Number(status) === 401) {
      return "登录验证已过期，请刷新页面后重试。";
    }
    return "服务器处理上传时出错，请稍后重试。";
  }

  async function responseError(response) {
    let code = "";
    try {
      const payload = await response.json();
      if (payload && typeof payload.error === "string") code = payload.error;
    } catch (error) {
      code = "";
    }
    const failure = new Error("request_failed");
    failure.code = code;
    failure.status = response.status;
    return failure;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function formatDate(value) {
    if (!value) return "未记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function addField(container, label, value) {
    const field = element("div", "rm-asset-field");
    field.append(element("span", "rm-asset-field-label", label));
    field.append(element("span", "rm-asset-field-value", value));
    container.append(field);
  }

  function renderTags(tags) {
    const container = element("div", "rm-asset-tags");
    (tags || []).forEach(function (tag) {
      container.append(element("span", "rm-asset-tag", tag));
    });
    if (!container.childNodes.length) {
      container.append(element("span", "rm-asset-tag-empty", "暂无标签"));
    }
    return container;
  }

  function createImage(asset, className, fallbackText, full) {
    const frame = element("div", className);
    const image = document.createElement("img");
    image.alt = asset.title || asset.filename || "图片资产";
    image.loading = full ? "eager" : "lazy";
    image.src = full ? asset.image_url : asset.thumbnail_url;
    const fallback = element("div", "rm-asset-image-fallback", fallbackText);
    fallback.hidden = true;
    image.addEventListener("error", function () {
      image.hidden = true;
      fallback.hidden = false;
    });
    frame.append(image, fallback);
    return frame;
  }

  function parseTags(value) {
    return value.split(/[,，]/).map(function (tag) { return tag.trim(); }).filter(Boolean);
  }

  function createBrowser(options) {
    const root = options.root;
    const fetcher = options.fetcher || window.fetch.bind(window);
    const apiBase = options.apiBase || "/api/assets";
    const state = {
      query: "", tag: "", offset: 0, limit: options.limit || DEFAULT_LIMIT,
      total: 0, loading: false, currentAssetId: "", uploadBusy: false,
    };
    let previewUrl = "";
    let selectedFile = null;

    const toolbar = element("form", "rm-asset-toolbar");
    const queryInput = document.createElement("input");
    queryInput.type = "search";
    queryInput.placeholder = "搜索标题、描述、标签或文件名";
    queryInput.setAttribute("aria-label", "搜索图片资产");
    const tagInput = document.createElement("input");
    tagInput.type = "search";
    tagInput.placeholder = "标签筛选";
    tagInput.setAttribute("aria-label", "按标签筛选");
    const searchButton = element("button", "rm-asset-search-button", "搜索");
    searchButton.type = "submit";
    const clearButton = element("button", "rm-asset-clear-button", "清除");
    clearButton.type = "button";
    toolbar.append(queryInput, tagInput, searchButton, clearButton);

    const summary = element("div", "rm-asset-summary");
    const body = element("div", "rm-asset-body");
    const pager = element("div", "rm-asset-pager");
    const previous = element("button", "rm-asset-page-button", "上一页");
    previous.type = "button";
    const pageText = element("span", "rm-asset-page-text", "");
    const next = element("button", "rm-asset-page-button", "下一页");
    next.type = "button";
    pager.append(previous, pageText, next);

    const detailOverlay = element("div", "rm-asset-detail-overlay");
    detailOverlay.hidden = true;
    const detail = element("section", "rm-asset-detail");
    detail.setAttribute("role", "dialog");
    detail.setAttribute("aria-modal", "true");
    detail.setAttribute("aria-label", "图片资产详情");
    const closeDetailButton = element("button", "rm-asset-detail-close", "关闭");
    closeDetailButton.type = "button";
    const detailBody = element("div", "rm-asset-detail-body");
    detail.append(closeDetailButton, detailBody);
    detailOverlay.append(detail);

    const uploadOverlay = element("div", "rm-asset-detail-overlay rm-asset-upload-overlay");
    uploadOverlay.hidden = true;
    const uploadDialog = element("section", "rm-asset-detail rm-asset-upload-dialog");
    uploadDialog.setAttribute("role", "dialog");
    uploadDialog.setAttribute("aria-modal", "true");
    uploadDialog.setAttribute("aria-label", "上传图片");
    const closeUploadButton = element("button", "rm-asset-detail-close", "关闭");
    closeUploadButton.type = "button";
    const uploadTitle = element("h2", "rm-asset-detail-title", "上传图片");
    const uploadForm = element("form", "rm-asset-upload-form");
    const dropZone = element("div", "rm-asset-drop-zone", "拖拽 PNG / JPEG 到这里，或点击选择文件");
    dropZone.tabIndex = 0;
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.name = "file";
    fileInput.accept = "image/png,image/jpeg,.png,.jpg,.jpeg";
    fileInput.hidden = true;
    const preview = element("div", "rm-asset-upload-preview");
    preview.hidden = true;
    const previewImage = document.createElement("img");
    const previewFacts = element("div", "rm-asset-upload-facts");
    preview.append(previewImage, previewFacts);
    const titleInput = document.createElement("input");
    titleInput.name = "title";
    titleInput.maxLength = 200;
    titleInput.placeholder = "标题（可选）";
    const descriptionInput = document.createElement("textarea");
    descriptionInput.name = "description";
    descriptionInput.maxLength = 4000;
    descriptionInput.rows = 4;
    descriptionInput.placeholder = "描述（可选）";
    const tagsInput = document.createElement("input");
    tagsInput.name = "tags";
    tagsInput.placeholder = "标签（多个标签用逗号分隔）";
    const uploadError = element("div", "rm-asset-form-error");
    uploadError.hidden = true;
    const uploadActions = element("div", "rm-asset-form-actions");
    const cancelUpload = element("button", "rm-asset-secondary-button", "取消");
    cancelUpload.type = "button";
    const submitUpload = element("button", "rm-asset-primary-button", "上传");
    submitUpload.type = "submit";
    submitUpload.disabled = true;
    uploadActions.append(cancelUpload, submitUpload);
    uploadForm.append(dropZone, fileInput, preview, titleInput, descriptionInput, tagsInput, uploadError, uploadActions);
    uploadDialog.append(closeUploadButton, uploadTitle, uploadForm);
    uploadOverlay.append(uploadDialog);

    const toast = element("div", "rm-asset-toast");
    toast.hidden = true;
    root.replaceChildren(toolbar, summary, body, pager, detailOverlay, uploadOverlay, toast);

    function showToast(message, kind) {
      toast.textContent = message;
      toast.className = "rm-asset-toast " + (kind || "success");
      toast.hidden = false;
      window.setTimeout(function () { toast.hidden = true; }, 3500);
    }

    function renderState(message, kind) {
      body.replaceChildren(element("div", "rm-asset-state " + kind, message));
    }

    function updatePager() {
      const first = state.total === 0 ? 0 : state.offset + 1;
      const last = Math.min(state.offset + state.limit, state.total);
      pageText.textContent = first + "–" + last + " / " + state.total;
      previous.disabled = state.offset === 0 || state.loading;
      next.disabled = state.offset + state.limit >= state.total || state.loading;
      pager.hidden = state.total === 0;
    }

    function renderCard(asset) {
      const card = element("article", "rm-asset-card");
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", "查看 " + (asset.title || asset.filename));
      card.append(createImage(asset, "rm-asset-thumbnail", "缩略图无法加载", false));
      const copy = element("div", "rm-asset-card-copy");
      copy.append(element("h3", "rm-asset-title", asset.title || asset.filename));
      copy.append(element("p", "rm-asset-description", asset.description || "暂无描述"));
      copy.append(renderTags(asset.tags));
      copy.append(element("p", "rm-asset-facts", asset.mime_type + " · " + asset.width + " × " + asset.height + " · " + formatBytes(asset.stored_bytes)));
      copy.append(element("p", "rm-asset-date", "更新于 " + formatDate(asset.updated_at)));
      card.append(copy);
      function open() { openDetail(asset.asset_id); }
      card.addEventListener("click", open);
      card.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      });
      return card;
    }

    function renderResults(payload) {
      state.total = payload.total;
      state.offset = payload.offset;
      summary.textContent = payload.total ? "共 " + payload.total + " 张已清理并持久保存的图片" : "";
      if (!payload.results.length) {
        const filtered = Boolean(state.query || state.tag);
        renderState(filtered ? "没有符合当前搜索条件的图片。" : "图片库还是空的。通过 Remember-Me 保存的图片会出现在这里。", filtered ? "no-results" : "empty");
      } else {
        const grid = element("div", "rm-asset-grid");
        payload.results.forEach(function (asset) { grid.append(renderCard(asset)); });
        body.replaceChildren(grid);
      }
      updatePager();
    }

    async function load() {
      if (state.loading) return;
      state.loading = true;
      updatePager();
      renderState("正在加载图片资产…", "loading");
      const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
      if (state.query) params.set("q", state.query);
      if (state.tag) params.set("tag", state.tag);
      try {
        const response = await fetcher(apiBase + "?" + params.toString());
        if (!response) return;
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "request_failed");
        renderResults(payload);
      } catch (error) {
        state.total = 0;
        summary.textContent = "";
        renderState("图片资产加载失败，请稍后重试。", "error");
        updatePager();
      } finally {
        state.loading = false;
        updatePager();
      }
    }

    function detailActions(asset) {
      const container = element("div", "rm-asset-detail-actions");
      const editButton = element("button", "rm-asset-secondary-button", "编辑信息");
      editButton.type = "button";
      const deleteButton = element("button", "rm-asset-danger-button", "删除图片");
      deleteButton.type = "button";
      container.append(editButton, deleteButton);

      editButton.addEventListener("click", function () {
        const form = element("form", "rm-asset-edit-form");
        const editTitle = document.createElement("input");
        editTitle.name = "title"; editTitle.maxLength = 200; editTitle.value = asset.title || "";
        const editDescription = document.createElement("textarea");
        editDescription.name = "description"; editDescription.maxLength = 4000; editDescription.rows = 5; editDescription.value = asset.description || "";
        const editTags = document.createElement("input");
        editTags.name = "tags"; editTags.value = (asset.tags || []).join(", ");
        const error = element("div", "rm-asset-form-error"); error.hidden = true;
        const actions = element("div", "rm-asset-form-actions");
        const cancel = element("button", "rm-asset-secondary-button", "取消"); cancel.type = "button";
        const save = element("button", "rm-asset-primary-button", "保存"); save.type = "submit";
        actions.append(cancel, save);
        form.append(editTitle, editDescription, editTags, error, actions);
        container.replaceWith(form);
        cancel.addEventListener("click", function () { openDetail(asset.asset_id); });
        form.addEventListener("submit", async function (event) {
          event.preventDefault(); save.disabled = true; save.textContent = "保存中…";
          try {
            const response = await fetcher(apiBase + "/" + encodeURIComponent(asset.asset_id), {
              method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ title: editTitle.value, description: editDescription.value, tags: parseTags(editTags.value) }),
            });
            if (!response) return;
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "request_failed");
            showToast("图片信息已更新"); await load(); await openDetail(asset.asset_id);
          } catch (requestError) {
            error.textContent = "保存失败，请检查输入后重试。"; error.hidden = false;
          } finally { save.disabled = false; save.textContent = "保存"; }
        });
      });

      deleteButton.addEventListener("click", function () {
        const confirm = element("div", "rm-asset-delete-confirm");
        confirm.append(element("p", "", "删除后将从 Remember-Me 图片库中永久移除，无法在新对话中再次找回。"));
        const actions = element("div", "rm-asset-form-actions");
        const cancel = element("button", "rm-asset-secondary-button", "取消"); cancel.type = "button";
        const confirmDelete = element("button", "rm-asset-danger-button", "确认删除"); confirmDelete.type = "button";
        actions.append(cancel, confirmDelete); confirm.append(actions); container.replaceWith(confirm);
        cancel.addEventListener("click", function () { openDetail(asset.asset_id); });
        confirmDelete.addEventListener("click", async function () {
          confirmDelete.disabled = true; confirmDelete.textContent = "删除中…";
          try {
            const response = await fetcher(apiBase + "/" + encodeURIComponent(asset.asset_id), { method: "DELETE" });
            if (!response) return;
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "request_failed");
            detailOverlay.hidden = true; state.currentAssetId = ""; state.offset = Math.max(0, Math.min(state.offset, Math.max(0, state.total - 2)));
            showToast("图片已永久删除"); await load();
          } catch (requestError) {
            showToast("删除失败，请稍后重试。", "error"); confirmDelete.disabled = false; confirmDelete.textContent = "确认删除";
          }
        });
      });
      return container;
    }

    async function openDetail(assetId) {
      state.currentAssetId = assetId;
      detailOverlay.hidden = false;
      detailBody.replaceChildren(element("div", "rm-asset-state loading", "正在加载详情…"));
      try {
        const response = await fetcher(apiBase + "/" + encodeURIComponent(assetId));
        if (!response) return;
        const asset = await response.json();
        if (!response.ok) throw new Error(asset.error || "request_failed");
        const fields = element("div", "rm-asset-detail-fields");
        addField(fields, "文件名", asset.filename); addField(fields, "格式", asset.mime_type);
        addField(fields, "尺寸", asset.width + " × " + asset.height); addField(fields, "存储大小", formatBytes(asset.stored_bytes));
        addField(fields, "创建时间", formatDate(asset.created_at)); addField(fields, "更新时间", formatDate(asset.updated_at));
        detailBody.replaceChildren(
          createImage(asset, "rm-asset-detail-image", "图片无法加载", true),
          element("h2", "rm-asset-detail-title", asset.title || asset.filename),
          element("p", "rm-asset-detail-description", asset.description || "暂无描述"),
          renderTags(asset.tags), fields, detailActions(asset)
        );
      } catch (error) {
        detailBody.replaceChildren(element("div", "rm-asset-state error", "资产详情加载失败。"));
      }
    }

    function resetUpload() {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = ""; selectedFile = null; fileInput.value = "";
      titleInput.value = ""; descriptionInput.value = ""; tagsInput.value = "";
      preview.hidden = true; previewImage.removeAttribute("src"); previewFacts.textContent = "";
      uploadError.hidden = true; submitUpload.disabled = true; submitUpload.textContent = "上传";
    }

    function closeUpload() { if (!state.uploadBusy) { uploadOverlay.hidden = true; resetUpload(); } }
    function openUpload() { resetUpload(); uploadOverlay.hidden = false; }

    function chooseFile(file) {
      uploadError.hidden = true;
      if (!file || !ALLOWED_TYPES.has(file.type)) { uploadError.textContent = "仅支持 PNG 和 JPEG 图片。"; uploadError.hidden = false; return; }
      if (file.size <= 0 || file.size > MAX_UPLOAD_BYTES) { uploadError.textContent = "图片必须小于或等于 10 MiB。"; uploadError.hidden = false; return; }
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      previewImage.src = previewUrl;
      previewImage.onload = function () {
        const pixels = previewImage.naturalWidth * previewImage.naturalHeight;
        if (!previewImage.naturalWidth || !previewImage.naturalHeight || pixels > MAX_IMAGE_PIXELS) {
          uploadError.textContent = "图片像素超过 20,000,000，无法上传。"; uploadError.hidden = false; selectedFile = null; submitUpload.disabled = true; return;
        }
        selectedFile = file; preview.hidden = false;
        previewFacts.textContent = file.name + " · " + formatBytes(file.size) + " · " + previewImage.naturalWidth + " × " + previewImage.naturalHeight;
        submitUpload.disabled = false;
      };
      previewImage.onerror = function () { uploadError.textContent = "图片无法读取或已损坏。"; uploadError.hidden = false; selectedFile = null; submitUpload.disabled = true; };
    }

    dropZone.addEventListener("click", function () { fileInput.click(); });
    dropZone.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });
    ["dragenter", "dragover"].forEach(function (name) { dropZone.addEventListener(name, function (event) { event.preventDefault(); dropZone.classList.add("dragging"); }); });
    ["dragleave", "drop"].forEach(function (name) { dropZone.addEventListener(name, function (event) { event.preventDefault(); dropZone.classList.remove("dragging"); }); });
    dropZone.addEventListener("drop", function (event) { chooseFile(event.dataTransfer.files[0]); });
    fileInput.addEventListener("change", function () { chooseFile(fileInput.files[0]); });

    uploadForm.addEventListener("submit", async function (event) {
      event.preventDefault(); if (!selectedFile || state.uploadBusy) return;
      state.uploadBusy = true; submitUpload.disabled = true; cancelUpload.disabled = true; closeUploadButton.disabled = true; submitUpload.textContent = "正在处理…";
      const data = new FormData(); data.append("file", selectedFile, selectedFile.name);
      data.append("title", titleInput.value); data.append("description", descriptionInput.value); data.append("tags", JSON.stringify(parseTags(tagsInput.value)));
      try {
        const response = await fetcher(apiBase, { method: "POST", body: data });
        if (!response) return;
        if (!response.ok) throw await responseError(response);
        const payload = await response.json();
        uploadOverlay.hidden = true; resetUpload(); state.offset = 0; await load(); await openDetail(payload.asset_id);
        showToast(payload.deduplicated ? "图片已存在，已打开已有资产。" : "图片上传成功");
      } catch (error) {
        uploadError.textContent = uploadErrorMessage(error.code, error.status);
        uploadError.hidden = false;
      } finally { state.uploadBusy = false; cancelUpload.disabled = false; closeUploadButton.disabled = false; submitUpload.disabled = !selectedFile; submitUpload.textContent = "上传"; }
    });

    toolbar.addEventListener("submit", function (event) { event.preventDefault(); state.query = queryInput.value.trim(); state.tag = tagInput.value.trim(); state.offset = 0; load(); });
    clearButton.addEventListener("click", function () { queryInput.value = ""; tagInput.value = ""; state.query = ""; state.tag = ""; state.offset = 0; load(); });
    previous.addEventListener("click", function () { state.offset = Math.max(0, state.offset - state.limit); load(); });
    next.addEventListener("click", function () { state.offset += state.limit; load(); });
    closeDetailButton.addEventListener("click", function () { detailOverlay.hidden = true; state.currentAssetId = ""; });
    detailOverlay.addEventListener("click", function (event) { if (event.target === detailOverlay) detailOverlay.hidden = true; });
    closeUploadButton.addEventListener("click", closeUpload); cancelUpload.addEventListener("click", closeUpload);
    uploadOverlay.addEventListener("click", function (event) { if (event.target === uploadOverlay) closeUpload(); });
    document.addEventListener("keydown", function (event) { if (event.key === "Escape") { if (!uploadOverlay.hidden) closeUpload(); else if (!detailOverlay.hidden) detailOverlay.hidden = true; } });
    if (options.uploadButton) options.uploadButton.addEventListener("click", openUpload);

    return { load: load, openUpload: openUpload, getState: function () { return Object.assign({}, state); } };
  }

  window.RememberMeAssetBrowser = {
    create: createBrowser,
    uploadErrorMessage: uploadErrorMessage,
  };
})();