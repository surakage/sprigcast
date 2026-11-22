from types import SimpleNamespace

from sprigcast.app import resources


def test_memory_text_names_task_managers_number_and_the_shared_total(monkeypatch):
    monkeypatch.setattr(resources, "_private_working_set", lambda process: 500_000_000)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1_190_000_000))
    assert resources.memory_text(process) == "500 MB (1190 MB with shared)"


def test_memory_text_without_shared_memory_is_one_number(monkeypatch):
    monkeypatch.setattr(resources, "_private_working_set", lambda process: 280_000_000)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=280_000_000))
    assert resources.memory_text(process) == "280 MB"
