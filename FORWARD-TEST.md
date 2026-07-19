# Forward-Test Erfolgskriterien

**Kriterien festgelegt: 2026-06-10, VOR Beginn der Datensammlung. Nicht nachträglich ändern.**

## Changelog
- **2026-06-29 — Exit-Logik umgebaut: festes 2-Stufen-Trailing → kontinuierliches
  ATR-Trailing N=2.5 (Backtest: Sharpe 1.38 vs 0.65, MaxDD −15.5% vs −83.7%, PF 1.61).
  Forward-Test NEU GESTARTET.** Materielle Änderung am Exit-Verhalten → die bisherigen
  Tage liefen auf der alten Exit-Logik und zählen nicht. Bewertung startet frisch ab 29.06.
- **2026-06-17 — Liquiditätsfilter IEX-Bug gefixt, Universum 29 → ~200, Test neu gestartet.**
  Volumen-Filter lief auf IEX-Volumen (~2-3% des Markts) → nur ~29 Mega-Caps. Auf 200k
  gesenkt → ~200 Aktien. Erste Tage (Start 10.06., +0,52%) zählten nicht.

## Zeitraum
6 Wochen Paper Trading: **2026-06-29 bis 2026-08-10**
(Start = erster voller Handelszyklus ab nächstem Handelstag)

## Erfolgskriterien (alle müssen erfüllt sein)

| # | Kriterium | Schwelle | Begründung |
|---|-----------|----------|------------|
| 1 | Sharpe Ratio | > 0.8 | Backtest zeigte 1.3 — Forward darf degradieren, aber muss klar positiv risk-adjusted sein |
| 2 | Max Drawdown | < 8% | Backtest -4.1%; Faktor 2 Toleranz, mehr = Risikomodell versagt |
| 3 | Profit Factor | > 1.3 | Backtest 3.25 ist fast sicher überzeichnet; 1.3 = real profitabel nach Slippage |
| 4 | Abgeschlossene Trades | ≥ 30 | Darunter keine statistische Aussage möglich |
| 5 | System-Uptime | kein Ausfall > 24h unbemerkt | Heartbeat + Telegram müssen Ausfälle melden |
| 6 | Stop-Coverage | 100% jederzeit | Keine Position ohne aktiven Stop (reconcile_stops) |

## Erwartung (ehrlich)
- Backtest: +21% / 419 Tage ≈ +2.9% in 6 Wochen. **Forward-Realität: vermutlich weniger.**
- Akzeptabel: jede positive Rendite bei Sharpe > 0.8.
- Win-Rate ~30% ist NORMAL für Trend-Following (wenige große Gewinner zahlen viele kleine Verluste). Niedrige Win-Rate allein ist KEIN Fehlschlag.

## Abbruchkriterien (sofort stoppen + analysieren)
- Drawdown > 10% gesamt
- 3+ Tage in Folge: Positionen ohne Stop-Coverage
- Claude-API > 48h offline ohne Alert

## Bekanntes Spannungsfeld (dokumentiert 2026-06-29)
Der N=2.5-Backtest hatte MaxDD **−15.5%** (auf gehebelter Notional-Basis) —
über dem 8%-Kriterium. Das 8%-Kriterium bleibt als Beobachtungsmarke bestehen,
wird aber im Forward-Test gegen die reale (kapital-begrenzte) Drawdown-Zahl
geprüft. Wenn der reale Forward-MaxDD deutlich unter dem Backtest-Wert liegt
(erwartet, da live max 15 Positionen statt unbegrenzter Notional), ist das
Kriterium haltbar. Falls real > 8%: bewusst entscheiden, nicht reflexartig nachjustieren.

## Entscheidung am 2026-08-10
- **Alle 6 Kriterien erfüllt** → Diskussion über nächste Phase (mehr Kapital-Simulation, Live-Erwägung)
- **1-2 verfehlt** → Ursachenanalyse, Parameter NICHT blind nachoptimieren, weitere 6 Wochen
- **3+ verfehlt** → Strategie-Grundannahmen prüfen, zurück ans Reißbrett

## Wochen-Check (jeden Montag)
```bash
cd ~/code/stockpilot && .venv/bin/python -c "
import asyncio
from storage.db import Database
async def t():
    db=Database(); await db.connect()
    cur=await db._db.execute('''SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),
        ROUND(SUM(pnl),2) FROM trades WHERE closed_at IS NOT NULL''')
    n,w,pnl=await cur.fetchone()
    print(f'Abgeschlossen: {n} | Wins: {w} | Realisiert: \${pnl}')
    await db.close()
asyncio.run(t())"
```
