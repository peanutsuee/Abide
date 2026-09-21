from utils import load_config


def test_independent_embedding_key_defaults_to_siliconflow(monkeypatch, tmp_path):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_EMBEDDING_API_KEY", "test-key")
    monkeypatch.delenv("OMBRE_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("OMBRE_EMBEDDING_BASE_URL", raising=False)

    config = load_config(str(tmp_path / "missing.yaml"))

    assert config["embedding"] == {
        "api_key": "test-key",
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "base_url": "https://api.siliconflow.com/v1",
        "independent": True,
    }


def test_explicit_embedding_environment_overrides_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("OMBRE_EMBEDDING_MODEL", "custom-model")
    monkeypatch.setenv("OMBRE_EMBEDDING_BASE_URL", "https://example.com/v1")

    config = load_config(str(tmp_path / "missing.yaml"))

    assert config["embedding"]["model"] == "custom-model"
    assert config["embedding"]["base_url"] == "https://example.com/v1"
