import re
import yaml
import json

# ── PRIMARY patterns: HTTP-specific. A file must match at least one
#    of these to be considered API-relevant at all. ──────────────
_PRIMARY_PATTERNS = [
    # Java/Spring annotations
    re.compile(r'@(?:RestController|Controller|RequestMapping|GetMapping|PostMapping'
               r'|PutMapping|DeleteMapping|PatchMapping|FeignClient)'),
    # JAX-RS
    re.compile(r'@(?:Path|GET|POST|PUT|DELETE|PATCH|Produces|Consumes)\b'),
    # Express / Koa / Fastify
    re.compile(r'(?:router|app)\.\s*(?:get|post|put|delete|patch|use|all)\s*\('),
    # FastAPI / Flask / Django
    re.compile(r'@(?:app|router)\.(?:get|post|put|delete|patch|route)\s*\('),
    # Go net/http
    re.compile(r'http\.Handle(?:Func)?\s*\(|mux\.(?:Handle|Route)'),
    # Next.js / file-based routing exports
    re.compile(r'export\s+(?:async\s+)?function\s+(?:GET|POST|PUT|DELETE|PATCH)\b'),

    # HTTP clients
    re.compile(r'RestTemplate|WebClient|\.exchange\(|\.retrieve\('),
    re.compile(r'HttpClient|OkHttpClient|CloseableHttpClient|HttpURLConnection'),
    re.compile(r'axios\.\w+\(|fetch\s*\(|got\.\w+\('),
    re.compile(r'requests\.(?:get|post|put|delete|patch)\s*\('),
    re.compile(r'httpx\.(?:get|post|put|delete|patch|AsyncClient|Client)'),
    re.compile(r'http\.(?:Get|Post|NewRequest)\s*\('),

    # URL / path constants
    re.compile(r'(?:static\s+final\s+String|export\s+const|const\s+val)\s+'
               r'[A-Z][A-Z0-9_]*\s*=\s*["\']'),
    re.compile(r'@Value\s*\('),
    re.compile(r'\$\{[^}]+\}'),

    # URL-like string literals
    re.compile(r'["\'](?:https?://|/api/|/v\d+/)'),

    # Gateway / proxy routing flags
    re.compile(r'(?:apigee|apiproc|amazonaws|dishcloud|gateway|proxy)\w*', re.IGNORECASE),
    # Environment / deployment identifiers
    # re.compile(r'(?:prod|test|int|dev|env)\w*', re.IGNORECASE),
    # Feature flags / toggles that control URL routing
    re.compile(r'(?:is\w*Enabled|use\w*Proxy|use\w*Gateway)', re.IGNORECASE),
]

# ── CONTEXTUAL patterns: only included if the file already has
#    primary matches. These add structural context. ─────────────
_CONTEXTUAL_PATTERNS = [
    # Class / interface declarations
    re.compile(r'^\s*(?:public\s+|private\s+|protected\s+)?'
               r'(?:abstract\s+)?(?:class|interface|object)\s+\w+', re.MULTILINE),
    # Import statements for HTTP-related types
    re.compile(r'import\s+.*(?:Controller|Mapping|Client|RestTemplate|WebClient'
               r'|FeignClient|RequestMapping|Path)\b'),
    # Package / module declaration
    re.compile(r'^package\s+', re.MULTILINE),
]

# Config files: extract only lines with URL-like values or relevant keys.
_CONFIG_RELEVANT_PATTERNS = [
    re.compile(r'https?://'),
    re.compile(r'(?:url|uri|host|base-url|base_url|endpoint|path|port|context-path'
               r'|server\.servlet|server\.port)', re.IGNORECASE),
    re.compile(r'(?:apigee|apiproc|gateway|proxy|routing|feign|ribbon)', re.IGNORECASE),
    re.compile(r'(?:apigee|apiproc|amazonaws|dishcloud|gateway|proxy)\w*', re.IGNORECASE),
    re.compile(r'(?:spring\.application\.name|server\.servlet\.context-path)', re.IGNORECASE),
]

_CONFIG_EXTENSIONS = {'.yml', '.yaml', '.properties', '.xml', '.json', '.toml'}

_OPENAPI_PATH_PATTERN = re.compile(
    r'(?:openapi|swagger|api-docs|api-spec|contract)'
    r'.*\.(?:ya?ml|json)$',
    re.IGNORECASE,
)

"""
Extracts only API-relevant lines from source files, drastically
reducing context size while preserving the information the LLM
needs to reconstruct URLs.
"""
class CodeCompressor:

    def __init__(self, context_lines: int = 2):
        self.context_lines = context_lines

    """
    Compress a single file's content to only its API-relevant lines.
    Returns the compressed content, or empty string if nothing relevant.
    """
    def compress_file(self, file_path: str, content: str) -> str:
        if _OPENAPI_PATH_PATTERN.search(file_path):
            return self.compress_openapi(content, file_path)
        ext = _get_extension(file_path)
        if ext in _CONFIG_EXTENSIONS:
            return self._compress_config(content)
        return self._compress_source(file_path, content)

    def _compress_source(self, file_path: str, content: str) -> str:
        lines = content.splitlines()

        # First pass: find lines matching primary (HTTP-specific) patterns
        primary_indices: set[int] = set()
        for i, line in enumerate(lines):
            for pattern in _PRIMARY_PATTERNS:
                if pattern.search(line):
                    start = max(0, i - self.context_lines)
                    end = min(len(lines), i + self.context_lines + 1)
                    primary_indices.update(range(start, end))
                    break

        # If no primary matches, this file has no API relevance
        if not primary_indices:
            return ""

        # Second pass: add contextual lines (class decls, imports, package)
        all_indices = set(primary_indices)
        for i, line in enumerate(lines):
            for pattern in _CONTEXTUAL_PATTERNS:
                if pattern.search(line):
                    start = max(0, i - self.context_lines)
                    end = min(len(lines), i + self.context_lines + 1)
                    all_indices.update(range(start, end))
                    break

        # whitespaces and identation matter in python.
        if file_path.endswith(".py"):
            return self._build_compressed(lines, sorted(all_indices))
        return self._normalize_whitespace(self._build_compressed(lines, sorted(all_indices)))

    def _compress_config(self, content: str) -> str:
        lines = content.splitlines()
        relevant_indices: set[int] = set()

        for i, line in enumerate(lines):
            for pattern in _CONFIG_RELEVANT_PATTERNS:
                if pattern.search(line):
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    relevant_indices.update(range(start, end))
                    break

        if not relevant_indices:
            return ""

        return self._build_compressed(lines, sorted(relevant_indices))

    """
    Build the compressed output from selected line indices.
    Inserts '...' markers where lines were skipped.
    """
    @staticmethod
    def _build_compressed(lines: list[str], indices: list[int]) -> str:
        result: list[str] = []
        prev_idx = -2

        for idx in indices:
            if idx > prev_idx + 1:
                result.append("  ...")
            result.append(lines[idx])
            prev_idx = idx

        if indices[-1] < len(lines) - 1:
            result.append("  ...")

        return "\n".join(result)

    """
    Normalize whitespace in the final compressed output.
    This helps reduce token usage without losing meaning.

    Should be used in non-python and non-config files.
    """
    def _normalize_whitespace(self, content: str) -> str:
        # Collapse 3+ blank lines into 1
        content = re.sub(r'\n{3,}', '\n\n', content)
        # Trim trailing whitespace per line
        content = re.sub(r'[ \t]+$', '', content, flags=re.MULTILINE)
        # Cap indentation at 2 spaces per level (4→2, 8→4, etc.)
        # Skip this for YAML/Python files
        return content



    def compress_openapi(self, content: str, file_path: str) -> str:
        """Extract only endpoint paths and HTTP methods from an OpenAPI spec."""
        try:
            if file_path.endswith(".json"):
                spec = json.loads(content)
            else:
                spec = yaml.safe_load(content)
        except Exception:
            return content  # fallback to raw if unparseable

        if not isinstance(spec, dict) or "paths" not in spec:
            return content

        lines = []

        # Grab base path info
        for key in ("basePath", "host"):  # Swagger 2.0
            if key in spec:
                lines.append(f"{key}: {spec[key]}")

        # OpenAPI 3.x servers
        for server in spec.get("servers", []):
            lines.append(f"server: {server.get('url', '')}")

        # Extract just path + methods
        for path, methods in spec["paths"].items():
            if not isinstance(methods, dict):
                continue
            http_methods = [
                m.upper() for m in methods
                if m.lower() in ("get", "post", "put", "delete", "patch", "head", "options")
            ]
            for method in http_methods:
                lines.append(f"{method} {path}")

        return "\n".join(lines)


    """
    Compress all files in a file_content_map. Returns a dict of
    file_path → compressed_content (only files with relevant content).
    """
    def compress_all(
        self, file_content_map: dict[str, str],
    ) -> dict[str, str]:
        compressed = {}
        for file_path, content in file_content_map.items():
            result = self.compress_file(file_path, content)
            if result:
                compressed[file_path] = result
        return compressed


def _get_extension(path: str) -> str:
    dot = path.rfind('.')
    return path[dot:].lower() if dot != -1 else ""
