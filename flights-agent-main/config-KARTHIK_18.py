"""Configuration module for T10 Flight Option Finder.

Loads environment variables safely and prevents credential leakage in logs.
"""

import os
import math
from pathlib import Path
from dotenv import load_dotenv

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment from .env file unless environment variables already exist.
# Keep local overrides from the process environment while preventing accidental
# clobbering of already-set system variables.
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)


class Config:
    """Application configuration with secret masking and validation."""

    def __init__(self):
        self.aviationstack_api_key = os.getenv("AVIATIONSTACK_API_KEY", "").strip()
        self.ignav_api_key = os.getenv("IGNAV_API_KEY", "").strip()
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", "").strip()
        self.nvidia_base_url = os.getenv(
            "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).strip()
        self.nvidia_model = os.getenv(
            "NVIDIA_MODEL", "nvidia/nemotron-3-nano-30b-a3b"
        ).strip()
        self.ignav_base_url = os.getenv(
            "IGNAV_BASE_URL", "https://ignav.com/api"
        ).strip()
        self.aviationstack_base_url = os.getenv(
            "AVIATIONSTACK_BASE_URL", "https://api.aviationstack.com/v1"
        ).strip()
        self.ranking_price_weight = self._ranking_weight("RANKING_PRICE_WEIGHT", 0.3)
        self.ranking_airline_weight = self._ranking_weight("RANKING_AIRLINE_WEIGHT", 0.5)
        self.ranking_departure_weight = self._ranking_weight("RANKING_DEPARTURE_WEIGHT", 0.2)
        self.default_comparison_currency = os.getenv("DEFAULT_COMPARISON_CURRENCY", "INR").strip().upper() or "INR"
        self.fx_max_age_days = self._fx_max_age_days()

    @staticmethod
    def _fx_max_age_days() -> int:
        try:
            value = int(os.getenv("ECB_FX_MAX_AGE_DAYS", "4"))
        except ValueError as exc:
            raise ValueError("ECB_FX_MAX_AGE_DAYS must be an integer from 1 to 7.") from exc
        if value < 1 or value > 7:
            raise ValueError("ECB_FX_MAX_AGE_DAYS must be an integer from 1 to 7.")
        return value

    @staticmethod
    def _ranking_weight(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError as exc:
            raise ValueError(f"{name} must be a finite non-negative number.") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number.")
        return value

    def validate(self, check_aviationstack: bool = True, check_ignav: bool = True, check_nvidia: bool = True) -> list[str]:
        """Validate presence of required configuration variables.
        
        Returns a list of missing configuration names (or empty list if valid).
        """
        missing = []
        if check_aviationstack and not self.aviationstack_api_key:
            missing.append("AVIATIONSTACK_API_KEY")
        if check_ignav and not self.ignav_api_key:
            missing.append("IGNAV_API_KEY")
        if check_nvidia and not self.nvidia_api_key:
            missing.append("NVIDIA_API_KEY")
        return missing

    def is_valid(self) -> bool:
        """Check if all essential keys are present."""
        return len(self.validate()) == 0

    @staticmethod
    def _mask_secret(secret: str) -> str:
        """Mask a sensitive secret value."""
        if not secret:
            return "<MISSING>"
        if len(secret) <= 8:
            return "***"
        return f"{secret[:4]}...{secret[-4:]}"

    def __repr__(self) -> str:
        return (
            f"Config("
            f"aviationstack_api_key={self._mask_secret(self.aviationstack_api_key)}, "
            f"ignav_api_key={self._mask_secret(self.ignav_api_key)}, "
            f"nvidia_api_key={self._mask_secret(self.nvidia_api_key)}, "
            f"nvidia_base_url='{self.nvidia_base_url}', "
            f"nvidia_model='{self.nvidia_model}', "
            f"ignav_base_url='{self.ignav_base_url}', "
            f"aviationstack_base_url='{self.aviationstack_base_url}'"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()


# Singleton instance
config = Config()
