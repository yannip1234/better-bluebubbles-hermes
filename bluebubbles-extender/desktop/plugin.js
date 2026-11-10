/**
 * Hermes Desktop runtime plugin for `bluebubbles-extender`.
 *
 * Registers the "BlueBubbles (iMessage)" configuration suite inside
 * Settings > Plugins (`ctx.registerSettingsPage`) with four sub-pages:
 *   1. Overview & Server (main page)
 *   2. Routing & Chats (`routing`)
 *   3. Access & Contacts (`access`)
 *   4. Behavior & Webhooks (`behavior`)
 */

import {
  atom,
  Badge,
  Button,
  cn,
  host,
  Input,
  ListRow,
  SegmentedControl,
  Separator,
  Textarea,
  ToggleRow,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const PASSWORD_MASK = '••••••••'

const DEFAULT_EFFECTIVE = {
  server_url: 'http://127.0.0.1:1234',
  password: '',
  platform_enabled: true,
  plugin_enabled: true,
  toolsets_enabled: true,
  allowed_chat_guids: [],
  ignore_direct_messages: false,
  ignore_group_messages: false,
  allowed_users: [],
  allow_all_users: false,
  home_channel: '',
  home_channel_name: '',
  suppress_restart_push: false,
  require_mention: false,
  mention_patterns: [],
  send_read_receipts: true,
  webhook_host: '127.0.0.1',
  webhook_port: 8645,
  webhook_path: '/bluebubbles-webhook'
}

function parseListText(raw) {
  if (Array.isArray(raw)) {
    return raw.map(item => String(item).trim()).filter(Boolean)
  }
  return String(raw || '')
    .split(/[,\n]+/)
    .map(item => item.trim())
    .filter(Boolean)
}

function formatListText(list) {
  if (!Array.isArray(list)) {
    return String(list || '')
  }
  return list.join('\n')
}

function createStore(ctx) {
  const $state = atom({
    loading: false,
    saving: false,
    testing: false,
    syncing: false,
    restarting: false,
    error: null,
    notice: null,
    selectedProfile: 'family',
    profiles: [
      { name: 'default', routing_mode: 'all' },
      { name: 'family', routing_mode: 'group_only' },
      { name: 'personal', routing_mode: 'dm_only' }
    ],
    effective: { ...DEFAULT_EFFECTIVE },
    draft: { ...DEFAULT_EFFECTIVE },
    meta: {},
    dirty: false,
    connectionTest: null,
    liveChats: [],
    loadingChats: false,
    liveContacts: [],
    loadingContacts: false
  })

  const patchState = patch => {
    $state.set({ ...$state.get(), ...patch })
  }

  const updateDraft = patch => {
    const cur = $state.get()
    const nextDraft = { ...cur.draft, ...patch }
    patchState({ draft: nextDraft, dirty: true, notice: null })
  }

  const loadProfile = async profileName => {
    const target = profileName || $state.get().selectedProfile || 'family'
    patchState({ loading: true, error: null, selectedProfile: target })
    try {
      const res = await ctx.rest(`/state?target_profile=${encodeURIComponent(target)}`)
      const profState = res?.profile_state || {}
      const eff = { ...DEFAULT_EFFECTIVE, ...(profState.effective || {}) }
      patchState({
        loading: false,
        selectedProfile: res?.selected_profile || target,
        profiles: Array.isArray(res?.profiles) && res.profiles.length ? res.profiles : $state.get().profiles,
        effective: eff,
        draft: { ...eff },
        meta: profState.meta || {},
        dirty: false
      })
    } catch (err) {
      // Fallback: read via gateway plugins.manage list if REST route is not mounted on current profile
      try {
        const rpc = await host.request(
          'plugins.manage',
          target && target !== 'default' ? { action: 'list', profile: target } : { action: 'list' }
        )
        const rows = Array.isArray(rpc?.plugins) ? rpc.plugins : []
        const row = rows.find(r => r.key === 'bluebubbles-extender' || r.name === 'bluebubbles-extender')
        if (row && Array.isArray(row.settings_schema)) {
          const eff = { ...DEFAULT_EFFECTIVE }
          for (const field of row.settings_schema) {
            if (field.type === 'secret') {
              eff[field.key] = field.has_value ? PASSWORD_MASK : ''
            } else if (field.value !== undefined) {
              eff[field.key] = field.value
            }
          }
          patchState({
            loading: false,
            selectedProfile: target,
            effective: eff,
            draft: { ...eff },
            meta: { has_password: eff.password === PASSWORD_MASK },
            dirty: false,
            error: null
          })
          return
        }
      } catch {
        // Keep primary REST error below
      }
      patchState({
        loading: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  }

  const saveProfile = async ({ syncPlugin = true, restart = false } = {}) => {
    const cur = $state.get()
    const target = cur.selectedProfile || 'family'
    patchState({ saving: true, error: null, notice: null })
    try {
      const res = await ctx.rest('/settings', {
        method: 'PUT',
        body: {
          target_profile: target,
          settings: cur.draft,
          sync_dotenv: true,
          sync_plugin: syncPlugin,
          restart
        }
      })
      const profState = res?.profile_state || {}
      const eff = { ...DEFAULT_EFFECTIVE, ...(profState.effective || cur.draft) }
      patchState({
        saving: false,
        profiles: Array.isArray(res?.profiles) && res.profiles.length ? res.profiles : cur.profiles,
        effective: eff,
        draft: { ...eff },
        meta: profState.meta || cur.meta,
        dirty: false,
        notice: restart
          ? `Saved "${target}" settings & triggered gateway restart.`
          : `Saved BlueBubbles settings for "${target}".`
      })
      host.notify?.({ kind: 'success', message: `Saved BlueBubbles settings (${target})` })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      patchState({ saving: false, error: msg })
      host.notifyError?.(err, `Failed to save BlueBubbles settings (${target})`)
    }
  }

  const testServer = async () => {
    const cur = $state.get()
    patchState({ testing: true, error: null, connectionTest: null })
    try {
      const res = await ctx.rest('/test-connection', {
        method: 'POST',
        body: {
          target_profile: cur.selectedProfile,
          server_url: cur.draft.server_url,
          password: cur.draft.password
        }
      })
      patchState({ testing: false, connectionTest: res })
    } catch (err) {
      patchState({
        testing: false,
        connectionTest: { ok: false, error: err instanceof Error ? err.message : String(err) }
      })
    }
  }

  const fetchLiveChats = async () => {
    const cur = $state.get()
    patchState({ loadingChats: true, error: null })
    try {
      const res = await ctx.rest(
        `/live/chats?target_profile=${encodeURIComponent(cur.selectedProfile)}&limit=100`
      )
      patchState({
        loadingChats: false,
        liveChats: Array.isArray(res?.chats) ? res.chats : [],
        error: res?.ok === false ? res.error : null
      })
    } catch (err) {
      patchState({
        loadingChats: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  }

  const fetchLiveContacts = async () => {
    const cur = $state.get()
    patchState({ loadingContacts: true, error: null })
    try {
      const res = await ctx.rest(
        `/live/contacts?target_profile=${encodeURIComponent(cur.selectedProfile)}&limit=200`
      )
      patchState({
        loadingContacts: false,
        liveContacts: Array.isArray(res?.contacts) ? res.contacts : [],
        error: res?.ok === false ? res.error : null
      })
    } catch (err) {
      patchState({
        loadingContacts: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  }

  const syncConnector = async () => {
    patchState({ syncing: true, error: null, notice: null })
    try {
      const res = await ctx.rest('/sync-plugin', {
        method: 'POST',
        body: { profiles: null }
      })
      patchState({
        syncing: false,
        notice: res?.ok
          ? `Synced BlueBubbles connector plugin to ${(res.synced_profiles || []).map(p => p.profile).join(', ')}.`
          : `Sync failed: ${res?.error || 'unknown error'}`
      })
    } catch (err) {
      patchState({
        syncing: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  }

  const restartGateways = async profileTarget => {
    patchState({ restarting: true, error: null, notice: null })
    try {
      const res = await ctx.rest('/restart-gateway', {
        method: 'POST',
        body: { target_profile: profileTarget || 'all' }
      })
      patchState({
        restarting: false,
        notice: res?.ok ? 'Gateway restart completed.' : 'Gateway restart reported warnings.'
      })
    } catch (err) {
      patchState({
        restarting: false,
        error: err instanceof Error ? err.message : String(err)
      })
    }
  }

  return {
    $state,
    updateDraft,
    loadProfile,
    saveProfile,
    testServer,
    fetchLiveChats,
    fetchLiveContacts,
    syncConnector,
    restartGateways
  }
}

function HeaderBar({ store, title, subtitle }) {
  const state = useValue(store.$state)

  useEffect(() => {
    if (!state.meta?.config_yaml_path && !state.loading) {
      void store.loadProfile(state.selectedProfile)
    }
  }, [])

  const profileOptions = (state.profiles || []).map(p => ({
    id: p.name,
    label: p.name === 'default' ? 'Mac / default' : p.name
  }))

  return jsxs('div', {
    className: 'mb-4 space-y-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center justify-between gap-2',
        children: [
          jsxs('div', {
            children: [
              jsx('h3', {
                className: 'text-base font-semibold text-(--ui-text-primary)',
                children: title
              }),
              subtitle &&
                jsx('p', {
                  className: 'text-xs text-(--ui-text-tertiary)',
                  children: subtitle
                })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Button, {
                disabled: state.loading || state.saving,
                onClick: () => void store.loadProfile(state.selectedProfile),
                size: 'sm',
                variant: 'outline',
                children: state.loading ? 'Refreshing…' : 'Refresh'
              }),
              jsx(Button, {
                disabled: state.saving || !state.dirty,
                onClick: () => void store.saveProfile({ syncPlugin: true, restart: false }),
                size: 'sm',
                children: state.saving ? 'Saving…' : 'Save Settings'
              }),
              jsx(Button, {
                disabled: state.saving,
                onClick: () => void store.saveProfile({ syncPlugin: true, restart: true }),
                size: 'sm',
                variant: 'secondary',
                children: 'Save & Restart'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className:
          'flex flex-wrap items-center justify-between gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--chrome-panel-bg) px-3 py-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', {
                className: 'text-xs font-medium text-(--ui-text-secondary)',
                children: 'Profile scope:'
              }),
              jsx(SegmentedControl, {
                onChange: nextProfile => void store.loadProfile(nextProfile),
                options: profileOptions.length
                  ? profileOptions
                  : [
                      { id: 'default', label: 'Mac / default' },
                      { id: 'family', label: 'family' },
                      { id: 'personal', label: 'personal' }
                    ],
                value: state.selectedProfile
              })
            ]
          }),
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-1.5',
            children: [
              jsx(Badge, {
                variant: state.draft.platform_enabled ? 'default' : 'secondary',
                children: state.draft.platform_enabled ? 'Adapter Enabled' : 'Adapter Off'
              }),
              jsx(Badge, {
                variant: 'outline',
                children: `Routing: ${state.meta?.routing_mode || 'custom'}`
              }),
              state.dirty &&
                jsx(Badge, {
                  variant: 'destructive',
                  children: 'Unsaved edits'
                })
            ]
          })
        ]
      }),
      state.error &&
        jsx('div', {
          className: 'rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive',
          role: 'alert',
          children: state.error
        }),
      state.notice &&
        jsx('div', {
          className:
            'rounded-md border border-(--ui-stroke-tertiary) bg-(--chrome-panel-bg) px-3 py-2 text-xs text-(--ui-text-secondary)',
          role: 'status',
          children: state.notice
        })
    ]
  })
}

function OverviewPage({ store }) {
  const state = useValue(store.$state)
  const d = state.draft

  return jsxs('div', {
    className: 'space-y-2',
    children: [
      jsx(HeaderBar, {
        store,
        title: 'BlueBubbles (iMessage) — Server & Activation',
        subtitle:
          'Configure BlueBubbles Server credentials, profile activation toggles, plugin sync, and live diagnostics.'
      }),
      jsx(ListRow, {
        title: 'BlueBubbles Server URL',
        description: 'Base HTTP/HTTPS URL of your macOS BlueBubbles Server (Settings -> API).',
        action: jsx(Input, {
          className: 'w-72 font-mono text-xs',
          value: d.server_url || '',
          placeholder: 'http://127.0.0.1:1234',
          onChange: e => store.updateDraft({ server_url: e.currentTarget.value })
        })
      }),
      jsx(ListRow, {
        title: 'BlueBubbles API Password',
        description: state.meta?.has_password
          ? 'Password is stored in .env. Leave masked to keep current password.'
          : 'Enter your BlueBubbles Server API password (stored in .env).',
        action: jsx(Input, {
          className: 'w-72 font-mono text-xs',
          type: 'password',
          value: d.password || '',
          placeholder: state.meta?.has_password ? PASSWORD_MASK : 'Enter API password',
          onChange: e => store.updateDraft({ password: e.currentTarget.value })
        })
      }),
      jsx(ListRow, {
        title: 'Live Connection Diagnostic',
        description: state.connectionTest
          ? state.connectionTest.ok
            ? `Connected (${state.connectionTest.latency_ms} ms) • BB v${state.connectionTest.server_info?.server_version || '?'} • macOS ${state.connectionTest.server_info?.os_version || '?'} • Private API: ${state.connectionTest.server_info?.private_api ? 'Yes' : 'No'}`
            : `Connection failed: ${state.connectionTest.error}`
          : 'Verify reachability, API password, and Private API helper status against BlueBubbles Server.',
        action: jsx(Button, {
          disabled: state.testing,
          onClick: () => void store.testServer(),
          size: 'sm',
          variant: 'outline',
          children: state.testing ? 'Testing…' : 'Test Connection'
        })
      }),
      jsx(Separator, { className: 'my-2' }),
      jsx(ToggleRow, {
        label: 'Enable BlueBubbles Platform Adapter',
        description: 'Set platforms.bluebubbles.enabled in this profile’s config.yaml.',
        checked: Boolean(d.platform_enabled),
        onChange: on => store.updateDraft({ platform_enabled: on })
      }),
      jsx(ToggleRow, {
        label: 'Enable Custom BlueBubbles Plugin Override (platforms/bluebubbles)',
        description: 'Load the custom BlueBubbles connector plugin (DM/Group GUID routing + FindMy Friends).',
        checked: Boolean(d.plugin_enabled),
        onChange: on => store.updateDraft({ plugin_enabled: on })
      }),
      jsx(ToggleRow, {
        label: 'Enable BlueBubbles Agent Toolset',
        description:
          'Expose tapback, edit_message, unsend_message, send_effect, chat_action, and lookup tools to Hermes.',
        checked: Boolean(d.toolsets_enabled),
        onChange: on => store.updateDraft({ toolsets_enabled: on })
      }),
      jsx(Separator, { className: 'my-2' }),
      jsx(ListRow, {
        title: 'Sync Connector Plugin & Restart Gateways',
        description:
          'Deploy the latest bluebubbles-connector/plugin code across all profiles or restart the profile gateways.',
        action: jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, {
              disabled: state.syncing,
              onClick: () => void store.syncConnector(),
              size: 'sm',
              variant: 'outline',
              children: state.syncing ? 'Syncing…' : 'Sync Plugin Files'
            }),
            jsx(Button, {
              disabled: state.restarting,
              onClick: () => void store.restartGateways('all'),
              size: 'sm',
              variant: 'outline',
              children: state.restarting ? 'Restarting…' : 'Restart Gateways'
            })
          ]
        })
      })
    ]
  })
}

function RoutingPage({ store }) {
  const state = useValue(store.$state)
  const d = state.draft
  const [guidsText, setGuidsText] = useState(() => formatListText(d.allowed_chat_guids))

  useEffect(() => {
    setGuidsText(formatListText(state.draft.allowed_chat_guids))
  }, [state.selectedProfile, state.effective])

  const applyPreset = preset => {
    if (preset === 'group_only') {
      store.updateDraft({ ignore_direct_messages: true, ignore_group_messages: false })
    } else if (preset === 'dm_only') {
      store.updateDraft({ ignore_direct_messages: false, ignore_group_messages: true })
    } else {
      store.updateDraft({ ignore_direct_messages: false, ignore_group_messages: false })
    }
  }

  const toggleChatGuid = guid => {
    const cur = new Set(parseListText(d.allowed_chat_guids))
    if (cur.has(guid)) {
      cur.delete(guid)
    } else {
      cur.add(guid)
    }
    const next = [...cur]
    setGuidsText(formatListText(next))
    store.updateDraft({ allowed_chat_guids: next })
  }

  return jsxs('div', {
    className: 'space-y-2',
    children: [
      jsx(HeaderBar, {
        store,
        title: 'Routing & Chats (DM ;-; vs Group ;+;)',
        subtitle:
          'Isolate profiles by DM (;-;) or Group (;+;) chat GUIDs and configure the default Home Channel.'
      }),
      jsx(ListRow, {
        title: 'Quick Routing Preset',
        description:
          'Apply a one-click posture (e.g., Family = Group Chats Only; Personal = Direct Messages Only).',
        action: jsxs('div', {
          className: 'flex items-center gap-1.5',
          children: [
            jsx(Button, {
              onClick: () => applyPreset('group_only'),
              size: 'sm',
              variant: d.ignore_direct_messages && !d.ignore_group_messages ? 'default' : 'outline',
              children: 'Group Only (;+;)'
            }),
            jsx(Button, {
              onClick: () => applyPreset('dm_only'),
              size: 'sm',
              variant: d.ignore_group_messages && !d.ignore_direct_messages ? 'default' : 'outline',
              children: 'DM Only (;-;)'
            }),
            jsx(Button, {
              onClick: () => applyPreset('all'),
              size: 'sm',
              variant: !d.ignore_direct_messages && !d.ignore_group_messages ? 'default' : 'outline',
              children: 'Allow Both'
            })
          ]
        })
      }),
      jsx(ToggleRow, {
        label: 'Ignore Direct Messages (;-;)',
        description: 'Drop all 1:1 direct message chats on this profile.',
        checked: Boolean(d.ignore_direct_messages),
        onChange: on => store.updateDraft({ ignore_direct_messages: on })
      }),
      jsx(ToggleRow, {
        label: 'Ignore Group Chats (;+;)',
        description: 'Drop all group chat messages on this profile.',
        checked: Boolean(d.ignore_group_messages),
        onChange: on => store.updateDraft({ ignore_group_messages: on })
      }),
      jsx(ListRow, {
        title: 'Allowed Chat GUIDs',
        description:
          'One chat GUID per line (or comma-separated). Supports exact GUIDs (any;+;chat0123456789abcdef0123456789ab) or DM handles (any;-;+15551234567). Leave empty to allow all chats permitted by the toggles above.',
        wide: true,
        below: jsxs('div', {
          className: 'mt-2 space-y-2',
          children: [
            jsx(Textarea, {
              className: 'min-h-24 font-mono text-xs',
              placeholder: 'any;+;chat0123456789abcdef0123456789ab\nany;-;+15551234567',
              value: guidsText,
              onChange: e => {
                const raw = e.currentTarget.value
                setGuidsText(raw)
                store.updateDraft({ allowed_chat_guids: parseListText(raw) })
              }
            }),
            jsxs('div', {
              className: 'flex items-center justify-between',
              children: [
                jsx('span', {
                  className: 'text-xs text-(--ui-text-tertiary)',
                  children: `${(d.allowed_chat_guids || []).length} chat GUID(s) configured`
                }),
                jsx(Button, {
                  disabled: state.loadingChats,
                  onClick: () => void store.fetchLiveChats(),
                  size: 'sm',
                  variant: 'outline',
                  children: state.loadingChats ? 'Loading Chats…' : 'Browse Live Chats from BlueBubbles'
                })
              ]
            }),
            state.liveChats.length > 0 &&
              jsx('div', {
                className:
                  'max-h-56 overflow-y-auto rounded-md border border-(--ui-stroke-tertiary) divide-y divide-(--ui-stroke-tertiary)',
                children: state.liveChats.map(chat => {
                  const selected = (d.allowed_chat_guids || []).includes(chat.guid)
                  const isHome = d.home_channel === chat.guid
                  return jsxs(
                    'div',
                    {
                      className: 'flex items-center justify-between gap-2 px-3 py-2 text-xs',
                      children: [
                        jsxs('div', {
                          className: 'min-w-0 flex-1',
                          children: [
                            jsxs('div', {
                              className: 'flex items-center gap-1.5 font-medium text-(--ui-text-primary)',
                              children: [
                                jsx(Badge, {
                                  variant: chat.kind === 'group' ? 'default' : 'secondary',
                                  children: chat.kind === 'group' ? 'Group ;+;' : 'DM ;-;'
                                }),
                                jsx('span', { className: 'truncate', children: chat.label })
                              ]
                            }),
                            jsx('div', {
                              className: 'truncate font-mono text-[11px] text-(--ui-text-tertiary)',
                              children: chat.guid
                            })
                          ]
                        }),
                        jsxs('div', {
                          className: 'flex shrink-0 items-center gap-1.5',
                          children: [
                            jsx(Button, {
                              onClick: () => toggleChatGuid(chat.guid),
                              size: 'sm',
                              variant: selected ? 'default' : 'outline',
                              children: selected ? 'Allowed ✓' : 'Allow'
                            }),
                            jsx(Button, {
                              onClick: () =>
                                store.updateDraft({
                                  home_channel: chat.guid,
                                  home_channel_name: chat.display_name || chat.label || ''
                                }),
                              size: 'sm',
                              variant: isHome ? 'default' : 'ghost',
                              children: isHome ? 'Home ✓' : 'Set Home'
                            })
                          ]
                        })
                      ]
                    },
                    chat.guid
                  )
                })
              })
          ]
        })
      }),
      jsx(Separator, { className: 'my-2' }),
      jsx(ListRow, {
        title: 'Home Channel (Chat GUID or Handle)',
        description: 'Default destination for cron job deliveries and proactive notifications.',
        action: jsx(Input, {
          className: 'w-72 font-mono text-xs',
          placeholder: 'any;+;chat0123456789abcdef0123456789ab',
          value: d.home_channel || '',
          onChange: e => store.updateDraft({ home_channel: e.currentTarget.value })
        })
      }),
      jsx(ListRow, {
        title: 'Home Channel Display Name',
        description: 'Human-readable label for the Home Channel.',
        action: jsx(Input, {
          className: 'w-72 text-xs',
          placeholder: 'Family Group / Personal DM',
          value: d.home_channel_name || '',
          onChange: e => store.updateDraft({ home_channel_name: e.currentTarget.value })
        })
      }),
      jsx(ToggleRow, {
        label: 'Suppress Gateway Restart Push Notifications',
        description: 'Keep gateway restarts silent on this profile (platforms.push.gateway_restart_notification: false).',
        checked: Boolean(d.suppress_restart_push),
        onChange: on => store.updateDraft({ suppress_restart_push: on })
      })
    ]
  })
}

function AccessPage({ store }) {
  const state = useValue(store.$state)
  const d = state.draft
  const [usersText, setUsersText] = useState(() => formatListText(d.allowed_users))

  useEffect(() => {
    setUsersText(formatListText(state.draft.allowed_users))
  }, [state.selectedProfile, state.effective])

  const toggleAllowedUser = address => {
    const cur = new Set(parseListText(d.allowed_users))
    if (cur.has(address)) {
      cur.delete(address)
    } else {
      cur.add(address)
    }
    const next = [...cur]
    setUsersText(formatListText(next))
    store.updateDraft({ allowed_users: next })
  }

  return jsxs('div', {
    className: 'space-y-2',
    children: [
      jsx(HeaderBar, {
        store,
        title: 'Access Control & Senders',
        subtitle:
          'Configure which phone numbers (E.164) and Apple ID emails can interact with this profile.'
      }),
      jsx(ToggleRow, {
        label: 'Allow All Senders (BLUEBUBBLES_ALLOW_ALL_USERS)',
        description:
          'Allow any participant in allowed chats to message the bot without checking the sender allowlist.',
        checked: Boolean(d.allow_all_users),
        onChange: on => store.updateDraft({ allow_all_users: on })
      }),
      jsx(ListRow, {
        title: 'Allowed Senders (Phone Numbers / Apple IDs)',
        description:
          'One phone number (+15551234567) or email (user@icloud.com) per line, or comma-separated.',
        wide: true,
        below: jsxs('div', {
          className: 'mt-2 space-y-2',
          children: [
            jsx(Textarea, {
              className: 'min-h-24 font-mono text-xs',
              placeholder: '+15551234567\nuser@icloud.com',
              value: usersText,
              onChange: e => {
                const raw = e.currentTarget.value
                setUsersText(raw)
                store.updateDraft({ allowed_users: parseListText(raw) })
              }
            }),
            jsxs('div', {
              className: 'flex items-center justify-between',
              children: [
                jsx('span', {
                  className: 'text-xs text-(--ui-text-tertiary)',
                  children: `${(d.allowed_users || []).length} allowed sender(s)`
                }),
                jsx(Button, {
                  disabled: state.loadingContacts,
                  onClick: () => void store.fetchLiveContacts(),
                  size: 'sm',
                  variant: 'outline',
                  children: state.loadingContacts
                    ? 'Loading Handles…'
                    : 'Browse Live Handles from BlueBubbles'
                })
              ]
            }),
            state.liveContacts.length > 0 &&
              jsx('div', {
                className:
                  'max-h-56 overflow-y-auto rounded-md border border-(--ui-stroke-tertiary) divide-y divide-(--ui-stroke-tertiary)',
                children: state.liveContacts.map(contact => {
                  const addr = contact.normalized || contact.address
                  const selected = (d.allowed_users || []).includes(addr)
                  return jsxs(
                    'div',
                    {
                      className: 'flex items-center justify-between gap-2 px-3 py-1.5 text-xs',
                      children: [
                        jsxs('div', {
                          className: 'min-w-0 flex-1',
                          children: [
                            jsx('span', {
                              className: 'font-mono text-(--ui-text-primary)',
                              children: addr
                            }),
                            contact.service &&
                              jsx('span', {
                                className: 'ml-2 text-(--ui-text-tertiary)',
                                children: `(${contact.service})`
                              })
                          ]
                        }),
                        jsx(Button, {
                          onClick: () => toggleAllowedUser(addr),
                          size: 'sm',
                          variant: selected ? 'default' : 'outline',
                          children: selected ? 'Allowed ✓' : 'Allow'
                        })
                      ]
                    },
                    addr
                  )
                })
              })
          ]
        })
      })
    ]
  })
}

function BehaviorPage({ store }) {
  const state = useValue(store.$state)
  const d = state.draft
  const [patternsText, setPatternsText] = useState(() => formatListText(d.mention_patterns))

  useEffect(() => {
    setPatternsText(formatListText(state.draft.mention_patterns))
  }, [state.selectedProfile, state.effective])

  return jsxs('div', {
    className: 'space-y-2',
    children: [
      jsx(HeaderBar, {
        store,
        title: 'Behavior, Wake-Words & Webhooks',
        subtitle:
          'Configure group wake-word requirements, read receipts, webhook listener bindings, and iMessage effects.'
      }),
      jsx(ToggleRow, {
        label: 'Require Wake-Word / Mention in Group Chats',
        description:
          'When enabled, the bot only responds in group chats if a wake-word pattern matches or a user replies to a bot message.',
        checked: Boolean(d.require_mention),
        onChange: on => store.updateDraft({ require_mention: on })
      }),
      jsx(ListRow, {
        title: 'Group Wake-Word Regex Patterns',
        description:
          'Regular expressions tested against inbound group messages when Require Mention is enabled (default: @hermes, hermes).',
        wide: true,
        below: jsx('div', {
          className: 'mt-2',
          children: jsx(Textarea, {
            className: 'min-h-20 font-mono text-xs',
            placeholder: '\\bhermes\\b\n@hermes',
            value: patternsText,
            onChange: e => {
              const raw = e.currentTarget.value
              setPatternsText(raw)
              store.updateDraft({ mention_patterns: parseListText(raw) })
            }
          })
        })
      }),
      jsx(ToggleRow, {
        label: 'Automatically Send Read Receipts',
        description: 'Mark inbound chats as read via the BlueBubbles Private API when accepted.',
        checked: Boolean(d.send_read_receipts),
        onChange: on => store.updateDraft({ send_read_receipts: on })
      }),
      jsx(Separator, { className: 'my-2' }),
      jsx(ListRow, {
        title: 'Webhook Listener Host',
        description: 'Local bind IP for the standalone BlueBubbles webhook listener.',
        action: jsx(Input, {
          className: 'w-56 font-mono text-xs',
          value: d.webhook_host || '127.0.0.1',
          onChange: e => store.updateDraft({ webhook_host: e.currentTarget.value })
        })
      }),
      jsx(ListRow, {
        title: 'Webhook Listener Port',
        description: 'Local TCP port for the standalone webhook listener (default: 8645).',
        action: jsx(Input, {
          className: 'w-36 font-mono text-xs',
          type: 'number',
          value: String(d.webhook_port ?? 8645),
          onChange: e =>
            store.updateDraft({
              webhook_port: Number.parseInt(e.currentTarget.value || '8645', 10) || 8645
            })
        })
      }),
      jsx(ListRow, {
        title: 'Webhook Listener Path',
        description: 'HTTP path where BlueBubbles Server posts webhook events.',
        action: jsx(Input, {
          className: 'w-56 font-mono text-xs',
          value: d.webhook_path || '/bluebubbles-webhook',
          onChange: e => store.updateDraft({ webhook_path: e.currentTarget.value })
        })
      }),
      jsx(Separator, { className: 'my-2' }),
      jsx(ListRow, {
        title: 'iMessage Formatting & Expressive Effects',
        description:
          'BlueBubbles strips raw Markdown markers so replies arrive as clean native text bubbles, and supports Private API bubble/screen effects via bluebubbles_send_effect (slam, loud, gentle, invisible-ink, confetti, lasers, fireworks, balloons, sparkles, heart, echo, spotlight) plus inline rich subject titles.'
      })
    ]
  })
}

export default {
  id: 'bluebubbles-extender',
  name: 'BlueBubbles (iMessage)',
  description:
    'Configure BlueBubbles Server credentials, DM/Group chat routing, sender allowlists, wake-words, and webhooks across Hermes profiles.',
  defaultEnabled: true,
  register(ctx) {
    const store = createStore(ctx)

    ctx.registerSettingsPage?.({
      id: 'bluebubbles',
      title: 'BlueBubbles (iMessage)',
      icon: 'comment-discussion',
      order: 10,
      render: function BlueBubblesOverview() {
        return jsx(OverviewPage, { store })
      },
      children: [
        {
          id: 'routing',
          title: 'Routing & Chats',
          render: function BlueBubblesRouting() {
            return jsx(RoutingPage, { store })
          }
        },
        {
          id: 'access',
          title: 'Access & Senders',
          render: function BlueBubblesAccess() {
            return jsx(AccessPage, { store })
          }
        },
        {
          id: 'behavior',
          title: 'Behavior & Webhooks',
          render: function BlueBubblesBehavior() {
            return jsx(BehaviorPage, { store })
          }
        }
      ]
    })
  }
}
