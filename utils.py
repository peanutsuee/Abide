# ============================================================
# Module: Common Utilities (utils.py)
# ??:??????
#
# Provides config loading, logging init, path safety, ID generation, etc.
# ????????????????????ID ???????
#
# Depended on by: server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# ????:server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# ============================================================

import os
import re
import uuid
import yaml
import logging
from pathlib import Path
from datetime import datetime


def load_config(config_path: str = None) -> dict:
    """
    Load configuration file.
    ???????

    Priority: environment variables > config.yaml > built-in defaults.
    ???:???? > config.yaml > ??????
    """
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- ??????(??,?????? config.yaml ???)---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "buckets_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"),
        "raw_evidence_root": None,
        "merge_threshold": 75,
        "dehydration": {
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
    }

    # --- Load user config from YAML file ---
    # --- ? YAML ??????????? ---
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"????????? YAML ??,??????: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"????????,??????: {e}"
            )

    # --- Environment variable overrides (highest priority) ---
    # --- ????????/?????(?????)---
    env_api_key = os.environ.get("OMBRE_API_KEY", "")
    if env_api_key:
        config.setdefault("dehydration", {})["api_key"] = env_api_key

    env_base_url = os.environ.get("OMBRE_BASE_URL", "")
    if env_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_base_url

    env_transport = os.environ.get("OMBRE_TRANSPORT", "")
    if env_transport:
        config["transport"] = env_transport

    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")
    if env_buckets_dir:
        config["buckets_dir"] = env_buckets_dir

    # Raw Evidence is explicitly opt-in at the import boundary.  Keep the
    # configured path inert here; the capture coordinator validates and opens
    # it only after a request explicitly enables capture.
    env_raw_evidence_root = os.environ.get("OMBRE_RAW_EVIDENCE_ROOT", "")
    if env_raw_evidence_root:
        config["raw_evidence_root"] = env_raw_evidence_root

    # OMBRE_DEHYDRATION_MODEL (with OMBRE_MODEL alias) overrides dehydration.model
    env_dehy_model = os.environ.get("OMBRE_DEHYDRATION_MODEL", "") or os.environ.get("OMBRE_MODEL", "")
    if env_dehy_model:
        config.setdefault("dehydration", {})["model"] = env_dehy_model

    # OMBRE_DEHYDRATION_BASE_URL overrides dehydration.base_url
    env_dehy_base_url = os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "")
    if env_dehy_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_dehy_base_url

    # OMBRE_EMBEDDING_MODEL overrides embedding.model
    env_embed_model = os.environ.get("OMBRE_EMBEDDING_MODEL", "")
    if env_embed_model:
        config.setdefault("embedding", {})["model"] = env_embed_model

    # OMBRE_EMBEDDING_BASE_URL overrides embedding.base_url
    env_embed_base_url = os.environ.get("OMBRE_EMBEDDING_BASE_URL", "")
    if env_embed_base_url:
        config.setdefault("embedding", {})["base_url"] = env_embed_base_url

    env_embed_api_key = os.environ.get("OMBRE_EMBEDDING_API_KEY", "")
    if env_embed_api_key:
        embedding_config = config.setdefault("embedding", {})
        embedding_config["api_key"] = env_embed_api_key
        if not env_embed_model:
            # The international and China services expose different catalogs.
            embedding_config["model"] = "Qwen/Qwen3-Embedding-0.6B"
        if not env_embed_base_url:
            embedding_config["base_url"] = "https://api.siliconflow.com/v1"
    if env_embed_api_key or env_embed_model or env_embed_base_url:
        config.setdefault("embedding", {})["independent"] = True

    return config


def ensure_bucket_storage(config: dict) -> None:
    """Create bucket directories only when a runtime actually needs storage."""
    buckets_dir = config["buckets_dir"]
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    ????????,override ???? base?
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def setup_logging(level: str = "INFO") -> None:
    """
    Initialize logging system.
    ????????

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    ??:MCP stdio ??? stdout ?????,????? stderr?
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler()],  # StreamHandler defaults to stderr
    )


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    ???????? ID(12 ?? UUID,??????)?
    """
    return uuid.uuid4().hex[:12]


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] ? word
    ?? Obsidian ????
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    ?????,?????????????????
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    ?????????,????????? base_dir ???
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Path safety check failed / ????????: "
            f"{target} is not inside / ?? {base} ?"
        )
    return target


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    ???? token ??

    Chinese ? 1 char = 1.5 tokens, English ? 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    ?? ? 1?=1.5token,?? ? 1?=1.3token?
    ????????????,??????
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(chinese_chars * 1.5 + english_words * 1.3 + len(text) * 0.05)


def now_iso() -> str:
    """
    Return current time as ISO format string.
    ??????? ISO ??????
    """
    return datetime.now().isoformat(timespec="seconds")
