# Bellwright Mod Manager

Tkinter GUI for managing Bellwright Steam Workshop mods. The Workshop
content folder (`steamapps\workshop\content\1812450` under your Steam
library) is discovered automatically; a directory can also be passed as an
argument.

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
  member row to toggle it individually. Group numbers are stable (lowest
  steam id first) and both the panel and the description pane are recomputed
  from the fresh group state after every click, so a resolved group leaves
  no stale entries behind.

## GUI

Left: filterable mod list with columns `On` / `Title` / `Author` /
`Steam ID` (click row = toggle on/off; the conflict panel refreshes with each
toggle). Right: conflict groups
with checkbox per member — each row reads `Title (Author)`; click to switch
a mod on (it moves behind its
conflicting peers, which switch off) or off again. Bottom:
description pane for the selected mod, with its hard conflicts and harmless
overlaps listed (unchecked mods are ignored in these lists). The `On` column
marks each row: `✓` enabled, blank disabled, `✗` enabled at open but now
off, `🗹` off at open but now on (marks settle once Apply clears them).
`Apply` confirms the switch counts first,
then writes
`active` flags into each `modinfo.json` (inside the Workshop content dir) and
refreshes the game's `modloadorder.json`. It also regenerates the manager's
own `modlist.txt` report — one line per mod, tab-separated
(`steam id`, `True/False`, `Title (Creator)`, description), rotated to
`modlist-bakN.txt` — in the same folder as the script;
checked mods are written first
and unchecked ones toward the end, so the game shows disabled entries at the
bottom while enabled mods keep their relative order. Scrollbars on
both panes; mouse wheel works
on the conflict panel. Groups are computed among checked mods only, which
is why the panel is often empty; it then shows a "No conflicts among
currently checked mods" hint. The divider between the two panes is draggable and
starts at roughly a 50/50 split; resizing the window grows/shrinks both
panes proportionally. The description pane has its own horizontal divider
above it, also draggable. Hovering any row (mod list or group member) pops
a tooltip with that mod's description, broken onto new lines after `.`,
`!`, `?` and wrapped at 70 columns. After changing a conflict group
(member click or combo click) the description pane shows that group's
members in two sections: `----- Selected Items in Group N -----` first,
then `----- Unselected Items in Group N -----`, each entry lettered to
match the group panel.

## Files

- `bellwright_mm.py` — everything (loader, conflict logic, GUI).
- `Start Mod Manager.cmd` — launcher.
