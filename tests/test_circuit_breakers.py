from decimal import Decimal
from pathlib import Path

import pytest

from bookforge.orchestrator.flags import FileFlagStore, set_channel
from bookforge.pipelines.p5_marketing.pipeline import (
    CampaignMetrics, PaidAdsManager,
)


class FakeAds:
    def __init__(self, metrics, spend_today=Decimal("0")):
        self.metrics = metrics
        self.spend_today = spend_today
        self.paused = []

    async def get_metrics(self, cid):
        return self.metrics[cid]

    async def pause_campaign(self, cid):
        self.paused.append(cid)

    async def total_spend_today(self):
        return self.spend_today


def store_all_on(tmp_path: Path) -> FileFlagStore:
    store = FileFlagStore(tmp_path / "flags.json")
    for ch in ("master", "paid", "amazon_ads"):
        set_channel(ch, True, store)
    return store


@pytest.mark.asyncio
async def test_flags_off_pauses_everything(tmp_path):
    store = FileFlagStore(tmp_path / "flags.json")  # todo OFF por defecto
    ads = FakeAds({})
    mgr = PaidAdsManager(ads=ads, flag_store=store)
    events = await mgr.tick(["c1", "c2"])
    assert ads.paused == ["c1", "c2"]
    assert all(e.rule == "flags_off" for e in events)


@pytest.mark.asyncio
async def test_acos_breaker(tmp_path):
    store = store_all_on(tmp_path)
    ads = FakeAds({
        "good": CampaignMetrics(campaign_id="good", spend_usd=Decimal("10"),
                                sales_usd=Decimal("50"), days_active=5),
        "bad": CampaignMetrics(campaign_id="bad", spend_usd=Decimal("40"),
                               sales_usd=Decimal("20"), days_active=5),
    })
    mgr = PaidAdsManager(ads=ads, flag_store=store)
    events = await mgr.tick(["good", "bad"])
    assert ads.paused == ["bad"]
    assert events[0].rule == "max_acos"


@pytest.mark.asyncio
async def test_daily_budget_breaker(tmp_path):
    store = store_all_on(tmp_path)
    ads = FakeAds({}, spend_today=Decimal("99"))
    mgr = PaidAdsManager(ads=ads, flag_store=store)
    events = await mgr.tick(["c1"])
    assert ads.paused == ["c1"]
    assert events[0].rule == "daily_budget"


@pytest.mark.asyncio
async def test_no_sales_14_days(tmp_path):
    store = store_all_on(tmp_path)
    ads = FakeAds({
        "stale": CampaignMetrics(campaign_id="stale", spend_usd=Decimal("3"),
                                 sales_usd=Decimal("0"), days_active=20),
    })
    # ACOS infinito tambien dispararia max_acos primero; validamos que pausa
    mgr = PaidAdsManager(ads=ads, flag_store=store)
    events = await mgr.tick(["stale"])
    assert ads.paused == ["stale"]
