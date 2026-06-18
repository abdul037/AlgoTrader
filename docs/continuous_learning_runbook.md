# Continuous-Learning Runbook

The learning layer is governed and fail-safe. It captures evidence by default,
but reviews, OpenAI calls, training, model gating, and automatic paper
promotion remain disabled until their rollout gates are signed.

The learner may only reduce, reorder, or reject deterministic candidates. It
cannot create eligibility, change order size, alter exits, select a broker,
relax risk controls, or affect the kill switch. Missing features, corrupt
artifacts, excessive schema drift, and inference failures fall back to the
deterministic score.

## Railway Services

Use the same repository and Supabase PostgreSQL database for separate Railway
services:

- API service: `sh scripts/start_railway.sh`
- Learning worker: `python scripts/run_learning_worker.py`
- Nightly training cron: `python scripts/run_learning_job.py nightly`
- Weekly evaluation cron: `python scripts/run_learning_job.py weekly`
- Daily Telegram digest cron: `python scripts/run_learning_job.py digest`

Configure Railway cron schedules in UTC. The always-on worker uses the
PostgreSQL-backed idempotent job queue and retries failed jobs up to
`LEARNING_JOB_MAX_ATTEMPTS`.

## Staged Flags

Capture-only:

```env
LEARNING_CAPTURE_ENABLED=true
LEARNING_WORKER_ENABLED=false
LEARNING_REVIEWS_ENABLED=false
LEARNING_OPENAI_ENABLED=false
LEARNING_TRAINING_ENABLED=false
LEARNING_AUTO_PROMOTE_PAPER_ENABLED=false
MODEL_DEPLOYMENT_MODE=shadow
```

Advisory review stage:

```env
LEARNING_WORKER_ENABLED=true
LEARNING_REVIEWS_ENABLED=true
LEARNING_OPENAI_ENABLED=true
MODEL_DEPLOYMENT_MODE=shadow
```

Shadow-training stage:

```env
LEARNING_TRAINING_ENABLED=true
MODEL_DEPLOYMENT_MODE=shadow
```

Do not set `MODEL_DEPLOYMENT_MODE=gating` or
`LEARNING_AUTO_PROMOTE_PAPER_ENABLED=true` until at least 20 clean shadow
sessions and every promotion threshold pass. Live promotion always requires a
signed protected API request.

## Private Artifacts

Create a private Supabase Storage bucket named `algobot-learning-models`.
Configure the service-role key only in Railway:

```env
LEARNING_SUPABASE_STORAGE_ENABLED=true
LEARNING_SUPABASE_URL=https://PROJECT_REF.supabase.co
LEARNING_SUPABASE_SERVICE_KEY=
LEARNING_SUPABASE_BUCKET=algobot-learning-models
```

Artifacts are addressed by SHA-256 hash and verified before inference. Never
expose the Supabase service-role key to a browser or commit it.

## OpenAI Critic

The critic uses the Responses API with strict structured output, `store=false`,
and no tools. Evidence is sanitized before submission. OpenAI failures never
block trading or reconciliation.

```env
LEARNING_OPENAI_API_KEY=
LEARNING_TRADE_CRITIC_MODEL=gpt-5.4-mini
LEARNING_WEEKLY_SYNTHESIS_MODEL=gpt-5.5
LEARNING_OPENAI_DAILY_BUDGET_USD=5
```

## Operations

Read-only endpoints:

- `GET /learning/status`
- `GET /learning/reviews`
- `GET /learning/models`
- `GET /learning/experiments`
- `GET /learning/drift`

Protected actions require `X-Control-Token`:

- `POST /learning/reviews/{execution_id}/retry`
- `POST /learning/jobs/process`
- `POST /learning/models/{model_id}/promote`
- `POST /learning/models/{model_id}/rollback`

Telegram commands:

- `/learning_status`
- `/trade_review EXECUTION_ID`
- `/learning_digest`
- `/model_status`

Rollback immediately on excessive drift, negative champion expectancy,
integrity failure, or unresolved reconciliation issues. Keep all historical
evidence immutable; regime weighting and model retirement replace deletion.
