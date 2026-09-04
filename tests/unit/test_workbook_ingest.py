"""读 .xlsx 的纯逻辑。

这些判断错了都**不报错**，只会让用户在几步之后对着一张长得不对的表：列名不是他写的
那个，或者编号列变成了可以求和的度量。所以用真实的 openpyxl 文件测，不用替身。
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from knowflow_analytics.ingest import (
    WorkbookError,
    list_sheets,
    preview_sheet,
    read_rows,
)


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class TestTypesComeFromTheCellsNotTheText:
    """按单元格的实际类型判，不看字符串长得像什么。"""

    def test_a_code_column_stays_text(self) -> None:
        """「001」这类编号是文本。

        判成数字它就会被当成可求和的度量——`status_code`、`year` 是同一个坑，
        而且不会报错，只会给出一个看起来正常的错数字。
        """

        data = _xlsx({"表": [["门店编号", "面积"], ["001", 128.5], ["002", 96.0]]})

        preview = preview_sheet(data, "表")

        types = {item.name: item.sql_type for item in preview.columns}
        assert types == {"门店编号": "text", "面积": "numeric"}

    def test_whole_numbers_and_decimals_are_told_apart(self) -> None:
        data = _xlsx({"表": [["件数", "金额"], [2, 10.5], [3, 20.0]]})

        types = {item.name: item.sql_type for item in preview_sheet(data, "表").columns}

        assert types == {"件数": "bigint", "金额": "numeric"}

    def test_a_date_column_does_not_become_a_timestamp(self) -> None:
        """openpyxl 对纯日期单元格也返回 datetime。

        照单全收会让「开业日期」变成时间戳，时间粒度上凭空多出一层没有的精度。
        """

        data = _xlsx({"表": [["开业日期", "下单时刻"],
                            [date(2021, 3, 1), datetime(2021, 3, 1, 10, 30)],
                            [date(2022, 6, 8), datetime(2022, 6, 8, 9, 0)]]})

        types = {item.name: item.sql_type for item in preview_sheet(data, "表").columns}

        assert types == {"开业日期": "date", "下单时刻": "timestamp"}

    def test_a_mixed_column_falls_back_to_text(self) -> None:
        """一列里既有数字又有文字：当文本存，不丢也不猜。"""

        data = _xlsx({"表": [["混合"], [1], ["两"], [3]]})

        assert preview_sheet(data, "表").columns[0].sql_type == "text"


class TestDirtyHeadersAreFixedAndDisclosed:
    """自动处理，但每一处改动都要说出来——用户要能在确认表里看见。"""

    def test_duplicate_headers_get_a_suffix_and_a_note(self) -> None:
        data = _xlsx({"表": [["名称", "名称"], ["A", "a1"], ["B", "b1"]]})

        preview = preview_sheet(data, "表")

        assert [item.name for item in preview.columns] == ["名称", "名称_2"]
        assert any("重名" in item for item in preview.changes)

    def test_a_blank_header_is_named_by_position(self) -> None:
        data = _xlsx({"表": [["名称", None], ["A", "x"], ["B", "y"]]})

        preview = preview_sheet(data, "表")

        assert [item.name for item in preview.columns] == ["名称", "第2列"]
        assert any("表头为空" in item for item in preview.changes)

    def test_surrounding_spaces_are_trimmed_and_disclosed(self) -> None:
        data = _xlsx({"表": [["  金额  "], [10], [20]]})

        preview = preview_sheet(data, "表")

        assert preview.columns[0].name == "金额"
        assert any("空格" in item for item in preview.changes)

    def test_an_empty_column_is_dropped_and_disclosed(self) -> None:
        """整列空既推不出类型，建出来也只是一列 NULL。"""

        data = _xlsx({"表": [["名称", "备注"], ["A", None], ["B", None]]})

        preview = preview_sheet(data, "表")

        assert [item.name for item in preview.columns] == ["名称"]
        assert any("整列为空" in item for item in preview.changes)

    def test_a_clean_sheet_reports_no_changes(self) -> None:
        """没动过的表不该弹出一张空的确认表。"""

        data = _xlsx({"表": [["名称", "金额"], ["A", 1], ["B", 2]]})

        assert preview_sheet(data, "表").changes == ()


class TestRefusalsSayWhichSheet:
    @pytest.mark.parametrize(
        ("rows", "code"),
        [
            pytest.param([["只有表头"]], "SHEET_HAS_NO_ROWS", id="只有表头"),
            pytest.param([["名称"], [None]], "SHEET_HAS_NO_COLUMNS", id="没有一列有数据"),
        ],
    )
    def test_an_unusable_sheet_is_refused(self, rows: list[list], code: str) -> None:
        with pytest.raises(WorkbookError) as excinfo:
            preview_sheet(_xlsx({"台账": rows}), "台账")

        assert excinfo.value.code == code
        assert "台账" in str(excinfo.value)

    def test_an_unknown_sheet_is_refused(self) -> None:
        with pytest.raises(WorkbookError) as excinfo:
            preview_sheet(_xlsx({"表": [["名称"], ["A"]]}), "不存在")

        assert excinfo.value.code == "SHEET_NOT_FOUND"

    def test_a_file_that_is_not_a_workbook_is_refused(self) -> None:
        with pytest.raises(WorkbookError) as excinfo:
            list_sheets("这不是一个 xlsx".encode())

        assert excinfo.value.code == "WORKBOOK_UNREADABLE"


class TestRowsFollowThePreview:
    """预览看到什么，落库就是什么——两条路必须用同一套列。"""

    def test_dropped_and_renamed_columns_line_up(self) -> None:
        data = _xlsx({
            "表": [
                ["名称", "名称", None, "空列"],
                ["A", "a1", "x", None],
                ["B", "b1", "y", None],
            ]
        })
        preview = preview_sheet(data, "表")

        rows = list(read_rows(data, preview))

        assert [item.name for item in preview.columns] == ["名称", "名称_2", "第3列"]
        assert rows == [("A", "a1", "x"), ("B", "b1", "y")]

    def test_blank_cells_become_null(self) -> None:
        data = _xlsx({"表": [["备注"], ["有"], [""], [None]]})

        rows = list(read_rows(data, preview_sheet(data, "表")))

        assert rows == [("有",), (None,), (None,)]


def test_every_sheet_is_listed() -> None:
    data = _xlsx({"第一张": [["A"], [1]], "第二张": [["B"], [2]]})

    assert list_sheets(data) == ("第一张", "第二张")


class TestInspectingEverySheetAtOnce:
    """一次把所有 sheet 看完。

    按 sheet 逐次调用意味着同一个文件要重传很多遍——一个 30MB 的台账有 8 张表就是
    240MB。而且读不出来的那张不该让整个文件失败。
    """

    @staticmethod
    def _previews(data: bytes) -> dict[str, dict]:
        from knowflow_analytics.application import AnalyticsApplication

        result = AnalyticsApplication.inspect_upload(
            AnalyticsApplication.__new__(AnalyticsApplication), data=data
        )
        return {item["sheet"]: item for item in result["previews"]}

    def test_every_sheet_comes_back_in_one_call(self) -> None:
        data = _xlsx({
            "销售": [["门店", "金额"], ["A", 1]],
            "档案": [["编号", "门店"], ["001", "A"]],
        })

        previews = self._previews(data)

        assert set(previews) == {"销售", "档案"}
        assert previews["档案"]["columns"][0]["type"] == "text"

    def test_an_unreadable_sheet_does_not_sink_the_others(self) -> None:
        """只有表头的那张带着自己的原因回去，其余照常可选。"""

        data = _xlsx({
            "好的": [["门店", "金额"], ["A", 1]],
            "只有表头": [["门店"]],
        })

        previews = self._previews(data)

        assert previews["好的"]["row_count"] == 1
        assert previews["只有表头"]["error"]["code"] == "SHEET_HAS_NO_ROWS"
        assert "只有表头" in previews["只有表头"]["error"]["message"]
