# Anti-overtrading audit — Phase 1 Slice B

Date: 2026-09-21. Scope: `pack/` quests, achievements, XP, and bot copy.

## Result

No quest, achievement, or XP rule rewards more trades, PnL, leverage, size, or win rate. NO TRADE remains worth more than I TOOK THIS (25 vs 20). Journal XP is 30 for any self-reported result, including LOSS. Extra `pnl`, `leverage`, and `size` fields on a journal are ignored.

## What was searched

Patterns: `take N trades`, `most trades`, `win rate`, `leverage`, `pnl`, `highest pnl`, `profit multiplier`.

Hits that remain are bans, tests that prove those fields do not change XP, or copy that refuses the reward:

- `pack/apps/api/app/domain/board.py` — the banned-reward check and the line "never ask for a trade, a profit, leverage, or a win rate."
- `pack/apps/api/tests/test_phase1_api.py` and `test_slice_b.py` — journal bodies include `pnl` and `leverage` and still award the same process XP.
- `pack/apps/web/src/copy.ts`, `JournalPanel.tsx`, `XpPreview.tsx`, `ReplayScreen.tsx`, `ReplayDrill.tsx` — disclaimers. The locked tagline still says profit is the outcome and discipline is the game.
- `pack/docs/PRODUCT_ARCHITECTURE.md` — the ban list itself.
- Weekly replay drill copy says a drill score is not a profit record.

`templates_are_process_only()` returns no hits. The test `test_quest_templates_are_deterministic_and_process_only` locks that in.

## Quest and achievement set

Daily drills: read and lock, NO TRADE, journal, Replay, evidence read. Three are surfaced per user per UTC day.

Weekly drills: three journals, two NO TRADE locks, five Replays, discipline streak of 5. Two are surfaced per ISO week.

Achievements A01–A18 are the architecture discipline list (First Lock, Clean Pass, Patient Scout, No Chase, Pack Oath, and the rest). None count I TOOK THIS, PnL, leverage, or win rate. Replay `TAKE` is a training label on a closed tape, not a live quest.

## Not a violation

The word "profit" appears in the locked tagline and in "not a profit record" / "not proof of PnL" disclaimers. Those sentences do not award XP.
