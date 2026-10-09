"""Read an uploaded file into a DataFrame. CSV/TSV and Excel (.xlsx/.xlsm)."""
import io
import os

import pandas as pd

EXCEL = (".xlsx", ".xlsm")
MAX_ROWS = int(os.getenv("MAX_ROWS", "1000000"))


def is_excel(filename: str) -> bool:
    return (filename or "").lower().endswith(EXCEL)


def sheet_names(content: bytes) -> list[str]:
    try:
        return list(pd.ExcelFile(io.BytesIO(content)).sheet_names)
    except Exception as e:
        raise ValueError(f"could not read the Excel file: {e}")


def read_table(filename: str, content: bytes, sheet: str | None = None) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith((".csv", ".txt", ".tsv")):
        sep = "\t" if name.endswith(".tsv") else ","
        df = None
        for enc in ("utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(content), sep=sep, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
            except pd.errors.EmptyDataError:
                raise ValueError("the file is empty")
            except pd.errors.ParserError as e:
                raise ValueError(f"could not parse the file as {'TSV' if sep == chr(9) else 'CSV'}: {e}")
    elif name.endswith(EXCEL):
        try:
            df = pd.read_excel(io.BytesIO(content), sheet_name=sheet or 0)
        except ValueError as e:
            raise ValueError(f"could not read sheet {sheet!r}: {e}")
        except Exception as e:
            raise ValueError(f"could not read the Excel file: {e}")
    elif name.endswith(".xls"):
        raise ValueError("old .xls files are not supported; save as .xlsx or CSV")
    else:
        raise ValueError("unsupported file type; upload .csv, .tsv or .xlsx")
    if df is None or df.shape[1] == 0:
        raise ValueError("no columns found in the file")
    if len(df) == 0:
        raise ValueError("the file has a header but no rows")
    if len(df) > MAX_ROWS:
        raise ValueError(f"{len(df)} rows exceeds the limit of {MAX_ROWS} (set MAX_ROWS to raise it)")
    df.columns = [str(c) for c in df.columns]
    return df
