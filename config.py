"""Configuration and business policy."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# Minimal .env reader, avoids a python-dotenv dependency.
ENV = BASE_DIR / ".env"
if ENV.exists():
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-latest")
# Triage runs on the cheap model: it is one call on every single turn.
ROUTER_MODEL = os.getenv("MISTRAL_ROUTER_MODEL", "mistral-small-latest")
API_KEY = os.getenv("MISTRAL_API_KEY", "")

# Shown in every client, always visible: the assistant is not a human advisor.
DISCLAIMER = ("Cet assistant utilise l'IA et peut commettre des erreurs. "
              "En cas de doute, contactez votre conseiller.")

# Access levels: a session starts PUBLIC, `/login` simulates strong auth.
PUBLIC = "public"
CUSTOMER = "customer"

# Agent roles. The router picks one per turn; each one sees a different slice of
# the tool registry, so a mis-route cannot widen the action space beyond its scope.
KNOWLEDGE = "knowledge"
TRANSACTION = "transaction"

# Policy thresholds: code, not prompt text, so they stay testable.
MAX_CARD_LIMIT_EUR = 8000.0
MAX_LIMIT_INCREASE_RATIO = 2.0

# SDK backoff on 429/5xx; past MAX_ELAPSED the turn surfaces a clean error.
RETRY_INITIAL_MS = 500
RETRY_MAX_INTERVAL_MS = 8_000
RETRY_MAX_ELAPSED_MS = 30_000
RETRY_EXPONENT = 2.0
