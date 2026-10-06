import pytest

from gmail_agent.files import MAX_NAME_BYTES, safe_filename, write_new_file


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system.ini", "system.ini"),
        ("/abs/path/file.txt", "file.txt"),
        (".bashrc", "bashrc"),
        ("..", "attachment"),
        ("", "attachment"),
        ("a\x00b\nc.txt", "abc.txt"),
        ('what?:"*<>|.doc', "what.doc"),
        ("CON.txt", "_CON.txt"),
        ("trailing. ", "trailing"),
        ("Fäktura.pdf", "Fäktura.pdf"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_long_names_keep_extension():
    name = safe_filename("å" * 300 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode()) <= MAX_NAME_BYTES


def test_write_new_file_never_overwrites(tmp_path):
    a = write_new_file(tmp_path, "x.txt", b"1")
    b = write_new_file(tmp_path, "x.txt", b"2")
    c = write_new_file(tmp_path, "x.txt", b"3")
    assert [p.name for p in (a, b, c)] == ["x.txt", "x_1.txt", "x_2.txt"]
    assert a.read_bytes() == b"1"


def test_write_new_file_creates_directory(tmp_path):
    p = write_new_file(tmp_path / "a" / "b", "../evil.txt", b"x")
    assert p == (tmp_path / "a" / "b" / "evil.txt").resolve()
