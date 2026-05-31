import shutil
from pathlib import Path

from langchain_community.vectorstores import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document
from constant_resolver import ConstantResolver
from structural_detector import StructuralDetector
from code_compressor import CodeCompressor, _OPENAPI_PATH_PATTERN


"""Manages the Chroma vector DB and retrieval with compressed context."""
class VectorStore:

    def __init__(
        self,
        chunks: list[Document],
        file_content_map: dict[str, str],
        config_file_paths: set[str],
        persist_directory: str = "./chroma_db",
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        retriever_k: int = 1000,
    ):
        self.file_content_map = file_content_map
        self.config_file_paths = config_file_paths
        self.constant_resolver = ConstantResolver(file_content_map)
        self.structural_detector = StructuralDetector(file_content_map)
        self.compressor = CodeCompressor()

        # Pre-compress all files once at init time
        self._compressed_map = self.compressor.compress_all(file_content_map)

        total_raw = sum(len(v) for v in file_content_map.values())
        total_compressed = sum(len(v) for v in self._compressed_map.values())
        ratio = (1 - total_compressed / total_raw) * 100 if total_raw else 0
        print(
            f"  Code compressor: {len(self._compressed_map)}/{len(file_content_map)} "
            f"files have API-relevant content "
            f"({total_raw:,} → {total_compressed:,} chars, {ratio:.0f}% reduction)"
        )
        print(
            f"  Structural detector found: "
            f"{len(self.structural_detector.controller_files)} controller(s), "
            f"{len(self.structural_detector.client_files)} client(s), "
            f"{len(self.structural_detector.constant_files)} constant file(s)"
        )

        embeddings = HuggingFaceEmbeddings(model_name=embedding_model)

        # Wipe any previously-persisted collection so we don't mix
        # embeddings from different projects across runs.
        persist_path = Path(persist_directory)
        if persist_path.exists():
            shutil.rmtree(persist_path)

        self.db = Chroma.from_texts(
            texts=[doc.page_content for doc in chunks],
            metadatas=[doc.metadata for doc in chunks],
            embedding=embeddings,
            persist_directory=persist_directory,
        )

        self.retriever = self.db.as_retriever(search_kwargs={"k": retriever_k})

    """
    Retrieve relevant context using a three-layer strategy:
    1. Vector-retrieved files (compressed)
    2. Structurally-detected API files the retriever missed (compressed)
    3. Constant-definition files referenced but not yet included (compressed)

    Config files are always included (compressed to relevant keys only).

    Returns (doc_filenames, context_string).
    """
    def retrieve(self, query: str) -> tuple[list[dict], str]:
        docs = self.retriever.invoke(query)

        # Collect all files that should be in context
        included_files: set[str] = set()
        ordered_files: list[str] = []

        # Layer 1: vector-retrieved files
        for doc in docs:
            fp = doc.metadata.get("file")
            if fp and fp not in included_files:
                included_files.add(fp)
                ordered_files.append(fp)

        # Layer 2: config files
        for cfg_path in sorted(self.config_file_paths):
            if cfg_path not in included_files:
                included_files.add(cfg_path)
                ordered_files.append(cfg_path)

        # Layer 3: structurally-detected API files the retriever missed
        missing = self.structural_detector.find_missing(included_files)
        struct_count = 0
        for label, paths in missing.items():
            for fp in paths:
                if fp not in included_files:
                    included_files.add(fp)
                    ordered_files.append(fp)
                    struct_count += 1
        if struct_count:
            print(f"  Structural detector: injecting {struct_count} missed file(s)")

        # Layer 4: constant-definition files
        # Build a quick context preview to find referenced constants
        preview = self._build_preview(ordered_files)
        const_supplement = self.constant_resolver.find_missing_definitions(
            preview, included_files,
        )
        const_count = 0
        for fp, name, _value in const_supplement:
            if fp not in included_files:
                included_files.add(fp)
                ordered_files.append(fp)
                const_count += 1
        if const_count:
            names = [n for _, n, _ in const_supplement]
            print(f"  Constant resolver: injecting {const_count} file(s) for: {', '.join(names)}")

        # Build the final compressed context
        context = self._build_context(ordered_files)

        # Calculate per-file token counts
        file_token_counts = self._calculate_token_counts(ordered_files)

        total_chars = len(context)
        approx_tokens = total_chars // 4
        print(f"  Final context: {len(ordered_files)} files, ~{approx_tokens:,} tokens ({total_chars:,} chars)")

        return file_token_counts, context

    """Calculate per-file token counts based on what ends up in context."""
    def _calculate_token_counts(self, file_paths: list[str]) -> list[dict]:
        full_content_files = (
            self.config_file_paths
            | self.structural_detector.constant_files
        )
        result = []
        for fp in file_paths:
            if _OPENAPI_PATH_PATTERN.search(fp):
                content = self._compressed_map.get(fp, "")
            elif fp in full_content_files:
                content = self.file_content_map.get(fp, "")
            else:
                content = self._compressed_map.get(fp, "")
            token_count = len(content) // 4 if content else 0
            result.append({"filename": fp, "token_count": token_count})
        return result

    """Build a quick text preview for constant resolution scanning."""
    def _build_preview(self, file_paths: list[str]) -> str:
        parts = []
        for fp in file_paths:
            compressed = self._compressed_map.get(fp, "")
            if compressed:
                parts.append(compressed)
        return "\n".join(parts)

    """
    Build the final context string from compressed file content.

    Structurally-detected constant files are sent UNCOMPRESSED because
    they define URLs via enums, dictionaries, or other patterns that
    the line-level compressor may not fully capture.
    """
    def _build_context(self, file_paths: list[str]) -> str:
        # Files that should bypass compression entirely
        full_content_files = (
            self.config_file_paths
            | self.structural_detector.constant_files
        )

        sections = []
        for fp in file_paths:
            if _OPENAPI_PATH_PATTERN.search(fp):
                compressed = self._compressed_map.get(fp)
                if compressed:
                    sections.append(
                        f"{'=' * 60}\n"
                        f"FILE: {fp} [OpenAPI — paths only]\n"
                        f"{'=' * 60}\n"
                        f"{compressed}"
                    )
            elif fp in full_content_files:
                # Send full content for constant-definition and config files
                full = self.file_content_map.get(fp, "")
                if full:
                    sections.append(
                        f"{'=' * 60}\n"
                        f"FILE: {fp}\n"
                        f"{'=' * 60}\n"
                        f"{full}"
                    )
            else:
                compressed = self._compressed_map.get(fp)
                if compressed:
                    sections.append(
                        f"{'=' * 60}\n"
                        f"FILE: {fp} [compressed — only API-relevant lines shown]\n"
                        f"{'=' * 60}\n"
                        f"{compressed}"
                    )

        return "\n\n".join(sections)
