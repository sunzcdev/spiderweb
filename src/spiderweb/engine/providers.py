"""Provider interfaces — LLM, embedding, tokenizer, reranker."""
import os
import sys
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
    fallback: list = field(default_factory=list)

    async def chat(self, messages: list[dict], **kwargs) -> str:
        import httpx
        from openai import AsyncOpenAI

        providers = [
            {"base_url": self.base_url, "api_key": self.api_key, "model": self.model, "_label": "primary"},
        ]
        for fb in self.fallback:
            key = os.environ.get(fb.get("api_key_env", ""), "")
            if not key:
                continue
            providers.append({
                "base_url": fb.get("base_url", self.base_url),
                "api_key": key,
                "model": fb.get("model", self.model),
                "_label": f"fallback:{fb.get('model', '?')}",
            })

        last_error = None
        for prov in providers:
            key = prov["api_key"] or os.environ.get("SPIDERWEB_LLM_API_KEY", "")
            if not key:
                continue
            try:
                timeout = httpx.Timeout(60.0, connect=15.0, read=60.0, write=60.0)
                client = AsyncOpenAI(api_key=key, base_url=prov["base_url"], timeout=timeout)
                response = await client.chat.completions.create(
                    model=prov["model"],
                    messages=messages,
                    temperature=kwargs.get("temperature", 0.0),
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except Exception as e:
                label = prov["_label"]
                if label == "primary":
                    print(f"    ⚠️ LLM 主通道失败: {e}", file=sys.stderr)
                else:
                    print(f"    ⚠️ {label} 也失败: {e}", file=sys.stderr)
                last_error = e
                continue

        raise last_error or RuntimeError("All LLM providers failed")


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
            api_key=config.get("api_key", ""),
            fallback=config.get("fallback", []),
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
            api_key=config.get("api_key", ""),
        )
    raise ValueError(f"Unknown embedding driver: {driver}")
