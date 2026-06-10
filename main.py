from chunker import DocumentChunker
from vector_store import VectorStore
from llm_client import BedrockLLM
from prompt_template import SYSTEM_PROMPT
from dotenv import load_dotenv
from langchain_core.documents import Document
import time



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
        print("Connecting to existing vector store (query-only mode)...")

        # Vector store — query-only mode (no chunks passed)
        self.store = VectorStore()
        print("  Vector store connected.")

    def ask(self, query: str) -> dict:
        citations, context = self.store.retrieve(query)

        prompt = SYSTEM_PROMPT.format(context=context, query=query)

        start_time = time.time()
        llm_result = self.llm.invoke(prompt)
        elapsed = time.time() - start_time

        result_text = llm_result["text"]
        final_answer = f"{result_text}"

        response = {
            "answer_raw": result_text,
            "answer_final": final_answer,
            "docs": citations,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "elapsed_seconds": round(elapsed, 2),
        }

        return response

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
