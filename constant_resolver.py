import re
from collections import defaultdict


# Patterns that capture constant definitions across languages.
# Each pattern should have two named groups: "name" and "value".
_DEFINITION_PATTERNS = [
    # Java: static final String FOO = "/api/v1/bar";
    re.compile(
        r'(?:static\s+)?(?:final\s+)?(?:String|string)\s+'
        r'(?P<name>[A-Z][A-Z0-9_]+)\s*=\s*"(?P<value>[^"]+)"',
    ),
    # Java: @Value("${some.property}")  — captures the property key
    re.compile(
        r'@Value\s*\(\s*"\$\{(?P<name>[^}]+)\}"\s*\)',
    ),
    # Java enum values with string constructor args: FOO_URL("/api/v1/bar")
    re.compile(
        r'(?P<name>[A-Z][A-Z0-9_]+)\s*\(\s*"(?P<value>[^"]+)"',
    ),
    # TypeScript / JavaScript: export const FOO = "/api/v1/bar";
    re.compile(
        r'(?:export\s+)?const\s+(?P<name>[A-Z][A-Z0-9_]+)\s*=\s*["\'](?P<value>[^"\']+)["\']',
    ),
    # Python: FOO = "/api/v1/bar"
    re.compile(
        r'^(?P<name>[A-Z][A-Z0-9_]{2,})\s*=\s*["\'](?P<value>[^"\']+)["\']',
        re.MULTILINE,
    ),
    # Kotlin: const val FOO = "/api/v1/bar"
    re.compile(
        r'const\s+val\s+(?P<name>[A-Z][A-Z0-9_]+)\s*=\s*"(?P<value>[^"]+)"',
    ),
    # Go: const Foo = "/api/v1/bar"  (exported constants start uppercase)
    re.compile(
        r'const\s+(?P<name>[A-Z]\w+)\s*=\s*"(?P<value>[^"]+)"',
    ),
]

# Patterns that detect *usage* of a constant name inside code that is
# likely URL-related (annotations, string concatenation, assignments).
_USAGE_PATTERN = re.compile(r'\b([A-Z][A-Z0-9_]{2,})\b')

# Heuristic: only chase constants whose names smell like URLs / paths.
_URL_HINT_KEYWORDS = {
    "URL", "URI", "PATH", "ENDPOINT", "ROUTE", "BASE", "HOST",
    "CONTROLLER", "API", "SERVICE", "PREFIX", "CONTEXT",
}


"""
Scans the full file corpus to build a constant → (file, value) index,
then resolves constants referenced in retrieved context.
"""
class ConstantResolver:

    def __init__(self, file_content_map: dict[str, str]):
        self.file_content_map = file_content_map
        # constant_name → list of (file_path, value | None)
        self._index: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
        self._build_index()

    def _build_index(self) -> None:
        for file_path, content in self.file_content_map.items():
            for pattern in _DEFINITION_PATTERNS:
                for match in pattern.finditer(content):
                    name = match.group("name")
                    value = match.group("value") if "value" in match.groupdict() else None
                    self._index[name].append((file_path, value))

    @staticmethod
    def _looks_like_url_constant(name: str) -> bool:
        parts = name.split("_")
        return any(part in _URL_HINT_KEYWORDS for part in parts)

    """
    Given the already-retrieved context string, find constant names
    that appear as references but whose *definitions* are not present.

    Returns a list of (file_path, constant_name, value) for files
    that should be added to the context.
    """
    def find_missing_definitions(
        self, context: str, already_included_files: set[str],
    ) -> list[tuple[str, str, str | None]]:
        referenced_names = set(_USAGE_PATTERN.findall(context))

        missing: list[tuple[str, str, str | None]] = []
        seen_files: set[str] = set()

        for name in sorted(referenced_names):
            if not self._looks_like_url_constant(name):
                continue
            if name not in self._index:
                continue

            # Check whether the definition is already visible in context
            definitions = self._index[name]
            all_present = all(fp in already_included_files for fp, _ in definitions)
            if all_present:
                continue

            for file_path, value in definitions:
                if file_path not in already_included_files and file_path not in seen_files:
                    seen_files.add(file_path)
                    missing.append((file_path, name, value))

        return missing

    """
    Build supplementary context sections with the full content of
    files that contain missing constant definitions. Returns
    individual sections as a list so the caller can batch them.
    """
    def build_supplement(
        self, context: str, already_included_files: set[str],
    ) -> tuple[list[str], list[tuple[str, str, str | None]]]:
        missing = self.find_missing_definitions(context, already_included_files)

        if not missing:
            return [], []

        supplement_files: dict[str, str] = {}
        for file_path, _name, _value in missing:
            if file_path not in supplement_files:
                content = self.file_content_map.get(file_path, "")
                supplement_files[file_path] = content

        sections = []
        for file_path, content in supplement_files.items():
            sections.append(
                f"{'=' * 60}\n"
                f"CONSTANT-DEFINITION FILE: {file_path}\n"
                f"{'=' * 60}\n"
                f"{content}"
            )

        return sections, missing
