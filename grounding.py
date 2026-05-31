import json
import re
from langchain_core.documents import Document
from structural_detector import StructuralDetector


"""
Evaluates how well an LLM answer is grounded in the source codebase.

Instead of naive token overlap (which fails when the LLM reconstructs
URLs or generates JSON), this evaluator checks whether the concrete
claims — URLs, paths, property names, class names — are traceable
back to the source files.

Also evaluates *completeness*: whether the LLM covered all the
structurally-detected API files in the codebase.
"""
class GroundingEvaluator:

    def __init__(
        self,
        file_content_map: dict[str, str],
        structural_detector: StructuralDetector | None = None,
    ):
        self.file_content_map = file_content_map
        self._full_corpus = "\n".join(file_content_map.values()).lower()
        self._detector = structural_detector or StructuralDetector(file_content_map)

    """
    Extract verifiable claims from the answer and check them against
    the full source corpus.

    Returns (score, issues) where score is 0.0–1.0.
    """
    def evaluate(self, answer: str) -> tuple[float, list[str]]:
        claims = self._extract_claims(answer)

        if not claims:
            return 1.0, []

        supported = 0
        issues = []

        for claim_type, value in claims:
            if self._verify_claim(claim_type, value):
                supported += 1
            else:
                issues.append(f"Unverified {claim_type}: {value}")

        score = supported / len(claims)
        return score, issues

    """
    Check whether the LLM answer references all structurally-detected
    API files. Returns (recall_score, missing_files) where recall_score
    is 0.0–1.0 representing the fraction of expected files that appear
    to be covered in the answer.
    """
    def evaluate_completeness(self, answer: str) -> tuple[float, list[str]]:
        answer_lower = answer.lower()

        # Collect expected files: controllers and clients
        expected_files = (
            self._detector.controller_files | self._detector.client_files
        )

        if not expected_files:
            return 1.0, []

        covered = 0
        missing = []

        for file_path in sorted(expected_files):
            if self._file_appears_covered(file_path, answer_lower):
                covered += 1
            else:
                missing.append(file_path)

        recall = covered / len(expected_files) if expected_files else 1.0
        return recall, missing

    """
    Heuristic check: does the answer seem to cover a given source file?
    We look for class names, path segments, or annotation values that
    are unique to that file.
    """
    def _file_appears_covered(self, file_path: str, answer_lower: str) -> bool:
        content = self.file_content_map.get(file_path, "")
        if not content:
            return False

        # Extract class/type name from the file
        class_match = re.search(
            r'(?:class|interface|object)\s+(\w+)', content,
        )
        if class_match:
            class_name = class_match.group(1).lower()
            if class_name in answer_lower:
                return True

        # Extract URL path literals from annotations / decorators
        path_literals = re.findall(
            r'(?:@\w+Mapping|@Path|@RequestMapping|@Route)\s*\(\s*'
            r'(?:value\s*=\s*)?["\']([^"\']+)["\']',
            content,
        )
        for path_lit in path_literals:
            # Strip leading slash and path params for matching
            clean = re.sub(r'\{[^}]+\}', '', path_lit).strip('/')
            segments = [s for s in clean.split('/') if len(s) > 2]
            if segments and all(seg.lower() in answer_lower for seg in segments):
                return True

        # Extract URL string constants used in client calls
        url_strings = re.findall(r'["\'](/[^"\']{4,})["\']', content)
        for url_str in url_strings:
            clean = re.sub(r'\{[^}]+\}', '', url_str).strip('/')
            segments = [s for s in clean.split('/') if len(s) > 2]
            if segments and all(seg.lower() in answer_lower for seg in segments):
                return True

        return False

    """Pull out verifiable facts from the LLM response."""
    def _extract_claims(self, answer: str) -> list[tuple[str, str]]:
        claims = []

        # Try to parse as JSON (strip markdown fences if present)
        json_str = answer.strip()
        json_str = re.sub(r"^```(?:json)?\s*", "", json_str)
        json_str = re.sub(r"\s*```$", "", json_str)

        try:
            data = json.loads(json_str)
            claims.extend(self._extract_claims_from_json(data))
        except (ValueError):
            # Not JSON — fall back to extracting URL-like patterns
            claims.extend(self._extract_claims_from_text(answer))

        return claims

    def _extract_claims_from_json(self, data: dict) -> list[tuple[str, str]]:
        claims = []

        # Project name should appear in build files
        if "projectName" in data and data["projectName"]:
            claims.append(("project_name", str(data["projectName"])))

        # Project version should appear in build files
        if "projectVersion" in data and data["projectVersion"]:
            claims.append(("project_version", str(data["projectVersion"])))

        # Each exposed URL should have path segments traceable to source
        for entry in data.get("exposes", []):
            url = entry.get("url", "") if isinstance(entry, dict) else str(entry)
            if url:
                claims.extend(self._claims_from_url(url, "exposed_url"))

        # Each dependency URL should have path segments traceable to source
        for entry in data.get("depends", []):
            url = entry.get("url", "") if isinstance(entry, dict) else str(entry)
            if url:
                claims.extend(self._claims_from_url(url, "dependency_url"))

        return claims

    """
    Break a URL into verifiable segments. We don't expect the full
    reconstructed URL to appear verbatim — we check that the meaningful
    path segments exist somewhere in the source.
    """
    def _claims_from_url(self, url: str, claim_type: str) -> list[tuple[str, str]]:
        claims = []

        # Extract HTTP method if present (e.g., "GET /api/v1/users")
        method_match = re.match(r"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(.+)", url, re.IGNORECASE)
        if method_match:
            url = method_match.group(2)

        # Strip protocol and host
        url = re.sub(r"https?://[^/]+", "", url)
        # Strip <UNKNOWN:...> placeholders — those are expected gaps
        url = re.sub(r"<UNKNOWN:[^>]+>", "", url)

        # Extract meaningful path segments (skip path params like {id})
        segments = [
            seg for seg in url.strip("/").split("/")
            if seg and not re.match(r"^\{.*\}$", seg) and not re.match(r"^:[\w]+$", seg)
        ]

        # Check non-trivial segments (length > 2 to skip things like "v1")
        meaningful = [s for s in segments if len(s) > 2]

        if meaningful:
            # The claim is that these path segments appear in the source
            for seg in meaningful:
                claims.append((claim_type + "_segment", seg))

        return claims

    """Fallback: extract URLs and identifiers from free text."""
    def _extract_claims_from_text(self, text: str) -> list[tuple[str, str]]:
        claims = []

        # URLs
        urls = re.findall(r"(?:https?://\S+|/[\w/\-{}:.]+)", text)
        for url in urls:
            claims.extend(self._claims_from_url(url, "text_url"))

        # Java-style class names (PascalCase with 2+ parts)
        class_names = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text)
        for name in class_names:
            claims.append(("class_name", name))

        return claims

    """Check if a claim is supported by the source corpus."""
    def _verify_claim(self, claim_type: str, value: str) -> bool:
        value_lower = value.lower()

        if claim_type in ("project_name", "project_version"):
            return value_lower in self._full_corpus

        if claim_type.endswith("_segment"):
            # Path segment: check if it appears in source (as annotation value,
            # config property, constant, etc.)
            return value_lower in self._full_corpus

        if claim_type == "class_name":
            return value_lower in self._full_corpus or value in "\n".join(self.file_content_map.values())

        # Default: substring check
        return value_lower in self._full_corpus

    @staticmethod
    def build_citations(docs: list[dict]) -> str:
        refs = []
        seen: set[tuple] = set()

        for doc in docs:
            key = doc['filename']

            if key not in seen:
                seen.add(key)
                if doc['token_count']:
                    refs.append(f"- {key} ({doc['token_count']} tokens)")
                else:
                    refs.append(f"- {key}")

        return "\n".join(sorted(refs))
