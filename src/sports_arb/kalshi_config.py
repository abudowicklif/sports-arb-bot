"""Environment-only Kalshi production credentials and explicit live arming."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from kalshi_python_async.auth import KalshiAuth

from sports_arb.errors import SportsArbError
from sports_arb.live_config import KILL_FILE, LIVE_PHRASE

KALSHI_LIVE_ENV_FLAG = "KALSHI_LIVE_TRADING"
KALSHI_LIVE_CONFIRM_ENV = "KALSHI_LIVE_CONFIRM"
KALSHI_API_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


class KalshiConfigError(SportsArbError):
    pass


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    key_id: str
    private_key_path: Path
    private_key_pem: str

    @classmethod
    def from_env(cls) -> KalshiCredentials:
        key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        path_raw = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id or not path_raw:
            raise KalshiConfigError(
                "missing environment variables: KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY_PATH"
            )
        path = Path(path_raw).expanduser().resolve()
        if not path.is_file():
            raise KalshiConfigError(f"Kalshi private key file does not exist: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise KalshiConfigError(
                f"Kalshi private key {path} is accessible by group/others; chmod 600 it"
            )
        try:
            pem = path.read_text(encoding="utf-8")
            # The official SDK detects RSA versus Ed25519 and validates the PEM.
            KalshiAuth(key_id, pem)
        except (ValueError, TypeError, OSError) as exc:
            raise KalshiConfigError(
                "KALSHI_PRIVATE_KEY_PATH must contain an unencrypted RSA or Ed25519 PEM"
            ) from exc
        return cls(key_id=key_id, private_key_path=path, private_key_pem=pem)


def assert_kalshi_live_armed(confirmation: str) -> None:
    if os.environ.get(KALSHI_LIVE_ENV_FLAG) != "YES":
        raise KalshiConfigError(
            f"set {KALSHI_LIVE_ENV_FLAG}=YES to arm real Kalshi order submission"
        )
    if os.environ.get(KALSHI_LIVE_CONFIRM_ENV) != LIVE_PHRASE:
        raise KalshiConfigError(f"set {KALSHI_LIVE_CONFIRM_ENV} to the required risk phrase")
    if confirmation != "REAL FUNDS":
        raise KalshiConfigError("live command requires --confirm-live 'REAL FUNDS'")
    if KILL_FILE.exists():
        raise KalshiConfigError(
            f"kill switch is active at {KILL_FILE}; inspect/reconcile before removing it"
        )
