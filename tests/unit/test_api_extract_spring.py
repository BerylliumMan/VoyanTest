"""046：Spring 源码提取器测试。"""

from pathlib import Path

from core.api_extract.base import ExtractContext
from core.api_extract.spring_source import SpringSourceExtractor

SAMPLE = """
package com.demo;

@RestController
@RequestMapping("/api/users")
public class UserController {
    private static final String ORDER_BASE = "/api/orders";

    @GetMapping
    public List<User> list(@RequestParam Integer page) { return null; }

    @GetMapping("/{id}")
    public User get(@PathVariable("id") Long id) { return null; }

    @PostMapping(value = "/search", produces = "application/json")
    public List<User> search(@RequestParam("keyword") String kw) { return null; }

    @RequestMapping(value = "/legacy", method = RequestMethod.PUT)
    public void legacy() {}
}
"""

UNRESOLVED = """
@RestController
@RequestMapping(Paths.API_BASE)
public class BadController {
    @GetMapping("/x")
    public void x() {}
}
"""

NOT_CONTROLLER = """
@Service
public class UserService {
    @GetMapping("/nope")
    public void nope() {}
}
"""


def _write(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")


def test_extracts_class_and_method_paths(tmp_path):
    _write(tmp_path, "UserController.java", SAMPLE)
    apis = SpringSourceExtractor().extract(ExtractContext(repo_dir=str(tmp_path)))
    got = {(a.method, a.path) for a in apis}
    assert ("GET", "/api/users") in got
    assert ("GET", "/api/users/{id}") in got
    assert ("POST", "/api/users/search") in got
    assert ("PUT", "/api/users/legacy") in got
    assert all(a.extractor == "spring_source" for a in apis)


def test_records_source_location(tmp_path):
    _write(tmp_path, "UserController.java", SAMPLE)
    apis = SpringSourceExtractor().extract(ExtractContext(repo_dir=str(tmp_path)))
    get_user = next(a for a in apis if a.path == "/api/users/{id}")
    assert get_user.source_file == "UserController.java"
    assert get_user.source_line and get_user.source_line > 0
    assert get_user.source_symbol and "get" in get_user.source_symbol


def test_unresolvable_path_marked_low_confidence(tmp_path):
    _write(tmp_path, "BadController.java", UNRESOLVED)
    apis = SpringSourceExtractor().extract(ExtractContext(repo_dir=str(tmp_path)))
    assert apis and all(a.confidence == "low" for a in apis)
    assert all(a.confidence_reason for a in apis)


def test_non_controller_class_ignored(tmp_path):
    _write(tmp_path, "UserService.java", NOT_CONTROLLER)
    apis = SpringSourceExtractor().extract(ExtractContext(repo_dir=str(tmp_path)))
    assert apis == []


def test_extracts_request_params(tmp_path):
    _write(tmp_path, "UserController.java", SAMPLE)
    apis = SpringSourceExtractor().extract(ExtractContext(repo_dir=str(tmp_path)))
    get_user = next(a for a in apis if a.path == "/api/users/{id}")
    names = {p.get("name") for p in (get_user.request_schema.get("params") or [])}
    assert "id" in names
