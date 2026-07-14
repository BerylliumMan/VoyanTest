import fitz
from PIL import Image
import base64
import io


def extract_text_from_pdf(file) -> str:
    file.seek(0)
    data = file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    texts = []
    for page in doc:
        texts.append(page.get_text("text"))
    doc.close()
    return "\n".join(texts)


def is_pdf_dual_layer(file) -> bool:
    file.seek(0)
    data = file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    has_text = False
    for page in doc:
        if page.get_text("text").strip():
            has_text = True
            break
    doc.close()
    return has_text


def render_pdf_pages_to_images(file) -> list[tuple[str, str]]:
    file.seek(0)
    data = file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        images.append(("png", b64))
    doc.close()
    return images


def validate_pdf(file) -> tuple[bool, str | None]:
    try:
        file.seek(0)
        data = file.read()
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.is_encrypted:
            doc.close()
            return False, "无法打开加密PDF文档"
        page_count = len(doc)
        doc.close()
        if page_count == 0:
            return False, "PDF文件中无有效内容"
        return True, None
    except fitz.FileDataError:
        return False, "PDF文件损坏"
    except Exception as e:  # noqa: BLE001 - 兜底所有 PyMuPDF / I/O 错误，对外统一返回解析失败
        return False, f"PDF文件解析失败: {e}"
