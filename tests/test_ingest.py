import io
from helpers import *
from app import ingest


def test_csv_tsv_and_encodings():
    csv = "a,b\n1,x\n2,y\n".encode()
    assert ingest.read_table("t.csv", csv).shape == (2, 2)
    assert ingest.read_table("t.tsv", b"a\tb\n1\tx\n").shape == (1, 2)
    assert ingest.read_table("t.csv", "name\nJos\xe9\n".encode("latin-1"))["name"][0] == "Jos\xe9"
    assert ingest.read_table("t.csv", b"\xef\xbb\xbfa,b\n1,2\n").columns.tolist() == ["a", "b"]    # BOM stripped


def test_excel_with_sheets():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        TITANIC.head(30).to_excel(xw, sheet_name="Pax", index=False)
        orders().to_excel(xw, sheet_name="Orders", index=False)
    data = buf.getvalue()
    assert ingest.sheet_names(data) == ["Pax", "Orders"]
    assert ingest.read_table("x.xlsx", data).shape == (30, 12)                                    # first sheet by default
    assert ingest.read_table("x.xlsx", data, "Orders").columns.tolist()[0] == "Order ID"
    assert "could not read sheet" in str(raises(lambda: ingest.read_table("x.xlsx", data, "Nope"), ValueError))


def test_excel_numeric_headers_become_text():
    buf = io.BytesIO()
    pd.DataFrame([[1, 2], [3, 4]]).to_excel(buf, index=False)
    assert ingest.read_table("x.xlsx", buf.getvalue()).columns.tolist() == ["0", "1"]


def test_bad_inputs_have_clear_messages():
    for name, content, needle in (("a.pdf", b"x", "unsupported"), ("a.xls", b"x", "not supported"),
                                  ("a.csv", b"", "empty"), ("a.csv", b"a,b\n", "no rows"),
                                  ("a.xlsx", b"not excel", "could not read")):
        assert needle in str(raises(lambda: ingest.read_table(name, content), ValueError)), name


def test_row_limit():
    old = ingest.MAX_ROWS
    ingest.MAX_ROWS = 2
    try:
        assert "exceeds" in str(raises(lambda: ingest.read_table("a.csv", b"a\n1\n2\n3\n"), ValueError))
    finally:
        ingest.MAX_ROWS = old
