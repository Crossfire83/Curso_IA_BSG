import os
import base64
import json
import shutil
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
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
            output_dimensionality=128
        )

        self.db = VectorSearchVectorStore.from_components(
            project_id=project_id,
            region=region,
            gcs_bucket_name=gcs_bucket,
            index_id=index_id,
            endpoint_id=endpoint_id,
            embedding=embedding_model,
            stream_update=False,
        )

        # Only upsert when in index mode
        if chunks is not None:
            # Bypass langchain's add_texts to avoid the INVALID_SPARSE_EMBEDDING
            # bug (it serializes empty sparse embeddings in batch mode).
            # Instead, we embed texts, build the batch JSON ourselves, upload to
            # GCS, and call update_embeddings directly.
            texts = [doc.page_content for doc in chunks]
            # Keep only string metadata to avoid MULTIPLE_VALUES errors
            # from numeric fields being sent as conflicting restricts.
            metadatas = [
                {k: v for k, v in doc.metadata.items() if isinstance(v, str)}
                for doc in chunks
            ]

            # Track the new prefix so we can exclude it from cleanup
            new_prefix = self._batch_index(
                texts=texts,
                metadatas=metadatas,
                embedding_model=embedding_model,
                gcs_bucket=gcs_bucket,
                index_id=index_id,
            )

            # Clean old files in the background (after index is already updated)
            threading.Thread(
                target=self._clear_gcs_staging,
                args=(gcs_bucket, new_prefix),
                daemon=True,
            ).start()


        self.retriever = self.db.as_retriever(search_kwargs={"k": retriever_k})

    def _clear_gcs_staging(self, bucket_name: str, exclude_prefix: str) -> None:
        """Delete old staged embedding files from the GCS bucket in the background.

        Runs after the index update so it doesn't block the user. Uses parallel
        batch deletes for speed. Skips blobs under `exclude_prefix` (the current
        indexing batch that the index is actively using).
        """
        from google.cloud import storage

        print(f"  [background] Clearing old files from gs://{bucket_name}/ ...")

        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blobs = [
                b for b in bucket.list_blobs()
                if not b.name.startswith(exclude_prefix)
            ]

            if not blobs:
                print("    [background] No old files to clean.")
                return

            # Parallel batch deletion (batches of 1000, up to 8 threads)
            batch_size = 1000
            batches = [
                blobs[i:i + batch_size]
                for i in range(0, len(blobs), batch_size)
            ]

            def delete_batch(batch):
                bucket.delete_blobs(batch)

            with ThreadPoolExecutor(max_workers=8) as executor:
                executor.map(delete_batch, batches)

            print(f"    [background] Deleted {len(blobs)} old file(s) from GCS bucket.")
        except Exception as exc:
            # Log but don't crash — this is best-effort background cleanup
            print(f"    [background] GCS cleanup error: {exc}")

    def _batch_index(
        self,
        texts: list[str],
        metadatas: list[dict],
        embedding_model,
        gcs_bucket: str,
        index_id: str,
    ) -> str:
        """Index chunks via batch update, writing clean JSON to GCS.

        This bypasses langchain's add_texts to avoid the
        INVALID_SPARSE_EMBEDDING bug where empty sparse embedding objects
        get serialized into the batch JSON and rejected by the index.

        We also store documents in GCS (for retrieval) via the vector store's
        internal document storage, matching what add_texts would do.

        Returns the GCS prefix used for this batch (so cleanup can skip it).
        """
        from google.cloud import aiplatform, storage

        print(f"    Embedding {len(texts)} chunk(s)...")
        embeddings = embedding_model.embed_documents(texts)

        # Generate unique IDs for each datapoint
        ids = [str(uuid.uuid4()) for _ in range(len(texts))]

        # Store documents in GCS via the vector store's document storage
        # so they can be retrieved later by the searcher.
        from langchain_core.documents import Document as LCDocument
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
            # Add string restricts only
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

        print(f"    Batch index update triggered with {len(records)} datapoint(s).")
        return prefix



    def retrieve(self, query: str) -> tuple[list[dict], str]:
        """
        Retrieve relevant document chunks for the given query.

        Returns (file_token_counts, context_string).

        Context is built from the retrieved chunks directly (not full documents),
        keeping token usage proportional to the number of relevant passages.
        """
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
