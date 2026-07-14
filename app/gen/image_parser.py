from PIL import Image, UnidentifiedImageError
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
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        # UnidentifiedImageError: 无法识别格式；OSError: 文件 I/O 损坏；
        # SyntaxError: 图像结构损坏；ValueError: 解码参数错误
        return False
