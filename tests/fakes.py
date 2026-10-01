"""Общие фейки для тестов: LLM без GPU, веб без сети, внешние сервисы."""

import hashlib
import re

from bot.llm import LoadedModel, StreamChunk, ToolsNotSupported
from bot.services import ProcResult, PythonResult, RunResult
from bot.web import SearchResult

DIM = 64


def fake_vector(text: str) -> list[float]:
    """Bag-of-words эмбеддинг: тексты с общими словами получаются похожими."""
    vec = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0
    return vec


class FakeLLM:
    def __init__(self, reply: str = "Ответ с кодом:\n```c\nint main(void);\n```") -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.chat_calls: list[dict] = []
        # Сценарий инструментов: список списков tool_calls по шагам
        self.tool_script: list[list[dict]] = []
        self.supports_tools = True
        self.facts_json = '{"facts": []}'
        self.chat_reply = "резюме диалога"
        # Ответы chat() по порядку, пока не кончатся: (подстрока промпта или "", ответ)
        self.scripted: list[tuple[str, str]] = []
        self.models = ["bge-m3:latest", "llama3.1:8b", "qwen2.5-coder:7b", "qwen2.5:3b",
                       "qwen2.5:7b", "qwen2.5vl:7b"]
        self.speeds = []
        self.unloaded: list[str] = []

    async def list_models(self, cached: bool = False) -> list[str]:
        return list(self.models)

    async def loaded_models(self):
        return [LoadedModel("qwen2.5:7b", 5 * 1024**3, 5 * 1024**3)]

    async def pull(self, model):
        if model == "bad:model":
            from bot.llm import LLMError

            raise LLMError("pull model manifest: file does not exist")
        yield {"status": "pulling manifest"}
        for done in (0, 2 * 1024**3, 4 * 1024**3):
            yield {"status": "pulling", "total": 4 * 1024**3, "completed": done}
        yield {"status": "success"}
        self.models.append(model)

    async def delete(self, model):
        self.models.remove(model)

    async def bench(self, model, prompt, num_predict=200):
        return {"gen_tps": 40.0 if "7b" in model else 80.0, "prompt_tps": 900.0, "load_s": 2.5,
                "total_s": 7.0, "tokens": 200.0}

    async def unload_all(self):
        self.unloaded.append("*")
        return ["qwen2.5:7b"]

    async def chat_stream(self, model, messages, tools=None, options=None):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages],
                           "tools": tools, "options": options})
        if tools and not self.supports_tools:
            raise ToolsNotSupported("model does not support tools")
        if tools and self.tool_script:
            yield StreamChunk(tool_calls=self.tool_script.pop(0))
            return
        for part in self.reply.split(" "):
            yield StreamChunk(part + " ")

    async def chat(self, model, messages, json_mode=False, options=None):
        self.chat_calls.append({"model": model, "messages": messages, "json": json_mode})
        prompt = messages[-1]["content"]
        for i, (needle, reply) in enumerate(self.scripted):
            if needle in prompt:
                return self.scripted.pop(i)[1]
        return self.facts_json if json_mode else self.chat_reply

    async def embed(self, model, texts):
        return [fake_vector(t) for t in texts]

    async def close(self) -> None:
        pass


class FakeWeb:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fetched: list[str] = []
        self.page_text = "Page body text"

    async def search(self, query: str, limit: int = 6, *, news: bool = False):
        self.queries.append(query)
        return [SearchResult("Ollama release", "https://example.com/ollama", "Ollama 0.34 released")]

    async def fetch(self, url: str, max_chars: int = 8000):
        self.fetched.append(url)
        return "Page title", self.page_text[:max_chars]

    async def close(self) -> None:
        pass


class FakeSandbox:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.projects: list[dict] = []
        self.python_runs: list[str] = []
        self.python_stdout = "42\n"
        self.result = RunResult(
            compiled=True, compile_command="cc -Wall -Wextra -Werror main.c -o fox_prog",
            compile_output="", valgrind_log=None,
            run=ProcResult(exit_code=0, signal=None, timed_out=False, stdout="hi\n", stderr="", duration_ms=3),
        )
        self.project_report = {
            "files": ["Makefile", "main.c"],
            "norminette": {"ok": True, "errors": 0, "files_checked": 1, "output": ""},
            "makefile": {"exists": True, "path": "Makefile", "rules": {}, "has_name": True,
                         "uses_wildcard": False, "has_flags": True, "issues": []},
            "build": {"attempted": True, "ok": True, "output": "", "relinks": False, "relink_output": ""},
        }

    async def run(self, files, **kwargs):
        self.runs.append({"files": files, **kwargs})
        return self.result

    async def python(self, code, timeout=20.0):
        self.python_runs.append(code)
        out = ProcResult(exit_code=0, signal=None, timed_out=False, stdout=self.python_stdout,
                         stderr="", duration_ms=5)
        return PythonResult(out, [b"\x89PNG-plot"] if "plt" in code else [])

    async def check_project(self, files):
        self.projects.append(files)
        return self.project_report

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class FakeSpeech:
    def __init__(self, text: str = "напомни через 10 минут выключить плиту") -> None:
        self.text = text
        self.synthesized: list[str] = []
        self.languages: list[str | None] = []  # язык каждого запроса transcribe
        self.voices: list[str] = []  # язык каждого запроса synthesize

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg", language: str | None = None) -> str:
        self.languages.append(language)
        return self.text

    async def synthesize(self, text: str, lang: str = "ru") -> bytes:
        self.synthesized.append(text)
        self.voices.append(lang)
        return b"OggS-fake"

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class FakeImages:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, seed=None) -> bytes:
        self.prompts.append(prompt)
        return b"\x89PNG-fake"

    async def unload(self) -> None:
        self.unloads = getattr(self, "unloads", 0) + 1

    async def health(self) -> bool:
        return False

    async def close(self) -> None:
        pass


class FakeYouTube:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.text = "В этом видео автор объясняет указатели в C. " * 20

    async def transcript(self, url: str):
        from bot.summarize import Transcript

        self.urls.append(url)
        return Transcript("Указатели в C", self.text, "автосубтитры (ru)", 754, url)

    async def close(self) -> None:
        pass


class FakeWeather:
    def __init__(self) -> None:
        from bot.briefing import Weather

        self.weather = Weather(now_temp=9.0, code=63, t_min=7.0, t_max=12.0, rain_chance=80, rain_mm=4.0, wind=10.0)
        self.queries: list[str] = []

    async def geocode(self, name: str):
        from bot.briefing import City

        self.queries.append(name)
        return City("Прага, Чехия", 50.08, 14.42) if name.lower().startswith("праг") else None

    async def forecast(self, city):
        return self.weather

    async def close(self) -> None:
        pass
