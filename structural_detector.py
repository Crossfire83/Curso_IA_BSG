import re


# ── Patterns that identify API-relevant files by content ───────
# Each entry: (label, compiled_regex)
_CONTENT_PATTERNS = [
    # Java/Spring controllers
    ("controller", re.compile(
        r'@(?:RestController|Controller|RequestMapping|GetMapping|PostMapping'
        r'|PutMapping|DeleteMapping|PatchMapping)',
    )),
    # JAX-RS endpoints
    ("controller", re.compile(
        r'@(?:Path|GET|POST|PUT|DELETE|PATCH)\b',
    )),
    # Spring Feign / RestTemplate / WebClient clients
    ("client", re.compile(
        r'@FeignClient|RestTemplate|WebClient\.builder|\.exchange\(|\.retrieve\(',
    )),
    # Generic HTTP client calls (OkHttp, Apache, java.net)
    ("client", re.compile(
        r'HttpClient|OkHttpClient|CloseableHttpClient|HttpURLConnection',
    )),
    # Express / Koa / Fastify routers
    ("controller", re.compile(
        r'(?:router|app)\.\s*(?:get|post|put|delete|patch|use)\s*\(',
    )),
    # FastAPI / Flask / Django
    ("controller", re.compile(
        r'@(?:app|router)\.(?:get|post|put|delete|patch|route)\s*\(',
    )),
    # Node/JS/TS HTTP clients (axios, fetch, got, node-fetch)
    ("client", re.compile(
        r'axios\.\w+\(|fetch\s*\(|got\.\w+\(|request\.\w+\(',
    )),
    # Python HTTP clients (requests, httpx, aiohttp)
    ("client", re.compile(
        r'requests\.(?:get|post|put|delete|patch)\s*\('
        r'|httpx\.(?:get|post|put|delete|patch|AsyncClient|Client)\s*\('
        r'|aiohttp\.ClientSession',
    )),
    # Go HTTP handlers
    ("controller", re.compile(
        r'http\.Handle(?:Func)?\s*\(|mux\.Handle',
    )),
    # Go HTTP clients
    ("client", re.compile(
        r'http\.(?:Get|Post|NewRequest)\s*\(',
    )),
    # URL constant definition files (any language)
    ("constants", re.compile(
        r'(?:static\s+final\s+String|export\s+const|const\s+val)\s+'
        r'[A-Z][A-Z0-9_]*(?:URL|URI|PATH|ENDPOINT|ROUTE|BASE|HOST|CONTROLLER)',
    )),
    # Java enums with URL-like string values
    ("constants", re.compile(
        r'enum\s+\w+.*\{[^}]*["\']/',
        re.DOTALL,
    )),
    # Enum constructors or fields with URL path strings
    ("constants", re.compile(
        r'\(\s*["\']/.+["\']',
    )),
    # Files with multiple string literals that look like URL paths
    ("constants", re.compile(
        r'["\']/(?:api|v\d+|service|internal)/',
    )),
]

# ── Patterns that identify API-relevant files by path ──────────
_PATH_PATTERNS = [
    ("controller", re.compile(r'controller', re.IGNORECASE)),
    ("controller", re.compile(r'routes?\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    ("client", re.compile(r'.*client\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    ("client", re.compile(r'.*services?.*(?:client|proxy|proxies|api)?s?.*\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    ("constants", re.compile(r'.*constants?.*\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    ("constants", re.compile(r'.*urls?.*\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    ("constants", re.compile(r'endpoints?\.(?:java|kt|ts|js|py|go)$', re.IGNORECASE)),
    # Dictionary, enum, and registry files that define URLs
    ("constants", re.compile(r'(?:url|uri|endpoint|route|path).*(?:dict|enum|registry|map|config)', re.IGNORECASE)),
    ("constants", re.compile(r'(?:dict|enum|registry|map).*(?:url|uri|endpoint|route|path)', re.IGNORECASE)),
]


"""
Scans the full file corpus to find API-relevant files using
structural patterns (annotations, naming conventions, HTTP markers)
rather than relying on vector similarity.
"""
class StructuralDetector:

    def __init__(self, file_content_map: dict[str, str]):
        self.file_content_map = file_content_map
        self._detected: dict[str, set[str]] = {
            "controller": set(),
            "client": set(),
            "constants": set(),
        }
        self._scan()

    def _scan(self) -> None:
        for file_path, content in self.file_content_map.items():
            # Check path-based patterns
            for label, pattern in _PATH_PATTERNS:
                if pattern.search(file_path):
                    self._detected[label].add(file_path)

            # Check content-based patterns
            for label, pattern in _CONTENT_PATTERNS:
                if pattern.search(content):
                    self._detected[label].add(file_path)

    @property
    def controller_files(self) -> set[str]:
        return self._detected["controller"]

    @property
    def client_files(self) -> set[str]:
        return self._detected["client"]

    @property
    def constant_files(self) -> set[str]:
        return self._detected["constants"]

    @property
    def all_api_files(self) -> set[str]:
        return self.controller_files | self.client_files | self.constant_files

    """
    Given the set of files already included in context, return the
    API-relevant files that are missing.
    """
    def find_missing(self, already_included: set[str]) -> dict[str, list[str]]:
        missing = {}
        for label in ("controller", "client", "constants"):
            diff = sorted(self._detected[label] - already_included)
            if diff:
                missing[label] = diff
        return missing

    """
    Build supplementary context sections for structurally-detected
    files that are not yet in context. Returns individual sections
    as a list so the caller can batch them.
    """
    def build_supplement(
        self, already_included: set[str],
    ) -> tuple[list[str], set[str]]:
        missing = self.find_missing(already_included)

        if not missing:
            return [], set()

        sections = []
        injected: set[str] = set()

        for label, file_paths in missing.items():
            tag = label.upper()
            for file_path in file_paths:
                if file_path in injected:
                    continue
                content = self.file_content_map.get(file_path, "")
                if not content:
                    continue
                injected.add(file_path)
                sections.append(
                    f"{'=' * 60}\n"
                    f"STRUCTURAL-{tag} FILE: {file_path}\n"
                    f"{'=' * 60}\n"
                    f"{content}"
                )

        return sections, injected
