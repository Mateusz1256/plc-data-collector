# Panel design

The reference mockup for the Panel: the password-protected web UI for managing
Connections, Tag Groups and Tags and previewing current Tag values (see
`GLOSSARY.md`). The Panel has no charts.

Status: implemented in 0.3.0 (`src/plc_data_collector/panel/`). The mockup
shows a Modbus TCP Connection; in 0.3.0, OPC UA is the only protocol that can
be added, and Modbus TCP, S7 and M-Bus are listed as coming in 0.4.0, 0.5.0
and 0.6.0.

Interactive canvas: https://claude.ai/artifact/LqVW6RZ81HYmGBfUsgRXeM

## Screens

| File | Screen |
|---|---|
| `live.dc.html` | Live tags: every Tag grouped by Connection and Tag Group, with value, quality, update time and freshness, plus the "pending changes / Apply changes" banner |
| `connections.dc.html` | Connections list with status, tags collected and last poll; the "Add connection" protocol picker |
| `connection-edit.dc.html` | Editing one Connection (Modbus TCP example): connection fields, Test connection, Tag Groups, Tags, the new-tag form with the Modbus function list |
| `settings.dc.html` | API keys (shown once on creation, revoke), data retention (on/off, days), configuration export and import |
| `login.dc.html` | Sign in with the admin password from `.env` |

## How to use these files

- They are mockups, not production code. The Panel is planned to be built with
  Jinja templates and htmx in FastAPI. Copy the markup structure, copy text,
  colours, spacing and states, and leave out the mockup runtime.
- Each file is HTML with inline styles and a small template layer: `<x-dc>`,
  `<helmet>`, `<sc-for>`, `<sc-if>`, `{{holes}}`, and a
  `class Component extends DCLogic` script that only produces sample data.
  `./support.js` exists only in the design tool, so the files do not render
  when opened straight from disk. Use the canvas link to see them working.
- Sample names (`plc-sim`, `press2-plc`, `mbus-gw`, `Line 1 filler`) are
  fictional demo data.

## Design rules

**Colour means something is wrong.** Healthy Connections and good readings stay
grey. Colour appears only for abnormal states, following the ISA-101
high-performance HMI idea. Every state also has its own shape and word, so
nothing relies on hue alone.

| State | Marker | Text colour | Background, when highlighted |
|---|---|---|---|
| Running / Good | hollow circle, `#56606B` stroke | `#56606B` | none |
| Degraded / stale | filled diamond `#C77700` | `#8A4B00` | `#FBE3B0` |
| Failed / Bad | filled square `#B42318` | `#B42318` (detail `#7A1A10`) | `#FBE4E1` |
| Disabled | dash `#8A939C` | `#6B747D` | none |

Palette:

| Token | Hex | Use |
|---|---|---|
| ground | `#E6E8EA` | page background |
| surface | `#F7F8F9` | panels, header |
| surface-sub | `#EEF0F2` | Tag Group header rows |
| ink | `#1D2329` | primary text |
| ink-muted | `#56606B` | secondary text, healthy states |
| rule | `#C5CBD1` | panel borders; `#D5DADF` for table rows |
| field-border | `#8A939C` | inputs, secondary buttons |
| action | `#2457A6` | primary buttons, links, focus ring, current nav item; hover `#173E7A` |
| info band | `#DCE6F5` with text `#173E7A` | pending-changes banner |

Type:

- **Archivo** (Google Fonts, variable width) for all UI. Headings and large
  values use `font-stretch: 87.5%`. Numbers use `font-variant-numeric: tabular-nums`.
- **IBM Plex Mono** only for things that are literally code or addresses: Tag
  addresses, Connection IDs, endpoints, API keys.
- Scale: page title 32 px, section 20 px, sub-section 16 px, body 15 px, table
  14 px, secondary 13 px. Live values are 22 px semi-bold.

Layout and behaviour:

- Max content width 1360 px (Settings 1040 px), 24 px side gutter, top
  navigation (Live, Connections, Settings).
- Panels: 1 px `rule` border, 6 px radius, no shadows. Inputs and buttons: 4 px
  radius, minimum height 44 px.
- Wide tables scroll horizontally inside their panel. Side columns stack at
  phone width.
- Freshness bar: 72 px track. Its width shrinks as a reading ages, from full at
  0 s to empty at 4× the Tag Group interval. It turns amber past 2×. It has
  no transition when `prefers-reduced-motion` is set.
- Visible focus ring: 2 px `action` outline, 2 px offset. Icon-only or hidden
  labels use visually hidden text.
- Copy: sentence case, plain verbs, and buttons that say what happens ("Apply
  changes", "Create key", "Review import"). An error says what happened and
  what to check.
