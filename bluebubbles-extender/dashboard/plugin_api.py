"""FastAPI backend routes for ``bluebubbles-extender``, mounted at ``/api/plugins/bluebubbles-extender/``."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_config_store():
    mod_name = "bluebubbles_extender_config_store"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    store_path = _PLUGIN_ROOT / "config_store.py"
    spec = importlib.util.spec_from_file_location(mod_name, store_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load config_store.py from {store_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load_config_store()

router = APIRouter()


class SaveSettingsPayload(BaseModel):
    target_profile: Optional[str] = Field(default=None, description="Profile name (e.g. default, family, personal)")
    settings: dict[str, Any] = Field(default_factory=dict, description="Settings key/value pairs to update")
    sync_dotenv: bool = Field(default=True, description="Also write matching BLUEBUBBLES_* keys to .env")
    sync_plugin: bool = Field(default=False, description="Also sync bluebubbles-connector/plugin into this profile")
    restart: bool = Field(default=False, description="Restart ai.hermes.gateway after saving")


class SyncPluginPayload(BaseModel):
    profiles: Optional[list[str]] = Field(default=None, description="Target profiles to sync (omit for all active)")
    restart: bool = Field(default=False, description="Restart ai.hermes.gateway after syncing")


def _select_default_profile(profiles: list[dict[str, Any]], requested: Optional[str]) -> str:
    names = [p["name"] for p in profiles]
    if requested and requested in names:
        return requested
    # Prefer the first non-default profile with BlueBubbles configured/installed (e.g. family or personal)
    for p in profiles:
        if p["name"] != "default" and (p.get("plugin_installed") or p.get("platform_enabled")):
            return p["name"]
    return names[0] if names else "default"


@router.get("/state")
async def get_extender_state(
    target_profile: Optional[str] = Query(default=None, description="Profile name to inspect"),
    reveal_password: bool = Query(default=False, description="Include unmasked password"),
) -> dict[str, Any]:
    """Return all Hermes profiles, the selected profile's full BlueBubbles settings, and the schema."""
    try:
        profiles = await asyncio.to_thread(cs.list_profiles)
        selected = _select_default_profile(profiles, target_profile)
        profile_state = await asyncio.to_thread(
            cs.read_profile_settings, selected, reveal_password=reveal_password
        )
        source_dir = await asyncio.to_thread(cs.find_connector_plugin_source)
        return {
            "ok": True,
            "selected_profile": selected,
            "profiles": profiles,
            "profile_state": profile_state,
            "schema": cs.SETTINGS_SCHEMA,
            "connector_source": str(source_dir) if source_dir else None,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/settings")
async def update_extender_settings(
    payload: SaveSettingsPayload,
    target_profile: Optional[str] = Query(default=None, description="Profile name override"),
) -> dict[str, Any]:
    """Save BlueBubbles settings for the selected profile and optionally sync plugin / restart gateway."""
    prof = (payload.target_profile or target_profile or "default").strip() or "default"
    try:
        updated = await asyncio.to_thread(
            cs.write_profile_settings,
            prof,
            payload.settings,
            sync_dotenv=payload.sync_dotenv,
        )
        sync_res = None
        if payload.sync_plugin:
            sync_res = await asyncio.to_thread(cs.sync_connector_plugin, [prof])
        restart_res = None
        if payload.restart:
            restart_res = await asyncio.to_thread(cs.restart_gateway)
        profiles = await asyncio.to_thread(cs.list_profiles)
        return {
            "ok": True,
            "selected_profile": prof,
            "profiles": profiles,
            "profile_state": updated,
            "sync_result": sync_res,
            "restart_result": restart_res,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/server-status")
async def get_server_status(
    target_profile: Optional[str] = Query(default=None, description="Profile name whose server to probe"),
) -> dict[str, Any]:
    """Probe the BlueBubbles Server configured for ``target_profile``."""
    try:
        profiles = await asyncio.to_thread(cs.list_profiles)
        selected = _select_default_profile(profiles, target_profile)
        return await asyncio.to_thread(cs.probe_bluebubbles_server, selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/chats")
async def get_live_chats(
    target_profile: Optional[str] = Query(default=None, description="Profile name"),
    limit: int = Query(default=100, ge=1, le=250),
) -> dict[str, Any]:
    """Fetch recent DMs and group chats from the live BlueBubbles Server for interactive picking."""
    try:
        profiles = await asyncio.to_thread(cs.list_profiles)
        selected = _select_default_profile(profiles, target_profile)
        return await asyncio.to_thread(cs.fetch_live_chats, selected, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/contacts")
async def get_live_contacts(
    target_profile: Optional[str] = Query(default=None, description="Profile name"),
) -> dict[str, Any]:
    """Fetch contacts from BlueBubbles ``/api/v1/contact`` for interactive allowlist picking."""
    try:
        profiles = await asyncio.to_thread(cs.list_profiles)
        selected = _select_default_profile(profiles, target_profile)
        return await asyncio.to_thread(cs.fetch_live_contacts, selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sync-plugin")
async def post_sync_plugin(payload: SyncPluginPayload) -> dict[str, Any]:
    """Copy ``bluebubbles-connector/plugin`` into each target profile's ``plugins/platforms/bluebubbles``."""
    res = await asyncio.to_thread(cs.sync_connector_plugin, payload.profiles)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Sync failed")
    restart_res = None
    if payload.restart:
        restart_res = await asyncio.to_thread(cs.restart_gateway)
    profiles = await asyncio.to_thread(cs.list_profiles)
    return {
        "ok": True,
        "sync_result": res,
        "restart_result": restart_res,
        "profiles": profiles,
    }


@router.post("/restart-gateway")
async def post_restart_gateway() -> dict[str, Any]:
    """Restart the Hermes gateway service."""
    res = await asyncio.to_thread(cs.restart_gateway)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error") or "Failed to restart gateway")
    return res
