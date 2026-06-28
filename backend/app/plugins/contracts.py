"""Plugin manifest contracts for TradeBot runtime discovery."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PluginManifest:
    """Normalized plugin.json payload."""

    name: str
    slug: str
    version: str
    service_provider: str
    description: str | None = None
    author: str | None = None
    requires: dict[str, Any] | None = None
    provides: dict[str, Any] | None = None
    permissions: list[str] | None = None
    settings_keys: list[str] | None = None


@dataclass(slots=True)
class DiscoveredPlugin:
    """Plugin folder + parsed manifest."""

    package_name: str
    root_dir: Path
    manifest_path: Path
    manifest: PluginManifest

    @property
    def module_path(self) -> str:
        return f"plugins.{self.package_name}.{self.manifest.service_provider}"

    @property
    def models_module_path(self) -> str:
        return f"plugins.{self.package_name}.backend.models"
