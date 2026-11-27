"""Fixed read-only Excel helper; path boundaries are validated by the calling tool, and no arbitrary code is accepted."""

import json
import sys
from contextlib import closing
from pathlib import Path

from openpyxl import load_workbook


def read_excel(path: Path, sheet: str | None, start_row: int, max_rows: int) -> dict:
    with closing(load_workbook(path, read_only=True, data_only=False)) as formulas:
        for ws in formulas.worksheets:
            # Valid XLSX files written via write_only mode may omit dimension metadata; force dimension scan in read_only mode.
            if ws.max_row is None or ws.max_column is None:
                ws.calculate_dimension(force=True)
        if sheet is None or sheet == "":
            return {
                "sheets": [
                    {"name": ws.title, "rows": ws.max_row, "columns": ws.max_column}
                    for ws in formulas.worksheets
                ]
            }
        if sheet not in formulas.sheetnames:
            raise ValueError(
                f"Worksheet {sheet!r} not found; available worksheets: {', '.join(formulas.sheetnames)}"
            )
        ws = formulas[sheet]
        end = min(ws.max_row, start_row + max_rows - 1)
        if ws.max_column > 200:
            raise ValueError(
                "Worksheet exceeds 200 columns, which read_excel does not support; refusing to silently drop trailing columns"
            )
        rows = []
        with closing(load_workbook(path, read_only=True, data_only=True)) as cached:
            # Stream formula and cached-value workbooks row-by-row in lockstep to avoid repeated XML random access.
            bounds = {"min_row": start_row, "max_row": end, "max_col": ws.max_column}
            for cells, values in zip(
                ws.iter_rows(**bounds),
                cached[sheet].iter_rows(**bounds, values_only=True),
                strict=True,
            ):
                # Attach cached_value only on formula cells so long text constants are not duplicated in context.
                rows.append(
                    [
                        {
                            "cell": c.coordinate,
                            "value": c.value,
                            **({"cached_value": value} if c.data_type == "f" else {}),
                        }
                        for c, value in zip(cells, values, strict=True)
                        if c.value is not None
                    ]
                )
        return {
            "sheet": sheet,
            "start_row": start_row,
            "rows": rows,
            "has_more": end < ws.max_row,
            "note": "Only formula cells include cached_value (the last saved file cache, not recalculated); constants return value only, and recalculated formula results come from verifier observations.",
        }


if __name__ == "__main__":
    args = json.load(sys.stdin)
    result = read_excel(
        Path(args["path"]),
        args.get("sheet"),
        args.get("start_row", 1),
        args.get("max_rows", 50),
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
