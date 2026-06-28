"""Provider interfaces — LLM, embedding, tokenizer, reranker."""
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs) -> str: ...


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...


@dataclass
class OpenAILLM(LLMProvider):
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    temperature: float = 0.3

    async def chat(self, messages: list[dict], **kwargs) -> str:
        from openai import AsyncOpenAI
        key = self.api_key or os.environ.get("SPIDERWEB_LLM_API_KEY", "")
        client = AsyncOpenAI(api_key=key, base_url=self.base_url)
        response = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=kwargs.get("temperature", self.temperature),
            max_tokens=kwargs.get("max_tokens", 2048),
        )
        return response.choices[0].message.content


@dataclass
class VoyageEmbedding(EmbeddingProvider):
    model: str = "voyage-4-large"
    api_key: str = ""
    _dimensions: int = 1024

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import voyageai
        key = self.api_key or os.environ.get("SPIDERWEB_EMBEDDING_API_KEY", "") or os.environ.get("VOYAGE_API_KEY", "")
        vo = voyageai.Client(api_key=key)
        result = vo.embed(texts, model=self.model, input_type="document")
        return result.embeddings

    @property
    def dimensions(self) -> int:
        return self._dimensions


@dataclass
class NoopEmbedding(EmbeddingProvider):
    """No-op provider when embedding is disabled."""
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []

    @property
    def dimensions(self) -> int:
        return 0


def create_llm_provider(config: dict) -> LLMProvider:
    driver = config.get("driver", "openai")
    if driver == "openai":
        return OpenAILLM(
            model=config.get("model", "deepseek-chat"),
            base_url=config.get("base_url", "https://api.deepseek.com/v1"),
        )
    raise ValueError(f"Unknown LLM driver: {driver}")


def create_embedding_provider(config: dict) -> EmbeddingProvider | None:
    driver = config.get("driver", "").lower()
    if not driver or driver == "none":
        return None
    if driver == "voyage":
        return VoyageEmbedding(
            model=config.get("model", "voyage-4-large"),
        )
    raise ValueError(f"Unknown embedding driver: {driver}")
