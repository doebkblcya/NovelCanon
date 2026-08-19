"""配置加载、强校验与 config hash（ADR-0003）。"""

import pytest
from pydantic import ValidationError

from novelcanon.config.hash import stable_config_hash
from novelcanon.config.settings import AppSettings, EmbeddingProfile, GenerationProfile


def test_unknown_field_rejected() -> None:
    # 拼写错误（db_paht）必须启动失败，不得静默忽略
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, db_paht="data/x.db")


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, log_level="VERBOSE")


def test_app_config_hash_stable() -> None:
    a = AppSettings(_env_file=None)
    b = AppSettings(_env_file=None)
    assert a.config_hash() == b.config_hash()
    assert len(a.config_hash()) == 64


def test_app_config_hash_changes_with_value() -> None:
    base = AppSettings(_env_file=None).config_hash()
    changed = AppSettings(_env_file=None, log_json=True).config_hash()
    assert base != changed


def test_profile_hash_stable() -> None:
    kwargs = dict(
        profile_id="g1",
        context_window=8000,
        max_output_tokens=1024,
        structured_output_mode="json_schema",
        tokenizer_id="t1",
    )
    a = GenerationProfile(**kwargs)
    b = GenerationProfile(**kwargs)
    assert a.config_hash == b.config_hash
    assert len(a.config_hash) == 64


def test_profile_hash_sensitive_to_fields() -> None:
    a = GenerationProfile(
        profile_id="g1",
        context_window=8000,
        max_output_tokens=1024,
        structured_output_mode="json_schema",
        tokenizer_id="t1",
    )
    b = GenerationProfile(
        profile_id="g1",
        context_window=8000,
        max_output_tokens=2048,
        structured_output_mode="json_schema",
        tokenizer_id="t1",
    )
    assert a.config_hash != b.config_hash


def test_embedding_profile_hash() -> None:
    a = EmbeddingProfile(
        profile_id="e1", tokenizer_id="tok", max_input_tokens=512, vector_dimension=1024
    )
    b = EmbeddingProfile(
        profile_id="e1", tokenizer_id="tok", max_input_tokens=512, vector_dimension=1024
    )
    assert a.config_hash == b.config_hash


def test_stable_config_hash_canonical() -> None:
    assert stable_config_hash({"b": 1, "a": [2, 1]}) == stable_config_hash({"a": [2, 1], "b": 1})
