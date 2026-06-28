"""Provider interfaces — LLM, embedding, tokenizer, reranker."""
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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


class TokenizerProvider(ABC):
    """Tokenizes text into space-separated tokens for FTS5 indexing."""
    @abstractmethod
    def tokenize(self, text: str) -> str: ...


class RerankerProvider(ABC):
    """Re-ranks search results by relevance to query."""
    @abstractmethod
    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]: ...


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


@dataclass
class JiebaTokenizer(TokenizerProvider):
    """Chinese word segmentation via jieba (pure Python)."""
    dict_path: str = ""
    _initialized: bool = field(default=False, init=False)

    def _init(self):
        if self._initialized:
            return
        import jieba
        self._jieba = jieba
        if self.dict_path and os.path.exists(self.dict_path):
            jieba.set_dictionary(self.dict_path)
        self._initialized = True

    def tokenize(self, text: str) -> str:
        self._init()
        tokens = self._jieba.cut(text.strip())
        return " ".join(t for t in tokens if t.strip())


def create_tokenizer_provider(config: dict) -> TokenizerProvider | None:
    driver = config.get("driver", "").lower()
    if not driver or driver == "none":
        return None
    if driver == "jieba":
        return JiebaTokenizer(dict_path=config.get("dict_path", ""))
    raise ValueError(f"Unknown tokenizer driver: {driver}")


@dataclass
class VoyageReranker(RerankerProvider):
    model: str = "rerank-2-lite"
    api_key: str = ""

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        import voyageai
        key = self.api_key or os.environ.get("SPIDERWEB_EMBEDDING_API_KEY", "") or os.environ.get("VOYAGE_API_KEY", "")
        vo = voyageai.Client(api_key=key)
        result = vo.rerank(query=query, documents=documents, model=self.model, top_k=top_n)
        return [(r.index, r.relevance_score) for r in result.results]


def create_reranker_provider(config: dict) -> RerankerProvider | None:
    driver = config.get("driver", "").lower()
    if not driver or driver == "none":
        return None
    if driver == "voyage":
        return VoyageReranker(model=config.get("model", "rerank-2-lite"))
    raise ValueError(f"Unknown reranker driver: {driver}")


def create_embedding_provider(config: dict) -> EmbeddingProvider | None:
    driver = config.get("driver", "").lower()
    if not driver or driver == "none":
        return None
    if driver == "voyage":
        return VoyageEmbedding(
            model=config.get("model", "voyage-4-large"),
        )
    raise ValueError(f"Unknown embedding driver: {driver}")
