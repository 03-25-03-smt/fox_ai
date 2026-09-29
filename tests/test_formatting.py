from bot.formatting import md_to_html, split_markdown


def test_html_is_escaped():
    assert md_to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_code_block_with_language():
    out = md_to_html('Пример:\n```c\n#include <unistd.h>\nint x = a->b;\n```\nконец')
    assert '<pre><code class="language-c">#include &lt;unistd.h&gt;\nint x = a-&gt;b;</code></pre>' in out
    assert out.startswith("Пример:\n") and out.endswith("\nконец")


def test_code_block_contents_not_formatted():
    out = md_to_html("```\n**not bold** `x`\n```")
    assert out == "<pre>**not bold** `x`</pre>"


def test_unclosed_code_block():
    assert md_to_html("```py\nprint(1)") == '<pre><code class="language-py">print(1)</code></pre>'


def test_inline_bold_header():
    out = md_to_html("# Заголовок\n**жирный** и `ft_strlen(<s>)`")
    assert out == "<b>Заголовок</b>\n<b>жирный</b> и <code>ft_strlen(&lt;s&gt;)</code>"


def test_short_text_not_split():
    assert split_markdown("hello", 100) == ["hello"]


def test_split_respects_limit_and_keeps_text():
    text = "\n".join(f"строка {i}" for i in range(500))
    chunks = split_markdown(text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_inside_code_block_reopens_fence():
    code = "\n".join(f"int v{i} = {i};" for i in range(100))
    text = f"Вот код:\n```c\n{code}\n```\nГотово"
    chunks = split_markdown(text, 300)
    assert len(chunks) > 2
    for c in chunks:
        assert len(c) <= 300
        assert c.count("```") % 2 == 0, c  # каждый кусок — валидный markdown
    assert chunks[1].startswith("```c\n")
    assert chunks[-1].endswith("Готово")


def test_split_very_long_line():
    chunks = split_markdown("x" * 1000, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "x" * 1000


def test_split_random_texts_invariants():
    import random

    rng = random.Random(42)
    for _ in range(200):
        lines = []
        for _ in range(rng.randint(1, 80)):
            kind = rng.random()
            if kind < 0.1:
                lines.append("```" + rng.choice(["", "c", "python"]))
            else:
                lines.append("y" * rng.randint(0, 400))
        text = "\n".join(lines)
        if text.count("```") % 2:
            text += "\n```"
        limit = rng.randint(50, 500)
        for chunk in split_markdown(text, limit):
            assert 0 < len(chunk) <= limit
            assert chunk.count("```") % 2 == 0
