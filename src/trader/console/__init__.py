"""Owner Console (docs/UI_REQUIREMENTS.md): a local web app on 127.0.0.1:8080.

Reads the journal with the ``console_rw`` role and live values from trader-core's internal
read-only API; every action is an owner command published to the outbox (``inbound_cmd``,
source ``console``) and goes through the same CommandAuthority as Telegram (UI-C2, UI-C3).
"""
