"""Vertex AI 通道（用 GCP 服务账号身份调用 Gemini，没有 API key）的测试。

不联网、不需要 GCP 账号：把 SDK 的 genai.Client 换成一个"记录员"，检查我们传给它的参数对不对，
以及凭证找不到时的报错是不是能照着做的提示。真正连到 Vertex AI 的验证在部署的时候做（见 docs/deployment.md）。
"""

import pytest
from google.auth.exceptions import DefaultCredentialsError

from app.config import Settings
from app.llm import gemini
from app.llm.factory import LLMNotConfigured, create_llm_client
from app.rag.embedder import GeminiEmbedder, create_embedder


class RecordingClient:
    """代替 genai.Client：只记录构造参数。"""

    calls: list[dict] = []

    def __init__(self, **kwargs):
        RecordingClient.calls.append(kwargs)


@pytest.fixture(autouse=True)
def recorder(monkeypatch):
    RecordingClient.calls = []
    monkeypatch.setattr(gemini.genai, "Client", RecordingClient)
    return RecordingClient


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_the_default_channel_is_the_api_key_and_nothing_changes_for_existing_setups(recorder):
    create_llm_client(settings(gemini_api_key="k", gemini_model="m"))
    [call] = recorder.calls
    assert call["api_key"] == "k" and "vertexai" not in call and "project" not in call


def test_vertex_uses_the_project_and_location_and_never_an_api_key(recorder):
    create_llm_client(settings(llm_provider="vertex", gcp_project="my-proj", gcp_location="europe-west1", gemini_model="m", gemini_api_key="must-not-be-used"))
    [call] = recorder.calls
    assert call["vertexai"] is True and call["project"] == "my-proj" and call["location"] == "europe-west1"
    assert "api_key" not in call  # 同时传 api_key 和 vertexai，SDK 会认为配置搞混了；而且 Vertex 的意义就是不用密钥


def test_the_default_location_is_used_when_none_is_given(recorder):
    create_llm_client(settings(llm_provider="vertex", gcp_project="p", gemini_model="m"))
    assert recorder.calls[0]["location"] == "us-central1"


@pytest.mark.parametrize("missing", [dict(gcp_project=""), dict(gemini_model="")])
def test_vertex_without_a_project_or_model_is_not_configured(missing):
    base = dict(llm_provider="vertex", gcp_project="p", gemini_model="m")
    with pytest.raises(LLMNotConfigured, match="GCP_PROJECT"):
        create_llm_client(settings(**{**base, **missing}))


def test_missing_google_credentials_become_an_actionable_message(monkeypatch):
    def no_credentials(**kwargs):
        raise DefaultCredentialsError("Your default credentials were not found. To set up Application Default Credentials, see ...")

    monkeypatch.setattr(gemini.genai, "Client", no_credentials)
    with pytest.raises(LLMNotConfigured) as error:
        create_llm_client(settings(llm_provider="vertex", gcp_project="p", gemini_model="m"))
    assert "gcloud auth application-default login" in str(error.value) and "service account" in str(error.value)
    assert "Traceback" not in str(error.value) and "default credentials were not found" not in str(error.value)  # 不把 SDK 的长异常原样抛出


# ============================================================ 向量化器走同一个通道
def test_the_embedder_follows_the_same_channel_and_keeps_the_same_index_id(recorder):
    api = create_embedder(settings(gemini_api_key="k"))
    vertex = create_embedder(settings(llm_provider="vertex", gcp_project="p"))
    assert recorder.calls[0]["api_key"] == "k" and recorder.calls[1]["vertexai"] is True
    # 模型和维度相同，向量 id 就相同：从 API key 切到 Vertex AI，已有的知识库索引不会被判为"不匹配"，也不用重新入库
    assert api.id == vertex.id == "gemini:gemini-embedding-001:768"


def test_embeddings_over_vertex_need_a_project_not_an_api_key():
    assert isinstance(create_embedder(settings(llm_provider="vertex", gcp_project="p")), GeminiEmbedder)
    with pytest.raises(LLMNotConfigured, match="GCP_PROJECT"):
        create_embedder(settings(llm_provider="vertex"))
    with pytest.raises(LLMNotConfigured, match="GEMINI_API_KEY"):
        create_embedder(settings())  # 默认通道仍然要 API key


def test_the_offline_hash_embedder_is_unaffected_by_the_provider():
    assert create_embedder(settings(llm_provider="vertex", embedding_provider="hash")).id.startswith("hash")


def test_embedder_credentials_error_is_also_actionable(monkeypatch):
    monkeypatch.setattr(gemini.genai, "Client", lambda **kw: (_ for _ in ()).throw(DefaultCredentialsError("no creds")))
    with pytest.raises(LLMNotConfigured, match="application-default login"):
        create_embedder(settings(llm_provider="vertex", gcp_project="p"))


def test_the_provider_setting_is_validated_and_defaults_to_the_api_key():
    assert Settings.model_fields["llm_provider"].default == "gemini-api"
    with pytest.raises(Exception):
        settings(llm_provider="openai")


def test_the_builder_has_its_own_safe_default_location(recorder):
    gemini.build_genai_client(vertex_project="p")  # 不经过 Settings，直接调用构造函数
    assert recorder.calls[0]["location"] == "us-central1"
