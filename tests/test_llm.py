import asyncio

from app.llm import build_chat_request, build_embedding_request, close_http_client, get_http_client, parse_json_strings, parse_rerank_scores


def test_openai_compatible_request_shapes():
    """OpenAI-compatible settings should produce bearer-auth /v1 requests."""
    settings = {
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1/",
        "api_key": "secret",
        "chat_model": "chat-model",
        "embedding_model": "embedding-model",
        "temperature": 0.3,
    }

    chat = build_chat_request(settings, "Question")
    embedding = build_embedding_request(settings, ["Text"])

    assert chat["url"] == "https://api.example.com/v1/chat/completions"
    assert chat["headers"] == {"Authorization": "Bearer secret"}
    assert chat["json"]["model"] == "chat-model"
    assert embedding["url"] == "https://api.example.com/v1/embeddings"
    assert embedding["json"]["model"] == "embedding-model"


def test_azure_openai_request_shapes():
    """Azure OpenAI settings should produce deployment URLs and api-key auth."""
    settings = {
        "provider": "azure_openai",
        "base_url": "https://my-resource.openai.azure.com/",
        "api_key": "secret",
        "chat_model": "chat-deployment",
        "embedding_model": "embedding-deployment",
        "api_version": "2024-02-15-preview",
        "temperature": 0.3,
    }

    chat = build_chat_request(settings, "Question")
    embedding = build_embedding_request(settings, ["Text"])

    assert chat["url"] == (
        "https://my-resource.openai.azure.com/openai/deployments/"
        "chat-deployment/chat/completions?api-version=2024-02-15-preview"
    )
    assert chat["headers"] == {"api-key": "secret"}
    assert "model" not in chat["json"]
    assert embedding["url"] == (
        "https://my-resource.openai.azure.com/openai/deployments/"
        "embedding-deployment/embeddings?api-version=2024-02-15-preview"
    )
    assert embedding["headers"] == {"api-key": "secret"}


def test_model_json_helpers_accept_fenced_output():
    """Retrieval helpers should parse common fenced JSON model responses."""
    queries = parse_json_strings('```json\n["api version", "deployment name"]\n```')
    scores = parse_rerank_scores('```json\n[{"id": 2, "score": 0.8}, {"id": 1, "score": 1.5}]\n```')

    assert queries == ["api version", "deployment name"]
    assert scores == {2: 0.8, 1: 1.0}


def test_shared_http_client_is_reused():
    """LLM HTTP helper should reuse one AsyncClient until it is closed."""
    first = get_http_client()
    second = get_http_client()

    assert first is second

    asyncio.run(close_http_client())
