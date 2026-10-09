"""US6 调度环境绑定正式字段（environment_id 列，兼容旧 description）——033。

TDD：先于实现编写（预期红：task_environment_id 尚不存在 / dataclass 无该字段）。
"""

from __future__ import annotations

from app.scheduler import ScheduledTask, task_environment_id


class _T:
    def __init__(self, environment_id=None, description=""):
        self.environment_id = environment_id
        self.description = description


def test_column_takes_priority_over_legacy_description():
    assert task_environment_id(_T(7, "environment_id=9")) == 7


def test_legacy_description_still_supported():
    assert task_environment_id(_T(None, "environment_id=9")) == 9


def test_none_when_neither():
    assert task_environment_id(_T(None, "")) is None
    assert task_environment_id(_T(None, "其他描述")) is None


def test_malformed_description_is_none():
    assert task_environment_id(_T(None, "environment_id=abc")) is None
    assert task_environment_id(_T(None, "environment_id=")) is None


def test_scheduler_dataclass_carries_environment_id():
    t = ScheduledTask(id="1", name="n", cron_expression="* * * * *", task_type="api_case", target_id=2,
                      environment_id=5)
    assert t.environment_id == 5
    assert ScheduledTask(id="1", name="n", cron_expression="* * * * *", task_type="api_case", target_id=2).environment_id is None
