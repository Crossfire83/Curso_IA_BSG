from chunker import DocumentChunker
from vector_store import VectorStore
from llm_client import BedrockLLM
from prompt_template import SYSTEM_PROMPT
from dotenv import load_dotenv
from langchain_core.documents import Document
import logging
import time

logger = logging.getLogger(__name__)



class DocumentRAG:
    """Orchestrates the full RAG pipeline: collect → chunk → embed → ask.

    Supports two initialization modes:
      - With documents: indexes them into the vector store (on upload/reload)
      - Without documents: connects to the existing vector store (on ask)
    """

    def __init__(
        self,
        region_name: str = "us-west-1",
        documents: list[Document] | None = None,
    ):
        # LLM is always needed
        self.llm = BedrockLLM(region_name=region_name)

        if documents is not None and len(documents) > 0:
            self._index_documents(documents)
        else:
            self._connect_query_only()

    def _index_documents(self, documents: list[Document]) -> None:
        """Index documents into the vector store (called on upload/reload)."""
        print(f"Indexing {len(documents)} document(s)...")

        # Chunk
        chunker = DocumentChunker()
        chunks = chunker.chunk(documents)
        print(f"  Chunks created: {len(chunks)}")

        # Vector store — index mode
        self.store = VectorStore(chunks=chunks)
        print("  Vector DB indexed successfully.")

    def _connect_query_only(self) -> None:
        """Connect to the existing vector store without re-indexing (called on ask)."""
        start = time.time()
        logger.info("[RAG] Connecting to vector store (query-only mode)...")

        # Vector store — query-only mode (no chunks passed)
        self.store = VectorStore()
        elapsed = time.time() - start
        logger.info("[RAG] Vector store connected in %.2fs", elapsed)

    def ask(self, query: str) -> dict:
        retrieval_start = time.time()
        citations, context = self.store.retrieve(query)
        retrieval_elapsed = time.time() - retrieval_start

        prompt = SYSTEM_PROMPT.format(context=context, query=query)

        llm_start = time.time()
        llm_result = self.llm.invoke(prompt)
        llm_elapsed = time.time() - llm_start

        total_elapsed = retrieval_elapsed + llm_elapsed

        result_text = llm_result["text"]
        final_answer = f"{result_text}"

        response = {
            "answer_raw": result_text,
            "answer_final": final_answer,
            "docs": citations,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "elapsed_seconds": round(total_elapsed, 2),
            "retrieval_seconds": round(retrieval_elapsed, 2),
            "llm_seconds": round(llm_elapsed, 2),
        }

        return response

    def ask_stream(self, query: str):
        """Streaming version of ask. Yields (event_type, data) tuples.

        Event types:
          - "token": a text chunk from the LLM
          - "done": final metadata dict (citations, tokens, timing)
        """
        request_start = time.time()
        logger.info("[RAG] ─── ask_stream START ───")
        logger.info("[RAG] Query: %.100s%s", query, "..." if len(query) > 100 else "")

        # Stage 1: Retrieval (embedding query + vector search)
        retrieval_start = time.time()
        logger.info("[RAG] Stage 1/2: Retrieval started (Vertex AI find_neighbors)")
        citations, context = self.store.retrieve(query)
        retrieval_elapsed = time.time() - retrieval_start
        logger.info(
            "[RAG] Stage 1/2: Retrieval done in %.2fs — %d chunk(s), %d file(s), ~%d context chars",
            retrieval_elapsed, sum(1 for _ in context.split("---")), len(citations), len(context),
        )

        prompt = SYSTEM_PROMPT.format(context=context, query=query)
        logger.info("[RAG] Prompt assembled: %d chars (~%d tokens)", len(prompt), len(prompt) // 4)

        # Stage 2: LLM streaming (Bedrock)
        llm_start = time.time()
        logger.info("[RAG] Stage 2/2: Bedrock streaming started")
        first_token_received = False
        for chunk in self.llm.invoke_stream(prompt):
            if not first_token_received:
                first_token_elapsed = time.time() - llm_start
                logger.info("[RAG] First token received in %.2fs (time-to-first-token)", first_token_elapsed)
                first_token_received = True
            yield ("token", chunk)
        llm_elapsed = time.time() - llm_start

        total_elapsed = time.time() - request_start
        usage = self.llm.last_usage

        logger.info(
            "[RAG] ─── ask_stream DONE ─── total=%.2fs | retrieval=%.2fs | llm=%.2fs | in_tokens=%d | out_tokens=%d",
            total_elapsed, retrieval_elapsed, llm_elapsed,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )

        yield ("done", {
            "docs": citations,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "elapsed_seconds": round(total_elapsed, 2),
            "retrieval_seconds": round(retrieval_elapsed, 2),
            "llm_seconds": round(llm_elapsed, 2),
        })

    @staticmethod
    def _format_citations(docs: list[dict]) -> str:
        refs = []
        seen: set[str] = set()
        for doc in docs:
            key = doc["filename"]
            if key not in seen:
                seen.add(key)
                token_count = doc.get("token_count")
                if token_count:
                    refs.append(f"- {key} ({token_count} tokens)")
                else:
                    refs.append(f"- {key}")
        return "\n".join(sorted(refs))


# ── Entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    from app import app
    load_dotenv()
    app.run(host="127.0.0.1", port=5000, debug=False)
