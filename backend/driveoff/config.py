import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "driveoff.db"))
EVIDENCE_DIR = DATA_DIR / "evidence"
CLIPS_DIR = DATA_DIR / "clips"
CACHE_DIR = DATA_DIR / "cache"
for d in (EVIDENCE_DIR, CLIPS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_API_BASE = os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
# "mock" runs without PayPal (tests, offline dev). Anything else talks to the sandbox.
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox" if PAYPAL_CLIENT_ID else "mock")
if PAYPAL_MODE != "mock" and "sandbox" not in PAYPAL_API_BASE:
    raise RuntimeError("Drive-Off only runs against the PayPal sandbox.")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
PLATE_HASH_SALT = os.environ.get("PLATE_HASH_SALT", "dev-salt")

# Safety thresholds for automatic charging (see docs/SPIKES.md, spike 3)
AUTO_MIN_CONF = float(os.environ.get("AUTO_MIN_CONF", "0.90"))
AUTO_MIN_AGREE = int(os.environ.get("AUTO_MIN_AGREE", "3"))
EVIDENCE_RETENTION_DAYS = int(os.environ.get("EVIDENCE_RETENTION_DAYS", "30"))
CURRENCY = "USD"
