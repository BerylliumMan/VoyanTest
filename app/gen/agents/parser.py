"""Document Parser agent — extracts text from uploaded documents."""
from app.gen.agents.base import BaseAgent


class Parser(BaseAgent[dict, list[str]]):
    name = "document_parser"

    async def run(self, input_data: dict) -> list[str]:
        """Return pre-extracted text chunks; file parsing is done upstream."""
        text = input_data.get("text", "")
        chunks: list[str] = []
        if text:
            chunks.append(text)
        return chunks
