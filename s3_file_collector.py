"""S3 bucket integration for the Document RAG tool.

This module provides S3FileCollector, which replicates the FileCollector
interface but reads objects from an AWS S3 bucket instead of the local
filesystem.
"""

from __future__ import annotations

import fnmatch
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_REGION = "us-west-1"
S3_TIMEOUT_SECONDS = 30
PAGE_EXTRACT_TIMEOUT_SECONDS = 30   # Max time per page for text extraction
PDF_EXTRACT_TIMEOUT_SECONDS = 300   # Max total time per PDF file

# AWS bucket names: 3–63 chars, lowercase letters, digits, and hyphens,
# no leading or trailing hyphen.
BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$')

# AWS region strings of the form <area>-<direction/name>-<number>, e.g. us-west-1.
REGION_RE = re.compile(r'^[a-z]{2}-[a-z]+-\d+$')


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CredentialsError(Exception):
    """Raised when no valid AWS credentials can be resolved."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class S3SourceConfig:
    """Immutable value object describing an S3 document source.

    Fields
    ------
    bucket : str
        Validated AWS bucket name (e.g. "my-bucket").
    prefix : str
        Normalised key prefix — no leading or trailing slash; may be "".
    region : str
        Validated AWS region string (e.g. "us-west-1"); never empty.
    """

    bucket: str
    prefix: str
    region: str

    def cache_key(self) -> str:
        """Return the canonical cache key string.

        Format:
            s3://<bucket>/<prefix>?region=<region>   (non-empty prefix)
            s3://<bucket>?region=<region>             (empty prefix)
        """
        if self.prefix:
            return f"s3://{self.bucket}/{self.prefix}?region={self.region}"
        return f"s3://{self.bucket}?region={self.region}"

    def canonical_uri(self) -> str:
        """Return the canonical S3 URI (without region query param).

        Format:
            s3://<bucket>/<prefix>   (non-empty prefix)
            s3://<bucket>            (empty prefix)
        """
        if self.prefix:
            return f"s3://{self.bucket}/{self.prefix}"
        return f"s3://{self.bucket}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_bucket_name(bucket: str) -> str | None:
    """Return an error message string if bucket is invalid, else None.

    Rules (AWS bucket naming):
    - Non-empty
    - 3–63 characters long
    - Only lowercase letters, digits, and hyphens
    - No leading or trailing hyphen
    - Must match BUCKET_RE
    """
    if not bucket or len(bucket) < 3 or len(bucket) > 63:
        return f"Invalid bucket name: '{bucket}'."
    if not BUCKET_RE.match(bucket):
        return f"Invalid bucket name: '{bucket}'."
    return None


def validate_region(region: str) -> str | None:
    """Return an error message string if region is invalid, else None.

    Uses REGION_RE to validate the format (e.g. ``us-west-1``).
    """
    if not REGION_RE.match(region):
        return f"Invalid AWS region: '{region}'."
    return None


# ---------------------------------------------------------------------------
# URI utilities
# ---------------------------------------------------------------------------

def build_s3_uri(bucket: str, prefix: str) -> str:
    """Build a canonical S3 URI from a validated bucket and prefix.

    Returns ``s3://<bucket>/<prefix>`` when prefix is non-empty,
    or ``s3://<bucket>`` when prefix is empty.
    """
    if prefix:
        return f"s3://{bucket}/{prefix}"
    return f"s3://{bucket}"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse a canonical S3 URI into ``(bucket, prefix)``.

    The prefix is ``""`` when the URI has no path component beyond the bucket.
    """
    # Strip the scheme
    without_scheme = uri[len("s3://"):]
    # Split bucket from the rest
    if "/" in without_scheme:
        bucket, prefix = without_scheme.split("/", 1)
    else:
        bucket = without_scheme
        prefix = ""
    return bucket, prefix


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------

def resolve_region(config_region: str) -> str:
    """Resolve the effective AWS region using priority order.

    Priority (highest to lowest):
    1. ``config_region`` — if non-empty, this wins.
    2. ``AWS_REGION`` environment variable — used when config_region is empty.
    3. ``DEFAULT_REGION`` — hardcoded fallback (``"us-west-1"``).
    """
    import os

    if config_region:
        return config_region
    env_region = os.environ.get("AWS_REGION", "")
    if env_region:
        return env_region
    return DEFAULT_REGION


# ---------------------------------------------------------------------------
# Safe PDF extraction (subprocess-isolated, per-page results)
# ---------------------------------------------------------------------------

def _extract_all_pages_worker(pdf_bytes: bytes, page_timeout: int) -> list[tuple[int, str, int]]:
    """Worker function for subprocess: opens PDF and extracts text per page.

    Returns a list of (page_number, text, total_pages) tuples for pages that succeeded.
    Runs in a child process so gunicorn's signal handler cannot interfere.
    """
    import threading
    import traceback

    # Configure logging in the child process
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [pdf-worker] %(message)s",
    )

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    results: list[tuple[int, str, int]] = []

    logging.info("PDF opened successfully: %d pages", total_pages)

    for page_num, page in enumerate(reader.pages, start=1):
        # Per-page timeout via thread
        text_holder: list[str | None] = [None]
        error_holder: list[str | None] = [None]

        def _worker(p=page, holder=text_holder, err_holder=error_holder):
            try:
                holder[0] = p.extract_text()
            except Exception:
                holder[0] = None
                err_holder[0] = traceback.format_exc()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=page_timeout)

        if t.is_alive():
            logging.warning(
                "Page %d/%d: extraction timed out after %ds",
                page_num, total_pages, page_timeout,
            )
            continue

        if error_holder[0]:
            logging.warning(
                "Page %d/%d: extraction failed:\n%s",
                page_num, total_pages, error_holder[0],
            )
            continue

        text = text_holder[0]
        if text and text.strip():
            results.append((page_num, text, total_pages))

    logging.info(
        "Extraction complete: %d/%d pages yielded text", len(results), total_pages,
    )
    return results


def extract_pdf_pages_safe(
    pdf_bytes: bytes,
    full_key: str = "<unknown>",
    timeout: int = PDF_EXTRACT_TIMEOUT_SECONDS,
    page_timeout: int = PAGE_EXTRACT_TIMEOUT_SECONDS,
) -> tuple[list[tuple[int, str, int]], str | None]:
    """Extract text from all PDF pages in a subprocess with a total timeout.

    Returns (pages_data, error_message).
    On success: ([(page_num, text, total_pages), ...], None)
    On failure/timeout: ([], error_description)
    """
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout
    import multiprocessing
    import traceback

    pdf_size_mb = len(pdf_bytes) / (1024 * 1024)
    logging.info(
        "Starting PDF extraction for %r (%.2f MB, timeout=%ds, page_timeout=%ds)",
        full_key, pdf_size_mb, timeout, page_timeout,
    )

    try:
        # Use 'spawn' to avoid issues with gunicorn's forked workers
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as executor:
            future = executor.submit(_extract_all_pages_worker, pdf_bytes, page_timeout)
            page_results = future.result(timeout=timeout)

        logging.info(
            "PDF extraction succeeded for %r: %d page(s) extracted",
            full_key, len(page_results),
        )
        return page_results, None

    except FuturesTimeout:
        err_msg = (
            f"PDF extraction timed out after {timeout}s "
            f"(file size: {pdf_size_mb:.2f} MB)"
        )
        logging.error("TIMEOUT for %r: %s", full_key, err_msg)
        return [], err_msg
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        logging.exception("FAILED extraction for %r: %s", full_key, err_msg)
        return [], err_msg


# ---------------------------------------------------------------------------
# S3FileCollector
# ---------------------------------------------------------------------------

class S3FileCollector:
    """Replicates the FileCollector interface for S3 sources.

    Raises CredentialsError if no AWS credentials can be resolved at
    construction time.
    Raises botocore.exceptions.ClientError for unrecoverable S3 API errors
    during collection.
    """

    def __init__(
        self,
        config: S3SourceConfig,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self.config = config
        self.exclude_patterns = exclude_patterns or []
        self.session, self.s3_client = self._resolve_session()

    def _resolve_session(self):
        """Build a boto3.Session with a 2-second IMDSv2 connect timeout.

        Region priority: S3SourceConfig.region > AWS_REGION env var > DEFAULT_REGION
        (config.region is already resolved to a non-empty string before this
        point, so we pass it directly to boto3.Session).

        Returns (session, s3_client).
        Raises CredentialsError if no valid credentials can be found.
        """
        import boto3
        import botocore.config
        import botocore.exceptions

        boto_cfg = botocore.config.Config(
            connect_timeout=2,
            retries={"mode": "standard"},
        )

        session = boto3.Session(region_name=self.config.region)
        try:
            # Calling get_credentials() and checking for None is the
            # lightweight way to detect a missing credential chain without
            # making a network call.
            credentials = session.get_credentials()
            if credentials is None:
                raise botocore.exceptions.NoCredentialsError()
            # Resolve (refresh) the credentials so expiry / malformed
            # token errors surface here rather than later.
            credentials.get_frozen_credentials()
        except botocore.exceptions.NoCredentialsError:
            raise CredentialsError(
                "AWS credentials could not be resolved. "
                "Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment "
                "variables, configure an AWS profile in ~/.aws/credentials, "
                "or attach an IAM role to the instance."
            )

        s3_client = session.client("s3", config=boto_cfg)
        return session, s3_client

    def _relative_key(self, full_key: str) -> str:
        """Strip config.prefix and any leading slash from *full_key*.

        Examples
        --------
        prefix="src", full_key="src/main.py"  → "main.py"
        prefix="",    full_key="main.py"       → "main.py"
        prefix="src", full_key="src/"          → ""
        """
        prefix = self.config.prefix
        if prefix and full_key.startswith(prefix):
            rel = full_key[len(prefix):]
        else:
            rel = full_key
        return rel.lstrip("/")

    def _matches_any(self, relative_key: str) -> bool:
        """Return True if *relative_key* matches any of the exclude patterns.

        Mirrors the matching logic used by FileCollector._matches_any so that
        the same pattern set produces identical filtering decisions for both
        local and S3 sources.
        """
        import os as _os
        return any(
            fnmatch.fnmatch(relative_key, p)
            or fnmatch.fnmatch(_os.path.basename(relative_key), p)
            or fnmatch.fnmatch(relative_key, f"*/{p}")
            or fnmatch.fnmatch(relative_key, f"**/{p}")
            for p in self.exclude_patterns
        )

    def collect(self) -> list[Document]:
        """List and download S3 objects, returning LangChain Documents.

        Behaviour:
        - Uses the ``list_objects_v2`` paginator with ``Bucket`` and ``Prefix``.
        - When ``config.prefix`` is ``""`` the paginator receives an empty
          prefix, causing S3 to return all objects in the bucket.
        - Each object key is converted to a relative key via ``_relative_key``.
        - Keys whose relative form matches any exclude pattern are skipped.
        - Pages that have no ``Contents`` key (no objects under prefix) are
          treated as empty — no exception is raised.
        - Any ``ClientError`` raised by the paginator is propagated to the
          caller without modification.
        - For each surviving key, ``get_object`` is called and the body is
          decoded as UTF-8 with ``errors='replace'``.
        - On per-object ``ClientError`` or ``Exception``, a warning is logged
          and the object is skipped; collection continues.

        Returns
        -------
        list[Document]
            LangChain Documents with ``page_content`` set to the decoded object
            body and ``metadata`` containing ``file`` (relative key).
            Returns an empty list when no objects exist
            under the prefix or all objects were excluded or failed to download.

        Raises
        ------
        botocore.exceptions.ClientError
            Re-raised without modification for unrecoverable S3 listing errors
            (e.g. ``AccessDenied``, ``NoSuchBucket``).
        """
        docs, _skipped, _total = self._collect_internal()
        return docs

    def collect_with_stats(self) -> tuple[list[Document], int, int]:
        """List and download S3 objects, returning documents plus counters.

        Returns
        -------
        tuple[list[Document], int, int]
            A 3-tuple of ``(docs, skipped, total)`` where:
            - ``docs`` — the successfully created Documents
            - ``skipped`` — number of objects that failed to download
            - ``total`` — total number of objects that survived listing/filtering
        """
        return self._collect_internal()

    def _collect_internal(self) -> tuple[list[Document], int, int]:
        """Internal implementation shared by ``collect`` and ``collect_with_stats``.

        Returns ``(docs, skipped, total)``.

        Raises
        ------
        botocore.exceptions.ClientError
            Re-raised without modification for unrecoverable S3 listing errors.
        """
        from botocore.exceptions import ClientError

        # --- Listing phase ---------------------------------------------------
        paginator = self.s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self.config.bucket,
            Prefix=self.config.prefix,
        )

        surviving: list[tuple[str, str]] = []

        for page in pages:
            for obj in page.get("Contents", []):
                full_key: str = obj["Key"]
                rel_key: str = self._relative_key(full_key)
                if self._matches_any(rel_key):
                    continue
                surviving.append((full_key, rel_key))

        total = len(surviving)
        skipped = 0
        docs: list[Document] = []

        # --- Download phase --------------------------------------------------
        logging.info(
            "Starting download phase: %d file(s) to process from %s",
            total, self.config.canonical_uri(),
        )
        for idx, (full_key, rel_key) in enumerate(surviving, start=1):
            try:
                logging.info(
                    "Processing file %d/%d: %r", idx, total, full_key,
                )
                response = self.s3_client.get_object(
                    Bucket=self.config.bucket,
                    Key=full_key,
                )
                pdf_bytes = response["Body"].read()
                logging.info(
                    "Downloaded %r: %.2f MB",
                    full_key, len(pdf_bytes) / (1024 * 1024),
                )

                # Extract text in a subprocess (protects gunicorn worker)
                pages_data, extract_err = extract_pdf_pages_safe(
                    pdf_bytes, full_key=full_key,
                )

                if extract_err:
                    logging.error(
                        "Skipping S3 object %r: %s", full_key, extract_err,
                    )
                    skipped += 1
                    continue

                if not pages_data:
                    logging.warning(
                        "Skipping S3 object %r: no extractable text in PDF",
                        full_key,
                    )
                    skipped += 1
                    continue

                for page_num, text, total_pages in pages_data:
                    docs.append(Document(
                        page_content=text,
                        metadata={
                            "file": rel_key,
                            "page": page_num,
                            "total_pages": total_pages,
                        },
                    ))

                logging.info(
                    "Finished %r: extracted %d page(s)",
                    full_key, len(pages_data),
                )

            except ClientError as exc:
                logging.exception(
                    "Skipping S3 object %r (ClientError): %s", full_key, exc,
                )
                skipped += 1
            except Exception as exc:
                logging.exception(
                    "Skipping S3 object %r (unexpected error): %s", full_key, exc,
                )
                skipped += 1

        logging.info(
            "Collection complete: %d document page(s) extracted, %d file(s) skipped, %d total",
            len(docs), skipped, total,
        )
        return docs, skipped, total
