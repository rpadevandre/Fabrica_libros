from pathlib import Path

from bookforge.core.models import MarketingFlags
from bookforge.orchestrator.flags import FileFlagStore, kill_switch, set_channel


def make_store(tmp_path: Path) -> FileFlagStore:
    return FileFlagStore(tmp_path / "flags.json")


def test_default_everything_off(tmp_path):
    store = make_store(tmp_path)
    flags = store.get()
    assert flags.master is False
    assert flags.channel_active("pinterest") is False


def test_channel_requires_master_and_group(tmp_path):
    store = make_store(tmp_path)
    set_channel("pinterest", True, store)
    assert store.get().channel_active("pinterest") is False  # falta master+grupo
    set_channel("organic", True, store)
    assert store.get().channel_active("pinterest") is False  # falta master
    set_channel("master", True, store)
    assert store.get().channel_active("pinterest") is True


def test_kill_switch_overrides_everything(tmp_path):
    store = make_store(tmp_path)
    for ch in ("master", "organic", "paid", "pinterest", "amazon_ads"):
        set_channel(ch, True, store)
    assert store.get().channel_active("amazon_ads") is True
    kill_switch(store)
    flags = store.get()
    assert flags.master is False
    assert flags.channel_active("pinterest") is False
    assert flags.channel_active("amazon_ads") is False


def test_corrupt_file_fails_safe(tmp_path):
    path = tmp_path / "flags.json"
    path.write_text("{not valid json")
    store = FileFlagStore(path)
    assert store.get() == MarketingFlags()  # todo OFF
