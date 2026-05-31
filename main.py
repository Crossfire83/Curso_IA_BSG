from file_collector import FileCollector
from chunker import DocumentChunker
from vector_store import VectorStore
from llm_client import BedrockLLM
from grounding import GroundingEvaluator
from prompt_template import SYSTEM_PROMPT
from exclude_patterns import EXCLUDE_PATTERNS

# ── Configuration ──────────────────────────────────────────────
LLM_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
"""
other models available:
- us.anthropic.claude-sonnet-4-20250514-v1:0
"""

"""Orchestrates the full RAG pipeline: collect → chunk → embed → ask."""
class CodebaseRAG:

    def __init__(
        self,
        source_dir: str,
        exclude_patterns: list[str] | None = None,
        model: str = LLM_MODEL,
        region_name: str = "us-west-2",
    ):
        # 1. Collect files
        collector = FileCollector(source_dir, exclude_patterns)
        raw_docs = collector.collect()
        print(f"Collected {len(raw_docs)} files from {source_dir}")

        # 2. Build lookup maps
        self.file_content_map = {
            doc.metadata["file"]: doc.page_content for doc in raw_docs
        }
        config_file_paths = {
            doc.metadata["file"]
            for doc in raw_docs
            if doc.metadata.get("is_config")
        }
        print(f"  Config files identified: {len(config_file_paths)}")

        # 3. Chunk
        chunker = DocumentChunker()
        chunks = chunker.chunk(raw_docs)
        print(f"  Chunks created: {len(chunks)}")

        # 4. Vector store
        self.store = VectorStore(
            chunks=chunks,
            file_content_map=self.file_content_map,
            config_file_paths=config_file_paths,
        )
        print("Vector DB created successfully.")

        # 5. LLM + grounding
        self.llm = BedrockLLM(model=model, region_name=region_name)
        self.grounding = GroundingEvaluator(
            self.file_content_map,
            structural_detector=self.store.structural_detector,
        )

    def ask(self, query: str, min_grounding: float = 0.7, invoke_llm: bool, print_citations: bool) -> dict:
        docs, context = self.store.retrieve(query)

        prompt = SYSTEM_PROMPT.format(context=context, query=query)

        if invoke_llm:
            result = self.llm.invoke(prompt)

            grounding_score, issues = self.grounding.evaluate(result)
            completeness_score, missing_files = self.grounding.evaluate_completeness(result)
        citations = self.grounding.build_citations(docs)

        if invoke_llm:
            if grounding_score < min_grounding:
                final_answer = "I couldn't retrieve what you want."
                flagged = True
            else:
                final_answer = f"{result}\n\nCitations:\n{citations}"
                flagged = False

            print(result)
            print(f"Grounding Score: {grounding_score:.2f}")
            print(f"Completeness Score: {completeness_score:.2f}")

        if print_citations:
            print(citations)

        response = {
            "docs": citations,
        }

        # add invocation results to the response if the invocation was enabled
        if invoke_llm:
            response |= {
                "answer_raw": result,
                "answer_final": final_answer,
                "grounding_score": grounding_score,
                "completeness_score": completeness_score,
                "missing_files": missing_files,
                "flagged": flagged,
                "issues": issues
            }

        return response


# ── Entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
