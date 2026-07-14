import csv
import io
import re


CSV_HEADER = ["用例ID", "所属模块", "标题", "前置条件", "测试步骤", "预期结果", "优先级"]


def _clean(value: str) -> str:
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"(\s+)(\d+\.)", r"\n\2", value)
    return value


def generate_csv(test_cases: list) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    for tc in test_cases:
        writer.writerow([
            tc.test_case_id,
            _clean(tc.module),
            _clean(tc.title),
            _clean(tc.preconditions),
            _clean(tc.test_steps),
            _clean(tc.expected_result),
            _clean(tc.priority),
        ])
    return output.getvalue()
