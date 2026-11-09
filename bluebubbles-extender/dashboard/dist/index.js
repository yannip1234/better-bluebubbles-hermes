/**
 * BlueBubbles Extender — Hermes Dashboard Plugin
 *
 * Full configuration UI for all BlueBubbles settings across Hermes profiles:
 * connection credentials, multi-profile DM (;-;) and group (;+;) chat routing,
 * live BlueBubbles chat & contact pickers, wake-word & read-receipt settings,
 * webhook listener config, Private API diagnostics, plugin deployment sync,
 * and gateway restart.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { Badge, Button, Input, Label } = SDK.components || {};
  const { useState, useEffect, useCallback } = SDK.hooks;

  const Checkbox = (SDK.components && SDK.components.Checkbox) || function (props) {
    const { checked, onCheckedChange, className, id, disabled } = props;
    return h("input", {
      id: id,
      type: "checkbox",
      checked: !!checked,
      disabled: !!disabled,
      className: className,
      onChange: function (e) {
        if (onCheckedChange) onCheckedChange(e.target.checked);
      },
    });
  };

  const API = "/api/plugins/bluebubbles-extender";
  const LS_PROFILE_KEY = "hermes.bluebubbles_extender.selectedProfile";

  function parseApiError(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "Unknown error");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && parsed.error) return String(parsed.error);
    } catch (_e) { /* fall through */ }
    return body || raw;
  }

  function routingLabel(mode) {
    if (mode === "group_only") return "Group-Only (;+;)";
    if (mode === "dm_only") return "DM-Only (;-;)";
    if (mode === "custom_allowlist") return "Custom Chat Allowlist";
    if (mode === "none") return "All Chats Blocked";
    return "All Allowed Chats";
  }

  // --- Reusable Tag List Editor ---------------------------------------------

  function TagListEditor(props) {
    const items = Array.isArray(props.items) ? props.items : [];
    const [draft, setDraft] = useState("");

    function addDraft() {
      const parts = draft
        .split(/[,\n]+/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      if (!parts.length) return;
      const next = items.slice();
      parts.forEach(function (p) {
        if (next.indexOf(p) === -1) next.push(p);
      });
      props.onChange(next);
      setDraft("");
    }

    function removeItem(target) {
      props.onChange(items.filter(function (x) { return x !== target; }));
    }

    return h("div", { className: "bbx-field" },
      h("div", { className: "bbx-input-group" },
        h(Input, {
          value: draft,
          placeholder: props.placeholder || "Add item and press Enter…",
          onChange: function (e) { setDraft(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter") {
              e.preventDefault();
              addDraft();
            }
          },
        }),
        h(Button, {
          type: "button",
          size: "sm",
          variant: "secondary",
          onClick: addDraft,
        }, "Add"),
        props.extraButton || null
      ),
      items.length > 0
        ? h("div", { className: "bbx-tags" },
            items.map(function (item) {
              const isGroup = item.indexOf(";+;") !== -1;
              return h("span", {
                key: item,
                className: isGroup ? "bbx-tag bbx-tag--group" : "bbx-tag",
              },
                h("span", null, item),
                h("button", {
                  type: "button",
                  className: "bbx-tag-remove",
                  title: "Remove " + item,
                  onClick: function () { removeItem(item); },
                }, "×")
              );
            })
          )
        : h("div", { className: "bbx-help" }, props.emptyText || "None configured.")
    );
  }

  // --- Main Page Component --------------------------------------------------

  function BlueBubblesExtenderPage() {
    const [profiles, setProfiles] = useState([]);
    const [selectedProfile, setSelectedProfile] = useState(function () {
      try {
        return window.localStorage.getItem(LS_PROFILE_KEY) || "";
      } catch (_e) {
        return "";
      }
    });
    const [form, setForm] = useState(null);
    const [meta, setMeta] = useState({});
    const [connectorSource, setConnectorSource] = useState(null);
    const [dirty, setDirty] = useState(false);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [syncDotenv, setSyncDotenv] = useState(true);
    const [passwordRevealed, setPasswordRevealed] = useState(false);

    const [banner, setBanner] = useState(null);
    const [serverStatus, setServerStatus] = useState(null);
    const [probingServer, setProbingServer] = useState(false);

    const [showChatPicker, setShowChatPicker] = useState(false);
    const [liveChats, setLiveChats] = useState([]);
    const [loadingChats, setLoadingChats] = useState(false);
    const [chatFilterKind, setChatFilterKind] = useState("all");
    const [chatSearch, setChatSearch] = useState("");

    const [showContactPicker, setShowContactPicker] = useState(false);
    const [liveContacts, setLiveContacts] = useState([]);
    const [loadingContacts, setLoadingContacts] = useState(false);
    const [contactSearch, setContactSearch] = useState("");

    const [showRawPreview, setShowRawPreview] = useState(false);

    const loadProfileState = useCallback(function (profName, revealPw) {
      setLoading(true);
      const qs = new URLSearchParams();
      if (profName) qs.set("target_profile", profName);
      if (revealPw) qs.set("reveal_password", "true");
      return SDK.fetchJSON(API + "/state?" + qs.toString())
        .then(function (res) {
          setProfiles(res.profiles || []);
          const actualProf = res.selected_profile || profName || "default";
          setSelectedProfile(actualProf);
          try {
            window.localStorage.setItem(LS_PROFILE_KEY, actualProf);
          } catch (_e) { /* ignore */ }
          const pState = res.profile_state || {};
          setForm(pState.effective || {});
          setMeta(pState.meta || {});
          setConnectorSource(res.connector_source || null);
          setPasswordRevealed(!!revealPw);
          setDirty(false);
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Failed to load settings: " + parseApiError(err) });
        })
        .finally(function () {
          setLoading(false);
        });
    }, []);

    const probeServer = useCallback(function (profName) {
      const target = profName || selectedProfile;
      setProbingServer(true);
      const qs = target ? "?target_profile=" + encodeURIComponent(target) : "";
      return SDK.fetchJSON(API + "/server-status" + qs)
        .then(function (res) {
          setServerStatus(res);
        })
        .catch(function (err) {
          setServerStatus({ ok: false, connected: false, error: parseApiError(err) });
        })
        .finally(function () {
          setProbingServer(false);
        });
    }, [selectedProfile]);

    useEffect(function () {
      loadProfileState(selectedProfile, false).then(function () {
        probeServer(selectedProfile);
      });
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    function selectProfile(profName) {
      if (profName === selectedProfile && form) return;
      setShowChatPicker(false);
      setShowContactPicker(false);
      setBanner(null);
      loadProfileState(profName, false).then(function () {
        probeServer(profName);
      });
    }

    function updateField(key, value) {
      setForm(function (prev) {
        return Object.assign({}, prev || {}, { [key]: value });
      });
      setDirty(true);
    }

    function applyRoutingPreset(preset) {
      if (!form) return;
      if (preset === "group_only") {
        updateField("ignore_direct_messages", true);
        updateField("ignore_group_messages", false);
      } else if (preset === "dm_only") {
        updateField("ignore_direct_messages", false);
        updateField("ignore_group_messages", true);
      } else if (preset === "all") {
        updateField("ignore_direct_messages", false);
        updateField("ignore_group_messages", false);
        updateField("allowed_chat_guids", []);
      }
    }

    function handleSave(opts) {
      if (!form) return;
      const options = opts || {};
      setSaving(true);
      setBanner(null);
      SDK.fetchJSON(API + "/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_profile: selectedProfile,
          settings: form,
          sync_dotenv: syncDotenv,
          sync_plugin: !!options.syncPlugin,
          restart: !!options.restart,
        }),
      })
        .then(function (res) {
          setProfiles(res.profiles || []);
          const pState = res.profile_state || {};
          setForm(pState.effective || {});
          setMeta(pState.meta || {});
          setPasswordRevealed(false);
          setDirty(false);
          const parts = ["Saved settings for profile '" + selectedProfile + "'."];
          if (res.sync_result && res.sync_result.ok) {
            parts.push("Synced plugin files.");
          }
          if (res.restart_result) {
            if (res.restart_result.ok) {
              parts.push("Gateway restarted.");
            } else {
              parts.push("Gateway restart warning: " + (res.restart_result.error || "failed"));
            }
          }
          setBanner({ ok: true, text: parts.join(" ") });
          probeServer(selectedProfile);
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Save failed: " + parseApiError(err) });
        })
        .finally(function () {
          setSaving(false);
        });
    }

    function handleSyncAllPlugins() {
      setSaving(true);
      setBanner(null);
      SDK.fetchJSON(API + "/sync-plugin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profiles: null, restart: false }),
      })
        .then(function (res) {
          if (res.profiles) setProfiles(res.profiles);
          const syncedList = ((res.sync_result && res.sync_result.synced) || [])
            .map(function (s) { return s.profile; })
            .join(", ");
          setBanner({
            ok: true,
            text: "Synced bluebubbles-connector/plugin into profile(s): " + (syncedList || "all"),
          });
          loadProfileState(selectedProfile, passwordRevealed);
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Plugin sync failed: " + parseApiError(err) });
        })
        .finally(function () {
          setSaving(false);
        });
    }

    function handleRestartGateway() {
      setSaving(true);
      setBanner(null);
      SDK.fetchJSON(API + "/restart-gateway", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      })
        .then(function (res) {
          setBanner({
            ok: true,
            text: "Hermes gateway restarted (" + (res.method || "ok") + ").",
          });
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Gateway restart failed: " + parseApiError(err) });
        })
        .finally(function () {
          setSaving(false);
        });
    }

    function loadLiveChats() {
      setLoadingChats(true);
      const qs = "?target_profile=" + encodeURIComponent(selectedProfile) + "&limit=150";
      SDK.fetchJSON(API + "/chats" + qs)
        .then(function (res) {
          if (!res.ok) {
            setBanner({ ok: false, text: "Could not load chats: " + (res.error || "failed") });
            return;
          }
          setLiveChats(res.chats || []);
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Could not load chats: " + parseApiError(err) });
        })
        .finally(function () {
          setLoadingChats(false);
        });
    }

    function toggleChatPicker() {
      const next = !showChatPicker;
      setShowChatPicker(next);
      if (next && liveChats.length === 0) {
        loadLiveChats();
      }
    }

    function loadLiveContacts() {
      setLoadingContacts(true);
      const qs = "?target_profile=" + encodeURIComponent(selectedProfile);
      SDK.fetchJSON(API + "/contacts" + qs)
        .then(function (res) {
          if (!res.ok) {
            setBanner({ ok: false, text: "Could not load contacts: " + (res.error || "failed") });
            return;
          }
          setLiveContacts(res.contacts || []);
        })
        .catch(function (err) {
          setBanner({ ok: false, text: "Could not load contacts: " + parseApiError(err) });
        })
        .finally(function () {
          setLoadingContacts(false);
        });
    }

    function toggleContactPicker() {
      const next = !showContactPicker;
      setShowContactPicker(next);
      if (next && liveContacts.length === 0) {
        loadLiveContacts();
      }
    }

    function toggleAllowedChatGuid(guid) {
      if (!form) return;
      const cur = Array.isArray(form.allowed_chat_guids) ? form.allowed_chat_guids.slice() : [];
      const idx = cur.indexOf(guid);
      if (idx >= 0) {
        cur.splice(idx, 1);
      } else {
        cur.push(guid);
      }
      updateField("allowed_chat_guids", cur);
    }

    function toggleAllowedUser(handle) {
      if (!form) return;
      const cur = Array.isArray(form.allowed_users) ? form.allowed_users.slice() : [];
      const idx = cur.indexOf(handle);
      if (idx >= 0) {
        cur.splice(idx, 1);
      } else {
        cur.push(handle);
      }
      updateField("allowed_users", cur);
    }

    if (loading && !form) {
      return h("div", { className: "bbx-root" },
        h("div", { className: "bbx-subtitle" }, "Loading BlueBubbles Extender configuration…")
      );
    }

    const eff = form || {};
    const filteredChats = liveChats.filter(function (c) {
      if (chatFilterKind !== "all" && c.kind !== chatFilterKind) return false;
      if (!chatSearch.trim()) return true;
      const q = chatSearch.trim().toLowerCase();
      return (
        String(c.display_name || "").toLowerCase().indexOf(q) !== -1 ||
        String(c.canonical_guid || "").toLowerCase().indexOf(q) !== -1 ||
        String(c.guid || "").toLowerCase().indexOf(q) !== -1
      );
    });

    const filteredContacts = liveContacts.filter(function (c) {
      if (!contactSearch.trim()) return true;
      const q = contactSearch.trim().toLowerCase();
      return (
        String(c.display_name || "").toLowerCase().indexOf(q) !== -1 ||
        (c.handles || []).some(function (hStr) { return hStr.toLowerCase().indexOf(q) !== -1; })
      );
    });

    return h("div", { className: "bbx-root" },
      // --- Top Header & Action Bar ---
      h("div", { className: "bbx-header" },
        h("div", { className: "bbx-title-group" },
          h("div", { className: "bbx-title" },
            h("span", null, "💬 BlueBubbles Extender"),
            Badge ? h(Badge, { variant: "secondary" }, "Profile: " + selectedProfile) : null,
            dirty && Badge ? h(Badge, { variant: " default" }, "Unsaved changes") : null
          ),
          h("div", { className: "bbx-subtitle" },
            "Configure BlueBubbles iMessage credentials, DM (;-;) vs Group (;+;) profile routing, live chat & contact allowlists, and Private API options."
          )
        ),
        h("div", { className: "bbx-actions" },
          h(Button, {
            type: "button",
            size: "sm",
            variant: "secondary",
            disabled: probingServer,
            onClick: function () { probeServer(selectedProfile); },
          }, probingServer ? "Probing…" : "Test Server"),
          h(Button, {
            type: "button",
            size: "sm",
            variant: "secondary",
            disabled: saving || !connectorSource,
            title: connectorSource ? "Sync from " + connectorSource : "Connector repo not found",
            onClick: handleSyncAllPlugins,
          }, "Sync Plugin Files"),
          h(Button, {
            type: "button",
            size: "sm",
            variant: "secondary",
            disabled: saving,
            onClick: handleRestartGateway,
          }, "Restart Gateway"),
          h(Button, {
            type: "button",
            size: "sm",
            disabled: saving,
            onClick: function () { handleSave({ syncPlugin: false, restart: false }); },
          }, saving ? "Saving…" : "Save Settings"),
          h(Button, {
            type: "button",
            size: "sm",
            disabled: saving,
            onClick: function () { handleSave({ syncPlugin: true, restart: true }); },
          }, saving ? "Applying…" : "Save & Restart Gateway")
        )
      ),

      // --- Status Banner ---
      banner
        ? h("div", { className: banner.ok ? "bbx-banner bbx-banner--ok" : "bbx-banner bbx-banner--err" },
            h("span", null, banner.text),
            h(Button, {
              type: "button",
              size: "sm",
              variant: "ghost",
              onClick: function () { setBanner(null); },
            }, "Dismiss")
          )
        : null,

      // --- Profile Switcher Bar ---
      h("div", { className: "bbx-profile-bar" },
        h("span", { className: "bbx-label", style: { marginRight: "0.35rem" } }, "Profiles:"),
        profiles.map(function (p) {
          const isActive = p.name === selectedProfile;
          return h("button", {
            key: p.name,
            type: "button",
            onClick: function () { selectProfile(p.name); },
            className: isActive ? "bbx-profile-pill bbx-profile-pill--active" : "bbx-profile-pill",
          },
            h("span", {
              className: p.platform_enabled ? "bbx-dot bbx-dot--on" : "bbx-dot bbx-dot--off",
              title: p.platform_enabled ? "Platform enabled" : "Platform disabled",
            }),
            h("span", null, p.name),
            h("span", { className: "bbx-env-tag" }, routingLabel(p.routing_mode))
          );
        })
      ),

      // --- Live Server & Private API Status Card ---
      h("div", { className: "bbx-card bbx-card--full" },
        h("div", { className: "bbx-card-head" },
          h("div", { className: "bbx-card-title" },
            h("span", {
              className: serverStatus && serverStatus.connected
                ? "bbx-dot bbx-dot--on"
                : "bbx-dot bbx-dot--err",
            }),
            h("span", null, "Live BlueBubbles Server & Private API Status")
          ),
          h("span", { className: "bbx-mono" }, meta.config_yaml_path || "")
        ),
        serverStatus
          ? serverStatus.connected
            ? h("div", { className: "bbx-status-grid" },
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "Server Version"),
                  h("span", { className: "bbx-stat-val" }, serverStatus.server_version || "Connected")
                ),
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "macOS Version"),
                  h("span", { className: "bbx-stat-val" }, serverStatus.os_version || "Unknown")
                ),
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "Private API"),
                  h("span", { className: "bbx-stat-val" },
                    serverStatus.private_api && serverStatus.helper_connected
                      ? "✓ Enabled & Helper Connected"
                      : serverStatus.private_api
                        ? "⚠ Enabled (Helper Disconnected)"
                        : "✗ Disabled"
                  )
                ),
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "iCloud Account"),
                  h("span", { className: "bbx-stat-val" }, serverStatus.detected_icloud || "—")
                ),
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "FindMy Friends Cached"),
                  h("span", { className: "bbx-stat-val" }, String(serverStatus.findmy_friends_cached || 0) + " friends")
                ),
                h("div", { className: "bbx-stat-box" },
                  h("span", { className: "bbx-stat-label" }, "Registered Webhooks"),
                  h("span", { className: "bbx-stat-val" },
                    String((serverStatus.webhooks || []).length) + " active"
                  )
                )
              )
            : h("div", { className: "bbx-help" },
                "Server probe failed: " + (serverStatus.error || "Unable to connect")
              )
          : h("div", { className: "bbx-help" }, "Probing BlueBubbles server…")
      ),

      // --- Settings Grid ---
      h("div", { className: "bbx-grid" },

        // 1. Connection & Activation
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "1. Connection & Profile Activation")
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "BlueBubbles Server URL ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_SERVER_URL")
            ),
            h(Input, {
              value: eff.server_url || "",
              placeholder: "http://127.0.0.1:1234",
              onChange: function (e) { updateField("server_url", e.target.value); },
            }),
            h("div", { className: "bbx-help" }, "Base HTTP URL of your macOS BlueBubbles Server.")
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Server API Password ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_PASSWORD")
            ),
            h("div", { className: "bbx-input-group" },
              h(Input, {
                type: passwordRevealed ? "text" : "password",
                value: eff.password || "",
                placeholder: meta.has_password ? "Stored in .env" : "Enter BlueBubbles API password",
                onChange: function (e) { updateField("password", e.target.value); },
              }),
              h(Button, {
                type: "button",
                size: "sm",
                variant: "secondary",
                onClick: function () {
                  const next = !passwordRevealed;
                  if (next && eff.password === "••••••••") {
                    loadProfileState(selectedProfile, true);
                  } else {
                    setPasswordRevealed(next);
                  }
                },
              }, passwordRevealed ? "Hide" : "Reveal")
            ),
            h("div", { className: "bbx-help" },
              "Saved to " + (meta.dotenv_path || ".env") + " (chmod 600)."
            )
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" }, "Enable BlueBubbles Platform"),
              h("div", { className: "bbx-help" }, "platforms.bluebubbles.enabled in config.yaml")
            ),
            h(Checkbox, {
              checked: !!eff.platform_enabled,
              onCheckedChange: function (v) { updateField("platform_enabled", v); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" }, "Enable Profile Plugin Override (platforms/bluebubbles)"),
              h("div", { className: "bbx-help" },
                meta.plugin_installed
                  ? "Installed at " + meta.plugin_dir
                  : "Not yet copied to this profile (click 'Sync Plugin Files')"
              )
            ),
            h(Checkbox, {
              checked: !!eff.plugin_enabled,
              onCheckedChange: function (v) { updateField("plugin_enabled", v); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" }, "Enable BlueBubbles Agent Toolset"),
              h("div", { className: "bbx-help" }, "Registers tapback, edit, unsend, effect, chat_action, and lookup tools")
            ),
            h(Checkbox, {
              checked: !!eff.toolsets_enabled,
              onCheckedChange: function (v) { updateField("toolsets_enabled", v); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" }, "Mirror Settings to Profile .env"),
              h("div", { className: "bbx-help" }, "Keep BLUEBUBBLES_* variables in .env synced with config.yaml")
            ),
            h(Checkbox, {
              checked: !!syncDotenv,
              onCheckedChange: function (v) { setSyncDotenv(!!v); },
            })
          )
        ),

        // 2. Multi-Profile DM & Group Chat Routing
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "2. Multi-Profile Chat Routing (;-; vs ;+;)"),
            h("span", { className: "bbx-env-tag" }, routingLabel(meta.routing_mode))
          ),
          h("div", { className: "bbx-field" },
            h("div", { className: "bbx-label" }, "Quick Routing Presets"),
            h("div", { className: "bbx-presets" },
              h(Button, {
                type: "button",
                size: "sm",
                variant: eff.ignore_direct_messages && !eff.ignore_group_messages ? "default" : "secondary",
                onClick: function () { applyRoutingPreset("group_only"); },
              }, "Group-Only (;+;)"),
              h(Button, {
                type: "button",
                size: "sm",
                variant: eff.ignore_group_messages && !eff.ignore_direct_messages ? "default" : "secondary",
                onClick: function () { applyRoutingPreset("dm_only"); },
              }, "DM-Only (;-;)"),
              h(Button, {
                type: "button",
                size: "sm",
                variant: !eff.ignore_direct_messages && !eff.ignore_group_messages && (!eff.allowed_chat_guids || !eff.allowed_chat_guids.length)
                  ? "default"
                  : "secondary",
                onClick: function () { applyRoutingPreset("all"); },
              }, "Allow Both (DMs & Groups)")
            )
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" },
                "Ignore Direct Messages (;-;) ",
                h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_IGNORE_DIRECT_MESSAGES")
              ),
              h("div", { className: "bbx-help" }, "Drop 1:1 DM chats on this profile so another profile can own DMs.")
            ),
            h(Checkbox, {
              checked: !!eff.ignore_direct_messages,
              onCheckedChange: function (v) { updateField("ignore_direct_messages", v); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" },
                "Ignore Group Chats (;+;) ",
                h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_IGNORE_GROUP_MESSAGES")
              ),
              h("div", { className: "bbx-help" }, "Drop group chats on this profile so it only responds to 1:1 DMs.")
            ),
            h(Checkbox, {
              checked: !!eff.ignore_group_messages,
              onCheckedChange: function (v) { updateField("ignore_group_messages", v); },
            })
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Allowed Chat GUIDs ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_ALLOWED_CHAT_GUIDS")
            ),
            h(TagListEditor, {
              items: eff.allowed_chat_guids || [],
              placeholder: "any;+;74ca28f2630d48908a8b72a9d3865376 or any;-;+16179431941",
              emptyText: "No chat GUID filter set — any chat matching the DM/Group toggles above is allowed.",
              onChange: function (next) { updateField("allowed_chat_guids", next); },
              extraButton: h(Button, {
                type: "button",
                size: "sm",
                variant: "secondary",
                onClick: toggleChatPicker,
              }, showChatPicker ? "Hide Chat Picker" : "Pick from Live Chats"),
            })
          ),

          // Live Chat Picker Drawer
          showChatPicker
            ? h("div", { className: "bbx-field" },
                h("div", { className: "bbx-input-group" },
                  h(Input, {
                    value: chatSearch,
                    placeholder: "Search live chats by name, phone, or GUID…",
                    onChange: function (e) { setChatSearch(e.target.value); },
                  }),
                  h(Button, {
                    type: "button",
                    size: "sm",
                    variant: chatFilterKind === "all" ? "default" : "secondary",
                    onClick: function () { setChatFilterKind("all"); },
                  }, "All"),
                  h(Button, {
                    type: "button",
                    size: "sm",
                    variant: chatFilterKind === "group" ? "default" : "secondary",
                    onClick: function () { setChatFilterKind("group"); },
                  }, "Groups (;+;)"),
                  h(Button, {
                    type: "button",
                    size: "sm",
                    variant: chatFilterKind === "dm" ? "default" : "secondary",
                    onClick: function () { setChatFilterKind("dm"); },
                  }, "DMs (;-;)"),
                  h(Button, {
                    type: "button",
                    size: "sm",
                    variant: "secondary",
                    disabled: loadingChats,
                    onClick: loadLiveChats,
                  }, loadingChats ? "…" : "↻")
                ),
                h("div", { className: "bbx-picker-box" },
                  loadingChats
                    ? h("div", { className: "bbx-picker-item" }, "Loading chats from BlueBubbles…")
                    : filteredChats.length === 0
                      ? h("div", { className: "bbx-picker-item" }, "No matching chats found.")
                      : filteredChats.map(function (ch) {
                          const allowedList = eff.allowed_chat_guids || [];
                          const isAllowed = allowedList.indexOf(ch.canonical_guid) !== -1 || allowedList.indexOf(ch.guid) !== -1;
                          const isHome = eff.home_channel === ch.canonical_guid || eff.home_channel === ch.guid;
                          return h("div", { key: ch.guid, className: "bbx-picker-item" },
                            h("div", { style: { minWidth: 0, flex: 1 } },
                              h("div", { className: "bbx-label" },
                                h("span", { className: "bbx-env-tag" }, ch.delimiter),
                                h("span", null, ch.display_name)
                              ),
                              h("div", { className: "bbx-mono", style: { opacity: 0.7 } }, ch.canonical_guid),
                              ch.last_message_text
                                ? h("div", { className: "bbx-help" }, ch.last_message_text)
                                : null
                            ),
                            h("div", { className: "bbx-actions" },
                              h(Button, {
                                type: "button",
                                size: "sm",
                                variant: isAllowed ? "default" : "secondary",
                                onClick: function () { toggleAllowedChatGuid(ch.canonical_guid); },
                              }, isAllowed ? "✓ Allowed" : "+ Allow"),
                              h(Button, {
                                type: "button",
                                size: "sm",
                                variant: isHome ? "default" : "secondary",
                                onClick: function () {
                                  updateField("home_channel", ch.canonical_guid);
                                  if (!eff.home_channel_name && ch.display_name) {
                                    updateField("home_channel_name", ch.display_name);
                                  }
                                },
                              }, isHome ? "★ Home" : "Set Home")
                            )
                          );
                        })
                )
              )
            : null
        ),

        // 3. Access Control & Sender Allowlist
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "3. Sender Access Control")
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" },
                "Allow All Senders ",
                h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_ALLOW_ALL_USERS")
              ),
              h("div", { className: "bbx-help" }, "Let any sender in an allowed chat trigger the bot without an allowlist check.")
            ),
            h(Checkbox, {
              checked: !!eff.allow_all_users,
              onCheckedChange: function (v) { updateField("allow_all_users", v); },
            })
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Allowed Senders (Phone / Apple ID) ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_ALLOWED_USERS")
            ),
            h(TagListEditor, {
              items: eff.allowed_users || [],
              placeholder: "+16179431941, yannipang@icloud.com",
              emptyText: eff.allow_all_users
                ? "Allow All Senders is enabled above."
                : "No allowed users set — add phone numbers or Apple ID emails.",
              onChange: function (next) { updateField("allowed_users", next); },
              extraButton: h(Button, {
                type: "button",
                size: "sm",
                variant: "secondary",
                onClick: toggleContactPicker,
              }, showContactPicker ? "Hide Contacts" : "Pick from Contacts"),
            })
          ),

          showContactPicker
            ? h("div", { className: "bbx-field" },
                h("div", { className: "bbx-input-group" },
                  h(Input, {
                    value: contactSearch,
                    placeholder: "Search BlueBubbles contacts by name, phone, or email…",
                    onChange: function (e) { setContactSearch(e.target.value); },
                  }),
                  h(Button, {
                    type: "button",
                    size: "sm",
                    variant: "secondary",
                    disabled: loadingContacts,
                    onClick: loadLiveContacts,
                  }, loadingContacts ? "…" : "↻")
                ),
                h("div", { className: "bbx-picker-box" },
                  loadingContacts
                    ? h("div", { className: "bbx-picker-item" }, "Loading contacts from BlueBubbles…")
                    : filteredContacts.length === 0
                      ? h("div", { className: "bbx-picker-item" }, "No matching contacts.")
                      : filteredContacts.slice(0, 80).map(function (c) {
                          const curUsers = eff.allowed_users || [];
                          return h("div", { key: c.display_name + "|" + (c.handles || []).join(","), className: "bbx-picker-item" },
                            h("div", null,
                              h("div", { className: "bbx-label" }, c.display_name),
                              h("div", { className: "bbx-mono", style: { opacity: 0.72 } }, (c.handles || []).join(", "))
                            ),
                            h("div", { className: "bbx-actions" },
                              (c.handles || []).map(function (hStr) {
                                const active = curUsers.indexOf(hStr) !== -1;
                                return h(Button, {
                                  key: hStr,
                                  type: "button",
                                  size: "sm",
                                  variant: active ? "default" : "secondary",
                                  onClick: function () { toggleAllowedUser(hStr); },
                                }, active ? "✓ " + hStr : "+ " + hStr);
                              })
                            )
                          );
                        })
                )
              )
            : null
        ),

        // 4. Home Channel & Notifications
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "4. Home Channel & Notifications")
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Home Channel Chat GUID / Address ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_HOME_CHANNEL")
            ),
            h(Input, {
              value: eff.home_channel || "",
              placeholder: "any;-;+16179431941 or any;+;74ca28f2630d48908a8b72a9d3865376",
              onChange: function (e) { updateField("home_channel", e.target.value); },
            }),
            h("div", { className: "bbx-help" },
              "Target chat for cron job deliveries and proactive messages. Also participates in DM/Group routing inference."
            )
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Home Channel Display Name ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_HOME_CHANNEL_NAME")
            ),
            h(Input, {
              value: eff.home_channel_name || "",
              placeholder: "Family Group / Personal DM",
              onChange: function (e) { updateField("home_channel_name", e.target.value); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" }, "Suppress Gateway Restart Push Notifications"),
              h("div", { className: "bbx-help" }, "Sets platforms.push.gateway_restart_notification: false")
            ),
            h(Checkbox, {
              checked: !!eff.suppress_restart_push,
              onCheckedChange: function (v) { updateField("suppress_restart_push", v); },
            })
          )
        ),

        // 5. Group Wake-Word & Read Receipts
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "5. Group Wake-Words & Read Receipts")
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" },
                "Automatically Send Read Receipts ",
                h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_SEND_READ_RECEIPTS")
              ),
              h("div", { className: "bbx-help" }, "Marks inbound chats as read when a message is accepted by the gateway.")
            ),
            h(Checkbox, {
              checked: !!eff.send_read_receipts,
              onCheckedChange: function (v) { updateField("send_read_receipts", v); },
            })
          ),
          h("div", { className: "bbx-field-row" },
            h("div", null,
              h("div", { className: "bbx-label" },
                "Require Mention / Wake-Word in Group Chats ",
                h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_REQUIRE_MENTION")
              ),
              h("div", { className: "bbx-help" }, "When enabled, group messages only trigger a turn if they match a wake word or reply to the bot.")
            ),
            h(Checkbox, {
              checked: !!eff.require_mention,
              onCheckedChange: function (v) { updateField("require_mention", v); },
            })
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Group Wake-Word Regex Patterns ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_MENTION_PATTERNS")
            ),
            h(TagListEditor, {
              items: eff.mention_patterns || [],
              placeholder: "\\bhermes\\b, @assistant",
              emptyText: "Using default Hermes wake-word patterns.",
              onChange: function (next) { updateField("mention_patterns", next); },
            })
          )
        ),

        // 6. Webhook Listener Settings
        h("div", { className: "bbx-card" },
          h("div", { className: "bbx-card-head" },
            h("div", { className: "bbx-card-title" }, "6. Webhook Listener Configuration")
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Webhook Bind Host ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_WEBHOOK_HOST")
            ),
            h(Input, {
              value: eff.webhook_host || "127.0.0.1",
              placeholder: "127.0.0.1",
              onChange: function (e) { updateField("webhook_host", e.target.value); },
            })
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Webhook Bind Port ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_WEBHOOK_PORT")
            ),
            h(Input, {
              type: "number",
              value: String(eff.webhook_port == null ? 8645 : eff.webhook_port),
              placeholder: "8645",
              onChange: function (e) {
                const n = parseInt(e.target.value, 10);
                updateField("webhook_port", isNaN(n) ? 8645 : n);
              },
            })
          ),
          h("div", { className: "bbx-field" },
            h(Label, { className: "bbx-label" },
              "Webhook HTTP Path ",
              h("span", { className: "bbx-env-tag" }, "BLUEBUBBLES_WEBHOOK_PATH")
            ),
            h(Input, {
              value: eff.webhook_path || "/bluebubbles-webhook",
              placeholder: "/bluebubbles-webhook",
              onChange: function (e) { updateField("webhook_path", e.target.value); },
            }),
            h("div", { className: "bbx-help" },
              "Note: When the gateway API server runs on port 8642, Hermes automatically multiplexes webhooks at /p/<profile>/bluebubbles-webhook."
            )
          )
        )
      ),

      // --- Collapsible Raw Config Preview ---
      h("div", { className: "bbx-card bbx-card--full" },
        h("div", { className: "bbx-card-head" },
          h("div", { className: "bbx-card-title" }, "Raw Profile Files Preview (" + selectedProfile + ")"),
          h(Button, {
            type: "button",
            size: "sm",
            variant: "secondary",
            onClick: function () { setShowRawPreview(!showRawPreview); },
          }, showRawPreview ? "Hide Raw Files" : "Show config.yaml & .env")
        ),
        showRawPreview
          ? h("div", { className: "bbx-grid" },
              h("div", { className: "bbx-field" },
                h("div", { className: "bbx-label" }, meta.config_yaml_path || "config.yaml"),
                h("pre", { className: "bbx-pre" }, meta.yaml_preview || "# Empty")
              ),
              h("div", { className: "bbx-field" },
                h("div", { className: "bbx-label" }, meta.dotenv_path || ".env"),
                h("pre", { className: "bbx-pre" }, meta.dotenv_preview || "# No BLUEBUBBLES_* keys in profile .env")
              )
            )
          : h("div", { className: "bbx-help" },
              "Click 'Show config.yaml & .env' to inspect the exact on-disk YAML and environment variables for this profile."
            )
      )
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("bluebubbles-extender", BlueBubblesExtenderPage);
  }
})();
