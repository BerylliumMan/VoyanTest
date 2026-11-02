"""046：技术栈探测测试。"""

from pathlib import Path

from core.api_extract.detect import detect_tech_stack


def _mk(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


POM_SPRING = """<project>
  <properties><java.version>17</java.version></properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def test_detects_maven_spring_project(tmp_path):
    _mk(tmp_path, "pom.xml", POM_SPRING)
    _mk(tmp_path, "src/main/java/com/demo/App.java", "package com.demo;\npublic class App {}\n")

    stack = detect_tech_stack(str(tmp_path))
    assert stack.spring is True
    assert stack.build_tool == "maven"
    assert "src/main/java" in stack.source_roots
    assert "spring_source" in stack.extractor_names()
    assert stack.evidence  # 有检测依据


def test_detects_spring_from_annotations_without_build_file(tmp_path):
    _mk(
        tmp_path,
        "src/main/java/com/demo/UserController.java",
        "@RestController\n@RequestMapping(\"/users\")\npublic class UserController {}\n",
    )

    stack = detect_tech_stack(str(tmp_path))
    assert stack.spring is True
    assert stack.build_tool is None
    assert any("annotation" in e.lower() or "注解" in e for e in stack.evidence)


def test_detects_gradle_and_openapi(tmp_path):
    _mk(tmp_path, "build.gradle", "plugins { id 'org.springframework.boot' version '3.2.0' }\n")
    _mk(tmp_path, "src/main/resources/openapi.yaml", "openapi: 3.0.1\npaths: {}\n")

    stack = detect_tech_stack(str(tmp_path))
    assert stack.build_tool == "gradle"
    assert stack.spring is True
    assert stack.openapi is True
    assert "src/main/resources/openapi.yaml" in stack.openapi_paths
    assert stack.extractor_names() == ["openapi", "spring_source"]


def test_multi_module_source_roots(tmp_path):
    _mk(tmp_path, "pom.xml", POM_SPRING)
    _mk(tmp_path, "user-service/src/main/java/a/A.java", "class A {}\n")
    _mk(tmp_path, "order-service/src/main/java/b/B.java", "class B {}\n")

    stack = detect_tech_stack(str(tmp_path))
    assert sorted(stack.source_roots) == [
        "order-service/src/main/java",
        "user-service/src/main/java",
    ]


def test_empty_repo_detects_nothing(tmp_path):
    stack = detect_tech_stack(str(tmp_path))
    assert stack.spring is False
    assert stack.openapi is False
    assert stack.build_tool is None
    assert stack.extractor_names() == []
    assert stack.source_roots == []


def test_ignores_target_and_node_modules(tmp_path):
    _mk(
        tmp_path,
        "target/classes/Controller.java",
        "@RestController\npublic class Controller {}\n",
    )
    _mk(
        tmp_path,
        "node_modules/pkg/Controller.java",
        "@RestController\npublic class Controller {}\n",
    )
    stack = detect_tech_stack(str(tmp_path))
    assert stack.spring is False


def test_missing_repo_dir_is_safe(tmp_path):
    stack = detect_tech_stack(str(tmp_path / "not-exists"))
    assert stack.spring is False
    assert stack.extractor_names() == []


def test_registry_selects_by_preference(tmp_path):
    import pytest

    from core.api_extract.base import ExtractorError
    from core.api_extract.registry import get_extractor, resolve_extractors

    _mk(tmp_path, "pom.xml", POM_SPRING)
    _mk(tmp_path, "src/main/resources/openapi.yaml", "openapi: 3.0.1\n")
    stack = detect_tech_stack(str(tmp_path))

    assert [e.name for e in resolve_extractors("auto", stack)] == ["openapi", "spring_source"]
    assert [e.name for e in resolve_extractors(None, stack)] == ["openapi", "spring_source"]
    assert [e.name for e in resolve_extractors("spring_source", stack)] == ["spring_source"]

    with pytest.raises(ExtractorError):
        get_extractor("bogus")
