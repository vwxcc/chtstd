import asyncio
import shutil

from executor import execute_submission
from protocol import parse_submission


def test_executor_runs_python_collects_file_and_feeds_input():
    async def run():
        updates = []

        async def on_status(text):
            updates.append(text)

        source = '''[[[PYTHON:sum]]]
a = int(input())
b = int(input())
print(a + b)
with open("sum.txt", "w", encoding="utf-8") as f:
    f.write(str(a + b))
[[[END]]]
[[[INPUT:sum]]]
12
8
[[[END]]]'''
        result = await execute_submission(parse_submission(source), asyncio.Event(), on_status)
        try:
            assert result.tasks[0].returncode == 0
            assert result.tasks[0].stdout.strip() == "20"
            assert any(path.name == "sum.txt" and path.read_text() == "20" for path in result.artifacts)
            assert updates and "sum" in updates[0]
        finally:
            if result.workspace:
                shutil.rmtree(result.workspace, ignore_errors=True)

    asyncio.run(run())


def test_executor_can_request_input_interactively():
    async def run():
        prompts = []

        async def on_status(text):
            pass

        async def on_input(prompt):
            prompts.append(prompt)
            return "Ada"

        result = await execute_submission(
            parse_submission('name = input("Your name: ")\nprint("Hi", name)'),
            asyncio.Event(),
            on_status,
            on_input,
        )
        try:
            assert result.tasks[0].returncode == 0
            assert "Hi Ada" in result.tasks[0].stdout
            assert prompts == ["Your name: "]
        finally:
            if result.workspace:
                shutil.rmtree(result.workspace, ignore_errors=True)

    asyncio.run(run())
