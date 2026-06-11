import os
import base64
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from langchain_core.documents import Document

logger = logging.getLogger(__name__)


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

    logger.info("Google credentials file written to %s", creds_path)


class VectorStore:
    """Manages the vector DB and retrieval for PDF documents.

    Supports two backends controlled by the VECTOR_STORE_BACKEND env var:
    - "chroma" (default): local Chroma DB
    - "google": Google Vertex AI Vector Search

    The constructor only establishes the connection. Use `index()` to upsert
    documents and `retrieve()` to query. A single instance supports both
    operations without needing to be recreated.
    """

    def __init__(
        self,
        persist_directory: str = "./chroma_db",
        embedding_model: str = "nomic-embed-text",
        retriever_k: int = 10,
    ):
        """Connect to the vector store backend.

        This only sets up the connection — no indexing happens here.
        Call `index(chunks)` to upsert documents.
        Call `retrieve(query)` to search.
        """
        self._retriever_k = retriever_k
        self._persist_directory = persist_directory
        self._embedding_model = embedding_model

        backend = os.environ.get("VECTOR_STORE_BACKEND", "chroma").lower()
        self._backend = backend

        t0 = time.time()
        if backend == "google":
            self._connect_google()
        else:
            self._connect_chroma(persist_directory, embedding_model)

        self.retriever = self.db.as_retriever(search_kwargs={"k": retriever_k})
        elapsed = time.time() - t0
        logger.info("[VectorStore] Connected to '%s' backend in %.2fs", backend, elapsed)

    # ------------------------------------------------------------------
    # Connection (called once at construction)
    # ------------------------------------------------------------------

    def _connect_chroma(self, persist_directory: str, embedding_model: str) -> None:
        """Connect to the local Chroma vector store."""
        from langchain_community.vectorstores import Chroma
        from langchain_huggingface import HuggingFaceEmbeddings

        self._embeddings = HuggingFaceEmbeddings(model_name=embedding_model)

        self.db = Chroma(
            persist_directory=persist_directory,
            embedding_function=self._embeddings,
        )

    def _connect_google(self) -> None:
        """Connect to Google Vertex AI Vector Search.

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

        ensure_google_credentials()

        project_id = os.environ["GCP_PROJECT_ID"]
        region = os.environ.get("GCP_REGION", "us-central1")
        gcs_bucket = os.environ["GCP_GCS_BUCKET"]
        index_id = os.environ["GCP_VS_INDEX_ID"]
        endpoint_id = os.environ["GCP_VS_ENDPOINT_ID"]

        self._gcp_project_id = project_id
        self._gcp_region = region
        self._gcs_bucket = gcs_bucket
        self._index_id = index_id

        aiplatform.init(project=project_id, location=region)

        self._google_embedding_model = GoogleGenerativeAIEmbeddings(
            model="text-multilingual-embedding-002",
            project=project_id,
            vertexai=True,
            output_dimensionality=128,
        )

        self.db = VectorSearchVectorStore.from_components(
            project_id=project_id,
            region=region,
            gcs_bucket_name=gcs_bucket,
            index_id=index_id,
            endpoint_id=endpoint_id,
            embedding=self._google_embedding_model,
            stream_update=False,
        )

    # ------------------------------------------------------------------
    # Index (call explicitly when you have documents to upsert)
    # ------------------------------------------------------------------

    def index(self, chunks: list[Document]) -> None:
        """Upsert document chunks into the vector store.

        For Chroma: wipes existing DB and rebuilds from scratch.
        For Google: batch-indexes via GCS upload + update_embeddings.
        """
        if not chunks:
            logger.warning("[VectorStore] index() called with empty chunks list — skipping.")
            return

        t0 = time.time()
        logger.info("[VectorStore] Indexing %d chunk(s) into '%s' backend...", len(chunks), self._backend)

        if self._backend == "google":
            self._index_google(chunks)
        else:
            self._index_chroma(chunks)

        # Refresh the retriever after indexing
        self.retriever = self.db.as_retriever(search_kwargs={"k": self._retriever_k})

        elapsed = time.time() - t0
        logger.info("[VectorStore] Indexing complete in %.2fs — %d chunk(s)", elapsed, len(chunks))

    def _index_chroma(self, chunks: list[Document]) -> None:
        """Wipe and rebuild the local Chroma DB."""
        from langchain_community.vectorstores import Chroma

        persist_path = Path(self._persist_directory)
        if persist_path.exists():
            shutil.rmtree(persist_path)

        self.db = Chroma.from_texts(
            texts=[doc.page_content for doc in chunks],
            metadatas=[doc.metadata for doc in chunks],
            embedding=self._embeddings,
            persist_directory=self._persist_directory,
        )

    def _index_google(self, chunks: list[Document]) -> None:
        """Batch-index chunks into Google Vertex AI Vector Search."""
        texts = [doc.page_content for doc in chunks]
        metadatas = [
            {k: v for k, v in doc.metadata.items() if isinstance(v, str)}
            for doc in chunks
        ]

        # Clean old staged files before generating new embeddings
        self._clear_gcs_staging(self._gcs_bucket)

        # Batch index
        self._batch_index(
            texts=texts,
            metadatas=metadatas,
            embedding_model=self._google_embedding_model,
            gcs_bucket=self._gcs_bucket,
            index_id=self._index_id,
        )

    # ------------------------------------------------------------------
    # Retrieve (call on each query)
    # ------------------------------------------------------------------

    def retrieve(self, query: str) -> tuple[list[dict], str]:
        """Retrieve relevant document chunks for the given query.

        Returns (file_token_counts, context_string).
        """
        t0 = time.time()
        docs = self.retriever.invoke(query)
        t1 = time.time()
        logger.info(
            "[VectorStore] retriever.invoke() took %.2fs — returned %d doc(s)",
            t1 - t0, len(docs),
        )

        # Build context from retrieved chunks grouped by source file
        context = self._build_context_from_chunks(docs)

        # Calculate per-file token counts based on included chunks
        file_token_counts = self._calculate_chunk_token_counts(docs)

        total_chars = len(context)
        approx_tokens = total_chars // 4
        logger.info(
            "[VectorStore] Retrieved context: %d chunk(s) from %d file(s), ~%d tokens (%d chars)",
            len(docs), len(file_token_counts), approx_tokens, total_chars,
        )

        return file_token_counts, context

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear_gcs_staging(self, bucket_name: str) -> None:
        """Delete old staged embedding files from the GCS bucket."""
        from google.cloud import storage

        logger.info("[VectorStore] Clearing old files from gs://%s/ ...", bucket_name)

        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blobs = list(bucket.list_blobs())

            if not blobs:
                logger.info("[VectorStore] No old files to clean.")
                return

            batch_size = 1000
            for i in range(0, len(blobs), batch_size):
                batch = blobs[i:i + batch_size]
                bucket.delete_blobs(batch)

            logger.info("[VectorStore] Deleted %d old file(s) from GCS bucket.", len(blobs))
        except Exception as exc:
            logger.error("[VectorStore] GCS cleanup error: %s", exc)

    def _batch_index(
        self,
        texts: list[str],
        metadatas: list[dict],
        embedding_model,
        gcs_bucket: str,
        index_id: str,
    ) -> None:
        """Index chunks via batch update, writing clean JSON to GCS."""
        from google.cloud import aiplatform, storage
        from langchain_core.documents import Document as LCDocument

        logger.info("[VectorStore] Embedding %d chunk(s)...", len(texts))
        embeddings = embedding_model.embed_documents(texts)

        # Generate unique IDs for each datapoint
        ids = [str(uuid.uuid4()) for _ in range(len(texts))]

        # Store documents in GCS via the vector store's document storage
        documents = [
            LCDocument(id=id_, page_content=text, metadata={**meta, "id": id_})
            for id_, text, meta in zip(ids, texts, metadatas)
        ]
        self.db._document_storage.mset(list(zip(ids, documents)))

        # Build batch records (without sparse_embedding field)
        records = []
        for id_, embedding, metadata in zip(ids, embeddings, metadatas):
            record = {
                "id": id_,
                "embedding": embedding,
            }
            restricts = [
                {"namespace": k, "allow": [v]}
                for k, v in metadata.items()
                if isinstance(v, str)
            ]
            if restricts:
                record["restricts"] = restricts
            records.append(record)

        # Write batch JSON to GCS
        file_content = "\n".join(json.dumps(r) for r in records)
        prefix = str(uuid.uuid4())

        storage_client = storage.Client()
        bucket = storage_client.bucket(gcs_bucket)
        blob = bucket.blob(f"{prefix}/documents.json")
        blob.upload_from_string(file_content)

        contents_delta_uri = f"gs://{gcs_bucket}/{prefix}"

        # Trigger the batch index update
        index = aiplatform.MatchingEngineIndex(index_id)
        index.update_embeddings(
            contents_delta_uri=contents_delta_uri,
            is_complete_overwrite=True,
        )

        logger.info("[VectorStore] Batch index update triggered with %d datapoint(s).", len(records))

    def _build_context_from_chunks(self, docs: list) -> str:
        """Build context string directly from retrieved chunks, grouped by source file."""
        from collections import OrderedDict

        file_chunks: OrderedDict[str, list[str]] = OrderedDict()
        for doc in docs:
            fp = doc.metadata.get("file", "unknown")
            if fp not in file_chunks:
                file_chunks[fp] = []
            file_chunks[fp].append(doc.page_content)

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
        file_pages: OrderedDict[str, set] = OrderedDict()
        for doc in docs:
            fp = doc.metadata.get("file", "unknown")
            file_chars[fp] = file_chars.get(fp, 0) + len(doc.page_content)
            page = doc.metadata.get("page")
            if page is not None:
                if fp not in file_pages:
                    file_pages[fp] = set()
                file_pages[fp].add(page)

        results = []
        for fp, chars in file_chars.items():
            entry: dict = {"filename": fp, "token_count": chars // 4}
            if fp in file_pages:
                entry["pages"] = sorted(file_pages[fp])
            results.append(entry)

        return results
