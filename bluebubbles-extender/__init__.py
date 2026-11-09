"""Hermes plugin ``bluebubbles-extender`` — Web UI & CLI configuration interface for BlueBubbles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import config_store as cs

_STANDALONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BlueBubbles Extender — Hermes Settings UI</title>
  <link rel="stylesheet" href="/assets/style.css" />
  <style>
    :root {
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    body {
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
    }
    input[type="text"], input[type="password"], input[type="number"], input:not([type]) {
      background: #161b22;
      color: #e6edf3;
      border: 1px solid rgba(128, 128, 128, 0.35);
      border-radius: 0.4rem;
      padding: 0.45rem 0.65rem;
      font-size: 0.85rem;
      width: 100%;
      box-sizing: border-box;
    }
    button.bbx-sdk-btn {
      background: #238636;
      color: #fff;
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 0.4rem;
      padding: 0.4rem 0.75rem;
      font-size: 0.82rem;
      font-weight: 500;
      cursor: pointer;
    }
    button.bbx-sdk-btn--secondary {
      background: #21262d;
      color: #c9d1d9;
      border-color: rgba(240,246,252,0.15);
    }
    button.bbx-sdk-btn--ghost {
      background: transparent;
      color: #c9d1d9;
      border-color: transparent;
    }
    button.bbx-sdk-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    span.bbx-sdk-badge {
      font-size: 0.72rem;
      padding: 0.15rem 0.5rem;
      border-radius: 999px;
      background: rgba(56, 139, 253, 0.18);
      border: 1px solid rgba(56, 139, 253, 0.45);
    }
  </style>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
</head>
<body>
  <div id="root"></div>
  <script>
    (function () {
      const React = window.React;
      const h = React.createElement;
      let registeredComponent = null;

      window.__HERMES_PLUGINS__ = {
        register: function (_name, Comp) {
          registeredComponent = Comp;
          const rootEl = document.getElementById("root");
          if (rootEl && window.ReactDOM) {
            window.ReactDOM.createRoot(rootEl).render(h(Comp));
          }
        }
      };

      window.__HERMES_PLUGIN_SDK__ = {
        sdkVersion: "1.1.0",
        React: React,
        hooks: {
          useState: React.useState,
          useEffect: React.useEffect,
          useCallback: React.useCallback,
          useMemo: React.useMemo,
          useRef: React.useRef,
        },
        fetchJSON: function (url, opts) {
          return fetch(url, opts || {}).then(function (r) {
            return r.text().then(function (txt) {
              if (!r.ok) throw new Error(r.status + ": " + txt);
              return txt ? JSON.parse(txt) : {};
            });
          });
        },
        components: {
          Button: function (props) {
            const v = props.variant || "default";
            const cls = "bbx-sdk-btn" + (v === "secondary" ? " bbx-sdk-btn--secondary" : v === "ghost" ? " bbx-sdk-btn--ghost" : "");
            return h("button", {
              type: props.type || "button",
              disabled: props.disabled,
              title: props.title,
              onClick: props.onClick,
              className: cls,
            }, props.children);
          },
          Input: function (props) {
            return h("input", {
              type: props.type || "text",
              value: props.value,
              placeholder: props.placeholder,
              onChange: props.onChange,
              onKeyDown: props.onKeyDown,
            });
          },
          Label: function (props) {
            return h("label", { className: props.className }, props.children);
          },
          Badge: function (props) {
            return h("span", { className: "bbx-sdk-badge" }, props.children);
          },
          Checkbox: function (props) {
            return h("input", {
              type: "checkbox",
              checked: !!props.checked,
              disabled: !!props.disabled,
              onChange: function (e) {
                if (props.onCheckedChange) props.onCheckedChange(e.target.checked);
              },
            });
          }
        },
        utils: {
          cn: function () {
            return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
          }
        }
      };
    })();
  </script>
  <script src="/assets/index.js"></script>
</body>
</html>
"""


def _coerce_cli_value(key: str, raw: str) -> Any:
    schema_entry = next((s for s in cs.SETTINGS_SCHEMA if s["key"] == key or s.get("env") == key), None)
    if schema_entry is None:
        raise ValueError(
            f"Unknown setting {key!r}. Valid keys: {', '.join(s['key'] for s in cs.SETTINGS_SCHEMA)}"
        )
    canon_key = schema_entry["key"]
    typ = schema_entry["type"]
    if typ == "boolean":
        return canon_key, cs._as_bool(raw)
    if typ == "integer":
        return canon_key, cs._as_int(raw, schema_entry.get("default", 0))
    if typ == "list":
        return canon_key, cs._as_list(raw)
    return canon_key, str(raw)


def _format_profile_summary(profile: Optional[str] = None) -> str:
    state = cs.read_profile_settings(profile)
    eff = state["effective"]
    meta = state["meta"]
    lines = [
        f"BlueBubbles Settings — profile: {state['profile']} ({meta['routing_mode']})",
        f"  config.yaml            : {meta['config_yaml_path']}",
        f"  .env                   : {meta['dotenv_path']}",
        f"  plugin installed       : {meta['plugin_installed']} ({meta['plugin_dir']})",
        "---",
    ]
    for item in cs.SETTINGS_SCHEMA:
        k = item["key"]
        env_tag = f" ({item['env']})" if item.get("env") else ""
        lines.append(f"  {k:<24}: {json.dumps(eff.get(k))}{env_tag}")
    return "\n".join(lines)


def _setup_cli_parser(subparser) -> None:
    sub = subparser.add_subparsers(dest="bb_action", help="BlueBubbles Extender action")

    p_show = sub.add_parser("show", help="Show BlueBubbles settings for a profile")
    p_show.add_argument("-p", "--profile", default=None, help="Target profile name (e.g. family, personal, default)")

    p_set = sub.add_parser("set", help="Update a BlueBubbles setting for a profile")
    p_set.add_argument("key", help="Setting key (e.g. ignore_direct_messages or BLUEBUBBLES_IGNORE_DIRECT_MESSAGES)")
    p_set.add_argument("value", help="New value (true/false, number, string, or comma-separated list)")
    p_set.add_argument("-p", "--profile", default=None, help="Target profile name")
    p_set.add_argument("--restart", action="store_true", help="Restart the Hermes gateway after saving")

    p_sync = sub.add_parser("sync", help="Sync bluebubbles-connector/plugin into profile plugin folders")
    p_sync.add_argument("-p", "--profile", action="append", default=None, help="Profile(s) to sync (default: all)")
    p_sync.add_argument("--restart", action="store_true", help="Restart the Hermes gateway after syncing")

    p_serve = sub.add_parser("serve", help="Run the standalone BlueBubbles Extender Web UI server")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8649, help="Bind port (default: 8649)")


def _handle_cli_command(args) -> int:
    action = getattr(args, "bb_action", None) or "show"
    if action == "show":
        print(_format_profile_summary(getattr(args, "profile", None)))
        return 0
    if action == "set":
        prof = getattr(args, "profile", None) or "default"
        canon_key, val = _coerce_cli_value(args.key, args.value)
        cs.write_profile_settings(prof, {canon_key: val})
        print(f"Updated {canon_key} = {json.dumps(val)} on profile {prof!r}.")
        if getattr(args, "restart", False):
            res = cs.restart_gateway()
            print(f"Gateway restart: {res}")
        return 0
    if action == "sync":
        res = cs.sync_connector_plugin(getattr(args, "profile", None))
        print(json.dumps(res, indent=2))
        if getattr(args, "restart", False):
            r_res = cs.restart_gateway()
            print(f"Gateway restart: {r_res}")
        return 0 if res.get("ok") else 1
    if action == "serve":
        _run_standalone_server(host=args.host, port=args.port)
        return 0
    print(_format_profile_summary(None))
    return 0


def _run_standalone_server(host: str = "127.0.0.1", port: int = 8649) -> None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse
    from .dashboard.plugin_api import router as api_router

    app = FastAPI(title="BlueBubbles Extender UI", version="1.0.0")
    app.include_router(api_router, prefix="/api/plugins/bluebubbles-extender")

    dist_dir = Path(__file__).resolve().parent / "dashboard" / "dist"

    @app.get("/", response_class=HTMLResponse)
    async def index_page():
        return _STANDALONE_HTML

    @app.get("/assets/index.js")
    async def asset_js():
        return FileResponse(dist_dir / "index.js", media_type="application/javascript")

    @app.get("/assets/style.css")
    async def asset_css():
        return FileResponse(dist_dir / "style.css", media_type="text/css")

    print(f"BlueBubbles Extender UI listening on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _slash_command_handler(raw_args: str = "") -> str:
    tokens = (raw_args or "").strip().split()
    if not tokens or tokens[0] == "show":
        prof = tokens[1] if len(tokens) > 1 else None
        return _format_profile_summary(prof)
    if tokens[0] == "set" and len(tokens) >= 3:
        key_arg = tokens[1]
        val_arg = tokens[2]
        prof = tokens[3] if len(tokens) > 3 else "default"
        try:
            canon_key, val = _coerce_cli_value(key_arg, val_arg)
            cs.write_profile_settings(prof, {canon_key: val})
            return f"Updated {canon_key} = {json.dumps(val)} for profile {prof!r}."
        except Exception as exc:
            return f"Error updating setting: {exc}"
    return (
        "Usage:\n"
        "  /bb-config show [profile]\n"
        "  /bb-config set <key> <value> [profile]"
    )


def register(ctx) -> None:
    """Hermes plugin entry point."""
    try:
        ctx.register_cli_command(
            name="bluebubbles-config",
            help="Configure BlueBubbles settings across profiles or launch the BlueBubbles Extender UI",
            setup_fn=_setup_cli_parser,
            handler_fn=_handle_cli_command,
            description="BlueBubbles Extender CLI & Web UI",
        )
    except Exception:
        pass

    try:
        ctx.register_command(
            name="bb-config",
            handler=_slash_command_handler,
            description="View or modify BlueBubbles settings (e.g. /bb-config show family)",
            args_hint="[show|set] [args]",
        )
    except Exception:
        pass
