#!/usr/bin/env python3
"""
Bitget account diagnostic.

Investigates a live Bitget account against the v2 API to answer:
  • Is the account unified (multi-assets / assetMode "union") or classic?
  • Which futures product types (USDT/USDC/COIN) actually hold positions?
  • Are there linked sub-accounts, and can this API key read them?
  • Does the classic /api/v2/mix/* path work, or is full UTA (/api/v2/uta/*) needed?

Reads credentials from the same settings the backend uses (BITGET_API_KEY /
BITGET_API_SECRET / BITGET_PASSPHRASE via app.core.config). Read-only — makes
no orders or state changes.

Run with the backend venv:
    backend/.venv/bin/python3 scripts/diagnose_bitget_account.py
"""
import asyncio
import sys
from pathlib import Path

# Make the backend package importable when run from the repo root.
BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import settings  # noqa: E402
from app.exchanges.bitget_sdk import BitgetClient, BitgetAPIError  # noqa: E402

PRODUCT_TYPES = ("USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES")


def _hr(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


async def _safe(label: str, coro):
    """Await a coroutine, printing a compact result or the error."""
    try:
        data = await coro
        return data
    except BitgetAPIError as e:
        print(f"  [{label}] Bitget error {e.code}: {e.message}")
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] error: {e}")
    return None


async def main() -> int:
    if not (settings.BITGET_API_KEY and settings.BITGET_API_SECRET and settings.BITGET_PASSPHRASE):
        print("BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE not configured in .env")
        return 1

    client = BitgetClient(
        api_key=settings.BITGET_API_KEY,
        api_secret=settings.BITGET_API_SECRET,
        passphrase=settings.BITGET_PASSPHRASE,
    )

    try:
        _hr("1. Spot account info (main account identity)")
        info = await _safe("account-info", client.get_account_info())
        if info:
            d = info.get("data") or {}
            print(f"  userId={d.get('userId') or d.get('userID')}  "
                  f"channelCode={d.get('channelCode', '')}  "
                  f"ips={d.get('ips', '')}  authorities={d.get('authorities', '')}")

        _hr("2. All-account balance (every account type, USDT)")
        allbal = await _safe("all-account-balance", client.get_all_account_balance())
        if allbal:
            total = 0.0
            for row in (allbal.get("data") or []):
                usdt = float(row.get("usdtBalance") or 0)
                total += usdt
                print(f"  {row.get('accountType', '?'):>10}: {usdt:.4f} USDT")
            print(f"  {'TOTAL':>10}: {total:.4f} USDT")

        _hr("3. Futures accounts per product type (assetMode = union → UNIFIED)")
        detected_mode = None
        for pt in PRODUCT_TYPES:
            res = await _safe(f"accounts:{pt}", client.get_futures_accounts(product_type=pt))
            if not res:
                continue
            for row in (res.get("data") or []):
                mode = row.get("assetMode")
                detected_mode = detected_mode or mode
                print(f"  {pt:>13} coin={row.get('marginCoin', '?'):>5} "
                      f"assetMode={mode} usdtEquity={row.get('usdtEquity', '0')} "
                      f"available={row.get('available', '0')} "
                      f"unionTotalMargin={row.get('unionTotalMargin') or row.get('unionTotalMagin', '')} "
                      f"unionMm={row.get('unionMm', '')}")
        print(f"\n  >>> Detected account mode: "
              f"{'UNIFIED (multi-assets)' if detected_mode == 'union' else detected_mode or 'unknown'}")

        _hr("4. Open positions per product type")
        grand = 0
        for pt in PRODUCT_TYPES:
            res = await _safe(f"positions:{pt}", client.get_futures_positions(product_type=pt))
            if not res:
                continue
            rows = [p for p in (res.get("data") or []) if float(p.get("total") or 0) != 0]
            grand += len(rows)
            print(f"  {pt}: {len(rows)} open position(s)")
            for p in rows:
                print(f"      {p.get('symbol')} {p.get('holdSide')} total={p.get('total')} "
                      f"lev={p.get('leverage')} mode={p.get('marginMode')} "
                      f"posMode={p.get('posMode')} uPnL={p.get('unrealizedPL')}")
        print(f"\n  >>> Total open positions across all product types: {grand}")

        _hr("5. Sub-accounts (linked accounts detection)")
        listing = await _safe("virtual-subaccount-list", client.get_virtual_subaccount_list(limit=500))
        if listing:
            subs = (listing.get("data") or {}).get("subAccountList") or []
            print(f"  virtual sub-accounts: {len(subs)}")
            for s in subs:
                print(f"      uid={s.get('subAccountUid')} name={s.get('subAccountName')} "
                      f"status={s.get('status')} perms={s.get('permList')}")
        for pt in PRODUCT_TYPES:
            res = await _safe(f"sub-assets:{pt}", client.get_sub_account_futures_assets(product_type=pt))
            if res:
                subs = res.get("data") or []
                print(f"  sub-account futures assets [{pt}]: {len(subs)} sub-account(s)")

        _hr("Summary")
        print(f"  Account mode : {'UNIFIED (union)' if detected_mode == 'union' else detected_mode or 'unknown'}")
        print(f"  Open positions found : {grand}")
        print("  If positions appear only under non-USDT product types, the old")
        print("  USDT-FUTURES-only code was the reason they were invisible.")
    finally:
        await client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
