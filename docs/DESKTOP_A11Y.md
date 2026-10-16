# Linux desktop accessibility (AT-SPI2) — design notes

## Transport

The project interpreter has no D-Bus bindings, so `device/linux/atspi_probe.py`
runs one-shot under `/usr/bin/python3` (system `python3-dbus`) as a
subprocess: JSON request on argv, JSON response on stdout, 30 s timeout,
per-call D-Bus timeouts, soft 25 s deadline, capped nodes/strings. Any
transport failure returns `supported:false` — never a traceback as data.

Verified live on Ubuntu GNOME Wayland: `org.a11y.Bus` → AT-SPI bus →
registry root children = applications (gnome-shell, Chrome, ptyxis, …),
each serving its own tree. Roles/names/states/bounds/actions read from
`org.a11y.atspi.Accessible` (+`Component`, `Action`, `Text`,
`EditableText`); defunct (`…/null`) refs are skipped, never fatal.

## Snapshot binding (stale-target rejection)

`desktop.a11y.inspect` stores the observation under `snapshot_id`
(120 s TTL, 20-entry rundown). `desktop.click`/`desktop.type` re-fetch
the live node and require role + name match; TTL expiry, unknown ids,
vanished paths, and role/name drift all refuse with "re-inspect first".
The X11 pointer fallback re-reads bounds immediately before acting and
refuses on any shift.

## Actuation backends (honest tiers)

| Action | Preferred backend | Fallback | Blocked |
|--------|-------------------|----------|---------|
| click | AT-SPI `DoAction(click/press/toggle)`, any session, post-state re-read | X11 `xdotool` at fresh bounds center (x11 session only) | Wayland without a clickable action (no injector: needs ydotool/uinput) |
| type | AT-SPI `EditableText` (`SetTextContents`/`InsertText`), content-verified by `GetText` | focused X11 `xdotool type` (focus verified first; content check when readable) | Wayland without an editable control; unfocusable controls (no blind typing) |

`applied:true` means: toolkit-acknowledged AND post-state/content verified
(`x11-type-unverified` marks the one weaker case explicitly). Typed text
is never echoed in results; traces/audit carry redacted metadata only.

## Wayland limitations (this host)

- No input injector installed (`ydotool`/`wtype`/`dotool` absent) and
  `/dev/uinput` is `600 root:root`, so native-Wayland pointer/keyboard
  injection is refused, not faked.
- Active-window title probe is X11-only; on Wayland use
  `desktop.a11y.inspect` (focused control reported).
- Screenshot has no CLI backend here; the tool probes
  grim/gnome-screenshot/import/scrot/maim and reports unsupported when
  absent (a Screenshot-portal call is the likely next backend).
