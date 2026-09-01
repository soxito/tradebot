"""Manifest-driven plugin discovery and mounting."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine

from app.plugins.contracts import DiscoveredPlugin, PluginManifest


def _resolve_plugins_root(plugins_dir: str) -> Path:
    raw_path = Path(plugins_dir)
    if raw_path.exists():
        return raw_path.resolve()

    project_root = Path(__file__).resolve().parents[3]
    if not raw_path.is_absolute():
        candidate = (project_root / raw_path).resolve()
        if candidate.exists():
            return candidate

    container_candidate = Path("/plugins")
    if container_candidate.exists():
        return container_candidate.resolve()

    return (project_root / "plugins").resolve()


class PluginLoader:
    """Loads and mounts plugins from plugin.json manifests."""

    def __init__(self, plugins_dir: str = "plugins", strict_mode: bool = False):
        self.plugins_root = _resolve_plugins_root(plugins_dir)
        self.strict_mode = strict_mode
        self._discovered: list[DiscoveredPlugin] = []
        self._mounted: set[str] = set()
        self._discovery_attempted = False
        self._ensure_import_path()

    def _ensure_import_path(self) -> None:
        parent = str(self.plugins_root.parent)
        if parent not in sys.path:
            sys.path.append(parent)

    @property
    def discovered(self) -> list[DiscoveredPlugin]:
        return list(self._discovered)

    def discover(self, force: bool = False) -> list[DiscoveredPlugin]:
        if self._discovery_attempted and not force:
            return list(self._discovered)

        self._discovery_attempted = True
        self._discovered = []

        if not self.plugins_root.exists():
            logger.warning(f"Plugin directory not found: {self.plugins_root}")
            return []

        for entry in sorted(self.plugins_root.iterdir()):
            if not entry.is_dir():
                continue

            manifest_path = entry / "plugin.json"
            if not manifest_path.exists():
                continue

            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = self._parse_manifest(payload)
                self._discovered.append(
                    DiscoveredPlugin(
                        package_name=entry.name,
                        root_dir=entry,
                        manifest_path=manifest_path,
                        manifest=manifest,
                    )
                )
            except Exception as exc:
                message = f"Failed to parse plugin manifest {manifest_path}: {exc}"
                if self.strict_mode:
                    raise RuntimeError(message) from exc
                logger.warning(message)

        if self._discovered:
            slugs = ", ".join(plugin.manifest.slug for plugin in self._discovered)
            logger.info(f"Discovered plugins: {slugs}")
        else:
            logger.info("No plugins discovered")

        return list(self._discovered)

    async def init_plugin_tables(self, engine: AsyncEngine) -> list[str]:
        """Initialize SQLAlchemy metadata for discovered plugins."""
        initialized: list[str] = []

        for plugin in self.discover():
            try:
                models_module = importlib.import_module(plugin.models_module_path)
                metadata_objects = self._extract_metadata_objects(models_module)
                if not metadata_objects:
                    continue

                async with engine.begin() as conn:
                    for metadata in metadata_objects:
                        await conn.run_sync(metadata.create_all)
                        await conn.run_sync(self._add_missing_columns, metadata)

                initialized.append(plugin.manifest.slug)
                logger.info(f"Initialized plugin tables: {plugin.manifest.slug}")
            except Exception as exc:
                message = f"Failed plugin table init for {plugin.manifest.slug}: {exc}"
                if self.strict_mode:
                    raise RuntimeError(message) from exc
                logger.warning(message)

        return initialized

    def mount_routers(self, app: FastAPI, api_prefix: str = "/api/v1") -> list[str]:
        """Mount plugin routers into the FastAPI app."""
        mounted: list[str] = []

        for plugin in self.discover():
            if plugin.manifest.slug in self._mounted:
                continue

            try:
                module = importlib.import_module(plugin.module_path)
                router = getattr(module, "router", None)
                if router is None:
                    raise AttributeError(
                        f"module '{plugin.module_path}' has no attribute 'router'"
                    )
                app.include_router(router, prefix=api_prefix)
                self._mounted.add(plugin.manifest.slug)
                mounted.append(plugin.manifest.slug)
                logger.info(
                    f"Mounted plugin router: {plugin.manifest.slug} ({plugin.module_path})"
                )
            except Exception as exc:
                message = f"Failed to mount router for {plugin.manifest.slug}: {exc}"
                if self.strict_mode:
                    raise RuntimeError(message) from exc
                logger.warning(message)

        return mounted

    @staticmethod
    def _add_missing_columns(sync_conn, metadata) -> None:
        """Add columns a plugin model gained since its table was created.

        ``create_all`` only ever CREATEs — it will not touch a table that
        already exists. Without this, adding a field to a plugin model works on
        a fresh database and silently breaks every existing one, which surfaces
        much later as an ``UndefinedColumn`` on an unrelated query.

        Additive only, and deliberately so: dropping or retyping a column is a
        decision that belongs in a reviewed migration, not in a boot path.
        Identifiers come from the model metadata, never from user input.
        """
        from sqlalchemy import inspect as sa_inspect, text
        from sqlalchemy.schema import CreateColumn

        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())

        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # just created with every column
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or column.primary_key:
                    continue
                try:
                    ddl = CreateColumn(column).compile(sync_conn.engine)
                    # Each ADD COLUMN runs inside its own SAVEPOINT. DDL is
                    # transactional on PostgreSQL: without this, one failing
                    # column aborts the whole transaction and silently rolls
                    # back every column added before it in this batch — plus
                    # anything else the plugin init still had pending.
                    with sync_conn.begin_nested():
                        sync_conn.execute(
                            text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')
                        )
                    logger.info(f"Added column {table.name}.{column.name}")
                except Exception as exc:  # noqa: BLE001
                    # A column that cannot be added (a NOT NULL with no server
                    # default on a populated table) must not stop the rest of
                    # boot.
                    logger.warning(
                        f"Could not add column {table.name}.{column.name}: {exc}"
                    )

    @staticmethod
    def _extract_metadata_objects(module: object) -> list[object]:
        metadata_objects: list[object] = []
        seen: set[int] = set()

        for obj in vars(module).values():
            if not isinstance(obj, type):
                continue
            if not obj.__name__.endswith("Base"):
                continue

            metadata = getattr(obj, "metadata", None)
            create_all = getattr(metadata, "create_all", None)
            if metadata is None or not callable(create_all):
                continue

            marker = id(metadata)
            if marker in seen:
                continue

            seen.add(marker)
            metadata_objects.append(metadata)

        return metadata_objects

    @staticmethod
    def _parse_manifest(payload: dict) -> PluginManifest:
        required = ("name", "slug", "version", "service_provider")
        missing = [field for field in required if not payload.get(field)]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")

        return PluginManifest(
            name=str(payload["name"]),
            slug=str(payload["slug"]),
            version=str(payload["version"]),
            service_provider=str(payload["service_provider"]),
            description=payload.get("description"),
            author=payload.get("author"),
            requires=payload.get("requires"),
            provides=payload.get("provides"),
            permissions=payload.get("permissions"),
            settings_keys=payload.get("settings_keys"),
        )
