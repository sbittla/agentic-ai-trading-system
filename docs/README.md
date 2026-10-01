# Documentation

| Document | What it covers | Read it when |
| --- | --- | --- |
| [REQUIREMENTS.md](REQUIREMENTS.md) | What the system must do: universe, trading windows, daily process, day-trader playbook, discipline rules, wash-sale process, every agent's requirements, acceptance tests | First; the source of truth |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Containers, repo layout, event flow, trade states, scheduling, failure handling | Before writing code |
| [DATA_SPEC.md](DATA_SPEC.md) | External sources and endpoints, indicator definitions, journal tables, message types | Building agents A1–A4 and the journal |
| [AGENT_PROMPTS.md](AGENT_PROMPTS.md) | Qwen prompts, JSON schemas, validation, evaluation set | Building agents A3, A5, A6, A11 |
| [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) | Every config field, validation rules, secrets | Changing any setting |
| [TEST_PLAN.md](TEST_PLAN.md) | Unit, replay, backtest, paper, live and chaos tests; go-live gates | Before paper and before live |
| [RUNBOOK.md](RUNBOOK.md) | Setup, daily routine, commands, incident playbooks, go-live checklist | Running the system |
| [DECISIONS.md](DECISIONS.md) | Why the main choices were made | Before changing a design choice |

Requirement IDs (for example `A7-2`, `WS-1`, `DR-3`) are used across all documents, in tests and in journal reason codes, so every behaviour can be traced from requirement to test to trade.
