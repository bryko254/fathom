from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_provider: str
    sandbox_mode: str
    sandbox_image: str
    query_timeout_s: int
    max_rows: int
    sandbox_memory: str
    sandbox_cpus: str
    data_dir: Path
    snapshot_row_cap: int

    @property
    def provider(self) -> str:
        if self.llm_provider == "auto":
            return "openai" if self.llm_api_key else "mock"
        return self.llm_provider

    @property
    def worker_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "sandbox" / "worker.py"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.vultrinference.com/v1").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL", "deepseek-v4.1-flash-1017"),
        llm_provider=os.environ.get("LLM_PROVIDER", "auto").strip().lower() or "auto",
        sandbox_mode=os.environ.get("SANDBOX_MODE", "docker").strip().lower() or "docker",
        sandbox_image=os.environ.get("SANDBOX_IMAGE", "fathom-sandbox:local"),
        query_timeout_s=int(os.environ.get("QUERY_TIMEOUT_S", "8")),
        max_rows=int(os.environ.get("MAX_ROWS", "500")),
        sandbox_memory=os.environ.get("SANDBOX_MEMORY", "128m"),
        sandbox_cpus=os.environ.get("SANDBOX_CPUS", "0.5"),
        data_dir=data_dir,
        snapshot_row_cap=int(os.environ.get("SNAPSHOT_ROW_CAP", "20000")),
    )
