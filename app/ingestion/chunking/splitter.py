from typing import List
import logfire


def _split_oversized_paragraph(paragraph: str, chunk_size: int) -> List[str]:
    """Split a single long paragraph without silently exceeding the chunk limit."""
    if len(paragraph) <= chunk_size:
        return [paragraph]

    chunks: List[str] = []
    remaining = paragraph
    while len(remaining) > chunk_size:
        split_at = remaining.rfind(" ", 0, chunk_size + 1)
        if split_at <= 0:
            split_at = chunk_size
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def chunk_text(text: str, chunk_size: int = 1500) -> List[str]:
    """
    Simple semantic-ish chunker that splits by paragraphs.
    Ensures chunks do not exceed the specified size.
    """
    with logfire.span("✂️ Text Chunking", text_length=len(text)):
        if not text.strip(): 
            return []
            
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""
        
        for paragraph in paragraphs:
            for p in _split_oversized_paragraph(paragraph, chunk_size):
                separator = "\n\n" if current_chunk else ""
                if len(current_chunk) + len(separator) + len(p) <= chunk_size:
                    current_chunk += separator + p
                else:
                    if current_chunk.strip():
                        chunks.append(current_chunk.strip())
                    current_chunk = p
        
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
            
        valid_chunks = [c for c in chunks if c.strip()]
        logfire.info(f"✅ Generated {len(valid_chunks)} chunks")
        return valid_chunks
