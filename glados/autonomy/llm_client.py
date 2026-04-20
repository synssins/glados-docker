"""
Simple LLM client for subagent use.

Provides a minimal interface for making LLM calls without the full
complexity of the main LLM processor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests
from loguru import logger


@dataclass
class LLMConfig:
    """Configuration for LLM API calls.

    `model` is intentionally required (no default). Callers resolve it
    from cfg.service_model("ollama_autonomy") or equivalent. Hard-coded
    defaults silently route to unintended backends; empty-required fails
    loud at construction time instead.
    """

    url: str
    model: str
    api_key: str | None = None
    timeout: float = 30.0

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def llm_call(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    json_response: bool = False,
) -> str | None:
    """
    Make a simple LLM call.

    Returns the assistant's response text, or None on error.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    data = {
        "model": config.model,
        "messages": messages,
        "stream": False,
    }

    if json_response:
        data["response_format"] = {"type": "json_object"}

    try:
        response = requests.post(
            config.url,
            headers=config.headers,
            json=data,
            timeout=config.timeout,
        )
        response.raise_for_status()
        result = response.json()

        # Handle OpenAI-style response
        if "choices" in result and result["choices"]:
            return result["choices"][0]["message"]["content"]

        # Handle Ollama-style response
        if "message" in result:
            return result["message"].get("content")

        logger.warning("LLM call: unexpected response format")
        return None

    except requests.Timeout:
        logger.warning("LLM call timed out")
        return None
    except requests.RequestException as e:
        logger.warning("LLM call failed: %s", e)
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM call: failed to parse response: %s", e)
        return None
