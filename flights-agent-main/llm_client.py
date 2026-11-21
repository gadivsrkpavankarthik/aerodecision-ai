"""NVIDIA NIM chat-completions client."""

from typing import Any, Optional

import requests

from config import config


class NIMError(Exception):
    """Exception raised for NVIDIA NIM API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class NIMClient:
    """Client for NVIDIA NIM Chat Completions and tool calling."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key if api_key is not None else config.nvidia_api_key
        self.base_url = (base_url or config.nvidia_base_url).rstrip("/")
        self.model = model or config.nvidia_model

    def _get_headers(self) -> dict[str, str]:
        if not self.api_key or not self.api_key.strip():
            raise NIMError("NVIDIA_API_KEY is not configured.")
        token = self.api_key.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or token.casefold() == "bearer":
            raise NIMError("NVIDIA_API_KEY is not configured.")
        authorization = "Bearer " + token
        return {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Send a completion request and return the validated assistant message."""
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.model.startswith("nvidia/nemotron-3"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        try:
            response = requests.post(url, json=payload, headers=self._get_headers(), timeout=8)
        except requests.Timeout as exc:
            raise NIMError("Timed out connecting to NVIDIA NIM.") from exc
        except requests.ConnectionError as exc:
            raise NIMError("Network error connecting to NVIDIA NIM.") from exc
        except requests.RequestException as exc:
            raise NIMError(f"Request to NVIDIA NIM failed ({type(exc).__name__}).") from exc

        if response.status_code == 410:
            raise NIMError(
                f"NVIDIA NIM model '{self.model}' is deprecated or gone (HTTP 410): {response.text[:300]}",
                status_code=410,
                response_body=response.text,
            )
        if response.status_code in (401, 403):
            raise NIMError(
                f"NVIDIA NIM authorization failed (HTTP {response.status_code}): {response.text[:300]}",
                status_code=response.status_code,
                response_body=response.text,
            )
        if not response.ok:
            raise NIMError(
                f"NVIDIA NIM API error (HTTP {response.status_code}): {response.text[:300]}",
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise NIMError(
                "NVIDIA NIM returned malformed JSON response.",
                response_body=response.text[:300],
            ) from exc
        if not isinstance(data, dict):
            raise NIMError("NVIDIA NIM returned a malformed response object.", response_body=response.text[:300])
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise NIMError("NVIDIA NIM returned an empty choices list.", response_body=response.text[:300])
        first_choice = choices[0]
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if not isinstance(message, dict):
            raise NIMError("NVIDIA NIM returned a malformed assistant message.", response_body=response.text[:300])
        return message


nim_client = NIMClient()
