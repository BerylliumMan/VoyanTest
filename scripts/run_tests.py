# run_tests.py
import asyncio
import argparse
import sys
from core.runner import run_test_case

def main():
    """
    命令行界面，用于触发测试用例运行。
    """
    parser = argparse.ArgumentParser(
        description="UI测试自动化运行器",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "case_id",
        type=int,
        help="您要运行的测试用例ID。"
    )
    parser.add_argument(
        "--browser",
        type=str,
        choices=['chromium', 'firefox', 'webkit'],
        help="覆盖项目设置中指定的浏览器。"
    )
    # 未来可以添加更多参数，例如 --headless, --env

    args = parser.parse_args()

    if not args.case_id:
        print("错误：需要测试用例ID。")
        parser.print_help()
        sys.exit(1)

    print(f"收到运行测试用例ID的请求：{args.case_id}")
    
    # 注意：run_test_case的当前实现尚不支持
    # 通过命令行参数覆盖浏览器。这是一个占位符
    # 用于未来增强。浏览器当前从项目设置中获取。
    if args.browser:
        print(f"请求浏览器覆盖：{args.browser}（功能待实现）")

    try:
        asyncio.run(run_test_case(args.case_id))
        print("\n测试运行完成。")
    except KeyboardInterrupt:
        print("\n测试运行被用户中断。")
        sys.exit(1)
    except Exception as e:
        print(f"\n发生错误：{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
