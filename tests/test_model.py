from types import SimpleNamespace

from capslock.model import OpenAIChatModel


def test_openai_adapter_omits_empty_tools_for_tool_free_calls() -> None:
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = OpenAIChatModel(client)
    model.complete(model="test", messages=[{"role": "user", "content": "hello"}], tools=[])
    assert "tools" not in calls[0]
