"""Unit tests for NVIDIA NIM LLM client."""

import requests
import pytest
from unittest.mock import patch, MagicMock
from llm_client import NIMClient, NIMError


def test_nim_missing_key():
    client = NIMClient(api_key="")
    with pytest.raises(NIMError, match="NVIDIA_API_KEY is not configured"):
        client.chat_completion([{"role": "user", "content": "hello"}])


def test_nim_successful_completion():
    client = NIMClient(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "READY", "tool_calls": None}}]
    }
    with patch("requests.post", return_value=mock_resp) as mock_post:
        message = client.chat_completion([{"role": "user", "content": "hello"}])
    assert message["role"] == "assistant"
    assert message["content"] == "READY"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer " + "test_key"


def test_nim_bearer_key_is_not_prefixed_twice():
    client = NIMClient(api_key="Bearer " + "test_key")
    assert client._get_headers()["Authorization"] == "Bearer " + "test_key"


def test_nim_rejects_empty_bearer_token():
    client = NIMClient(api_key="Bearer ")

    with pytest.raises(NIMError, match="NVIDIA_API_KEY is not configured"):
        client._get_headers()


def test_nemotron_disables_reasoning_for_structured_output():
    client = NIMClient(api_key="test_key", model="nvidia/nemotron-3-nano-30b-a3b")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
    with patch("requests.post", return_value=mock_resp) as mock_post:
        client.chat_completion([{"role": "user", "content": "Parse this request"}])

    assert mock_post.call_args.kwargs["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_nim_tool_calls_response():
    client = NIMClient(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "search_flights",
                        "arguments": '{"origin": "DEL", "destination": "BOM", "travel_date": "2026-09-24"}',
                    },
                }],
            }
        }]
    }
    with patch("requests.post", return_value=mock_resp):
        message = client.chat_completion([{"role": "user", "content": "Find flight DEL to BOM"}])
    assert message["tool_calls"][0]["function"]["name"] == "search_flights"


def test_nim_malformed_json_shape_is_reported():
    client = NIMClient(api_key="test_key")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = "[]"
    mock_resp.json.return_value = []
    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(NIMError, match="malformed response object"):
            client.chat_completion([{"role": "user", "content": "hello"}])


def test_nim_unavailable_service_is_reported():
    client = NIMClient(api_key="test_key")
    with patch("requests.post", side_effect=requests.Timeout()):
        with pytest.raises(NIMError, match="Timed out"):
            client.chat_completion([{"role": "user", "content": "hello"}])


def test_nim_410_error_handling():
    client = NIMClient(api_key="test_key", model="meta/llama-3.3-70b-instruct")
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 410
    mock_resp.text = "Model deprecated"
    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(NIMError, match="deprecated or gone"):
            client.chat_completion([{"role": "user", "content": "hello"}])
