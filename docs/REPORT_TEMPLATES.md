# Message and Report Templates

**Version:** 1.0 (September 30, 2026) · Used by A10 and A11

All numbers are filled by code from the journal. Qwen writes only the fields marked *(narrative)*, each at most 3 sentences (AGENT_PROMPTS.md §7). Every message starts with the mode tag `[PAPER]` or `[LIVE]` (SEC-7). Example values below are illustrative.

---

## 1. Pre-flight (7:00 AM)

```
[PAPER] Pre-flight Thu Oct 15 — GREEN
Alpaca ok · data ok · Qwen ok (p95 9.8s) · clock +0.2s · reconcile ok
Equity $25,412 (HWM $25,610, -0.8%) · risk/trade 0.50%
Today: no CPI or jobs report · FOMC no (next: Oct 28)
Locked: META (wash sale to Nov 12), TSLA (earnings Oct 21)
```

## 2. Game plan (9:00 AM and 2:45 PM)

```
[PAPER] Morning plan Thu Oct 15
Market: QQQ +0.4% pre-market, above prior close — with us
Watch (max 4):
 1. NVDA  ORB         trigger 190.40  stop 189.61  T1 191.37  — gap +1.2% on supplier news
 2. AMZN  VWAP_RECLAIM watch VWAP     stop dip low — strong close yesterday
Off-limits: META (wash lock), TSLA (earnings 10/21), SPCX (history check pending)
Trades left today: 3 · Daily loss budget: $381
```

## 3. Approval request (approve mode)

```
[PAPER] APPROVE? NVDA ORB (planned)  code K7Q2
Buy 32 @ ≤190.49  stop 189.61  T1 191.37 (16)  T2 192.25 (16)
Risk $28 (0.11%) · R:R 2.0 · conf 0.72 · review: agree
Thesis (narrative): Broke the 5-min range on 1.9x volume with QQQ above VWAP...
Reply YES K7Q2 or NO K7Q2 within 90s. No reply = no trade.
```

## 4. Trade events

```
[PAPER] FILLED NVDA 32 @ 190.47 (slip -0.02) · stops live at Alpaca
[PAPER] T1 NVDA 16 @ 191.37 (+1.0R) · stop -> 190.47 (break-even)
[PAPER] CLOSED NVDA 16 @ 191.87 trail (+1.6R) · trade +1.3R, +$36.80
[PAPER] HALT: daily loss limit (-1.5%) — no more entries today
```

## 5. Morning summary (10:30) and daily review (4:30 PM)

```
[PAPER] Daily review Thu Oct 15
P&L +$8.80 (+0.03%) · 2 trades (1W 1L) · +0.3R total · max intraday DD -0.3%
Trades:
 NVDA ORB      morning  +1.3R  grade A  exit trail   +$36.80
 AMZN VWAP_REC morning  -1.0R  grade B  exit stop   -$28.00 (entry 0.2 ATR above trigger)
No-trade log: 6 setups rejected — MF-1 x3, CK-6 chase x2, A6 disagree x1
Rule breaches: none · Commands: YES x2 · Rejected senders: none
Locks: AMZN wash-sale until Nov 15 — avoid in other accounts (IRA)
What went well (narrative): ...
What to fix (narrative): ...
Tomorrow focus (narrative): ...
```

**Trade grade:** A = all checklist items passed with margin and plan followed; B = passed but at least one item near its limit (for example chase > 0.15 ATR5); C = unplanned or a rule exception. Grades come from code, not the model.

## 6. Weekly review (Friday 5:00 PM)

| Section | Contents |
| --- | --- |
| Results | Trades, win rate, expectancy (R), profit factor, P&L, drawdown, vs performance standards (REQUIREMENTS.md 6.4) |
| By setup | Trades, expectancy, last-20 expectancy, status (live / paper / disabled per RC-3) |
| By window and ticker | Expectancy morning vs afternoon; per ticker |
| Process | Rule breaches, overrides (DR-8), approval response times, missed approvals |
| Pipeline | Setups → proposals → reviews → fills, top rejection reasons |
| Locks and tax | Active wash-sale locks, deferred losses (balanced policy only) |
| Decisions needed | Setups to return to paper; config changes proposed (applied only by the owner) |

## 7. Monthly review

Weekly contents for the month, plus: equity curve, monthly drawdown vs LL-6, tax-lot summary and wash-sale report (A12-3), key ages and security check (SEC-16), and the go-live / auto-mode gate status.
