from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from main import CodebaseRAG, EXCLUDE_PATTERNS
from standard_prompt import STANDARD_PROMPT

app = FastAPI(title="System Interconnectivity RAG")

# ── In-process cache ──────────────────────────────────────────
_rag_cache: dict[str, CodebaseRAG] = {}


# ── Request / Response models ─────────────────────────────────
class AskRequest(BaseModel):
    source_dir: str
    query: str | None = None
    invoke_llm: bool = True
    print_citations: bool = True

    @field_validator("source_dir")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be empty or whitespace-only")
        return v


class AskResponse(BaseModel):
    docs: str
    answer_raw: str | None = None
    answer_final: str | None = None
    grounding_score: float | None = None
    completeness_score: float | None = None
    missing_files: list[str] | None = None
    flagged: bool | None = None
    issues: list[str] | None = None


# ── Endpoint ──────────────────────────────────────────────────
@app.post("/ask")
async def ask(request: AskRequest) -> AskResponse:
    try:
        if request.source_dir not in _rag_cache:
            _rag_cache[request.source_dir] = CodebaseRAG(
                source_dir=request.source_dir,
                exclude_patterns=EXCLUDE_PATTERNS,
            )
        rag = _rag_cache[request.source_dir]
        result: dict[str, Any] = rag.ask(
            query=request.query or STANDARD_PROMPT,
            invoke_llm=request.invoke_llm,
            print_citations=request.print_citations,
        )
        return AskResponse(**result)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": str(exc)},
        )
