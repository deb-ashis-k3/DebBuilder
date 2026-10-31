"""RAG on LangChain (n8n's embeddings / vector stores / document loaders / text splitters /
retrievers / rerankers / Q&A + summarization chains).

Everything pluggable is a sub-node, wired like models and tools:

    Vector Store: Insert ─┬─ <Vector store> ── <Embeddings>
                          └─ <Document loader> ── <Text splitter>
    Q&A Chain ── <Retriever> ── <Vector store> ── <Embeddings>      (+ optional <Reranker>)
    AI Agent ── Vector Store Tool ── <Vector store> ── <Embeddings>

Vector stores are LangChain `VectorStore`s. PGVector, Qdrant, Pinecone and Supabase talk to the
service directly (SQL / REST: no extra client libraries); In-Memory is LangChain's own store.
Scores are always "higher = more similar".
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.vectorstores import InMemoryVectorStore, VectorStore
from pydantic import ConfigDict, Field, create_model

from ..netguard import guarded_client
from ..engine.expressions import evaluate
from .ai import AI, MODEL_COMMON, SUB_TOOL, TOOL_COMMON, _opt, _tool_name, message_text, model_name
from .base import NodeContext, NodeError, NodeType, ai_input, prop, register

SUB_EMBEDDING = dict(group="ai_embedding", color="#f59e0b", inputs=[], outputs=[])
SUB_DOCUMENT = dict(group="ai_document", color="#06b6d4", inputs=[], outputs=[])
SUB_SPLITTER = dict(group="ai_text_splitter", color="#84cc16", inputs=[], outputs=[])
SUB_VECTOR = dict(group="ai_vector_store", color="#6366f1", inputs=[], outputs=[])
SUB_RETRIEVER = dict(group="ai_retriever", color="#14b8a6", inputs=[], outputs=[])
SUB_RERANKER = dict(group="ai_reranker", color="#f97316", inputs=[], outputs=[])


async def _one(ctx: NodeContext, handle: str, what: str, i: int = 0, required: bool = True):
    got = await ctx.supply(handle, i)
    if not got and required:
        raise NodeError(f"Connect a {what} sub-node to the '{what}' input", ctx.name)
    return got[0] if got else None


def doc_json(d: Document, score: float | None = None) -> dict:
    out: dict = {"document": {"pageContent": d.page_content, "metadata": d.metadata}}
    if score is not None:
        out["score"] = round(float(score), 6)
    return out


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


async def _emb_openai(ctx: NodeContext):
    from langchain_openai import OpenAIEmbeddings

    _, cred = await ctx.credential()
    p = ctx.params()
    custom = (cred.get("base_url") or "").rstrip("/") not in ("", "https://api.openai.com/v1")
    return OpenAIEmbeddings(**_opt({"model": p["model"], "api_key": cred.get("api_key") or "not-needed",
                                    # other OpenAI-compatible servers can't count tokens with tiktoken
                                    "check_embedding_ctx_length": not custom},
                                   base_url=cred.get("base_url"), dimensions=p.get("dimensions"),
                                   chunk_size=p.get("batch_size")))


_TOKENIZER_HELP = ("Set TIKTOKEN_CACHE_DIR to a folder holding the downloaded tokenizer file, allow outbound access to "
                   "openaipublic.blob.core.windows.net, or use the Character / Recursive splitter instead.")


async def _tiktoken(name: str = "cl100k_base"):
    """The tiktoken encoding (downloaded once, then cached); None when it can't be loaded (e.g. no internet)."""
    def load():
        import tiktoken

        return tiktoken.get_encoding(name)

    try:
        return await asyncio.wait_for(asyncio.to_thread(load), 30)
    except Exception:  # noqa: BLE001 - network / proxy / timeout
        return None


async def _emb_azure(ctx: NodeContext):
    from langchain_openai import AzureOpenAIEmbeddings

    _, cred = await ctx.credential()
    p = ctx.params()
    if not p.get("model") or not cred.get("endpoint") or not cred.get("api_key"):
        raise NodeError("Azure embeddings need a deployment name and a credential with endpoint and API key", ctx.name)
    # Long-input splitting needs the tiktoken file; without it (offline servers) send texts as they are.
    can_count = await _tiktoken() is not None
    if not can_count:
        ctx.log("Tokenizer (tiktoken cl100k_base) unavailable: embedding texts without token-length splitting. "
                + _TOKENIZER_HELP)
    return AzureOpenAIEmbeddings(**_opt({"azure_deployment": p["model"], "azure_endpoint": cred["endpoint"],
                                         "api_key": cred["api_key"], "api_version": cred.get("api_version"),
                                         "check_embedding_ctx_length": can_count},
                                        dimensions=p.get("dimensions")))


async def _emb_gemini(ctx: NodeContext):
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    _, cred = await ctx.credential()
    model = ctx.param("model")
    return GoogleGenerativeAIEmbeddings(model=model if model.startswith("models/") else f"models/{model}",
                                        google_api_key=cred["api_key"])


async def _emb_ollama(ctx: NodeContext):
    from langchain_ollama import OllamaEmbeddings

    _, cred = await ctx.credential()
    return OllamaEmbeddings(model=ctx.param("model"), base_url=cred.get("base_url"))


async def _emb_mistral(ctx: NodeContext):
    from langchain_openai import OpenAIEmbeddings

    _, cred = await ctx.credential()
    return OpenAIEmbeddings(model=ctx.param("model"), api_key=cred["api_key"], base_url="https://api.mistral.ai/v1",
                            check_embedding_ctx_length=False)


class CohereEmbeddings(Embeddings):
    """Cohere /v2/embed (documents and queries use different input types)."""

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    def _body(self, texts: list[str], kind: str) -> dict:
        return {"model": self.model, "texts": texts, "input_type": kind, "embedding_types": ["float"]}

    @staticmethod
    def _vectors(r: httpx.Response) -> list[list[float]]:
        if r.status_code >= 400:
            raise RuntimeError(f"Cohere embed failed ({r.status_code}): {r.text[:300]}")
        return r.json()["embeddings"]["float"]

    async def _aembed(self, texts: list[str], kind: str) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as c:
            for k in range(0, len(texts), 96):  # API limit per call
                r = await c.post("https://api.cohere.com/v2/embed", json=self._body(texts[k:k + 96], kind),
                                 headers={"Authorization": f"Bearer {self.api_key}"})
                out += self._vectors(r)
        return out

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._aembed(texts, "search_document")

    async def aembed_query(self, text: str) -> list[float]:
        return (await self._aembed([text], "search_query"))[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return asyncio.run(self.aembed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        return asyncio.run(self.aembed_query(text))


async def _emb_cohere(ctx: NodeContext):
    _, cred = await ctx.credential()
    return CohereEmbeddings(cred["api_key"], ctx.param("model"))


for _type, _label, _cred, _fn, _extra in [
    ("embeddings.openai", "Embeddings OpenAI", "openai", _emb_openai,
     [prop("dimensions", "Dimensions (empty = model default)", "number", None), prop("batch_size", "Batch size", "number", 512)]),
    ("embeddings.azure_openai", "Embeddings Azure OpenAI", "azure_openai", _emb_azure,
     [prop("dimensions", "Dimensions (empty = model default)", "number", None)]),
    ("embeddings.gemini", "Embeddings Google Gemini", "google_genai", _emb_gemini, []),
    ("embeddings.ollama", "Embeddings Ollama", "ollama", _emb_ollama, []),
    ("embeddings.mistral", "Embeddings Mistral Cloud", "mistral", _emb_mistral, []),
    ("embeddings.cohere", "Embeddings Cohere", "cohere", _emb_cohere, []),
]:
    # No preset model names: the list comes from the credential's own server (Azure takes a deployment name).
    register(NodeType(
        type=_type, label=_label, icon="binary", description=f"{_label}: turns text into vectors for vector stores.",
        properties=[
            prop("credential", "Credential", "credential", None, credential_types=[_cred], required=True),
            prop("model", "Deployment name", "string", "", required=True) if _cred == "azure_openai"
            else prop("model", "Model", "combo", "", required=True, load_options="models"),
            *_extra,
        ],
        provides="ai_embedding", supply=_fn, **SUB_EMBEDDING,
    ))

# ---------------------------------------------------------------------------
# Text splitters
# ---------------------------------------------------------------------------


async def _split_recursive(ctx: NodeContext):
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(chunk_size=int(ctx.param("chunk_size") or 1000),
                                          chunk_overlap=int(ctx.param("chunk_overlap") or 0))


async def _split_character(ctx: NodeContext):
    from langchain_text_splitters import CharacterTextSplitter

    from .transform import unescape_sep

    sep = unescape_sep(ctx.param("separator") or "\n\n")  # "\n" typed in the form; other characters kept as is
    return CharacterTextSplitter(separator=sep, chunk_size=int(ctx.param("chunk_size") or 1000),
                                 chunk_overlap=int(ctx.param("chunk_overlap") or 0))


async def _split_token(ctx: NodeContext):
    from langchain_text_splitters import TokenTextSplitter

    if await _tiktoken() is None:
        raise NodeError("The Token Splitter needs the tiktoken tokenizer (cl100k_base), which couldn't be downloaded. "
                        + _TOKENIZER_HELP, ctx.name)
    return TokenTextSplitter(chunk_size=int(ctx.param("chunk_size") or 500),
                             chunk_overlap=int(ctx.param("chunk_overlap") or 0), encoding_name="cl100k_base")


_SIZES = [prop("chunk_size", "Chunk size", "number", 1000), prop("chunk_overlap", "Chunk overlap", "number", 200)]
for _type, _label, _desc, _props, _fn in [
    ("splitter.recursive", "Recursive Character Text Splitter",
     "Splits on paragraphs, then lines, then words, keeping chunks under the size (recommended).", _SIZES, _split_recursive),
    ("splitter.character", "Character Text Splitter", "Splits on one separator.",
     [prop("separator", "Separator", "string", "\\n\\n"), *_SIZES], _split_character),
    ("splitter.token", "Token Splitter", "Splits by tokens (cl100k_base).",
     [prop("chunk_size", "Chunk size (tokens)", "number", 500), prop("chunk_overlap", "Chunk overlap", "number", 0)], _split_token),
]:
    register(NodeType(type=_type, label=_label, icon="scissors", description=_desc, properties=_props,
                      provides="ai_text_splitter", supply=_fn, **SUB_SPLITTER))

# ---------------------------------------------------------------------------
# Document loader
# ---------------------------------------------------------------------------


class ItemLoader:
    """Turns input items into LangChain Documents (then chunks them with the attached splitter)."""

    def __init__(self, ctx: NodeContext, splitter):
        self.ctx, self.splitter = ctx, splitter

    def _eval(self, raw: Any, items: list, i: int) -> Any:
        ctx = self.ctx
        # the node's per-execution expression scope (cached by items list): no per-item rebuild, O(n) over items
        return evaluate(raw, ctx._expr_scope(items).context(items[i], i))

    def load(self, items: list[dict]) -> list[Document]:
        p = self.ctx.raw_params
        docs: list[Document] = []
        for i, item in enumerate(items):
            if p.get("mode") == "binary":
                from .files import file_text

                props = list((item.get("binary") or {}).keys()) if not p.get("binary_property") else [p["binary_property"]]
                for prop_name in props:
                    data, ref = self.ctx.file(item, prop_name)
                    meta = {"source": ref.get("fileName"), "mimeType": ref.get("mimeType"), "item_index": i}
                    for row in p.get("metadata") or []:
                        if row.get("name"):
                            meta[row["name"]] = self._eval(row.get("value"), items, i)
                    text = file_text(data, ref)
                    if text.strip():
                        docs.append(Document(page_content=text, metadata=meta))
                continue
            if p.get("mode") == "expression":
                text = self._eval(p.get("text") or "", items, i)
                if text is None:
                    continue  # missing field: no document (instead of one saying "null")
                text = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
            else:
                text = json.dumps(item, default=str, ensure_ascii=False)
            if not str(text).strip():
                continue
            meta: dict = {"source": "item", "item_index": i}
            for row in p.get("metadata") or []:
                if row.get("name"):
                    meta[row["name"]] = self._eval(row.get("value"), items, i)
            docs.append(Document(page_content=str(text), metadata=meta))
        return self.splitter.split_documents(docs) if self.splitter else docs


async def _loader_default(ctx: NodeContext):
    splitter = await _one(ctx, "ai_text_splitter", "Text Splitter", required=False)
    if splitter is None and ctx.param("split", default=True):
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    return ItemLoader(ctx, splitter)


register(NodeType(
    type="loader.default", label="Default Data Loader", icon="file-input",
    description="Turns the input items into documents for a vector store (and splits them into chunks).",
    properties=[
        prop("mode", "Text of each document", "options", "expression", options=[
            {"label": "From an expression (e.g. one field)", "value": "expression"},
            {"label": "The whole item as JSON", "value": "json"},
            {"label": "Files on the item (PDF, Word, HTML, text, CSV, Excel…)", "value": "binary"}]),
        prop("binary_property", "Binary property (empty = every file on the item)", "string", "", show_if={"mode": ["binary"]}),
        prop("text", "Text", "text", "{{ $json.text or $json.content or $json.body }}", rows=2, show_if={"mode": ["expression"]}),
        prop("metadata", "Metadata", "collection", [], fields=[prop("name", "Name"), prop("value", "Value", "string", "")]),
        prop("split", "Split into chunks when no splitter is attached (Recursive, 1000 / 200)", "boolean", True),
    ],
    ai_inputs=[ai_input("ai_text_splitter", max=1)],
    provides="ai_document", supply=_loader_default, **SUB_DOCUMENT,
))

# ---------------------------------------------------------------------------
# Vector stores
# ---------------------------------------------------------------------------


class RestVectorStore(VectorStore):
    """Shared plumbing for vector stores reached over SQL/HTTP: async methods do the work; the sync
    ones LangChain requires delegate to them (the engine is async throughout)."""

    def __init__(self, embedding: Embeddings):
        self._embedding = embedding

    @property
    def embeddings(self) -> Embeddings:
        return self._embedding

    async def _upsert(self, ids: list[str], texts: list[str], vectors: list[list[float]], metas: list[dict]) -> None:
        raise NotImplementedError

    async def _query(self, vector: list[float], k: int, filter: dict | None) -> list[tuple[Document, float]]:
        raise NotImplementedError

    async def aadd_texts(self, texts, metadatas=None, *, ids=None, **kw) -> list[str]:
        texts = list(texts)
        metas = list(metadatas or [{} for _ in texts])
        ids = list(ids or [str(uuid.uuid4()) for _ in texts])
        vectors = await self._embedding.aembed_documents(texts)
        await self._upsert(ids, texts, vectors, metas)
        return ids

    async def aadd_documents(self, documents, **kw) -> list[str]:
        return await self.aadd_texts([d.page_content for d in documents], [d.metadata for d in documents], **kw)

    async def asimilarity_search_with_score(self, query: str, k: int = 4, filter: dict | None = None, **kw):
        return await self._query(await self._embedding.aembed_query(query), k, filter)

    async def asimilarity_search(self, query: str, k: int = 4, **kw) -> list[Document]:
        return [d for d, _ in await self.asimilarity_search_with_score(query, k, **kw)]

    async def asimilarity_search_with_relevance_scores(self, query: str, k: int = 4, **kw):
        return await self.asimilarity_search_with_score(query, k, **kw)

    def add_texts(self, texts, metadatas=None, **kw):
        return asyncio.run(self.aadd_texts(texts, metadatas, **kw))

    def similarity_search(self, query: str, k: int = 4, **kw):
        return asyncio.run(self.asimilarity_search(query, k, **kw))

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, **kw):
        raise NotImplementedError("Create vector stores with their DebBuilder node")


class PGVectorStore(RestVectorStore):
    """Postgres + pgvector: one table, rows tagged with a collection name, cosine distance."""

    def __init__(self, embedding, conninfo: str, table: str, collection: str):
        super().__init__(embedding)
        self.conninfo, self.table, self.collection = conninfo, table, collection

    def _t(self) -> str:
        if not self.table.replace("_", "").isalnum():
            raise ValueError("Table name may only contain letters, digits and _")
        return self.table

    async def _conn(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self.conninfo, autocommit=True)

    async def _upsert(self, ids, texts, vectors, metas):
        async with await self._conn() as c:
            try:
                await c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError("pgvector isn't installed on this Postgres server (https://github.com/pgvector/pgvector)") from e
            await c.execute(f"CREATE TABLE IF NOT EXISTS {self._t()} (id uuid PRIMARY KEY, collection text NOT NULL, "
                            "text text, metadata jsonb, embedding vector)")
            await c.execute(f"CREATE INDEX IF NOT EXISTS {self._t()}_collection_idx ON {self._t()} (collection)")
            async with c.cursor() as cur:
                await cur.executemany(
                    f"INSERT INTO {self._t()} (id, collection, text, metadata, embedding) VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding",
                    [(i, self.collection, t, json.dumps(m, default=str), str(v)) for i, t, m, v in zip(ids, texts, metas, vectors)])

    async def _query(self, vector, k, filter):
        async with await self._conn() as c:
            rows = await (await c.execute(
                f"SELECT text, metadata, 1 - (embedding <=> %s::vector) AS score FROM {self._t()} "
                "WHERE collection = %s AND metadata @> %s::jsonb ORDER BY embedding <=> %s::vector LIMIT %s",
                (str(vector), self.collection, json.dumps(filter or {}), str(vector), k))).fetchall()
        return [(Document(page_content=t or "", metadata=m or {}), float(s)) for t, m, s in rows]


class QdrantStore(RestVectorStore):
    def __init__(self, embedding, url: str, api_key: str | None, collection: str):
        super().__init__(embedding)
        self.url, self.collection = url.rstrip("/"), collection
        self.headers = {"api-key": api_key} if api_key else {}

    async def _req(self, c, method, path, **kw):
        r = await c.request(method, f"{self.url}/collections/{self.collection}{path}", headers=self.headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Qdrant {method} {path or '/'} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    async def _upsert(self, ids, texts, vectors, metas):
        async with guarded_client(timeout=60) as c:
            exists = await c.get(f"{self.url}/collections/{self.collection}", headers=self.headers)
            if exists.status_code == 404:
                await self._req(c, "PUT", "", json={"vectors": {"size": len(vectors[0]), "distance": "Cosine"}})
            points = [{"id": i, "vector": v, "payload": {"page_content": t, "metadata": m}}
                      for i, t, m, v in zip(ids, texts, metas, vectors)]
            await self._req(c, "PUT", "/points?wait=true", json={"points": points})

    async def _query(self, vector, k, filter):
        body: dict = {"vector": vector, "limit": k, "with_payload": True}
        if filter:
            body["filter"] = {"must": [{"key": f"metadata.{key}", "match": {"value": v}} for key, v in filter.items()]}
        async with guarded_client(timeout=60) as c:
            res = (await self._req(c, "POST", "/points/search", json=body)).get("result") or []
        return [(Document(page_content=(p.get("payload") or {}).get("page_content", ""),
                          metadata=(p.get("payload") or {}).get("metadata") or {}), float(p.get("score", 0))) for p in res]


class PineconeStore(RestVectorStore):
    def __init__(self, embedding, api_key: str, index: str, namespace: str):
        super().__init__(embedding)
        self.api_key, self.index, self.namespace = api_key, index, namespace
        self.host: str | None = None

    def _h(self) -> dict:
        return {"Api-Key": self.api_key, "X-Pinecone-API-Version": "2025-04"}

    async def _host(self, c) -> str:
        if not self.host:
            r = await c.get(f"https://api.pinecone.io/indexes/{self.index}", headers=self._h())
            if r.status_code >= 400:
                raise RuntimeError(f"Pinecone index '{self.index}' not found ({r.status_code}): {r.text[:200]}")
            self.host = r.json()["host"]
        return self.host

    async def _upsert(self, ids, texts, vectors, metas):
        async with httpx.AsyncClient(timeout=60) as c:
            host = await self._host(c)
            for k in range(0, len(ids), 100):
                batch = [{"id": i, "values": v, "metadata": {**_flat(m), "text": t}}
                         for i, t, m, v in list(zip(ids, texts, metas, vectors))[k:k + 100]]
                r = await c.post(f"https://{host}/vectors/upsert", headers=self._h(),
                                 json={"vectors": batch, "namespace": self.namespace})
                if r.status_code >= 400:
                    raise RuntimeError(f"Pinecone upsert failed ({r.status_code}): {r.text[:300]}")

    async def _query(self, vector, k, filter):
        async with httpx.AsyncClient(timeout=60) as c:
            host = await self._host(c)
            body: dict = {"vector": vector, "topK": k, "includeMetadata": True, "namespace": self.namespace}
            if filter:
                body["filter"] = {key: {"$eq": v} for key, v in filter.items()}
            r = await c.post(f"https://{host}/query", headers=self._h(), json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Pinecone query failed ({r.status_code}): {r.text[:300]}")
        out = []
        for m in r.json().get("matches") or []:
            meta = dict(m.get("metadata") or {})
            out.append((Document(page_content=str(meta.pop("text", "")), metadata=meta), float(m.get("score", 0))))
        return out


def _flat(meta: dict) -> dict:
    """Pinecone metadata values must be strings, numbers, booleans or lists of strings."""
    out = {}
    for k, v in (meta or {}).items():
        if isinstance(v, (str, int, float, bool)) or (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            out[k] = v
        elif v is not None:
            out[k] = json.dumps(v, default=str)
    return out


class SupabaseStore(RestVectorStore):
    """LangChain's Supabase layout: a `documents(content, metadata, embedding)` table and a
    `match_documents(query_embedding, match_count, filter)` function (see Supabase's LangChain guide)."""

    def __init__(self, embedding, url: str, api_key: str, table: str, query_name: str):
        super().__init__(embedding)
        self.url, self.table, self.query_name = url.rstrip("/"), table, query_name
        self.headers = {"apikey": api_key, "Authorization": f"Bearer {api_key}"}

    async def _upsert(self, ids, texts, vectors, metas):
        rows = [{"id": i, "content": t, "metadata": m, "embedding": v} for i, t, m, v in zip(ids, texts, metas, vectors)]
        async with guarded_client(timeout=60) as c:
            r = await c.post(f"{self.url}/rest/v1/{self.table}", json=rows,
                             headers={**self.headers, "Prefer": "resolution=merge-duplicates"})
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase insert failed ({r.status_code}): {r.text[:300]}")

    async def _query(self, vector, k, filter):
        async with guarded_client(timeout=60) as c:
            r = await c.post(f"{self.url}/rest/v1/rpc/{self.query_name}", headers=self.headers,
                             json={"query_embedding": vector, "match_count": k, "filter": filter or {}})
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {self.query_name}() failed ({r.status_code}): {r.text[:300]}")
        return [(Document(page_content=row.get("content", ""), metadata=row.get("metadata") or {}),
                 float(row.get("similarity", 0))) for row in r.json()]


class MemoryStore(InMemoryVectorStore):
    """LangChain's in-memory store, taking the same {key: value} metadata filter as the others."""

    async def asimilarity_search_with_score(self, query, k=4, filter=None, **kw):
        if isinstance(filter, dict):
            want = filter
            filter = lambda d: all(d.metadata.get(key) == v for key, v in want.items())  # noqa: E731
        return await super().asimilarity_search_with_score(query, k, filter=filter, **kw)


_MEMORY_STORES: dict[str, MemoryStore] = {}
_MEMORY_STORES_USED: dict[str, float] = {}
MEMORY_STORE_IDLE_SECONDS = 24 * 3600  # an in-memory store nobody touched for a day is dropped
MEMORY_STORE_MAX = 500


def _evict_memory_stores(now: float) -> None:
    for k in [k for k, t in _MEMORY_STORES_USED.items() if now - t > MEMORY_STORE_IDLE_SECONDS]:
        _MEMORY_STORES.pop(k, None)
        _MEMORY_STORES_USED.pop(k, None)
    while len(_MEMORY_STORES) > MEMORY_STORE_MAX:
        k = min(_MEMORY_STORES_USED, key=_MEMORY_STORES_USED.get)
        _MEMORY_STORES.pop(k, None)
        _MEMORY_STORES_USED.pop(k, None)


async def _vs_memory(ctx: NodeContext):
    import time

    emb = await _one(ctx, "ai_embedding", "Embedding")
    # Per workspace: two users' "default" stores never mix.
    key = f"{ctx.run.meta.get('owner_id')}:{ctx.param('memory_key') or 'default'}"
    now = time.time()
    store = _MEMORY_STORES.get(key)
    if store is None:
        store = _MEMORY_STORES[key] = MemoryStore(emb)
    _MEMORY_STORES_USED[key] = now
    _evict_memory_stores(now)
    store.embedding = emb  # follow the attached embeddings node
    return store


async def _vs_pgvector(ctx: NodeContext):
    emb = await _one(ctx, "ai_embedding", "Embedding")
    _, c = await ctx.credential()
    from psycopg.conninfo import make_conninfo

    conninfo = make_conninfo(host=c.get("host"), port=c.get("port"), dbname=c.get("database"), user=c.get("user"),
                             password=c.get("password"), sslmode="require" if c.get("ssl") else "prefer")
    return PGVectorStore(emb, conninfo, ctx.param("table") or "debbuilder_vectors", ctx.param("collection") or "default")


async def _vs_qdrant(ctx: NodeContext):
    emb = await _one(ctx, "ai_embedding", "Embedding")
    _, c = await ctx.credential()
    return QdrantStore(emb, c["url"], c.get("api_key"), ctx.param("collection") or "documents")


async def _vs_pinecone(ctx: NodeContext):
    emb = await _one(ctx, "ai_embedding", "Embedding")
    _, c = await ctx.credential()
    if not ctx.param("index"):
        raise NodeError("Set the Pinecone index name", ctx.name)
    return PineconeStore(emb, c["api_key"], ctx.param("index"), ctx.param("namespace") or "")


async def _vs_supabase(ctx: NodeContext):
    emb = await _one(ctx, "ai_embedding", "Embedding")
    _, c = await ctx.credential()
    return SupabaseStore(emb, c["url"], c["api_key"], ctx.param("table") or "documents",
                         ctx.param("query_name") or "match_documents")


def _cred(t: str) -> dict:
    return prop("credential", "Credential", "credential", None, credential_types=[t], required=True)


for _type, _label, _desc, _props, _fn in [
    ("vs.memory", "In-Memory Vector Store", "Fast and simple, kept in the server process (lost on restart). Great for testing.",
     [prop("memory_key", "Store name", "string", "default", description="Nodes using the same name share the store")], _vs_memory),
    ("vs.pgvector", "Postgres PGVector Store", "Postgres with the pgvector extension.",
     [_cred("postgres"), prop("table", "Table", "string", "debbuilder_vectors"),
      prop("collection", "Collection", "string", "default", description="Separates document sets in one table")], _vs_pgvector),
    ("vs.qdrant", "Qdrant Vector Store", "Qdrant (cloud or self-hosted); the collection is created on first insert.",
     [_cred("qdrant"), prop("collection", "Collection", "string", "documents")], _vs_qdrant),
    ("vs.pinecone", "Pinecone Vector Store", "Pinecone serverless or pod index.",
     [_cred("pinecone"), prop("index", "Index", "string", "", required=True), prop("namespace", "Namespace", "string", "")], _vs_pinecone),
    ("vs.supabase", "Supabase Vector Store", "Supabase (pgvector) with LangChain's documents table + match function.",
     [_cred("supabase"), prop("table", "Table", "string", "documents"),
      prop("query_name", "Query function", "string", "match_documents")], _vs_supabase),
]:
    register(NodeType(type=_type, label=_label, icon="database", description=_desc, properties=_props,
                      ai_inputs=[ai_input("ai_embedding", True, 1)], provides="ai_vector_store", supply=_fn, **SUB_VECTOR))

# ---------------------------------------------------------------------------
# Vector store operations (flow nodes)
# ---------------------------------------------------------------------------


def _filter(ctx: NodeContext, i: int) -> dict | None:
    rows = ctx.raw_params.get("filter") or []
    f = {r["name"]: ctx.eval(r.get("value"), i) for r in rows if r.get("name")}
    return f or None


async def _insert(ctx: NodeContext):
    store = await _one(ctx, "ai_vector_store", "Vector Store")
    loader = await _one(ctx, "ai_document", "Document")
    docs = loader.load(ctx.items)
    if not docs:
        return {"main": [{"inserted": 0}]}
    step = ctx.start_sub_run(store, "vector_store", "insert", {"documents": len(docs),
                                                               "sample": [d.page_content[:300] for d in docs[:3]]})
    try:
        ids = []
        size = max(1, int(ctx.param("batch_size") or 200))
        for k in range(0, len(docs), size):
            ids += await store.aadd_documents(docs[k:k + size])
    except Exception as e:
        ctx.end_sub_run(step, status="error", error=str(e))
        raise NodeError(f"Insert failed: {e}", ctx.name) from e
    ctx.end_sub_run(step, output={"inserted": len(ids)})
    return {"main": [{"inserted": len(ids), "ids": ids[:1000]}]}


register(NodeType(
    type="rag.insert", label="Vector Store: Insert Documents", icon="database-zap",
    description="Embed the input items (via the document loader) and store them in a vector store.",
    properties=[prop("batch_size", "Embedding batch size", "number", 200)],
    ai_inputs=[ai_input("ai_vector_store", True, 1), ai_input("ai_document", True, 1)],
    execute=_insert, **AI,
))


async def _rerank(reranker, query: str, docs: list[tuple[Document, float]], top: int):
    if not reranker or not docs:
        return docs[:top]
    return await reranker.arerank(query, docs, top)


async def _search(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items or [{}])):
        store = await _one(ctx, "ai_vector_store", "Vector Store", i)
        reranker = await _one(ctx, "ai_reranker", "Reranker", i, required=False)
        query = str(ctx.param("query", i) or "")
        if not query.strip():
            raise NodeError("The search query is empty", ctx.name)
        k = int(ctx.param("top_k", i) or 4)
        step = ctx.start_sub_run(store, "vector_store", "search", {"query": query, "top_k": k, "filter": _filter(ctx, i)})
        try:
            hits = await store.asimilarity_search_with_score(query, k=k * (3 if reranker else 1), filter=_filter(ctx, i))
            hits = await _rerank(reranker, query, hits, k)
        except Exception as e:
            ctx.end_sub_run(step, status="error", error=str(e))
            raise NodeError(f"Search failed: {e}", ctx.name) from e
        ctx.end_sub_run(step, output=[doc_json(d, s) for d, s in hits])
        out += [doc_json(d, s) for d, s in hits]
    return {"main": out}


_FILTER = prop("filter", "Metadata filter (equals)", "collection", [], fields=[prop("name", "Key"), prop("value", "Value", "string", "")])
register(NodeType(
    type="rag.search", label="Vector Store: Search", icon="search",
    description="Find the documents most similar to a query (one output item per document, with its score).",
    properties=[prop("query", "Query", "text", "{{ $json.query or $json.chatInput or $json.text }}", rows=2),
                prop("top_k", "Results (top K)", "number", 4), _FILTER],
    ai_inputs=[ai_input("ai_vector_store", True, 1), ai_input("ai_reranker", max=1)],
    execute=_search, **AI,
))

# ---------------------------------------------------------------------------
# Rerankers
# ---------------------------------------------------------------------------


class CohereReranker:
    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    async def arerank(self, query: str, docs: list[tuple[Document, float]], top: int) -> list[tuple[Document, float]]:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.cohere.com/v2/rerank", headers={"Authorization": f"Bearer {self.api_key}"},
                             json={"model": self.model, "query": query, "top_n": top,
                                   "documents": [d.page_content for d, _ in docs]})
        if r.status_code >= 400:
            raise RuntimeError(f"Cohere rerank failed ({r.status_code}): {r.text[:300]}")
        return [(docs[x["index"]][0], float(x["relevance_score"])) for x in r.json().get("results") or []]


async def _rerank_cohere(ctx: NodeContext):
    _, cred = await ctx.credential()
    return CohereReranker(cred["api_key"], ctx.param("model"))


register(NodeType(
    type="reranker.cohere", label="Reranker Cohere", icon="arrow-up-down",
    description="Re-orders retrieved documents by relevance to the query (fetches 3× candidates, keeps the best).",
    properties=[_cred("cohere"), prop("model", "Model", "combo", "rerank-v3.5", options=["rerank-v3.5", "rerank-multilingual-v3.0"])],
    provides="ai_reranker", supply=_rerank_cohere, **SUB_RERANKER,
))

# ---------------------------------------------------------------------------
# Retrievers
# ---------------------------------------------------------------------------


class StoreRetriever(BaseRetriever):
    """Vector store search as a LangChain retriever (optionally reranked)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    store: Any
    k: int = 4
    reranker: Any = None
    search_filter: dict | None = None

    def _get_relevant_documents(self, query, *, run_manager=None):
        return asyncio.run(self._aget_relevant_documents(query, run_manager=None))

    async def _aget_relevant_documents(self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun | None = None):
        hits = await self.store.asimilarity_search_with_score(query, k=self.k * (3 if self.reranker else 1), filter=self.search_filter)
        hits = await _rerank(self.reranker, query, hits, self.k)
        return [Document(page_content=d.page_content, metadata={**d.metadata, "score": round(s, 6)}) for d, s in hits]


async def _ret_store(ctx: NodeContext):
    store = await _one(ctx, "ai_vector_store", "Vector Store")
    reranker = await _one(ctx, "ai_reranker", "Reranker", required=False)
    return StoreRetriever(store=store, k=int(ctx.param("top_k") or 4), reranker=reranker, search_filter=_filter(ctx, 0))


class MultiQueryRetriever(BaseRetriever):
    """Asks the model for several rewordings of the question and merges what each finds."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    model: Any
    base: Any
    n: int = 3

    def _get_relevant_documents(self, query, *, run_manager=None):
        return asyncio.run(self._aget_relevant_documents(query))

    async def _aget_relevant_documents(self, query: str, *, run_manager=None):
        cfg = {"callbacks": run_manager.get_child()} if run_manager else {}
        res = await self.model.ainvoke([HumanMessage(
            f"Write {self.n} different rewordings of this question to search a document database. "
            f"One per line, no numbering.\n\nQuestion: {query}")], config=cfg)
        queries = [query] + [q.strip(" -•\t") for q in message_text(res).splitlines() if q.strip()][: self.n]
        seen, out = set(), []
        for docs in await asyncio.gather(*(self.base.ainvoke(q, config=cfg) for q in queries)):
            for d in docs:
                if d.page_content not in seen:
                    seen.add(d.page_content)
                    out.append(d)
        return out


async def _ret_multi(ctx: NodeContext):
    model = await ctx.require_model()
    base = await _one(ctx, "ai_retriever", "Retriever")
    return MultiQueryRetriever(model=model, base=base, n=int(ctx.param("queries") or 3))


class CompressionRetriever(BaseRetriever):
    """Keeps only the parts of each document that are relevant to the question (LLM extraction)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    model: Any
    base: Any

    def _get_relevant_documents(self, query, *, run_manager=None):
        return asyncio.run(self._aget_relevant_documents(query))

    async def _aget_relevant_documents(self, query: str, *, run_manager=None):
        cfg = {"callbacks": run_manager.get_child()} if run_manager else {}
        docs = await self.base.ainvoke(query, config=cfg)

        async def squeeze(d: Document) -> Document | None:
            res = await self.model.ainvoke([HumanMessage(
                "Extract, word for word, only the parts of the context relevant to the question. "
                f"If nothing is relevant, reply NO_OUTPUT.\n\nQuestion: {query}\n\nContext:\n{d.page_content}")], config=cfg)
            text = message_text(res).strip()
            return None if not text or "NO_OUTPUT" in text else Document(page_content=text, metadata=d.metadata)
        return [d for d in await asyncio.gather(*(squeeze(d) for d in docs)) if d]


async def _ret_compress(ctx: NodeContext):
    return CompressionRetriever(model=await ctx.require_model(), base=await _one(ctx, "ai_retriever", "Retriever"))


register(NodeType(
    type="retriever.vector_store", label="Vector Store Retriever", icon="search",
    description="Retrieves the top documents from a vector store (optionally reranked).",
    properties=[prop("top_k", "Results (top K)", "number", 4), _FILTER],
    ai_inputs=[ai_input("ai_vector_store", True, 1), ai_input("ai_reranker", max=1)],
    provides="ai_retriever", supply=_ret_store, **SUB_RETRIEVER,
))
register(NodeType(
    type="retriever.multi_query", label="MultiQuery Retriever", icon="list-tree",
    description="Rewords the question several ways with the model and merges what each finds (better recall).",
    properties=[prop("queries", "Rewordings", "number", 3)],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_retriever", True, 1)],
    provides="ai_retriever", supply=_ret_multi, **SUB_RETRIEVER,
))
register(NodeType(
    type="retriever.compression", label="Contextual Compression Retriever", icon="minimize-2",
    description="Keeps only the passages of each document that answer the question.",
    properties=[],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_retriever", True, 1)],
    provides="ai_retriever", supply=_ret_compress, **SUB_RETRIEVER,
))

# ---------------------------------------------------------------------------
# Chains: Question & Answer, Summarization
# ---------------------------------------------------------------------------

QA_SYSTEM = ("Use the following context to answer the question. If the context doesn't contain the answer, say you don't know. "
             "Don't make anything up.\n\n----------------\nContext:\n{context}")


def _context(docs: list[Document]) -> str:
    return "\n\n".join(d.page_content for d in docs)


async def _qa(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items or [{}])):
        model = await ctx.require_model(i)
        retriever = await _one(ctx, "ai_retriever", "Retriever", i)
        q = str(ctx.param("query", i) or "")
        if not q.strip():
            raise NodeError("The question is empty", ctx.name)
        cbs = {"callbacks": ctx.callbacks()}
        docs = await retriever.ainvoke(q, config=cbs)
        system = (ctx.param("system_prompt", i) or QA_SYSTEM).replace("{context}", _context(docs))
        res = await model.ainvoke([SystemMessage(system), HumanMessage(q)], config=cbs)
        item: dict = {"response": message_text(res)}
        if ctx.param("include_sources", i):
            item["sources"] = [doc_json(d) for d in docs]
        out.append(item)
    return {"main": out}


register(NodeType(
    type="ai.qa_chain", label="Question and Answer Chain", icon="message-circle-question",
    description="Answer a question from documents found by a retriever (classic RAG).",
    properties=[prop("query", "Question", "text", "{{ $json.chatInput or $json.query or $json.question }}", rows=2),
                prop("system_prompt", "System prompt ({context} = retrieved text)", "text", QA_SYSTEM, rows=5),
                prop("include_sources", "Include source documents", "boolean", False)],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_retriever", True, 1)],
    execute=_qa, **AI,
))

SUMMARY_PROMPT = "Write a concise summary of the following:\n\n{text}\n\nCONCISE SUMMARY:"
COMBINE_PROMPT = "Combine these partial summaries into one concise summary:\n\n{text}\n\nCONCISE SUMMARY:"


async def _summarize(ctx: NodeContext):
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    model = await ctx.require_model()
    cbs = {"callbacks": ctx.callbacks()}
    loader = await _one(ctx, "ai_document", "Document", required=False)
    if loader is not None:
        groups = [loader.load(ctx.items)]  # all input items summarised together
    else:
        groups = []
        for i in range(len(ctx.items or [{}])):
            text = ctx.param("text", i)
            groups.append([Document(page_content=text if isinstance(text, str) else json.dumps(text, default=str))])
    size = int(ctx.param("chunk_size") or 6000)
    prompt, combine = ctx.raw_params.get("prompt") or SUMMARY_PROMPT, ctx.raw_params.get("combine_prompt") or COMBINE_PROMPT
    out = []
    for docs in groups:
        chunks = RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=0).split_documents(docs)
        if ctx.param("method") == "stuff" or len(chunks) <= 1:
            res = await model.ainvoke([HumanMessage(prompt.replace("{text}", _context(chunks)))], config=cbs)
            out.append({"output": message_text(res)})
            continue
        parts = await asyncio.gather(*(model.ainvoke([HumanMessage(prompt.replace("{text}", c.page_content))], config=cbs)
                                       for c in chunks))
        res = await model.ainvoke([HumanMessage(combine.replace("{text}", "\n\n".join(message_text(r) for r in parts)))], config=cbs)
        out.append({"output": message_text(res), "chunks": len(chunks)})
    return {"main": out}


register(NodeType(
    type="ai.summarize", label="Summarization Chain", icon="text-quote",
    description="Summarise long text or documents (map-reduce over chunks, or all at once).",
    properties=[
        prop("text", "Text (when no document loader is attached)", "text", "{{ $json.text or $json.content }}", rows=2),
        prop("method", "Method", "options", "map_reduce", options=[
            {"label": "Map-reduce (summarise chunks, then combine)", "value": "map_reduce"},
            {"label": "Stuff (all text in one prompt)", "value": "stuff"}]),
        prop("chunk_size", "Chunk size (characters)", "number", 6000),
        prop("prompt", "Summary prompt ({text})", "text", SUMMARY_PROMPT, rows=3),
        prop("combine_prompt", "Combine prompt ({text})", "text", COMBINE_PROMPT, rows=3),
    ],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_document", max=1)],
    execute=_summarize, **AI,
))

# ---------------------------------------------------------------------------
# Tools: vector store, think
# ---------------------------------------------------------------------------


async def _tool_vector_store(ctx: NodeContext) -> BaseTool:
    store = await _one(ctx, "ai_vector_store", "Vector Store")
    model = await _one(ctx, "ai_model", "Model", required=False)
    k = int(ctx.param("top_k") or 4)

    async def call(query: str) -> str:
        docs = await StoreRetriever(store=store, k=k).ainvoke(query)
        if model is None:  # return the passages; the agent reads them itself
            return json.dumps([{"content": d.page_content, "metadata": d.metadata} for d in docs], default=str)
        res = await model.ainvoke([SystemMessage(QA_SYSTEM.replace("{context}", _context(docs))), HumanMessage(query)])
        return message_text(res)

    name = _tool_name(ctx)
    return StructuredTool.from_function(
        coroutine=call, name=name, description=ctx.param("tool_description"),
        args_schema=create_model(f"{name}_args", query=(str, Field(..., description="What to look up"))),
    )


register(NodeType(
    type="tool.vector_store", label="Vector Store Tool", icon="library",
    description="Lets an agent search a vector store. With a model attached it answers from the documents; without, it returns them.",
    properties=[*TOOL_COMMON, prop("top_k", "Results (top K)", "number", 4)],
    ai_inputs=[ai_input("ai_vector_store", True, 1), ai_input("ai_model", max=1)],
    provides="ai_tool", supply=_tool_vector_store, **SUB_TOOL,
))


async def _tool_think(ctx: NodeContext) -> BaseTool:
    async def think(thought: str) -> str:
        return thought

    return StructuredTool.from_function(
        coroutine=think, name="think",
        description=ctx.param("tool_description") or "Use to think about something: it records the thought and changes nothing. "
                                                     "Use it when complex reasoning or a scratchpad is needed.",
    )


register(NodeType(
    type="tool.think", label="Think Tool", icon="lightbulb",
    description="A scratchpad: gives the agent room to reason step by step before acting.",
    properties=[prop("tool_description", "Description", "text", "", rows=2)],
    provides="ai_tool", supply=_tool_think, **SUB_TOOL,
))

# ---------------------------------------------------------------------------
# More chat models (OpenAI-compatible providers + Azure) and memories
# ---------------------------------------------------------------------------

COMPATIBLE = {  # credential type -> (label, base URL); models come from the provider's own list
    "groq": ("Groq Chat Model", "https://api.groq.com/openai/v1"),
    "mistral": ("Mistral Cloud Chat Model", "https://api.mistral.ai/v1"),
    "deepseek": ("DeepSeek Chat Model", "https://api.deepseek.com/v1"),
    "openrouter": ("OpenRouter Chat Model", "https://openrouter.ai/api/v1"),
    "xai": ("xAI Grok Chat Model", "https://api.x.ai/v1"),
    "together": ("Together AI Chat Model", "https://api.together.xyz/v1"),
}


def _compatible_model(cred_type: str):
    async def supply(ctx: NodeContext):
        from langchain_openai import ChatOpenAI

        _, cred = await ctx.credential()
        p = ctx.params()
        return ChatOpenAI(**_opt({"model": model_name(ctx, p), "api_key": cred["api_key"], "base_url": COMPATIBLE[cred_type][1],
                                  "streaming": True}, temperature=p.get("temperature"), max_tokens=p.get("max_tokens")))
    return supply


for _cred_type, (_label, _url, *_) in COMPATIBLE.items():
    register(NodeType(
        type=f"model.{_cred_type}", label=_label, icon="brain", description=f"{_label} for AI nodes.",
        properties=[_cred(_cred_type),
                    prop("model", "Model", "combo", "", required=True, load_options="models"),
                    *MODEL_COMMON],
        provides="ai_model", supply=_compatible_model(_cred_type),
        group="ai_model", color="#a855f7", inputs=[], outputs=[],
    ))


async def _azure_chat(ctx: NodeContext):
    from langchain_openai import AzureChatOpenAI

    _, cred = await ctx.credential()
    p = ctx.params()
    return AzureChatOpenAI(**_opt({"azure_deployment": model_name(ctx, p), "azure_endpoint": cred["endpoint"], "api_key": cred["api_key"],
                                   "api_version": cred.get("api_version"), "streaming": True},
                                  temperature=p.get("temperature"), max_tokens=p.get("max_tokens")))


register(NodeType(
    type="model.azure_openai", label="Azure OpenAI Chat Model", icon="brain", description="Azure OpenAI deployment for AI nodes.",
    properties=[_cred("azure_openai"), prop("model", "Deployment name", "string", "", required=True), *MODEL_COMMON],
    provides="ai_model", supply=_azure_chat, group="ai_model", color="#a855f7", inputs=[], outputs=[],
))


class BufferMemory:
    """n8n's Simple Memory: the last N exchanges per session key, kept in the server process.

    window = 0 means "no limit" (still capped at MAX_MESSAGES per session). The store is bounded: least recently
    used sessions are evicted past MAX_SESSIONS, and sessions idle for IDLE_SECONDS are dropped."""

    from collections import OrderedDict as _OD

    _store: "_OD[str, list]" = _OD()
    _seen: dict[str, float] = {}
    MAX_SESSIONS = 5000
    MAX_MESSAGES = 1000
    IDLE_SECONDS = 24 * 3600

    def __init__(self, session_key: str, window: int):
        self.session_key, self.window = session_key, max(0, int(window or 0))

    def _rows(self, create: bool = False) -> list:
        import time

        now = time.time()
        cls = type(self)
        rows = cls._store.get(self.session_key)
        if rows is None and create:
            rows = cls._store[self.session_key] = []
        if rows is not None:
            cls._store.move_to_end(self.session_key)
            cls._seen[self.session_key] = now
        while len(cls._store) > cls.MAX_SESSIONS:
            k, _ = cls._store.popitem(last=False)
            cls._seen.pop(k, None)
        if len(cls._seen) and next(iter(cls._store), None) is not None:
            for k in list(cls._store)[:50]:  # oldest first: drop the idle ones
                if now - cls._seen.get(k, now) <= cls.IDLE_SECONDS:
                    break
                cls._store.pop(k, None)
                cls._seen.pop(k, None)
        return rows if rows is not None else []

    async def load(self):
        from langchain_core.messages import AIMessage

        rows = self._rows()
        if self.window:
            rows = rows[-self.window * 2:]
        return [HumanMessage(c) if r == "human" else AIMessage(c) for r, c in rows]

    async def save(self, human: str, ai: str) -> None:
        rows = self._rows(create=True)
        rows += [("human", human), ("ai", ai)]
        keep = min(self.window * 2 * 4, self.MAX_MESSAGES) if self.window else self.MAX_MESSAGES
        if len(rows) > keep:
            del rows[:len(rows) - keep]  # bounded (no rows[:-0] slicing bug when window is 0)

    async def clear(self) -> None:
        self._store.pop(self.session_key, None)
        self._seen.pop(self.session_key, None)


class RedisMemory(BufferMemory):
    def __init__(self, cred: dict, session_key: str, window: int, ttl: int | None):
        super().__init__(session_key, window)
        self.cred, self.ttl = cred, ttl

    def _client(self):
        from ..services.brokers import redis_client

        return redis_client(self.cred)

    async def load(self):
        from langchain_core.messages import AIMessage

        r = self._client()
        try:
            raw = await r.lrange(f"chat:{self.session_key}", -self.window * 2 if self.window else 0, -1)
        finally:
            await r.aclose()
        out = []
        for x in raw:
            m = json.loads(x)
            out.append(HumanMessage(m["content"]) if m["role"] == "human" else AIMessage(m["content"]))
        return out

    async def save(self, human: str, ai: str) -> None:
        r = self._client()
        key = f"chat:{self.session_key}"
        try:
            await r.rpush(key, json.dumps({"role": "human", "content": human}), json.dumps({"role": "ai", "content": ai}))
            if self.ttl:
                await r.expire(key, self.ttl)
        finally:
            await r.aclose()

    async def clear(self) -> None:
        r = self._client()
        try:
            await r.delete(f"chat:{self.session_key}")
        finally:
            await r.aclose()


def _session(ctx: NodeContext) -> str:
    return f"{ctx.run.meta['workflow_id']}:{ctx.param('session_key') or 'default'}"


def _window(ctx: NodeContext, default: int = 10) -> int:
    raw = ctx.param("window")
    if raw in (None, ""):
        return default
    try:
        return max(0, int(float(raw)))  # 0 = no limit
    except (TypeError, ValueError) as e:
        raise NodeError(f"Context window must be a number (got {raw!r})", ctx.name) from e


async def _mem_buffer(ctx: NodeContext):
    return BufferMemory(_session(ctx), _window(ctx))


async def _mem_redis(ctx: NodeContext):
    _, c = await ctx.credential()
    ttl = ctx.param("ttl")
    return RedisMemory(c, _session(ctx), _window(ctx), int(ttl) if ttl else None)


_MEM_PROPS = [prop("session_key", "Session key", "string", "{{ $json.session_id or $json.sessionId or 'default' }}"),
              prop("window", "Context window (exchanges)", "number", 10, description="0 = no limit")]
register(NodeType(
    type="memory.buffer", label="Simple Memory", icon="brain-circuit",
    description="Keeps recent chat turns in the server's memory (no setup; lost on restart).",
    properties=_MEM_PROPS, provides="ai_memory", supply=_mem_buffer,
    group="ai_memory", color="#0ea5e9", inputs=[], outputs=[],
))
register(NodeType(
    type="memory.redis", label="Redis Chat Memory", icon="database",
    description="Chat history in Redis, shared across server restarts and instances.",
    properties=[_cred("redis"), *_MEM_PROPS, prop("ttl", "Expire after (seconds, empty = never)", "number", None)],
    provides="ai_memory", supply=_mem_redis, group="ai_memory", color="#0ea5e9", inputs=[], outputs=[],
))
