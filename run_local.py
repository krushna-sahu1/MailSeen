"""Run MailSeen locally with the settings in .env (python run_local.py)."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

from app import app  # noqa: E402  (must import after the environment is set)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
