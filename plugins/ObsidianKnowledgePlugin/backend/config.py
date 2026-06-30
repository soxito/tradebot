"""
ObsidianKnowledgePlugin — Configuration

Settings loaded from environment variables with OBSIDIAN_ prefix.
All settings are optional and have safe defaults so the plugin never
breaks the main application when Obsidian is not installed.
"""
from pathlib import Path
from pydantic_settings import BaseSettings


class ObsidianSettings(BaseSettings):
    # ── Vault location ─────────────────────────────────────────────────────────
    # Absolute or ~ path to the Obsidian vault directory.
    OBSIDIAN_VAULT_PATH: str = "~/obsidian-vault/tradebot"

    # ── Obsidian Local REST API (community plugin) ─────────────────────────────
    # When set, notes are pushed live to Obsidian in addition to being written
    # to disk.  Leave blank to use file-write-only mode.
    OBSIDIAN_REST_URL: str = ""         # e.g. https://localhost:27124
    OBSIDIAN_REST_TOKEN: str = ""       # API key shown in the plugin settings

    # ── Sync behaviour ─────────────────────────────────────────────────────────
    OBSIDIAN_AUTO_SYNC_MINUTES: int = 15          # background job interval
    OBSIDIAN_EXPORT_DECISIONS: bool = True        # export AgentDecision records
    OBSIDIAN_EXPORT_SIGNALS: bool = True          # export Signal records
    OBSIDIAN_EXPORT_COMMUNITIES: bool = True      # mirror graphify communities

    # ── Agent context injection ────────────────────────────────────────────────
    # When True, recent vault notes are injected into agent prompts.
    OBSIDIAN_INJECT_CONTEXT: bool = False
    OBSIDIAN_CONTEXT_NOTES_LIMIT: int = 5        # max notes per agent call
    OBSIDIAN_CONTEXT_TOKEN_BUDGET: int = 800     # rough token cap for context

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def vault_path(self) -> Path:
        return Path(self.OBSIDIAN_VAULT_PATH).expanduser().resolve()


obsidian_settings = ObsidianSettings()
