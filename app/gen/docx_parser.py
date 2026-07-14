from docx import Document


def extract_text(file) -> str:
    doc = Document(file)
    paragraphs = [p.text for p in doc.paragraphs]
    return "\n".join(paragraphs)
