"""上传路径 ASCII 安全化单测（MCP 中文文件名静默失败兼容层）。"""
import os

from core.upload_paths import cleanup_upload_temps, prepare_upload_args


def test_ascii_path_passthrough():
    args = {"paths": ["/tmp/req_verify_ui.md"]}
    new_args, temps = prepare_upload_args(args)
    assert new_args is args and temps == []


def test_no_paths_passthrough():
    assert prepare_upload_args({"element": "x"})[1] == []
    assert prepare_upload_args({"paths": []})[1] == []


def test_cjk_path_copied_to_ascii(tmp_path):
    src = tmp_path / "需求文档.md"
    src.write_text("hello", encoding="utf-8")
    args = {"paths": [str(src)]}
    new_args, temps = prepare_upload_args(args)
    assert len(temps) == 1
    assert new_args["paths"][0].isascii()
    assert new_args["paths"][0] != str(src)
    with open(new_args["paths"][0], encoding="utf-8") as f:
        assert f.read() == "hello"
    # 原文件保留
    assert src.exists()
    # 扩展名保留
    assert new_args["paths"][0].endswith(".md")
    cleanup_upload_temps(temps)
    assert not os.path.exists(temps[0])


def test_cjk_missing_file_passthrough(tmp_path):
    args = {"paths": [str(tmp_path / "不存在.pdf")]}
    new_args, temps = prepare_upload_args(args)
    assert temps == [] and new_args["paths"][0].endswith("不存在.pdf")


def test_mixed_paths_only_cjk_copied(tmp_path):
    cjk = tmp_path / "中文.txt"
    cjk.write_text("a", encoding="utf-8")
    ascii_p = tmp_path / "ascii.txt"
    ascii_p.write_text("b", encoding="utf-8")
    args = {"paths": [str(ascii_p), str(cjk)]}
    new_args, temps = prepare_upload_args(args)
    assert len(temps) == 1
    assert new_args["paths"][0] == str(ascii_p)
    assert new_args["paths"][1].isascii()
    cleanup_upload_temps(temps)


def test_drop_args_shape_supported(tmp_path):
    cjk = tmp_path / "拖拽文件.txt"
    cjk.write_text("x", encoding="utf-8")
    args = {"element": "drop zone", "target": "e5", "paths": [str(cjk)]}
    new_args, temps = prepare_upload_args(args)
    assert len(temps) == 1 and new_args["target"] == "e5"
    cleanup_upload_temps(temps)
