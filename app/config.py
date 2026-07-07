from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Mealie ──────────────────────────────────────────────────────
    mealie_base_url: str
    mealie_api_key: str
    mealie_shopping_list_id: str

    # ── Bring! ──────────────────────────────────────────────────────
    bring_email: str
    bring_password: str
    bring_list_name: str = "Shopping"

    # ── Sync behaviour ──────────────────────────────────────────────
    poll_interval: int = 60
    on_complete: str = "check"          # check | delete
    bring_to_mealie: str = "note"        # note | food
    freshness_debounce_seconds: int = 5
    # Suspend polling during a local-time window, e.g. "23:00-07:00". Empty disables.
    quiet_hours: str = ""

    # ── System ──────────────────────────────────────────────────────
    db_path: str = "/data/sync.db"
    timezone: str = "Europe/Berlin"
    port: int = 8000
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()  # type: ignore[call-arg]
