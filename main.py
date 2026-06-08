from chunker import DocumentChunker
from vector_store import VectorStore
from llm_client import BedrockLLM
from prompt_template import SYSTEM_PROMPT
from dotenv import load_dotenv
from langchain_core.documents import Document

# ── Configuration ──────────────────────────────────────────────
LLM_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
"""
other models available:
- us.anthropic.claude-sonnet-4-20250514-v1:0
"""


class DocumentRAG:
    """Orchestrates the full RAG pipeline: collect → chunk → embed → ask.

    Supports two initialization modes:
      - With documents: indexes them into the vector store (on upload/reload)
      - Without documents: connects to the existing vector store (on ask)
    """

    def __init__(
        self,
        model: str = LLM_MODEL,
        region_name: str = "us-west-1",
        documents: list[Document] | None = None,
    ):
        # LLM is always needed
        self.llm = BedrockLLM(model=model, region_name=region_name)

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

        result = self.llm.invoke(prompt)

        final_answer = f"{result}\n\nCitations:\n{self._format_citations(citations)}"

        response = {
            "answer_raw": result,
            "answer_final": final_answer,
            "docs": citations,
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
