"""把表格文件变成一张受治理链路能读的物理表。"""

from knowflow_analytics.ingest.workbook import (
    SheetPreview,
    WorkbookColumn,
    WorkbookError,
    list_sheets,
    preview_sheet,
    read_rows,
)

__all__ = [
    "SheetPreview",
    "WorkbookColumn",
    "WorkbookError",
    "list_sheets",
    "preview_sheet",
    "read_rows",
]
