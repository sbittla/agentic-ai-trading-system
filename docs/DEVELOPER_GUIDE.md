# Developer Guide

**Version:** 1.1 (September 30, 2026)

How to set up a development environment, the coding rules for this repo, and the safe way to change strategies, prompts and limits.

---

## 1. Local setup (Windows)

Nothing in this repo runs on the host: every service, helper and check runs in a container.

**Checks from WSL (repo root):** `./run.sh test` builds the `dev` image and runs `ruff`, `mypy`, `pytest` and the traceability check against the working tree (mounted at `/workspace`, with `PYTHONPATH=/workspace/src`). Extra pytest arguments pass through: `./run.sh test tests/unit/test_options.py -k payoff`.

**Editing in VS Code:** open the repo in the dev container (`.devcontainer/devcontainer.json`, *Reopen in Container*). It uses the same image as production (`dev` target) with Postgres and Ollama from Compose, so nothing but Docker Desktop and VS Code is installed on Windows.

Other containerized entry points:

```bash
./run.sh setup              # local files from the *.example templates (toolbox container)
./run.sh replay 10          # whole system on 10 synthetic days, no keys or GPU
./run.sh backtest 500       # playbook on real minute bars (BT-6)
docker compose -f docker/docker-compose.yml --profile replay run --rm replay replay --day 2026-10-15 --speed 20
```

Postgres integration tests run when the `TEST_PG_*` URLs are set (one per role, `postgresql+psycopg://<role>:<password>@postgres:5432/trader`); without them they are skipped.

## 2. Coding rules

- **DV-1:** Money is `Decimal`; time is timezone-aware (IF-1, IF-2).
- **DV-2:** Agents depend only on interfaces (INTERFACES.md); no agent imports `alpaca` or the HTTP client directly.
- **DV-3:** No agent may write to config or change a limit at runtime; config objects are frozen dataclasses.
- **DV-4:** Every rule in code cites its requirement ID in a comment and has a test marked `@pytest.mark.rule("<ID>")`.
- **DV-5:** Every rejection or exit sets a reason code from TRACEABILITY.md; free-text reasons are extra, not instead.
- **DV-6:** No `print`; structured logging with `structlog`, never logging secrets (SEC-6).
- **DV-7:** Pure functions for calculations (indicators, sizing, checklist) so they test without I/O.
- **DV-8:** Async I/O with explicit timeouts on every external call (IF-4).

## 3. Branches, commits, reviews

- `main` is always deployable to paper. Work on short branches: `feat/a07-sizing`, `fix/orb-narrow-range`.
- Commit messages: `<area>: <change> (<rule IDs>)`, for example `a07: halve risk in drawdown (RC-1, LL-1)`.
- Pull requests need green CI and, for anything touching `risk`, `wash_sale`, execution or prompts, a replay diff of the recorded-day library attached.
- Tag releases `vMAJOR.MINOR.PATCH`; the live system only runs tagged versions.

## 4. Safe change procedures

| Change | Procedure |
| --- | --- |
| New or changed setup | Update STRATEGY_SPEC.md (new version) → unit tests on labelled bars → replay library diff → backtest walk-forward → paper only for 30 trades (SC-2) |
| Prompt change | New prompt file version → evaluation set (AGENT_PROMPTS.md §8) → replay diff → paper |
| Model change (Qwen tag) | Pin new tag → evaluation set → latency check → replay diff → paper for 5 days |
| Risk limit loosened | Config commit with reason → 20 paper trades under the new value (CF-3) |
| Risk limit tightened | Config commit; applies next trading day |
| Broker adapter change | Contract tests (IF-5) → chaos tests → paper for 5 days |
| Fill model change (SimBroker) | Version bump → re-run calibration against paper fills |

## 5. Definition of done (any task)

- [ ] Code, tests and docs updated together; TRACEABILITY.md regenerated if rule IDs changed
- [ ] CI green, including the traceability check (from milestone M3)
- [ ] Replay library diff reviewed when behaviour changes
- [ ] No new secrets, no new network destinations without updating SECURITY.md
