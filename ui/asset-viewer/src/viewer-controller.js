const ALLOWED_IMAGE_MIME_TYPES = new Set(["image/jpeg", "image/png"]);
const DEFAULT_CONNECT_TIMEOUT_MS = 8000;


function asText(value) {
  return typeof value === "string" ? value : "";
}


export function createViewerController(
  app,
  documentRef = document,
  options = {},
) {
  const connectTimeoutMs =
    options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
  const statusNode = documentRef.getElementById("status");
  const viewerNode = documentRef.getElementById("viewer");
  const imageNode = documentRef.getElementById("image");
  const titleNode = documentRef.getElementById("title");
  const filenameNode = documentRef.getElementById("filename");
  const dimensionsNode = documentRef.getElementById("dimensions");
  const mimeNode = documentRef.getElementById("mime");
  const tagsNode = documentRef.getElementById("tags");

  let phase = "loading";
  let connectTimer;

  function setStatus(message, state = "loading") {
    statusNode.textContent = message;
    statusNode.dataset.state = state;
    statusNode.hidden = false;
  }

  function clearImage() {
    imageNode.onload = null;
    imageNode.onerror = null;
    imageNode.removeAttribute("src");
    viewerNode.hidden = true;
  }

  function showError(message) {
    phase = "error";
    clearImage();
    setStatus(message, "error");
  }

  function renderToolResult(result) {
    const structured = result?.structuredContent;
    const rememberMe = result?._meta?.rememberMe;

    if (!rememberMe && structured) {
      showError("Image metadata received, but image bytes were unavailable.");
      return;
    }
    if (!structured || !rememberMe || rememberMe.schemaVersion !== 1) {
      showError("The image result was unavailable.");
      return;
    }
    if (!ALLOWED_IMAGE_MIME_TYPES.has(rememberMe.mimeType)) {
      showError("The image type was not allowed.");
      return;
    }
    if (
      typeof rememberMe.imageBase64 !== "string"
      || rememberMe.imageBase64.length === 0
    ) {
      showError("Image metadata received, but image bytes were unavailable.");
      return;
    }

    const filename = asText(structured.filename);
    const title = asText(structured.title) || filename;
    titleNode.textContent = title;
    filenameNode.textContent = filename;
    dimensionsNode.textContent =
      Number.isInteger(structured.width) && Number.isInteger(structured.height)
        ? `${structured.width} x ${structured.height}`
        : "";
    mimeNode.textContent = asText(structured.mime_type);

    tagsNode.replaceChildren();
    const tags = Array.isArray(structured.tags) ? structured.tags : [];
    for (const value of tags) {
      if (typeof value !== "string") {
        continue;
      }
      const tagNode = documentRef.createElement("span");
      tagNode.className = "tag";
      tagNode.textContent = value;
      tagsNode.appendChild(tagNode);
    }

    imageNode.alt = title;
    imageNode.onload = () => {
      phase = "ready";
      statusNode.textContent = "Image loaded.";
      statusNode.dataset.state = "ready";
      statusNode.hidden = true;
      viewerNode.hidden = false;
    };
    imageNode.onerror = () => {
      showError("The image could not be decoded safely.");
    };
    imageNode.src =
      `data:${rememberMe.mimeType};base64,${rememberMe.imageBase64}`;
  }

  app.ontoolinput = () => {
    if (phase !== "ready" && phase !== "error") {
      phase = "waiting";
      setStatus("Connected. Waiting for image...");
    }
  };
  app.ontoolresult = renderToolResult;
  app.ontoolcancelled = () => {
    showError("The image request was cancelled.");
  };
  app.onhostcontextchanged = (context) => {
    if (context?.theme === "dark" || context?.theme === "light") {
      documentRef.documentElement.dataset.theme = context.theme;
    }
  };
  app.onteardown = async () => {
    clearTimeout(connectTimer);
    clearImage();
    return {};
  };
  app.onerror = () => {
    showError("The viewer encountered a protocol error.");
  };

  async function connect(transport) {
    phase = "connecting";
    setStatus("Connecting to host...");
    const controller = new AbortController();
    connectTimer = setTimeout(() => controller.abort(), connectTimeoutMs);
    try {
      await app.connect(transport, { signal: controller.signal });
      clearTimeout(connectTimer);
      if (phase === "connecting") {
        phase = "waiting";
        setStatus("Connected. Waiting for image...");
      }
    } catch (_error) {
      clearTimeout(connectTimer);
      showError(
        controller.signal.aborted
          ? "The viewer connection timed out."
          : "The viewer could not connect to the host.",
      );
    }
  }

  return {
    connect,
    renderToolResult,
  };
}