"""Test AI gen parse pipeline: _clean_text, _split_numbered_steps, fallback logic."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.gen.analyzer import _clean_text
from app.gen.adapter import _split_numbered_steps, _split_expected_results, import_test_cases
from app.gen.csv_generator import _clean as csv_clean
from app.gen.models import TestCase as GenTestCase
from app import db_models


# ===== _clean_text (analyzer.py) =====
# Fix: (\d+\.\s) -> (\d+\.) to support Chinese "1.步骤" format

class TestCleanText:
    @pytest.mark.asyncio
    async def test_english_format(self):
        result = _clean_text("1. text 2. more 3. final")
        assert result == "1. text\n2. more\n3. final", repr(result)

    @pytest.mark.asyncio
    async def test_chinese_format(self):
        """Key fix: "1.步骤 2.步骤" no space after dot"""
        result = _clean_text("1.点击【新增】 2.输入数据 3.点击【保存】")
        assert result == "1.点击【新增】\n2.输入数据\n3.点击【保存】", repr(result)

    @pytest.mark.asyncio
    async def test_br_tags(self):
        result = _clean_text("1. step<br>2. step2")
        assert result == "1. step\n2. step2", repr(result)

    @pytest.mark.asyncio
    async def test_chinese_with_newlines(self):
        result = _clean_text("1.步骤1\n2.步骤2 3.步骤3")
        assert result == "1.步骤1\n2.步骤2\n3.步骤3", repr(result)

    @pytest.mark.asyncio
    async def test_empty_string(self):
        assert _clean_text("") == ""

    @pytest.mark.asyncio
    async def test_single_step(self):
        result = _clean_text("1.点击【新增】")
        assert result == "1.点击【新增】", repr(result)


# ===== csv_generator._clean (same regex) =====

class TestCsvClean:
    @pytest.mark.asyncio
    async def test_chinese_format(self):
        result = csv_clean("1.步骤1 2.步骤2")
        assert result == "1.步骤1\n2.步骤2", repr(result)

    @pytest.mark.asyncio
    async def test_english_format(self):
        result = csv_clean("1. step 2. step")
        assert result == "1. step\n2. step", repr(result)


# ===== _split_numbered_steps (adapter.py) =====

class TestSplitNumberedSteps:
    @pytest.mark.asyncio
    async def test_english_format(self):
        result = _split_numbered_steps("1. step1 2. step2")
        assert result == ["step1", "step2"]

    @pytest.mark.asyncio
    async def test_chinese_format(self):
        result = _split_numbered_steps("1.步骤1 2.步骤2 3.步骤3")
        assert result == ["步骤1", "步骤2", "步骤3"]

    @pytest.mark.asyncio
    async def test_with_newlines(self):
        result = _split_numbered_steps("1.步骤1\n2.步骤2\n3.步骤3")
        assert result == ["步骤1", "步骤2", "步骤3"]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        assert _split_numbered_steps("") == []
        assert _split_numbered_steps(None) == []
        assert _split_numbered_steps("   ") == []

    @pytest.mark.asyncio
    async def test_single_item(self):
        result = _split_numbered_steps("1.only_step")
        assert result == ["only_step"]


# ===== _split_expected_results (adapter.py) =====

class TestSplitExpectedResults:
    @pytest.mark.asyncio
    async def test_basic(self):
        result = _split_expected_results("1.结果1 2.结果2")
        assert result == ["结果1", "结果2"]

    @pytest.mark.asyncio
    async def test_empty(self):
        assert _split_expected_results("") == []


# ===== adapter.py fallback logic (step/result count match) =====

class TestStepResultAlignment:
    """Test the padding/truncation logic inside import_test_cases."""

    def _align(self, steps: list, results: list) -> list:
        """Simulate adapter.py lines 97-105."""
        steps_text = list(steps)
        results_text = list(results)
        if len(results_text) > len(steps_text):
            base = results_text[:len(steps_text) - 1] if len(steps_text) > 1 else []
            extras = results_text[len(steps_text) - 1:]
            results_text = base + ['；'.join(extras)]
        elif len(results_text) < len(steps_text):
            last = results_text[-1] if results_text else ''
            results_text = results_text + [last] * (len(steps_text) - len(results_text))
        return list(zip(steps_text, results_text))

    @pytest.mark.asyncio
    async def test_exact_match(self):
        aligned = self._align(["s1", "s2", "s3"], ["r1", "r2", "r3"])
        assert aligned == [("s1", "r1"), ("s2", "r2"), ("s3", "r3")]

    @pytest.mark.asyncio
    async def test_more_results_than_steps(self):
        """3 steps, 5 results: extra 2 merged into last step."""
        aligned = self._align(["s1", "s2", "s3"], ["r1", "r2", "r3", "r4", "r5"])
        assert len(aligned) == 3
        assert aligned[0] == ("s1", "r1")
        assert aligned[1] == ("s2", "r2")
        assert aligned[2] == ("s3", "r3；r4；r5")

    @pytest.mark.asyncio
    async def test_more_steps_than_results(self):
        """5 steps, 3 results: last result repeated for remaining."""
        aligned = self._align(["s1", "s2", "s3", "s4", "s5"], ["r1", "r2", "r3"])
        assert len(aligned) == 5
        assert aligned[0] == ("s1", "r1")
        assert aligned[1] == ("s2", "r2")
        assert aligned[2] == ("s3", "r3")
        assert aligned[3] == ("s4", "r3")
        assert aligned[4] == ("s5", "r3")

    @pytest.mark.asyncio
    async def test_no_results(self):
        """No results at all: empty string repeated."""
        aligned = self._align(["s1", "s2"], [])
        assert aligned == [("s1", ""), ("s2", "")]

    @pytest.mark.asyncio
    async def test_one_step_many_results(self):
        """1 step with 3 results: all merged."""
        aligned = self._align(["s1"], ["r1", "r2", "r3"])
        assert aligned == [("s1", "r1；r2；r3")]

    @pytest.mark.asyncio
    async def test_tc001_real_data(self):
        """Simulate TC-001: 10 steps, 3 results."""
        steps = [f"step{i}" for i in range(1, 11)]
        results = ["页面提示保存成功", "数据列表刷新显示新增数据", "所有字段值与输入一致"]
        aligned = self._align(steps, results)
        assert len(aligned) == 10
        assert aligned[0] == ("step1", "页面提示保存成功")
        assert aligned[1] == ("step2", "数据列表刷新显示新增数据")
        assert aligned[2] == ("step3", "所有字段值与输入一致")
        assert aligned[3] == ("step4", "所有字段值与输入一致")
        assert aligned[9] == ("step10", "所有字段值与输入一致")


# ===== Full adapter import_test_cases integration =====

class TestImportTestCases:
    """Test full import flow with DB fixture."""

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, db):
        self.db = db
        from app.crud.project import create_project
        from app.models import ProjectCreate
        self.project = await create_project(db, ProjectCreate(name="Gen测试项目"))

    def _make_gen_tc(self, tc_id, steps, results):
        return GenTestCase(
            test_case_id=tc_id,
            module="测试模块",
            title=f"用例{tc_id}",
            preconditions="用户已登录",
            test_steps=steps,
            expected_result=results,
            priority="高",
        )

    @pytest.mark.asyncio
    async def test_import_exact_match(self):
        """5 steps, 5 results — all created as TestStep records."""
        tc = self._make_gen_tc("TC-001",
            "1.步骤1 2.步骤2 3.步骤3 4.步骤4 5.步骤5",
            "1.结果1 2.结果2 3.结果3 4.结果4 5.结果5",
        )
        created = await import_test_cases(self.db, self.project.id, [tc])
        assert len(created) == 1
        # Verify TestSteps
        result = await self.db.execute(
            select(db_models.TestStep)
            .where(db_models.TestStep.case_id == created[0].id)
            .order_by(db_models.TestStep.step_order)
        )
        steps_db = result.scalars().all()
        assert len(steps_db) == 5
        assert steps_db[0].parsed_result == "结果1"
        assert steps_db[4].parsed_result == "结果5"

    @pytest.mark.asyncio
    async def test_import_more_steps_than_results(self):
        """3 steps, 1 result — last result repeated."""
        tc = self._make_gen_tc("TC-002",
            "1.打开页面 2.输入数据 3.点击保存",
            "1.页面提示保存成功",
        )
        created = await import_test_cases(self.db, self.project.id, [tc])
        result = await self.db.execute(
            select(db_models.TestStep)
            .where(db_models.TestStep.case_id == created[0].id)
            .order_by(db_models.TestStep.step_order)
        )
        steps_db = result.scalars().all()
        assert len(steps_db) == 3
        assert steps_db[0].parsed_result == "页面提示保存成功"
        assert steps_db[1].parsed_result == "页面提示保存成功"
        assert steps_db[2].parsed_result == "页面提示保存成功"

    @pytest.mark.asyncio
    async def test_import_more_results_than_steps(self):
        """1 step, 3 results — merged with ；."""
        tc = self._make_gen_tc("TC-003",
            "1.点击【新增】",
            "1.弹窗打开 2.字段显示 3.按钮可用",
        )
        created = await import_test_cases(self.db, self.project.id, [tc])
        result = await self.db.execute(
            select(db_models.TestStep)
            .where(db_models.TestStep.case_id == created[0].id)
            .order_by(db_models.TestStep.step_order)
        )
        steps_db = result.scalars().all()
        assert len(steps_db) == 1
        assert "弹窗打开" in steps_db[0].parsed_result
        assert "字段显示" in steps_db[0].parsed_result
        assert "按钮可用" in steps_db[0].parsed_result

    @pytest.mark.asyncio
    async def test_import_chinese_format(self):
        """Test with Chinese format without space after dot."""
        tc = self._make_gen_tc("TC-004",
            "1.点击【新增】 2.在【名称】框输入数据 3.点击【保存】",
            "1.保存成功 2.列表刷新 3.数据显示正确",
        )
        created = await import_test_cases(self.db, self.project.id, [tc])
        result = await self.db.execute(
            select(db_models.TestStep)
            .where(db_models.TestStep.case_id == created[0].id)
            .order_by(db_models.TestStep.step_order)
        )
        steps_db = result.scalars().all()
        assert len(steps_db) == 3
        assert steps_db[0].description == "点击【新增】"
        assert steps_db[1].description == "在【名称】框输入数据"
        assert steps_db[2].description == "点击【保存】"
