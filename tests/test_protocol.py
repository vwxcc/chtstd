import pytest

from protocol import ProtocolError, parse_submission


def test_unmarked_code_becomes_single_python_task():
    parsed = parse_submission('print("hello")')
    assert len(parsed.python_tasks) == 1
    assert parsed.python_tasks[0].name == "main"
    assert 'print("hello")' in parsed.python_tasks[0].body


def test_multiple_tasks_input_and_file():
    text = '''[[[PYTHON:first]]]
name = input("Name: ")
print(name)
[[[END]]]

[[[INPUT:first]]]
Timur
[[[END]]]

[[[FILE:out/info.txt]]]
ready
[[[END]]]'''
    parsed = parse_submission(text)
    assert [task.name for task in parsed.python_tasks] == ["first"]
    assert parsed.input_by_task["first"] == "Timur"
    assert parsed.files[0].name == "out/info.txt"
    assert parsed.files[0].body == "ready"


def test_duplicate_python_names_rejected():
    text = '[[[PYTHON:x]]]\nprint(1)\n[[[END]]]\n[[[PYTHON:x]]]\nprint(2)\n[[[END]]]'
    with pytest.raises(ProtocolError, match="уникальными"):
        parse_submission(text)


@pytest.mark.parametrize("path", ["../secrets.txt", "/tmp/evil", "a/../../b", ".env", "dir/.secret"])
def test_unsafe_file_path_rejected(path):
    text = f"[[[FILE:{path}]]]\nx\n[[[END]]]"
    with pytest.raises(ProtocolError):
        parse_submission(text)


def test_missing_end_rejected():
    with pytest.raises(ProtocolError):
        parse_submission("[[[PYTHON:x]]]\nprint(1)")


def test_orphan_input_rejected():
    with pytest.raises(ProtocolError, match="неизвестные Python-задачи"):
        parse_submission("[[[INPUT:missing]]]\n1\n[[[END]]]")


def test_text_outside_blocks_rejected():
    with pytest.raises(ProtocolError, match="вне блоков"):
        parse_submission("Вот код:\n[[[PYTHON:x]]]\nprint(1)\n[[[END]]]")
