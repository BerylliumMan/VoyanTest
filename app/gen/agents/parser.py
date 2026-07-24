"""Document Parser agent — extracts text from uploaded documents."""
from app.gen.agents.base import BaseAgent
from app.gen.multi_file import extract_multi_file_content


class Parser(BaseAgent[dict, list[str]]):
    name = "document_parser"

    async def run(self, input_data: dict) -> list[str]:
        files = input_data.get("files", [])
        text = input_data.get("text", "")
        chunks = []
        if text:
            chunks.append(text)
        if files:
            for f in files:
                try:
                    content = await extract_multi_file_content(f)
                    if content:
                        chunks.append(content)
                except Exception:
                    chunks.append(f"")
        return chunks
