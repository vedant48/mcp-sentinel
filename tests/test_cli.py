import os
import stat

from mcp_sentinel import cli


def test_init_creates_distinct_private_credentials_without_printing_them(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config"
    monkeypatch.setattr(cli, "CONFIG", config)
    cli.init_tokens()
    first = (config / "control.token").read_text()
    second = (config / "remote.token").read_text()
    assert first != second
    assert len(first.strip()) >= 32
    assert first.strip() not in capsys.readouterr().out
    if os.name == "posix":
        assert stat.S_IMODE((config / "control.token").stat().st_mode) == 0o600
    cli.init_tokens()
    assert (config / "control.token").read_text() == first
    assert (config / "remote.token").read_text() == second
