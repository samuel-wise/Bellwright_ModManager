# Bellwright-inspired Steam Workshop Mod Manager

Tkinter GUI for managing Steam Workshop mods, modelled on how Bellwright
handles them. The Workshop
content folder (`steamapps\workshop\content\<game id>` under your Steam
library) is discovered automatically; a directory can also be passed as an
argument. Any other game with Workshop mods in the same content folder is
picked up too: the game dropdown in the toolbar lists every game that has
subscribed items (`steam id - title`, titles from Steam's own manifests) and
switches the whole manager to that game, Bellwright included by default.

Replaces the in-game mod menu's blunt "disable everything that overlaps"
behaviour with explicit control.

## Run

Double-click `Start Mod Manager.cmd`, or:

```
python bellwright_mm.py [content_dir] [--smoke]
```

`--smoke` prints mod/group counts and exits (no GUI). Stdlib only.

## What counts as a conflict

Checked against the game's own conflict popups: each mod lists edited assets
(`modifiedAssets`/`deletedAssets`/`referencingAssets`) and per-asset hashes
in `modinfo.json`. Only currently enabled mods are compared; disabled ones
just sit at the end of the load order.

- Same edited asset path in two mods = conflict: whichever loads later wins
  those files (matching the game's own per-mod conflict list, e.g.
  `Easier Fast Travel` vs `Group Fast Travel`, `Coin & Craft`, `Easy Village`).
- Same blueprint family, different suffix but equal content hash also
  counts (e.g. `BP_TimeFactor_AurorasGift` vs `BP_TimeFactor_DayfathersBoon`
  from the four Lingering Dawn day-length mods): each ships its own override
  of the same game setting under a different path, so stacking them is just
  last-loaded-wins; the game's popup misses this, the manager groups them.
- Same asset path + equal hashes still conflicts on paper, but both ship the
  same data, so it stacks fine either way
  (why `Easier Fast Travel` + `Road Lamps` + statue mods all stay on together).
- Hash-missing overlaps are treated like equal hashes: shown as an
  "overlap" note, usually fine; decide yourself with the description pane.

## Load order

The game stores its load priority in
`%LOCALAPPDATA%\Bellwright\Saved\Config\modloadorder.json`
(a `modLoadOrder` array of `{name, steamId}`, stored in load order — the last
array entry loads last and takes precedence for overlapping assets; the
in-game mod menu writes the same file). The manager uses that file as the
source of truth
for the current order, with numeric steam ids as fallback. `modlist.txt` is
never read for ordering either — it exists only as the report the manager
writes on Apply. The left list shows and edits this order:

- Drag a row up or down to reorder the load order; a plain click toggles the
  mod. Checked rows always sit above unchecked ones, so what the list shows
  is what Apply will write.
- `Reload` / `Reset` re-read every `modinfo.json` plus the game's order file
  from disk, resync the checkboxes to the current `active` flags and drop
  unsaved edits, so mods toggled inside the game show up without restarting
  the manager.
- Mod descriptions are read from each mod's `modinfo.json`; when one is
  empty or shorter than 10 words (usually placeholder text), the text is
  taken from the item's Steam Workshop page instead. Missing texts and
  creator names are fetched on a background thread (opening the window
  never waits for them) and cached in `workshop-desc-cache.json` next to
  the manager script; offline reloads reuse the cache. Creator names are
  looked up once per unique creator id. The list shows the name in its own
  `Author` column; the report shows it as part of `Title (Creator)`. The manager regenerates
  `modlist.txt` itself on Apply (built directly from the `modinfo.json`
  data); it reads mod info only from `modinfo.json` and the
  Workshop, never back from `modlist.txt`.
- `Auto-resolve all` works over what is currently checked: within each
  conflict group it keeps the checked mods that are mutually compatible
  (largest first) and drops checked ones that clash, pushing kept mods
  toward the end so their edits load last. Nothing gets switched on by
  itself: start from `Unselect all`, tick your favourites, then hit it to
  tidy up conflicts. Solo mods are untouched throughout.
- `Best fill groups` checks, per group, the largest mutually compatible set
  ignoring current checks (greedy: biggest mods first, keep it if compatible
  with everything kept). Use it after a clear to get a working baseline in
  one click; re-running it is stable.
- `Select all` ticks every mod (solo ones included), handy as a starting
  point before `Auto-resolve all` prunes clashes among them; `Unselect all`
  clears everything for a from-scratch baseline.
- Members of a group are lettered `a. b. c. ...` and the maximal compatible
  combinations are listed compactly below them, e.g. `Combo A: a,c,d (3)`,
  `Combo B: b,d (2)`. Click a combo row to apply it to that group; click a
  member row to select its description in the pane. Group numbers are stable (lowest
  steam id first) and both the panel and the description pane are recomputed
  from the fresh group state after every click, so a resolved group leaves
  no stale entries behind.

## GUI

Left: filterable mod list with columns `On` / `Title` / `Author` /
`Steam ID` (click row = toggle on/off; the conflict panel refreshes with each
toggle). Top of the right column: an Authors strip — one row per creator
(steam id + name) with a checkbox that switches that whole author's mods
on/off in one click; it has its own scrollbar, and the divider below it is
draggable (clamped to half the column). Below: conflict groups; each member row is a radio
button (one choice per group) reading `[x]/[ ] a. Title (Author)` — choosing
one writes that mod's description into the bottom pane. Switch mods themselves
with the left list or a combo row. Each member row ends with
its last-updated date: the cached Workshop activity stamp when known, else
the time Steam downloaded the mod. Bottom:
description pane with its own scrollbar (mouse wheel works on it), showing the
chosen mod's text broken onto sentence lines, with its hard conflicts and
harmless overlaps listed (unchecked mods are ignored in these lists). The `On` column
marks each row: `✓` enabled, blank disabled, `✗` enabled at open but now
off, `🗹` off at open but now on (marks settle once Apply clears them).
`Apply` confirms the switch counts first,
then writes
`active` flags into each `modinfo.json` (inside the Workshop content dir) and
refreshes the game's `modloadorder.json`. It also regenerates the manager's
own `modlist.txt` report — one line per mod, tab-separated
(`steam id`, `True/False`, `Title (Creator)`, description) — under
`modlists\<steam id - title>\` next to the script, with older copies
rotated into that folder's `backups\` subfolder;
checked mods are written first
and unchecked ones toward the end, so the game shows disabled entries at the
bottom while enabled mods keep their relative order. Scrollbars on
both panes and on the description pane; the mouse wheel works
on the conflict panel and on the description pane. Groups are computed among checked mods only, which
is why the panel is often empty; it then shows a "No conflicts among
currently checked mods" hint. The divider between the two panes is draggable and
starts at roughly a 50/50 split; resizing the window grows/shrinks both
panes proportionally. The description pane has its own horizontal divider
above it, also draggable. Hovering any row (mod list or group member) pops
a tooltip with that mod's description, broken onto new lines after `.`,
`!`, `?` and wrapped at 70 columns. The pane mirrors each group's current
radio choice: a `----- Group N, choice a. -----` head, then that mod's
text; toggling checks keeps the chosen member and refreshes its conflict
lines.

## Files

- `bellwright_mm.py` — everything (loader, conflict logic, GUI).
- `Start Mod Manager.cmd` — launcher.
- `steam-workshop-games.txt` — cached `steam id -> title` lines for games
  with Workshop items (dropdown labels; rebuilt from Steam's own manifests).
- `modlists\` — per-game `modlist.txt` reports, backups under `backups\`.
