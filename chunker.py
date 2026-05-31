from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

"""
Chunks documents with a whole-file-first strategy.

Small files are kept as a single chunk to preserve relationships
(e.g., class-level annotations with method-level annotations).
Large files are split with generous overlap.
"""
class DocumentChunker:

    def __init__(
        self,
        whole_file_threshold: int = 8000,
        large_chunk_size: int = 6000,
        large_chunk_overlap: int = 1000,
    ):
        self.whole_file_threshold = whole_file_threshold
        self.large_splitter = RecursiveCharacterTextSplitter(
            chunk_size=large_chunk_size,
            chunk_overlap=large_chunk_overlap,
            separators=["\n\n\n", "\n\n", "\n", " "],
        )

    def chunk(self, raw_docs: list[Document]) -> list[Document]:
        chunks = []

        for doc in raw_docs:
            header = f"### FILE: {doc.metadata['file']}\n"
            content = doc.page_content

            if len(content) <= self.whole_file_threshold:
                chunks.append(Document(
                    page_content=header + content,
                    metadata=doc.metadata,
                ))
            else:
                sub_chunks = self.large_splitter.split_text(content)
                for i, sub in enumerate(sub_chunks):
                    chunks.append(Document(
                        page_content=f"{header}(part {i + 1}/{len(sub_chunks)})\n{sub}",
                        metadata={
                            **doc.metadata,
                            "chunk_index": i,
                            "total_chunks": len(sub_chunks),
                        },
                    ))

        return chunks
