"""Writable state may be mounted separately from application sources."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("IACPAAS_DATA_DIR", str(ROOT / ".local")))
LOGS = Path(os.getenv("IACPAAS_LOG_DIR", str(ROOT / "logs")))
ENV_FILE = Path(os.getenv("IACPAAS_ENV_FILE", str(ROOT / ".env")))


def ollama_url():
    return os.getenv("OLLAMA_URL") or ("http://127.0.0.1:11435" if os.name == "nt" else "http://127.0.0.1:11434")


def load_environment(override=False):
    from dotenv import load_dotenv, dotenv_values
    if os.getenv('ATLAS_DOCKER','false').lower()=='true':
        # Runtime login may change these; deployment passwords/hosts stay owned by Compose.
        for name,value in dotenv_values(ENV_FILE).items():
            if name in {'IACPAAS_BACKEND','IACPAAS_KNOWN_IRS','IACPAAS_JWT','IACPAAS_API_KEY'} and value is not None:
                os.environ[name]=value
    else:
        load_dotenv(ENV_FILE,override=override)
