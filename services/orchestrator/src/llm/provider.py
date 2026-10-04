import logging
import os
from typing import Dict, List, Optional

import httpx
from openai import OpenAI

logger = logging.getLogger(__name__)


class UniversalLLMProvider:
    """Connects the Python agent loop using the OpenAI-compatible SDK interface."""

    def __init__(self, endpoint_url: str, model: str, api_key: Optional[str] = None):
        self.endpoint_url = endpoint_url
        self.model = model

        base_url = endpoint_url.split("/chat/completions")[0].split("/responses")[0]
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None

        if "127.0.0.1" in endpoint_url or "localhost" in endpoint_url or "11434" in endpoint_url:
            self.client = OpenAI(base_url=base_url, api_key="ollama", http_client=http_client)
        else:
            if "azure.com" in endpoint_url and (not api_key or api_key.lower() in ["none", "", "entra"]):
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                token_provider = get_bearer_token_provider(
                    DefaultAzureCredential(), "https://ai.azure.com/.default"
                )
                logger.info("🔐 Azure Entra ID authentication enabled via OpenAI SDK.")
                self.client = OpenAI(base_url=base_url, api_key=token_provider, http_client=http_client)
            else:
                self.client = OpenAI(base_url=base_url, api_key=api_key or "sk-dummy", http_client=http_client)

    def generate(self, context: list, require_json: bool = True) -> Optional[str]:
        try:
            if "azure.com" in self.endpoint_url and "/responses" in self.endpoint_url:
                response = self.client.responses.create(model=self.model, input=context)
                try:
                    return response.output[0].content[0].text
                except (KeyError, IndexError, AttributeError):
                    return str(getattr(response, "output", response))
            else:
                kwargs = {"model": self.model, "messages": context}
                if require_json:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
        except Exception as e:
            logger.error(f"❌ [SDK Connection Error] Failed to generate: {e}")
            raise RuntimeError(f"LLM Provider unreachable: {e}")


class MockLLMProvider:
    """Mocks structured JSON generation for unit tests."""

    def __init__(self, mock_responses: List[str]):
        self.mock_responses = mock_responses
        self.index = 0

    def generate(self, context: List[Dict[str, str]]) -> str:
        if self.index < len(self.mock_responses):
            response = self.mock_responses[self.index]
            self.index += 1
            return response
        return '{"reasoning": "No more instructions.", "tool": "finish_task", "tool_args": {}}'