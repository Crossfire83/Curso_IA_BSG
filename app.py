from __future__ import annotations

import json
import threading

from dotenv import load_dotenv
from typing import Generator
from botocore.exceptions import ClientError
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from main import DocumentRAG
from s3_file_collector import (
    S3FileCollector,
    S3SourceConfig,
    CredentialsError,
    validate_bucket_name,
    resolve_region,
)
from standard_prompt import STANDARD_PROMPT


import logging

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


@app.after_request
def log_non_200_responses(response):
    if response.status_code != 200:
        logger.warning(
            "%s %s → %s | body: %s",
            request.method,
            request.path,
            response.status,
            response.get_data(as_text=True)[:500],
        )
    return response

_rag_cache: dict[str, DocumentRAG] = {}
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# PDF upload validation constants
# ---------------------------------------------------------------------------
ALLOWED_EXTENSION = '.pdf'
ALLOWED_MIME_TYPE = 'application/pdf'
MAX_FILE_SIZE = 52_428_800  # 50 MB
RESPONSE_MIME_TYPE = 'application/json'


# ---------------------------------------------------------------------------
# PDF upload validation helpers
# ---------------------------------------------------------------------------

def validate_extension(filename: str) -> str | None:
    """Return error string if filename does not end with .pdf (case-insensitive), else None."""
    if not filename.lower().endswith(ALLOWED_EXTENSION):
        return f"'{filename}' does not have a .pdf extension."
    return None


def validate_mime_type(filename: str, content_type: str) -> str | None:
    """Return error string if content_type is not application/pdf, else None."""
    if content_type != ALLOWED_MIME_TYPE:
        return f"'{filename}' has invalid content type '{content_type}' (expected application/pdf)."
    return None


def validate_file_size(file) -> str | None:
    """Return error string if file exceeds MAX_FILE_SIZE, else None.

    Seeks to end of stream to measure size, then resets to beginning.
    """
    file.seek(0, 2)  # seek to end
    size = file.tell()
    file.seek(0)     # reset for later read
    if size > MAX_FILE_SIZE:
        return f"'{file.filename}' ({size} bytes) exceeds the 50 MB limit."
    return None


def validate_upload_batch(files: list) -> tuple[bool, list[str]]:
    """Validate all files in a batch. Returns (all_valid, list_of_errors)."""
    errors: list[str] = []
    for f in files:
        ext_err = validate_extension(f.filename)
        if ext_err:
            errors.append(ext_err)
        mime_err = validate_mime_type(f.filename, f.content_type)
        if mime_err:
            errors.append(mime_err)
        size_err = validate_file_size(f)
        if size_err:
            errors.append(size_err)
    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# S3 environment helpers
# ---------------------------------------------------------------------------

def _get_configured_bucket() -> tuple[str | None, str | None]:
    """Return (bucket_name, error_message).

    Returns (bucket, None) on success.
    Returns (None, error_msg) when the env var is missing or the name is invalid.
    """
    import os
    bucket = os.environ.get("S3_BUCKET_NAME", "").strip()
    if not bucket:
        return None, "S3_BUCKET_NAME environment variable is not configured."
    err = validate_bucket_name(bucket)
    if err:
        return None, f"Invalid bucket name in S3_BUCKET_NAME: '{bucket}'."
    return bucket, None


@app.post("/s3/files")
def s3_upload_files():
    # 1. Resolve configured bucket
    bucket, err = _get_configured_bucket()
    if err:
        return jsonify({"error": err}), 500

    # 2. Validate file list
    files = request.files.getlist("files")
    valid_files = [f for f in files if f.filename and f.filename.strip()]
    if not valid_files:
        return jsonify({"error": "No files provided."}), 400

    # 2b. Validate PDF constraints (extension, MIME type, size)
    ok, validation_errors = validate_upload_batch(valid_files)
    if not ok:
        return jsonify({"error": "Validation failed.", "errors": validation_errors}), 400

    # 3. Build S3 client
    try:
        s3 = _make_s3_client()
    except CredentialsError as exc:
        return jsonify({"error": str(exc)}), 500

    # 4. Upload each file; stop immediately on ClientError
    uploaded: list[str] = []
    for file in valid_files:
        try:
            s3.put_object(Bucket=bucket, Key=file.filename, Body=file.read())
            uploaded.append(file.filename)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            if error_code == "NoSuchBucket":
                return jsonify({"error": f"Bucket '{bucket}' not found."}), 404
            elif error_code == "AccessDenied":
                return jsonify({"error": f"Access denied to bucket '{bucket}'. Check IAM permissions."}), 403
            else:
                return jsonify({"error": f"S3 error: {error_code}. {message}"}), 500

    # 5. All uploads succeeded — re-index the vector store from S3
    with _cache_lock:
        cache_key = f"s3://{bucket}?region={resolve_region('')}"
        try:
            config = S3SourceConfig(bucket=bucket, prefix="", region=resolve_region(""))
            collector = S3FileCollector(config=config)
            docs, _skipped, _total = collector.collect_with_stats()

            if docs:
                _rag_cache[cache_key] = DocumentRAG(
                    documents=docs,
                    region_name=resolve_region(""),
                )
        except Exception:
            # If re-indexing fails, clear cache so next reload can retry
            _rag_cache.pop(cache_key, None)

    return jsonify({"uploaded": uploaded}), 200


@app.get("/s3/config")
def s3_config():
    bucket, err = _get_configured_bucket()
    if err:
        return jsonify({"error": err}), 500
    return jsonify({"bucket": bucket}), 200


@app.get("/")
def index() -> str:
    return render_template("index.html", standard_prompt=STANDARD_PROMPT)


@app.post("/ask")
def ask() -> Response | tuple[Response, int]:
    source_type: str = (request.form.get("source_type") or "s3").strip()
    query: str = (request.form.get("query") or "").strip() or STANDARD_PROMPT

    # Reject local source type
    if source_type == "local":
        return jsonify({"error": "Local directory source is not supported."}), 400

    # Always use S3 pipeline — ignore source_dir entirely
    return Response(
        stream_with_context(_ask_generator_s3(query)),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _make_s3_client():
    """Create and return a configured boto3 S3 client.

    Region resolution: AWS_REGION env var → DEFAULT_REGION ("us-west-1").
    Raises CredentialsError if no credentials can be resolved.
    """
    import boto3
    import botocore.config
    import botocore.exceptions

    region = resolve_region("")  # reads AWS_REGION or falls back to DEFAULT_REGION
    cfg = botocore.config.Config(connect_timeout=2, retries={"mode": "standard"})
    session = boto3.Session(region_name=region)

    creds = session.get_credentials()
    if creds is None:
        raise CredentialsError("AWS credentials could not be resolved.")
    creds.get_frozen_credentials()

    return session.client("s3", config=cfg)


@app.delete("/s3/files/<path:key>")
def s3_delete_file(key: str):
    # 1. Validate bucket configuration
    bucket, err = _get_configured_bucket()
    if err:
        return jsonify({"error": err}), 500

    # 2. Validate key is not blank
    if not key or not key.strip():
        return jsonify({"error": "Key must not be empty."}), 400

    # 3. Build S3 client
    try:
        s3 = _make_s3_client()
    except CredentialsError:
        return jsonify({"error": "AWS credentials could not be resolved."}), 500

    # 4. Delete the object (non-existent keys succeed silently on S3)
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if error_code == "NoSuchBucket":
            return jsonify({"error": f"Bucket '{bucket}' not found."}), 404
        if error_code == "AccessDenied":
            return jsonify({"error": f"Access denied to bucket '{bucket}'. Check IAM permissions."}), 403
        return jsonify({"error": f"S3 error: {error_code}. {message}"}), 500

    # 5. Invalidate RAG cache and re-index for this bucket
    with _cache_lock:
        cache_key = f"s3://{bucket}?region={resolve_region('')}"
        try:
            config = S3SourceConfig(bucket=bucket, prefix="", region=resolve_region(""))
            collector = S3FileCollector(config=config)
            docs, _skipped, _total = collector.collect_with_stats()

            if docs:
                _rag_cache[cache_key] = DocumentRAG(
                    documents=docs,
                    region_name=resolve_region(""),
                )
            else:
                _rag_cache.pop(cache_key, None)
        except Exception:
            _rag_cache.pop(cache_key, None)

    # 6. Return success
    return jsonify({"deleted": key}), 200


def _ask_generator_s3(query: str) -> Generator[str, None, None]:
    # 1. Read bucket from environment
    bucket, err = _get_configured_bucket()
    if err:
        yield _sse_error(err)
        return

    # 2. Build config and cache key
    region = resolve_region("")
    config = S3SourceConfig(bucket=bucket, prefix="", region=region)
    cache_key = config.cache_key()

    try:
        # 3. Check cache; on miss, connect in query-only mode (no re-indexing).
        #    Indexing only happens on upload or explicit reload.
        with _cache_lock:
            if cache_key not in _rag_cache:
                _rag_cache[cache_key] = DocumentRAG(
                    region_name=region,
                )

        rag = _rag_cache[cache_key]

        # 4. Run the query
        result = rag.ask(query=query)

        # 5. Yield token event
        answer_text = result.get("answer_final") or result.get("answer_raw") or ""
        yield f"data: {json.dumps({'token': answer_text})}\n\n"

        # 6. Yield done event
        meta = {
            "done": True,
            "docs": result.get("docs"),
        }
        yield f"event: done\ndata: {json.dumps(meta)}\n\n"

    except CredentialsError as exc:
        yield _sse_error(f"AWS credentials error: {exc}")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchBucket":
            yield _sse_error(f"Bucket '{bucket}' does not exist.")
        elif error_code == "AccessDenied":
            yield _sse_error(f"Access denied to bucket '{bucket}'. Check your IAM permissions.")
        else:
            yield _sse_error(f"AWS S3 error ({error_code}): {exc}")
    except TimeoutError as exc:
        yield _sse_error(f"Connection to S3 timed out: {exc}")
    except Exception as exc:
        yield _sse_error(str(exc))


def _sse_error(message: str) -> str:
    return f"event: error\ndata: {json.dumps({'error': message})}\n\n"


def _sse_warning(message: str) -> str:
    return f"event: warning\ndata: {json.dumps({'warning': message})}\n\n"


@app.get("/s3/files")
def list_s3_files() -> tuple[Response, int]:
    """List all object keys in the configured S3 bucket.

    Returns:
        200: {"files": ["key1", "key2", ...]} — empty list when bucket is empty
        403: {"error": "Access denied to bucket '<bucket>'. Check IAM permissions."}
        404: {"error": "Bucket '<bucket>' not found."}
        500: {"error": "<message>"}
    """
    # 1. Resolve bucket from environment
    bucket, err = _get_configured_bucket()
    if err:
        return _json_error(err, 500)

    # 2. Build S3 client (raises CredentialsError on failure)
    try:
        client = _make_s3_client()
    except CredentialsError as exc:
        return _json_error(str(exc), 500)

    # 3. Paginate over all objects and collect keys
    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket)
        keys: list[str] = []
        for page in pages:
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if code == "NoSuchBucket":
            return _json_error(f"Bucket '{bucket}' not found.", 404)
        elif code == "AccessDenied":
            return _json_error(
                f"Access denied to bucket '{bucket}'. Check IAM permissions.", 403
            )
        else:
            return _json_error(f"S3 error: {code}. {message}", 500)

    return app.response_class(
        response=json.dumps({"files": keys}),
        status=200,
        mimetype=RESPONSE_MIME_TYPE,
    )


def _json_error(message: str, status: int) -> tuple[Response, int]:
    """Return a JSON error response tuple."""
    return (
        app.response_class(
            response=json.dumps({"error": message}),
            status=status,
            mimetype=RESPONSE_MIME_TYPE,
        ),
        status,
    )


@app.post("/reload")
def reload_cache() -> tuple[Response, int]:
    """Re-index the vector store from S3 documents."""
    try:
        bucket, err = _get_configured_bucket()
        if err:
            return _json_error(err, 500)

        region = resolve_region("")
        config = S3SourceConfig(bucket=bucket, prefix="", region=region)
        cache_key = config.cache_key()

        # Collect documents from S3 and re-index
        collector = S3FileCollector(config=config)
        docs, skipped, total = collector.collect_with_stats()

        if not docs:
            with _cache_lock:
                _rag_cache.pop(cache_key, None)
            return _json_error(
                f"No files found in {config.canonical_uri()}. Nothing to index.", 404
            )

        with _cache_lock:
            _rag_cache[cache_key] = DocumentRAG(
                documents=docs,
                region_name=region,
            )

        payload = {
            "status": "ok",
            "indexed": len(docs),
            "skipped": skipped,
            "total": total,
        }
        return app.response_class(
            response=json.dumps(payload),
            status=200,
            mimetype=RESPONSE_MIME_TYPE,
        )
    except CredentialsError as exc:
        return _json_error(str(exc), 500)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if error_code == "NoSuchBucket":
            return _json_error("Bucket not found.", 404)
        elif error_code == "AccessDenied":
            return _json_error("Access denied. Check IAM permissions.", 403)
        return _json_error(f"S3 error: {error_code}. {message}", 500)
    except Exception as exc:
        return _json_error(str(exc), 500)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
