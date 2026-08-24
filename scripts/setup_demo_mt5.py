"""
One-shot script: register the MetaQuotes Demo account (10011555655) in TradeBot,
and set it as the default demo account in Room Settings.

Usage:
    cd /Users/sakhilematsimela/Sites/tradebot
    source .venv/bin/activate
    python scripts/setup_demo_mt5.py --password YOUR_MT5_PASSWORD

The script:
  1. Upserts an MT5Account row (type=demo, login=10011555655, server=MetaQuotes-Demo).
  2. Sets mt5_live_mode=False and mt5_demo_account_id on RoomSettings (row 1).
  3. Sets mt5_demo_account_id + mt5_demo_execute=True on TelegramSniperSettings.
  4. Prints the account ID so you can reference it in the UI.

Re-running is safe — the account row is updated in-place.
"""
import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def main(password: str, dry_run: bool = False) -> None:
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # ── 1. Upsert MT5 demo account ────────────────────────────────────
        try:
            from plugins.MT5TradingPlugin.backend.models import (
                MT5Account, MT5AccountType, MT5AccountStatus,
            )
        except ImportError:
            print("ERROR: MT5TradingPlugin is not installed. Aborting.")
            return

        LOGIN = "10011555655"
        SERVER = "MetaQuotes-Demo"
        NAME = "MetaQuotes Demo"

        result = await db.execute(
            select(MT5Account).where(MT5Account.login == LOGIN, MT5Account.server == SERVER)
        )
        acct = result.scalars().first()

        if acct is None:
            acct = MT5Account(
                name=NAME,
                server=SERVER,
                login=LOGIN,
                password_encrypted=password,
                account_type=MT5AccountType.DEMO,
                status=MT5AccountStatus.DISCONNECTED,
                user_id=1,
            )
            db.add(acct)
            await db.flush()
            print(f"✅ Created MT5 demo account (id={acct.id}): {LOGIN} @ {SERVER}")
        else:
            acct.name = NAME
            acct.password_encrypted = password
            acct.account_type = MT5AccountType.DEMO
            print(f"♻️  Updated existing MT5 demo account (id={acct.id}): {LOGIN} @ {SERVER}")

        acct_id = acct.id

        # ── 2. Room Settings ──────────────────────────────────────────────
        from app.models.database import RoomSettings
        rs = await db.get(RoomSettings, 1)
        if rs is None:
            rs = RoomSettings(id=1)
            db.add(rs)
        rs.mt5_live_mode = False          # default: demo
        rs.mt5_demo_account_id = acct_id
        rs.allow_mt5 = True
        rs.execution_enabled = True
        rs.dry_run = True                 # keep dry-run until user confirms
        print(f"📋 RoomSettings: mt5_live_mode=False, mt5_demo_account_id={acct_id}, dry_run=True")

        # ── 3. Telegram Sniper Settings ───────────────────────────────────
        try:
            from plugins.TelegramSignalNewsPlugin.backend.models import TelegramSniperSettings
            result2 = await db.execute(select(TelegramSniperSettings).limit(1))
            ss = result2.scalars().first()
            if ss is None:
                ss = TelegramSniperSettings()
                db.add(ss)
            ss.mt5_execute = True
            ss.mt5_demo_account_id = acct_id
            ss.mt5_demo_execute = True          # route forex signals to demo
            ss.force_telegram_signals = True    # never skip a Telegram channel signal
            ss.multi_tp_execute = True          # place TP1/TP2/TP3 as separate orders
            ss.enabled = True
            print(f"📡 TelegramSniperSettings: force_telegram_signals=True, mt5_demo_execute=True")
        except Exception as e:
            print(f"⚠️  TelegramSniperSettings update skipped: {e}")

        if dry_run:
            await db.rollback()
            print("\n🔍 DRY RUN — no changes written.")
        else:
            await db.commit()
            print(f"\n✅ Done! Demo account id={acct_id}.")
            print("   Next step: go to Room Settings → MT5 → set 'Demo account' to this account.")
            print("   Then turn dry_run=False when you're ready to place real orders.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register MetaQuotes Demo account in TradeBot")
    parser.add_argument("--password", required=True, help="MT5 account password")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    args = parser.parse_args()
    asyncio.run(main(args.password, args.dry_run))
