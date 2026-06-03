from __future__ import annotations

import json
import threading

from dotenv import load_dotenv
from typing import Generator
from botocore.exceptions import ClientError
from flask import Flask, Response, render_template, request, stream_with_context
from main import CodebaseRAG, EXCLUDE_PATTERNS
from s3_file_collector import (
    S3FileCollector,
    S3SourceConfig,
    CredentialsError,
    validate_bucket_name,
    validate_region,
    build_s3_uri,
    resolve_region,
    DEFAULT_REGION,
)
from standard_prompt import STANDARD_PROMPT

app = Flask(__name__)

_rag_cache: dict[str, CodebaseRAG] = {}
_cache_lock = threading.Lock()


@app.get("/")
def index() -> str:
    return render_template("index.html", standard_prompt=STANDARD_PROMPT)


@app.post("/ask")
def ask() -> Response:
    source_type: str = (request.form.get("source_type") or "local").strip()
    query: str = (request.form.get("query") or "").strip() or STANDARD_PROMPT

    if source_type == "s3":
        bucket = (request.form.get("bucket") or "").strip()
        prefix = (request.form.get("prefix") or "").strip().strip("/")
        region = (request.form.get("region") or "").strip()
        return Response(
            stream_with_context(_ask_generator_s3(bucket, prefix, region, query)),
            content_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    source_dir: str = (request.form.get("source_dir") or "").strip()
    return Response(
        stream_with_context(_ask_generator(source_dir, query)),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _ask_generator(source_dir: str, query: str) -> Generator[str, None, None]:
    if not source_dir:
        yield _sse_error("source_dir must not be empty")
        return
    try:
        with _cache_lock:
            if source_dir not in _rag_cache:
                _rag_cache[source_dir] = CodebaseRAG(
                    source_dir=source_dir,
                    exclude_patterns=EXCLUDE_PATTERNS,
                )
        rag = _rag_cache[source_dir]
        result = rag.ask(query=query, invoke_llm=True, print_citations=True)

        # Emit the answer text as a single SSE data chunk
        answer_text = result.get("answer_final") or result.get("answer_raw") or ""
        yield f"data: {json.dumps({'token': answer_text})}\n\n"

        # Emit the final metadata event
        meta = {
            "done": True,
            "docs": result.get("docs"),
            "grounding_score": result.get("grounding_score"),
            "completeness_score": result.get("completeness_score"),
            "missing_files": result.get("missing_files"),
            "flagged": result.get("flagged"),
            "issues": result.get("issues"),
        }
        yield f"event: done\ndata: {json.dumps(meta)}\n\n"
    except Exception as exc:  # noqa: BLE001
        yield _sse_error(str(exc))


def _ask_generator_s3(
    bucket: str,
    prefix: str,
    region: str,
    query: str,
) -> Generator[str, None, None]:
    # 1. Validate inputs
    bucket_err = validate_bucket_name(bucket)
    if bucket_err:
        yield _sse_error(bucket_err)
        return

    effective_region = resolve_region(region)
    region_err = validate_region(effective_region)
    if region_err:
        yield _sse_error(region_err)
        return

    # 2. Build config and cache key
    config = S3SourceConfig(bucket=bucket, prefix=prefix, region=effective_region)
    cache_key = config.cache_key()

    try:
        # 3. Check cache; on miss, collect from S3
        with _cache_lock:
            if cache_key not in _rag_cache:
                collector = S3FileCollector(config=config, exclude_patterns=list(EXCLUDE_PATTERNS))
                docs, skipped, total = collector.collect_with_stats()

                # 4. Handle empty-docs case
                if not docs:
                    yield _sse_error(
                        f"No files were collected from {config.canonical_uri()}. "
                        "Check that the bucket exists, the prefix is correct, and "
                        "that your credentials have read access."
                    )
                    return

                # 5. Emit warning if some objects were skipped
                if skipped > 0:
                    yield _sse_warning(
                        f"{skipped} of {total} object(s) could not be downloaded and were skipped."
                    )

                # 6. Build and cache the RAG instance
                _rag_cache[cache_key] = CodebaseRAG(
                    documents=docs,
                    region_name=effective_region,
                )

        rag = _rag_cache[cache_key]

        # 7. Run the query
        result = rag.ask(query=query, invoke_llm=True, print_citations=True)

        # 8. Yield token event
        answer_text = result.get("answer_final") or result.get("answer_raw") or ""
        yield f"data: {json.dumps({'token': answer_text})}\n\n"

        # 9. Yield done event
        meta = {
            "done": True,
            "docs": result.get("docs"),
            "grounding_score": result.get("grounding_score"),
            "completeness_score": result.get("completeness_score"),
            "missing_files": result.get("missing_files"),
            "flagged": result.get("flagged"),
            "issues": result.get("issues"),
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
    except Exception as exc:  # noqa: BLE001
        yield _sse_error(str(exc))


def _sse_error(message: str) -> str:
    return f"event: error\ndata: {json.dumps({'error': message})}\n\n"


def _sse_warning(message: str) -> str:
    return f"event: warning\ndata: {json.dumps({'warning': message})}\n\n"


@app.post("/reload")
def reload_cache() -> tuple[Response, int]:
    try:
        source_dir: str = (request.form.get("source_dir") or "").strip()
        with _cache_lock:
            cleared = _rag_cache.pop(source_dir, None)
        payload = {"status": "ok", "cleared": source_dir if cleared is not None else None}
        return app.response_class(
            response=json.dumps(payload),
            status=200,
            mimetype="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        payload = {"status": "error", "message": str(exc)}
        return app.response_class(
            response=json.dumps(payload),
            status=500,
            mimetype="application/json",
        )


if __name__ == "__main__":
    load_dotenv()

    app.run(host="127.0.0.1", port=5000, debug=False)
