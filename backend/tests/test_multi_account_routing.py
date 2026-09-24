"""Demo accounts always trade; each live account trades only when live trading
is on AND its own switch is on."""
from __future__ import annotations

import pytest

from app.agents import execution


class _Acct:
    def __init__(self, id, kind):
        self.id, self.account_type = id, kind


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DB:
    def __init__(self, accounts):
        self.accounts = {a.id: a for a in accounts}

    async def get(self, model, key):
        return self.accounts.get(key)

    async def execute(self, stmt):
        return _Result(list(self.accounts.values()))


class _S:
    def __init__(self, dry_run, demo=5, live=7):
        self.dry_run, self.mt5_demo_account_id, self.mt5_account_id = dry_run, demo, live


@pytest.fixture
def toggles(monkeypatch):
    state: dict = {}

    async def _ids(db, venue):
        return dict(state)

    monkeypatch.setattr("app.services.crypto_accounts.live_enabled_ids", _ids)
    return state


def _accounts():
    return [_Acct(5, "demo"), _Acct(7, "live"), _Acct(8, "live"), _Acct(9, "demo")]


@pytest.mark.asyncio
async def test_live_off_trades_every_demo_account_and_no_live_one(toggles):
    toggles.update({7: True, 8: True})
    routing = await execution.mt5_targets(_DB(_accounts()), _S(dry_run=True))
    assert [a.id for a in routing["targets"]] == [5, 9]


@pytest.mark.asyncio
async def test_live_on_adds_only_the_live_accounts_switched_on(toggles):
    toggles.update({7: True, 8: False})
    routing = await execution.mt5_targets(_DB(_accounts()), _S(dry_run=False))
    assert [a.id for a in routing["targets"]] == [5, 9, 7]


@pytest.mark.asyncio
async def test_every_enabled_live_account_trades_not_just_the_configured_one(toggles):
    toggles.update({7: True, 8: True})
    routing = await execution.mt5_targets(_DB(_accounts()), _S(dry_run=False))
    assert {a.id for a in routing["lives"]} == {7, 8}


@pytest.mark.asyncio
async def test_switching_the_configured_live_account_off_stops_it(toggles):
    toggles.update({7: False})
    routing = await execution.mt5_targets(_DB(_accounts()), _S(dry_run=False))
    assert 7 not in [a.id for a in routing["targets"]]


@pytest.mark.asyncio
async def test_one_failing_account_does_not_stop_the_others(monkeypatch, toggles):
    toggles.update({7: True})
    placed = []

    async def _place(db, account, s, **kw):
        if account.id == 5:
            return {"venue": "mt5", "status": "error", "reason": "broker down"}
        placed.append(account.id)
        return {"venue": "mt5", "status": "placed", "ticket": "T"}

    monkeypatch.setattr(execution, "_place_on", _place)

    class _Policy(_S):
        allow_mt5, allow_crypto, allow_sim = True, False, False

    orders = await execution._route(
        _DB(_accounts()), _Policy(dry_run=False), symbol="XAUUSD", action="buy", price=100.0,
        stop_loss=99.0, take_profit=103.0, level_source="OBLIVION plan",
    )
    assert placed == [9, 7]
    assert [o["status"] for o in orders].count("error") == 1


@pytest.mark.asyncio
async def test_demo_accounts_cannot_be_switched_off():
    from fastapi import HTTPException

    from app.api.room_trading import LiveToggle, set_live
    from app.models.database import CryptoAccount

    class _CryptoDB:
        async def get(self, model, key):
            return CryptoAccount(id=key, name="demo", exchange="bitget", account_type="demo",
                                 api_key_enc="k", api_secret_enc="s")

    with pytest.raises(HTTPException) as exc:
        await set_live("crypto", 3, LiveToggle(enabled=False), db=_CryptoDB())
    assert exc.value.status_code == 400
