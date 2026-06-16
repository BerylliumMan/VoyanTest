#!/usr/bin/env python3
"""
VoyanTest CLI — 命令行测试执行工具，用于 CI/CD 流水线。

用法:
  voyan run --project-id 1 [--env-id 2] [--case-ids 1,2,3] [--output report.json] [--headless]
  voyan run-single --case-id 5 [--env-id 2] [--output report.json] [--headless]
  voyan list-projects
  voyan list-cases --project-id 1

退出码:
  0  全部通过
  1  存在失败或错误
  2  项目/用例未找到
  3  数据库连接失败
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import os
import sys
from typing import TYPE_CHECKING

# ── 路径初始化：确保项目根目录在 sys.path ──────────────────────────
_project_root = os.path.abspath(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _get_db() -> Session:
    """创建数据库会话；连接失败时直接退出（exit 3）。"""
    try:
        from app.database import SessionLocal

        return SessionLocal()
    except Exception as exc:
        print(f"错误: 数据库连接失败: {exc}", file=sys.stderr)
        sys.exit(3)


# ────────────────────────────────────────────────────────────────────
# 查询类命令
# ────────────────────────────────────────────────────────────────────


def list_projects() -> None:
    """列出所有项目。"""
    db = _get_db()
    try:
        from app import crud

        projects = crud.get_all_projects(db)
        if not projects:
            print("(没有找到任何项目)")
            return

        print(f"{'ID':<6} {'名称':<30} {'基础URL':<45} {'浏览器':<12} {'创建时间'}")
        print("-" * 120)
        for p in projects:
            base = p.base_url or "-"
            browser = getattr(p, "browser", None) or "-"
            created = getattr(p, "created_at", None)
            created_str = str(created)[:19] if created else "-"
            print(f"{p.id:<6} {p.name:<30} {base:<45} {browser:<12} {created_str}")
    finally:
        db.close()


def list_cases(project_id: int) -> None:
    """列出某项目下所有测试用例。"""
    db = _get_db()
    try:
        from app import crud

        project = crud.get_project(db, project_id)
        if not project:
            print(f"错误: 项目 ID {project_id} 未找到", file=sys.stderr)
            sys.exit(2)

        cases = crud.get_all_test_cases_for_project(db, project_id)
        if not cases:
            print(f"项目 '{project.name}' (ID={project_id}) 下没有测试用例")
            return

        print(f"项目: {project.name} (ID={project_id})")
        print(f"{'ID':<6} {'编号':<8} {'名称':<45} {'模块ID':<8} {'初始化':<6} {'创建时间'}")
        print("-" * 110)
        for c in cases:
            pcn = getattr(c, "project_case_number", None) or "-"
            mod = c.module_id or "-"
            is_init = "是" if getattr(c, "is_init", False) else "否"
            created = getattr(c, "created_at", None)
            created_str = str(created)[:19] if created else "-"
            print(f"{c.id:<6} {str(pcn):<8} {c.name:<45} {str(mod):<8} {is_init:<6} {created_str}")
    finally:
        db.close()


# ────────────────────────────────────────────────────────────────────
# 执行辅助
# ────────────────────────────────────────────────────────────────────


def _resolve_case_ids(
    db: Session,
    project_id: int,
    case_ids_arg: list[int] | None,
) -> list[int]:
    """解析用例 ID 列表；若未指定则查询项目下所有用例。"""
    from app import crud

    if case_ids_arg:
        # 验证每个 ID 有效
        for cid in case_ids_arg:
            case = crud.get_test_case(db, cid)
            if not case:
                print(f"错误: 测试用例 ID {cid} 未找到", file=sys.stderr)
                sys.exit(2)
        return case_ids_arg

    cases = crud.get_all_test_cases_for_project(db, project_id)
    if not cases:
        project = crud.get_project(db, project_id)
        name = project.name if project else f"ID={project_id}"
        print(f"警告: 项目 '{name}' 下没有测试用例")
        sys.exit(0)
    ids: list[int] = []
    for c in cases:
        ids.append(int(c.id))  # pyright: ignore[reportArgumentType]
    return ids


async def _execute_and_report(
    case_ids: list[int],
    project_id: int,
    env_id: int | None,
    headless: bool,
    output_path: str | None,
) -> None:
    """调用 runner 执行并汇总结果。"""
    from core.runner import run_batch_test_cases

    print(
        f"开始执行: 项目ID={project_id}, 用例数={len(case_ids)}"
        f"{', 环境ID=' + str(env_id) if env_id else ''}"
        f"{', headless=True' if headless else ''}"
    )

    results = await run_batch_test_cases(
        case_ids=case_ids,
        project_id=project_id,
        environment_id=env_id,
    )

    if results is None:
        print("错误: 测试执行未返回结果（浏览器可能启动失败）", file=sys.stderr)
        sys.exit(1)

    passed = sum(1 for r in results if r.get("status") == "passed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    total = len(results)

    summary = f"{passed}/{total} 通过, {failed} 失败"
    print()
    print("=" * 60)
    print(f"执行完成: {summary}")
    print("=" * 60)

    # 逐用例打印结果
    for r in results:
        cid = r.get("case_id", "?")
        status = r.get("status", "?")
        icon = "✓" if status == "passed" else "✗"
        err = r.get("error", "")
        detail = f" — {err}" if err else ""
        print(f"  {icon} 用例 {cid}: {status}{detail}")

    # 输出 JSON 报告
    if output_path:
        report_data = {
            "project_id": project_id,
            "environment_id": env_id,
            "headless": headless,
            "summary": {"total": total, "passed": passed, "failed": failed},
            "results": results,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            _json.dump(report_data, f, ensure_ascii=False, indent=2)
        print(f"\n报告已保存: {output_path}")

    if failed > 0:
        sys.exit(1)
    sys.exit(0)


def cmd_run(args: argparse.Namespace) -> None:
    """run 子命令：批量执行测试用例。"""
    # 设置 headless 环境变量
    if args.headless:
        os.environ["HEADLESS"] = "true"

    db = _get_db()
    try:
        from app import crud

        project = crud.get_project(db, args.project_id)
        if not project:
            print(f"错误: 项目 ID {args.project_id} 未找到", file=sys.stderr)
            sys.exit(2)

        # 解析 case-ids 参数
        if args.case_ids:
            case_ids_arg: list[int] | None = [int(x.strip()) for x in args.case_ids.split(",")]
        else:
            case_ids_arg = None
        case_ids = _resolve_case_ids(db, args.project_id, case_ids_arg)
    finally:
        db.close()

    asyncio.run(
        _execute_and_report(
            case_ids=case_ids,
            project_id=args.project_id,
            env_id=args.env_id,
            headless=args.headless,
            output_path=args.output,
        )
    )


def cmd_run_single(args: argparse.Namespace) -> None:
    """run-single 子命令：执行单个测试用例。"""
    if args.headless:
        os.environ["HEADLESS"] = "true"

    db = _get_db()
    try:
        from app import crud

        case = crud.get_test_case(db, args.case_id)
        if not case:
            print(f"错误: 测试用例 ID {args.case_id} 未找到", file=sys.stderr)
            sys.exit(2)

        project_id: int = int(case.project_id)  # pyright: ignore[reportArgumentType]
        project = crud.get_project(db, project_id)
        if not project:
            print(f"错误: 用例 {args.case_id} 所属项目 ID {project_id} 未找到", file=sys.stderr)
            sys.exit(2)
    finally:
        db.close()

    asyncio.run(
        _execute_and_report(
            case_ids=[args.case_id],
            project_id=project_id,
            env_id=args.env_id,
            headless=args.headless,
            output_path=args.output,
        )
    )


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="voyan",
        description="VoyanTest CLI — 命令行测试执行工具，用于 CI/CD 流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  voyan run --project-id 1                          # 执行项目全部用例
  voyan run --project-id 1 --case-ids 1,2,3         # 执行指定用例
  voyan run --project-id 1 --env-id 2 --headless    # 指定环境 + 无头模式
  voyan run --project-id 1 --output report.json     # 结果写入 JSON 文件
  voyan run-single --case-id 5 --env-id 2           # 执行单个用例
  voyan list-projects                               # 列出所有项目
  voyan list-cases --project-id 1                   # 列出项目全部用例
        """,
    )

    sub = parser.add_subparsers(dest="command", help="可用命令")

    # ── run ─────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="批量执行测试用例")
    _ = p_run.add_argument("--project-id", type=int, required=True, help="项目 ID")
    _ = p_run.add_argument("--env-id", type=int, default=None, help="环境 ID（可选）")
    _ = p_run.add_argument(
        "--case-ids",
        type=str,
        default=None,
        help="用例 ID 列表，逗号分隔（默认执行项目全部用例）",
    )
    _ = p_run.add_argument("--output", type=str, default=None, help="结果 JSON 文件路径")
    _ = p_run.add_argument("--headless", action="store_true", default=False, help="无头模式运行")

    # ── run-single ──────────────────────────────────────────────────
    p_single = sub.add_parser("run-single", help="执行单个测试用例")
    _ = p_single.add_argument("--case-id", type=int, required=True, help="用例 ID")
    _ = p_single.add_argument("--env-id", type=int, default=None, help="环境 ID（可选）")
    _ = p_single.add_argument("--output", type=str, default=None, help="结果 JSON 文件路径")
    _ = p_single.add_argument("--headless", action="store_true", default=False, help="无头模式运行")

    # ── list-projects ───────────────────────────────────────────────
    _ = sub.add_parser("list-projects", help="列出所有项目")

    # ── list-cases ──────────────────────────────────────────────────
    p_cases = sub.add_parser("list-cases", help="列出项目的测试用例")
    _ = p_cases.add_argument("--project-id", type=int, required=True, help="项目 ID")

    args = parser.parse_args()

    if args.command == "list-projects":
        list_projects()
    elif args.command == "list-cases":
        list_cases(args.project_id)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "run-single":
        cmd_run_single(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
