"""START-01 fix. Ambient profiles and checkout .env.local stay out of tests."""

from __future__ import annotations

import os
from pathlib import Path

from guardian.service import load_local_env


def test_start_fix_fixture_hides_home_profile_and_dotenv(tmp_path: Path, monkeypatch) -> None:
    assert Path(os.environ["HOME"]) == tmp_path / "home"
    assert Path(os.environ["USERPROFILE"]) == tmp_path / "home"
    assert Path(os.environ["XDG_CONFIG_HOME"]) == tmp_path / "xdg-config"
    assert Path(os.environ["XDG_STATE_HOME"]) == tmp_path / "xdg-state"
    assert Path(os.environ["XDG_DATA_HOME"]) == tmp_path / "xdg-data"
    assert "MONITOR_SECRET_PROFILE" not in os.environ
    assert "MONITOR_ACCOUNTS_FILE" not in os.environ
    assert "MONITOR_DELIVERY_PROFILE" not in os.environ
    assert "MONITOR_STATE_DIR" not in os.environ
    assert "GUARDIAN_RESOLVER_MODE" not in os.environ
    assert "GUARDIAN_DOH_ENDPOINTS" not in os.environ
    assert "ETHERSCAN_API_KEY" not in os.environ
    assert load_local_env() == []
    assert "ETHERSCAN_API_KEY" not in os.environ

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env.local").write_text(
        'ETHERSCAN_API_KEY="canary-dotenv-startfix"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("guardian.runtime_binding.source_checkout_root", lambda: checkout)
    assert load_local_env() == []
    assert "ETHERSCAN_API_KEY" not in os.environ
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GUARDIAN_CONFIG_DIR", raising=False)
    assert load_local_env() == ["ETHERSCAN_API_KEY"]
    assert os.environ["ETHERSCAN_API_KEY"] == "canary-dotenv-startfix"
