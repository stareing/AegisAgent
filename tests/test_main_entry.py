from __future__ import annotations

import pytest

import agent_framework.main as main_module


def test_main_delegates_to_cli_run(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_run(argv):
        called["argv"] = argv
        return 7

    monkeypatch.setattr(main_module, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        main_module.main(["--mock"])

    assert exc.value.code == 7
    assert called["argv"] == ["--mock"]

