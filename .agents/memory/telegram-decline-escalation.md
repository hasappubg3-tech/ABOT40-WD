---
name: Telegram bot decline-escalation flow
description: How the forced-subscription "لا لاحقاً" 5-stage decline ladder works across bot/callback_handlers.py, bot/features.py, bot/content_delivery.py — read before modifying notif_* logic.
---

## Key structural facts (non-obvious from a quick skim)
- The decline counter and any "force next gate" / "file block" state must live in MongoDB `user_stats`, not `ctx.user_data` — python-telegram-bot's `ctx.user_data` here is in-memory only (no `PicklePersistence`/persistence configured in this project), so anything stored there is lost on every restart. This matters a lot for a multi-stage escalation ladder that's supposed to survive across sessions.
- The decline chain has two different lying sub-paths that both terminate at "stage 4 severity": (1) direct decline via the "كافي لا تلح" button (`notif_anger_`), and (2) the channel-recheck loop (`notif_chan_` → `notif_check_` → `notif_check2_`) reachable from *both* stage 3 and stage 4 messages, which ends with a popup if the user lies twice about subscribing. When a request says "on the Nth time change message X", check both paths — the same user-facing wording may appear in more than one handler.
- "Show the next escalation stage immediately, not after waiting for the periodic re-trigger counter (`notif_every_opens`)" requires a one-shot "force" flag consumed inside `should_notify()`, since the periodic gate is otherwise driven purely by an opens-count delta (`opens - last_notif_opens >= every_opens`).

## Design pattern used for a hard content block
When a feature needs to "block user X from receiving files for T time", the check must be placed at the top of the actual content-serving path (`send_items` in `content_delivery.py`), not only in the callback handler that triggers the block — otherwise the block is bypassed by any other route into content delivery (e.g. compound button children, quiz/exam flows reusing `send_items`).
