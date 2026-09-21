from pathlib import Path


ASSET_VIEWER_URI = "ui://remember-me/asset-viewer.html"
ASSET_VIEWER_MIME_TYPE = "text/html;profile=mcp-app"
ASSET_VIEWER_PATH = Path(__file__).with_name("asset_viewer.html")

ASSET_VIEWER_TOOL_META = {
    "ui": {"resourceUri": ASSET_VIEWER_URI},
    "ui/resourceUri": ASSET_VIEWER_URI,
}

ASSET_VIEWER_RESOURCE_META = {
    "ui": {
        "csp": {
            "connectDomains": [],
            "resourceDomains": [],
        },
        "prefersBorder": True,
    }
}


def load_asset_viewer_html() -> str:
    return ASSET_VIEWER_PATH.read_text(encoding="utf-8")


ASSET_VIEWER_HTML = load_asset_viewer_html()