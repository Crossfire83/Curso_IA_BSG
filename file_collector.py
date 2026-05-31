import os
from pathlib import Path
from fnmatch import fnmatch
from langchain_core.documents import Document

CONFIG_EXTENSIONS = {
    ".yml", ".yaml", ".properties", ".xml", ".json", ".toml", ".gradle"
}


"""Walks a directory tree and collects files as LangChain Documents."""
class FileCollector:

    def __init__(self, root_dir: str, exclude_patterns: list[str] | None = None):
        self.root = Path(root_dir).resolve()
        self.exclude_patterns = exclude_patterns or []

    def _matches_any(self, path: str) -> bool:
        return any(
            fnmatch(path, p)
            or fnmatch(os.path.basename(path), p)
            or fnmatch(path, f"*/{p}")       # match nested directories
            or fnmatch(path, f"**/{p}")
            for p in self.exclude_patterns
        )

    @staticmethod
    def is_config_file(rel_path: str) -> bool:
        return Path(rel_path).suffix.lower() in CONFIG_EXTENSIONS

    def collect(self) -> list[Document]:
        documents = []

        for dirpath, _, filenames in os.walk(self.root):
            for filename in sorted(filenames):
                full_path = Path(dirpath) / filename
                rel_path = str(full_path.relative_to(self.root))

                if self._matches_any(rel_path):
                    continue

                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    print(f"Skipping {rel_path}: {e}")
                    continue

                documents.append(Document(
                    page_content=content,
                    metadata={
                        "file": rel_path,
                        "is_config": self.is_config_file(rel_path),
                    },
                ))

        return documents
