"""Python в песочнице: сервис /python, команда /py, инструмент run_python."""

import base64
import json
import shutil

import pytest
from aiogram.methods import SendPhoto

from .conftest import ADMIN
from .test_units import load_service


def tool_call(name: str, **args) -> list[dict]:
    return [{"function": {"name": name, "arguments": json.dumps(args)}}]


@pytest.mark.skipif(shutil.which("python3") is None, reason="нужен python3")
def test_sandbox_python_service(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    sandbox = load_service("sandbox")
    monkeypatch.setattr(sandbox, "WORK_ROOT", str(tmp_path))
    client = TestClient(sandbox.app)
    data = client.post("/python", json={"code": "print(sum(range(101)))"}).json()
    assert data["run"]["stdout"].strip() == "5050" and data["images"] == []
    err = client.post("/python", json={"code": "x = 1\n1/0"}).json()["run"]
    assert "ZeroDivisionError" in err["stderr"] and 'user_code.py", line 2' in err["stderr"]
    slow = client.post("/python", json={"code": "while True: pass", "timeout": 1}).json()["run"]
    assert slow["timed_out"]
    # без matplotlib графика не будет, но и падать не должно
    plot = client.post("/python", json={"code": "import os\nprint(os.getcwd() != '/')"}).json()
    assert plot["run"]["stdout"].strip() == "True"


async def test_py_runs_code_directly(env):
    await env.send(ADMIN, "/py print(6*7)")
    assert env.sandbox.python_runs == ["print(6*7)"]
    assert "42" in env.last_text() and not env.llm.calls


async def test_py_strips_code_fence_and_sends_plot(env):
    await env.send(ADMIN, "/py ```python\nimport matplotlib.pyplot as plt\nplt.plot([1, 2])\n```")
    assert env.sandbox.python_runs[-1].startswith("import matplotlib")
    assert env.session.of_type(SendPhoto)


async def test_py_task_in_words_uses_tool(env):
    env.llm.tool_script = [tool_call("run_python", code="import matplotlib.pyplot as plt\nprint(1850*24.3)")]
    env.llm.reply = "Это 44955 крон."
    env.sandbox.python_stdout = "44955.0\n"
    await env.send(ADMIN, "/py сколько 1850 евро в кронах по курсу 24.3")
    assert "run_python" in env.last_user_prompt() or env.llm.calls[0]["tools"]
    tool_msgs = [m for m in env.llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert "44955.0" in tool_msgs[0]["content"]
    assert env.session.of_type(SendPhoto)  # график из инструмента дошёл
    assert env.assistant.images == {}


async def test_python_tool_offered_in_chat(env):
    await env.send(ADMIN, "сколько секунд в високосном году?")
    names = [t["function"]["name"] for t in env.llm.calls[-1]["tools"]]
    assert "run_python" in names
    assert "run_python" in env.system_prompt()


def test_python_result_text():
    from bot.services import ProcResult, PythonResult

    ok = PythonResult(ProcResult(0, None, False, "", "", 1), [base64.b64decode("iVBO")])
    assert "print()" not in ok.as_text() and "графиков: 1" in ok.as_text()
    killed = PythonResult(ProcResult(None, "SIGKILL", False, "", "", 1), [])
    assert "SIGKILL" in killed.as_text()
