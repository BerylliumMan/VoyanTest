"""037 T005：视觉帧压缩模块（core/vision_frame.py）。

覆盖：文件→data URL（JPEG 压缩/缩放上限/缺文件）、b64→data URL（裸值/data 前缀/无效输入）、
PIL 兜底路径不抛错。
"""
import base64
import io

from PIL import Image

from core.vision_frame import b64_to_frame_data_url, frame_to_data_url


def _png_bytes(w=200, h=100, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_frame_to_data_url_compresses_to_jpeg(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_png_bytes())
    url = frame_to_data_url(str(p))
    assert url and url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"  # JPEG 头


def test_frame_to_data_url_respects_max_width(tmp_path):
    p = tmp_path / "wide.png"
    p.write_bytes(_png_bytes(w=2000, h=400))
    url = frame_to_data_url(str(p), max_width=400)
    raw = base64.b64decode(url.split(",", 1)[1])
    im = Image.open(io.BytesIO(raw))
    assert im.width == 400 and im.height == 80


def test_frame_to_data_url_missing_file():
    assert frame_to_data_url("/nonexistent/x.png") is None


def test_b64_to_frame_data_url_bare_and_prefixed():
    b64 = base64.b64encode(_png_bytes()).decode()
    url1 = b64_to_frame_data_url(b64)
    url2 = b64_to_frame_data_url("data:image/png;base64," + b64)
    assert url1 and url1.startswith("data:image/jpeg;base64,")
    assert url1 == url2


def test_b64_to_frame_data_url_invalid_inputs():
    assert b64_to_frame_data_url("") is None
    assert b64_to_frame_data_url("!!!not-base64!!!") is None


def test_b64_to_frame_data_url_scales_small_untouched():
    b64 = base64.b64encode(_png_bytes(w=300, h=150)).decode()
    url = b64_to_frame_data_url(b64, max_width=1152)
    raw = base64.b64decode(url.split(",", 1)[1])
    im = Image.open(io.BytesIO(raw))
    assert im.width == 300  # 小于上限不缩放
