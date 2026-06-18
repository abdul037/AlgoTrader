"""Validate safety-critical Railway deployment environment variables."""

from __future__ import annotations

import os


def as_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def validate() -> list[str]:
    errors: list[str] = []
    database_url = os.environ.get("DATABASE_URL", "")
    stage = os.environ.get("DEPLOYMENT_STAGE", "").strip().lower()
    paper_auto_mode = os.environ.get("PAPER_AUTO_OPERATION_MODE", "").strip().lower()

    if not database_url.startswith("postgresql+psycopg://"):
        errors.append("DATABASE_URL must use postgresql+psycopg")
    if "sslmode=require" not in database_url:
        errors.append("DATABASE_URL must require TLS")
    if stage not in {"bootstrap", "shadow", "supervised", "unattended"}:
        errors.append("DEPLOYMENT_STAGE must be bootstrap, shadow, supervised, or unattended")
    if paper_auto_mode not in {"shadow", "supervised", "unattended"}:
        errors.append("PAPER_AUTO_OPERATION_MODE must be shadow, supervised, or unattended")
    if len(os.environ.get("CONTROL_API_TOKEN", "")) < 32:
        errors.append("CONTROL_API_TOKEN must contain at least 32 characters")
    if os.environ.get("EXECUTION_MODE", "").strip().lower() != "paper":
        errors.append("EXECUTION_MODE must be paper")
    if as_bool("ENABLE_REAL_TRADING"):
        errors.append("ENABLE_REAL_TRADING must be false")
    if not as_bool("ALPACA_ENABLED"):
        errors.append("ALPACA_ENABLED must be true")
    if not os.environ.get("ALPACA_API_KEY"):
        errors.append("ALPACA_API_KEY is required")
    if not os.environ.get("ALPACA_SECRET_KEY"):
        errors.append("ALPACA_SECRET_KEY is required")
    alpaca_base_url = os.environ.get("ALPACA_BASE_URL", "").rstrip("/")
    if alpaca_base_url.removesuffix("/v2") != "https://paper-api.alpaca.markets":
        errors.append("ALPACA_BASE_URL must use Alpaca Paper")
    if os.environ.get("ALPACA_EXPECTED_ACCOUNT_NUMBER", "") != "PA3B287XBZYU":
        errors.append("ALPACA_EXPECTED_ACCOUNT_NUMBER must be PA3B287XBZYU")
    if not as_bool("ALPACA_RECONCILIATION_ENABLED"):
        errors.append("ALPACA_RECONCILIATION_ENABLED must be true")
    if not as_bool("ALPACA_REQUIRE_BRACKET_ORDERS"):
        errors.append("ALPACA_REQUIRE_BRACKET_ORDERS must be true")
    if as_bool("PAPER_SIMULATED_FALLBACK_ENABLED"):
        errors.append("PAPER_SIMULATED_FALLBACK_ENABLED must be false")
    if as_bool("LEARNING_OPENAI_ENABLED") and not as_bool("LEARNING_REVIEWS_ENABLED"):
        errors.append("LEARNING_OPENAI_ENABLED requires LEARNING_REVIEWS_ENABLED")
    if as_bool("LEARNING_AUTO_PROMOTE_PAPER_ENABLED") and not as_bool("LEARNING_TRAINING_ENABLED"):
        errors.append("LEARNING_AUTO_PROMOTE_PAPER_ENABLED requires LEARNING_TRAINING_ENABLED")
    if as_bool("LEARNING_LIVE_PROMOTION_ENABLED"):
        errors.append("LEARNING_LIVE_PROMOTION_ENABLED must remain false in Railway paper deployment")
    if (
        os.environ.get("MODEL_DEPLOYMENT_MODE", "shadow").strip().lower() == "gating"
        and not as_bool("LEARNING_TRAINING_ENABLED")
    ):
        errors.append("MODEL_DEPLOYMENT_MODE=gating requires LEARNING_TRAINING_ENABLED")

    if stage in {"bootstrap", "shadow"}:
        required_true = {"SCREENER_SCHEDULER_ENABLED"}
        required_false = {
            "PAPER_AUTO_APPROVE_PROPOSALS",
            "AUTO_EXECUTION_WORKER_ENABLED",
            "AUTO_PROPOSE_ENABLED",
            "AUTO_EXECUTE_AFTER_APPROVAL",
            "LEARNING_TRAINING_ENABLED",
            "LEARNING_AUTO_PROMOTE_PAPER_ENABLED",
            "LEARNING_LIVE_PROMOTION_ENABLED",
        }
        errors.extend(f"{name} must be true in {stage} mode" for name in required_true if not as_bool(name))
        errors.extend(f"{name} must be false in {stage} mode" for name in required_false if as_bool(name))
        if paper_auto_mode != "shadow":
            errors.append(f"PAPER_AUTO_OPERATION_MODE must be shadow in {stage} mode")
        if as_bool("INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED"):
            errors.append(f"INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED must be false in {stage} mode")
        if os.environ.get("MODEL_DEPLOYMENT_MODE", "shadow").strip().lower() != "shadow":
            errors.append(f"MODEL_DEPLOYMENT_MODE must be shadow in {stage} mode")

    if stage in {"supervised", "unattended"}:
        required_true = {
            "PAPER_AUTO_APPROVE_PROPOSALS",
            "AUTO_EXECUTION_WORKER_ENABLED",
            "INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED",
        }
        errors.extend(f"{name} must be true in {stage} mode" for name in required_true if not as_bool(name))
        if paper_auto_mode != stage:
            errors.append(f"PAPER_AUTO_OPERATION_MODE must be {stage} in {stage} mode")

    if stage == "bootstrap":
        required_true = {"AUTOMATION_PAUSED_DEFAULT", "KILL_SWITCH_ENABLED"}
        errors.extend(f"{name} must be true in bootstrap mode" for name in required_true if not as_bool(name))

    if stage == "shadow":
        required_false = {"AUTOMATION_PAUSED_DEFAULT", "KILL_SWITCH_ENABLED"}
        errors.extend(f"{name} must be false in shadow mode" for name in required_false if as_bool(name))

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"Railway environment error: {error}")
        return 1
    print("Railway environment validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
