import os
import base64
import json
import shutil
from pathlib import Path

from langchain_core.documents import Document


def ensure_google_credentials() -> None:
    """Ensure the Google service account credentials file exists on disk.

    Flow:
    1. Read the path from GOOGLE_APPLICATION_CREDENTIALS env var.
    2. If the file already exists, do nothing.
    3. Otherwise, decode GOOGLE_SA_CREDENTIALS_BASE64 and write it as JSON
       to the path specified by GOOGLE_APPLICATION_CREDENTIALS.
    """
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS is not set.")

    if os.path.isfile(creds_path):
        return

    creds_b64 = os.environ.get("GOOGLE_SA_CREDENTIALS_BASE64", "")
    if not creds_b64:
        raise ValueError(
            "GOOGLE_SA_CREDENTIALS_BASE64 is not set but credentials file "
            f"does not exist at {creds_path}"
        )

    decoded_bytes = base64.b64decode(creds_b64)
    credentials_dict = json.loads(decoded_bytes.decode("utf-8"))

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(creds_path), exist_ok=True)

    with open(creds_path, "w", encoding="utf-8") as f:
        json.dump(credentials_dict, f)

    print(f"Google credentials file written to {creds_path}")

class VectorStore:
    """Manages the vector DB and retrieval for PDF documents.

    Supports two backends controlled by the VECTOR_STORE_BACKEND env var:
    - "chroma" (default): local Chroma DB
    - "google": Google Vertex AI Vector Search

    Supports two modes:
    - index: upserts chunks into the vector store (used on upload/reload)
    - query-only: connects to the existing store without re-indexing (used on ask)
    """

    def __init__(
        self,
        chunks: list[Document] | None = None,
        persist_directory: str = "./chroma_db",
        embedding_model: str = "nomic-embed-text",
        retriever_k: int = 50,
    ):
        """Initialize the vector store.

        If `chunks` is provided, upserts them into the store (index mode).
        If `chunks` is None, connects to the existing store for retrieval only (query mode).
        """
        self._retriever_k = retriever_k
        self._persist_directory = persist_directory
        self._embedding_model = embedding_model

        backend = os.environ.get("VECTOR_STORE_BACKEND", "chroma").lower()
        self._backend = backend

        index_mode = chunks is not None

        if backend == "google":
            self._init_google(chunks, retriever_k)
        else:
            self._init_chroma(chunks, persist_directory, embedding_model, retriever_k)

        if index_mode and chunks:
            print(
                f"  Vector store ({backend}): indexed "
                f"{len(chunks)} chunk(s)"
            )
        else:
            print(f"  Vector store ({backend}): connected in query-only mode")

    def _init_chroma(
        self,
        chunks: list[Document] | None,
        persist_directory: str,
        embedding_model: str,
        retriever_k: int,
    ) -> None:
        """Initialize local Chroma vector store.

        If chunks is provided: wipes existing DB and indexes fresh.
        If chunks is None: connects to the existing persisted DB.
        """
        from langchain_community.vectorstores import Chroma
        from langchain_huggingface import HuggingFaceEmbeddings

        embeddings = HuggingFaceEmbeddings(model_name=embedding_model)
        persist_path = Path(persist_directory)

        if chunks is not None:
            # Index mode: wipe and rebuild
            if persist_path.exists():
                shutil.rmtree(persist_path)

            self.db = Chroma.from_texts(
                texts=[doc.page_content for doc in chunks],
                metadatas=[doc.metadata for doc in chunks],
                embedding=embeddings,
                persist_directory=persist_directory,
            )
        else:
            # Query-only mode: connect to existing persisted DB
            self.db = Chroma(
                persist_directory=persist_directory,
                embedding_function=embeddings,
            )

        self.retriever = self.db.as_retriever(search_kwargs={"k": retriever_k})

    def _init_google(
        self,
        chunks: list[Document] | None,
        retriever_k: int,
    ) -> None:
        """Initialize Google Vertex AI Vector Search backend.

        If chunks is provided: upserts them into the index.
        If chunks is None: connects for retrieval only.

        Required env vars:
          - GCP_PROJECT_ID: Google Cloud project ID
          - GCP_REGION: GCP region (e.g. us-central1)
          - GCP_GCS_BUCKET: GCS bucket name for staging vectors
          - GCP_VS_INDEX_ID: Vertex AI Vector Search index resource ID
          - GCP_VS_ENDPOINT_ID: Vertex AI Vector Search endpoint resource ID
          - GOOGLE_APPLICATION_CREDENTIALS: path to the SA JSON file
          - GOOGLE_SA_CREDENTIALS_BASE64: base64-encoded SA JSON (used to create the file)
        """
        from google.cloud import aiplatform
        from langchain_google_vertexai import VectorSearchVectorStore
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        # Ensure credentials file exists on disk before any GCP calls
        ensure_google_credentials()

        project_id = os.environ["GCP_PROJECT_ID"]
        region = os.environ.get("GCP_REGION", "us-central1")
        gcs_bucket = os.environ["GCP_GCS_BUCKET"]
        index_id = os.environ["GCP_VS_INDEX_ID"]
        endpoint_id = os.environ["GCP_VS_ENDPOINT_ID"]

        aiplatform.init(project=project_id, location=region)

        embedding_model = GoogleGenerativeAIEmbeddings(
            # to be retired on April 1, 2027,
            # see https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions#embeddings-models
            model="text-multilingual-embedding-002",
            project=project_id,
            vertexai=True,
            output_dimensionality=768
        )

        self.db = VectorSearchVectorStore.from_components(
            project_id=project_id,
            region=region,
            gcs_bucket_name=gcs_bucket,
            index_id=index_id,
            endpoint_id=endpoint_id,
            embedding=embedding_model,
            stream_update=True,
        )

        # Only upsert when in index mode
        if chunks is not None:
            batch_size = 1000
            texts = [doc.page_content for doc in chunks]
            metadatas = [doc.metadata for doc in chunks]
            for i in range(0, len(texts), batch_size):
                self.db.add_texts(
                    texts=texts[i:i + batch_size],
                    metadatas=metadatas[i:i + batch_size],
                )
                print(f"    Indexed batch {i // batch_size + 1} "
                      f"({min(i + batch_size, len(texts))}/{len(texts)} chunks)")


        self.retriever = self.db.as_retriever(search_kwargs={"k": retriever_k})



    """
    Retrieve relevant document chunks for the given query.

    Returns (file_token_counts, context_string).

    Context is built from the retrieved chunks directly (not full documents),
    keeping token usage proportional to the number of relevant passages.
    """
    def retrieve(self, query: str) -> tuple[list[dict], str]:
        docs = self.retriever.invoke(query)

        # Build context from retrieved chunks grouped by source file
        context = self._build_context_from_chunks(docs)

        # Calculate per-file token counts based on included chunks
        file_token_counts = self._calculate_chunk_token_counts(docs)

        total_chars = len(context)
        approx_tokens = total_chars // 4
        print(
            f"  Retrieved context: {len(docs)} chunk(s) from "
            f"{len(file_token_counts)} file(s), ~{approx_tokens:,} tokens ({total_chars:,} chars)"
        )

        return file_token_counts, context

    def _build_context_from_chunks(self, docs: list) -> str:
        """Build context string directly from retrieved chunks, grouped by source file."""
        from collections import OrderedDict

        # Group chunks by file, preserving retrieval order
        file_chunks: OrderedDict[str, list[str]] = OrderedDict()
        for doc in docs:
            fp = doc.metadata.get("file", "unknown")
            if fp not in file_chunks:
                file_chunks[fp] = []
            file_chunks[fp].append(doc.page_content)

        # Format sections per file
        sections = []
        for fp, chunks in file_chunks.items():
            chunk_text = "\n---\n".join(chunks)
            sections.append(
                f"{'=' * 60}\n"
                f"DOCUMENT: {fp} ({len(chunks)} fragment(s))\n"
                f"{'=' * 60}\n"
                f"{chunk_text}"
            )
        return "\n\n".join(sections)

    def _calculate_chunk_token_counts(self, docs: list) -> list[dict]:
        """Calculate per-file token counts based on the retrieved chunks."""
        from collections import OrderedDict

        file_chars: OrderedDict[str, int] = OrderedDict()
        for doc in docs:
            fp = doc.metadata.get("file", "unknown")
            file_chars[fp] = file_chars.get(fp, 0) + len(doc.page_content)

        return [
            {"filename": fp, "token_count": chars // 4}
            for fp, chars in file_chars.items()
        ]
