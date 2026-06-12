from PIL import Image
import base64
import io


def encode_image(file) -> str:
    img = Image.open(file)
    buf = io.BytesIO()
    img.save(buf, format=img.format or "PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def validate_image(file) -> bool:
    try:
        img = Image.open(file)
        img.verify()
        return True
    except Exception:
        return False
