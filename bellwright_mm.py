"""Bellwright Mod Manager: toggle Workshop mods, auto-resolve asset conflicts.

Scans the Bellwright Workshop content folder, shows every mod with its
activation state, detects conflicts (mods touching the same game asset with
different asset hashes) and lets you pick which mod wins per conflict group.
Apply writes the active flags back into every modinfo.json and regenerates
modlist.txt in the tab-separated format the game expects, plus the game's
modloadorder.json. Descriptions come from each mod's modinfo.json and are
topped up from the Steam Workshop item pages (cached locally).

Usage: python bellwright_mm.py [content_dir] [--smoke]
"""
import html
import json
import os
import re
import sys
import textwrap
import time
import urllib.request
import tkinter as tk
from tkinter import ttk

APP_ID = "1812450"


def find_content_dir():
    """Locate the Workshop content folder for this game without hardcoding:
    explicit env hints first, then standard Steam folders, then one level of
    every mounted drive."""
    tail = ("steamapps", "workshop", "content", APP_ID)
    seen, roots = set(), []

    def add(path):
        if path and path not in seen:
            seen.add(path)
            roots.append(path)

    for key in ("STEAM_PATH", "STEAM_ROOT"):
        add(os.environ.get(key))
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if base:
            add(base)
            add(os.path.join(base, "Steam"))
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":" + os.sep
        if os.path.isdir(root):
            add(root)
    candidates = []
    for root in roots:
        candidates.append(os.path.join(root, *tail))
        candidates.append(os.path.join(root, "Steam", *tail))
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            candidates.append(os.path.join(root, name, "Steam", *tail))
            candidates.append(os.path.join(root, name, *tail))
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return None


DEFAULT_DIR = find_content_dir()


def read_info(mod_dir):
    path = os.path.join(mod_dir, "modinfo.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except UnicodeDecodeError:
        with open(path, encoding="utf-16") as fh:
            return json.load(fh)


def desc_cache_path(dirpath):
    return os.path.join(support_dir(), "workshop-desc-cache.json")


def support_dir():
    """All helper data files live next to the manager script."""
    return os.path.dirname(os.path.abspath(__file__))


def load_desc_cache(dirpath):
    try:
        with open(desc_cache_path(dirpath), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return dict((str(k), str(v)) for k, v in data.items())
    except Exception:
        return {}


def save_desc_cache(dirpath, cache):
    try:
        with open(desc_cache_path(dirpath), "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


def fetch_desc(sid):
    """Grab one item's description block from its Steam Workshop page."""
    url = ("https://steamcommunity.com/sharedfiles/filedetails/"
           "?id=%s&appid=%s" % (sid, APP_ID))
    req = urllib.request.Request(
        url, headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0"})
    page = ""
    for attempt in range(3):
        try:
            page = urllib.request.urlopen(req, timeout=12).read().decode(
                "utf-8", "replace")
            break
        except Exception:
            time.sleep(2)
    if not page:
        return ""
    m = re.search('id="highlightContent">(.+?)</div>', page, re.S)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    return " ".join(html.unescape(text).split())


def load_mods(dirpath):
    mods = []
    for sid in sorted((d for d in os.listdir(dirpath) if d.isdigit()), key=int):
        info = read_info(os.path.join(dirpath, sid))
        if info is None:
            continue
        assets = set()
        for key in ("modifiedAssets", "createdAssets", "deletedAssets", "assetTree"):
            assets.update(info.get(key) or [])
        desc = " ".join((info.get("description") or "").split())
        edited = set(info.get("modifiedAssets") or [])
        edited.update(info.get("deletedAssets") or [])
        edited.update(info.get("referencingAssets") or [])
        hs = dict(info.get("assetHashes") or {})
        stems = {}
        for p in sorted(edited):
            h = hs.get(p)
            if h:
                stems.setdefault(_stem(p), set()).add(h)
        mods.append({"sid": sid,
                     "title": info.get("title") or "(untitled)",
                     "folder": info.get("folderName") or sid,
                     "desc": desc,
                     "active": bool(info.get("active", True)),
                     "edited": edited,
                     "assets": assets,
                     "stems": stems,
                     "hashes": hs})
    blanks = [m for m in mods if not m["desc"]]
    if blanks:
        cache = load_desc_cache(dirpath)
        changed = False
        for m in blanks[:60]:
            note = cache.get(m["sid"]) or fetch_desc(m["sid"])
            if note:
                m["desc"] = note
                cache[m["sid"]] = note
                changed = True
        if changed:
            save_desc_cache(dirpath, cache)
    return mods


def config_path():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "."
    return os.path.join(base, "Bellwright", "Saved", "Config",
                        "modloadorder.json")


def load_order(dirpath, mods):
    """Load order priority: modloadorder.json (the game's own file), then
    steam ids ascending. New ids appended."""
    known = set(m["sid"] for m in mods)
    order = []

    def absorb(sid):
        sid = str(sid).strip()
        if sid in known and sid not in order:
            order.append(sid)

    cfg = config_path()
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            for entry in data.get("modLoadOrder") or []:
                absorb(entry.get("steamId"))
        except (UnicodeError, ValueError):
            pass
    for sid in sorted(known, key=int):
        absorb(sid)
    return order


def _stem(path):
    """Asset basename without its trailing name suffix: BP_TimeFactor_
    AurorasGift -> bp_timefactor; plain names keep their whole basename."""
    base = path.rsplit("/", 1)[-1]
    parts = base.split("_")
    if len(parts) > 1:
        return "_".join(parts[:-1]).lower()
    return base.lower()


def _stem_map(edited, hashes):
    """stem -> hashes of edited assets with that stem (non-empty hashes)."""
    out = {}
    for p in sorted(edited):
        h = hashes.get(p)
        if h:
            out.setdefault(_stem(p), set()).add(h)
    return out


def conflicts(a, b):
    """Hard conflict: both mods edit the same game file, so only the one
    loaded later actually applies to those files. Also counts edited assets
    that only differ by their name suffix (same blueprint family with the
    same content hash, e.g. BP_TimeFactor_AurorasGift vs
    BP_TimeFactor_DayfathersBoon): one override of that setting wins."""
    if a["edited"] & b["edited"]:
        return True
    sa, sb = a.get("stems"), b.get("stems")
    if sa is None or sb is None:
        sa = _stem_map(a["edited"], a["hashes"])
        sb = _stem_map(b["edited"], b["hashes"])
    for st, hs in sa.items():
        other = sb.get(st)
        if other and hs & other:
            return True
    return False


def shared_paths(a, b):
    """Shared asset paths with equal hashes or hashes too missing to compare:
    usually stack fine, shown as a note rather than a conflict."""
    out = set()
    for path in a["assets"] & b["assets"]:
        h1, h2 = a["hashes"].get(path), b["hashes"].get(path)
        if h1 and h2 and h1 != h2:
            continue
        out.add(path)
    return out


def conflict_groups(mods):
    groups, used = [], set()
    for m in mods:
        if m["sid"] in used:
            continue
        comp, stack = [], [m]
        while stack:
            cur = stack.pop()
            if cur["sid"] in used:
                continue
            used.add(cur["sid"])
            comp.append(cur)
            stack.extend(o for o in mods
                         if o["sid"] not in used and conflicts(cur, o))
        groups.append(comp)
    return groups


def auto_pick(group):
    """Greedy largest-compatible-subset: biggest mods first, keep compatible."""
    chosen = []
    for m in sorted(group, key=lambda x: -len(x["assets"])):
        if all(not conflicts(m, c) for c in chosen):
            chosen.append(m)
    return chosen


def best_sets(group):
    """All maximal compatible combinations, ties kept. Greedy pass starting
    from each member (asset-count order), deduped; largest-size results only.
    Pairwise clashes are tabulated once up front so the greedy passes just
    test set membership."""
    order = sorted(group, key=lambda x: (-len(x["assets"]), int(x["sid"])))
    ids = [x["sid"] for x in order]
    clash = dict((sid, set()) for sid in ids)
    for i in range(len(order)):
        for j in range(i + 1, len(order)):
            if conflicts(order[i], order[j]):
                clash[ids[i]].add(ids[j])
                clash[ids[j]].add(ids[i])
    seen, results = set(), []
    for start in order:
        chosen, chosen_ids = [start], {start["sid"]}
        for m in order:
            s = m["sid"]
            if s in chosen_ids or clash[s] & chosen_ids:
                continue
            chosen.append(m)
            chosen_ids.add(s)
        key = frozenset if False else frozenset(chosen_ids)
        if key not in seen:
            seen.add(key)
            results.append(chosen)
    best = max(len(c) for c in results)
    return [c for c in results if len(c) == best][:6]


def write_active(dirpath, sid, active):
    mod_dir = os.path.join(dirpath, sid)
    info = read_info(mod_dir)
    if info is None:
        return False
    info["active"] = bool(active)
    with open(os.path.join(mod_dir, "modinfo.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(info, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return True


def write_modlist(dirpath, sel, order=None):
    home = support_dir()
    list_file = os.path.join(home, "modlist.txt")
    if os.path.isfile(list_file):
        n = 0
        while os.path.isfile(os.path.join(home, "modlist-bak%d.txt" % n)):
            n += 1
        os.replace(list_file, os.path.join(home, "modlist-bak%d.txt" % n))
    mods = load_mods(dirpath)
    by_id = dict((m["sid"], m) for m in mods)
    seq = [s for s in (order or []) if s in by_id]
    seq += [m["sid"] for m in mods if m["sid"] not in seq]

    def is_on(s):
        return bool(sel.get(s, by_id[s]["active"]))

    # checked mods first, unchecked last so disabled entries sit at the
    # bottom of the game's list; enabled ones keep their relative order
    seq.sort(key=lambda s: 0 if is_on(s) else 1)
    lines = []
    for sid in seq:
        m = by_id[sid]
        on = "True" if is_on(sid) else "False"
        lines.append("%s\t%s\t%s\t%s\t%s"
                     % (sid, on, m["folder"], m["title"], m["desc"]))
    with open(list_file, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(lines) + "\n")
    entries = [{"name": by_id[s]["folder"], "steamId": int(s)} for s in seq]
    cfg = config_path()
    if os.path.isdir(os.path.dirname(cfg)):
        with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"modLoadOrder": entries}, fh, indent=1,
                      ensure_ascii=False)
            fh.write("\n")
    return len(lines)

def letter_for(li):
    """a., b., ... wraps to a1., b1. ... past 26 members."""
    return chr(97 + li) if li < 26 else "%s%d" % (chr(97 + li % 26), li // 26)


def tip_text(text):
    """Break after end-of-sentence punctuation, wrap lines at 70 columns."""
    spaced = text.replace(". ", ".\n").replace("! ", "!\n")
    spaced = spaced.replace("? ", "?\n")
    out = []
    for line in spaced.split("\n"):
        out.extend(textwrap.wrap(line, width=70) or [""])
    return "\n".join(out)


class ToolTip:
    """Shared hover popup; shows one window at a time."""

    def __init__(self):
        self.win = None
        self.last = None

    def show(self, widget, text):
        if self.win is not None and self.last == text:
            return
        self.hide()
        self.last = text
        if not text:
            return
        text = tip_text(text)
        self.win = tk.Toplevel(widget)
        self.win.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 12,
                                         widget.winfo_rooty()
                                         + widget.winfo_height()))
        tk.Label(self.win, text=text, justify="left", bg="#ffffe0",
                 padx=6, pady=3).pack()
        self.win.overrideredirect(True)

    def hide(self):
        self.last = None
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class App:
    def __init__(self, root, dirpath):
        self.dirpath = dirpath
        self.sel = {}
        self.mods = []
        self.order = []
        root.title("Bellwright Mod Manager")
        self.root = root
        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.filter_var, width=40)
        ent.pack(side="left", padx=4)
        ent.bind("<KeyRelease>", lambda e: self.refresh_tree())
        ttk.Button(top, text="Reload", command=self.reload).pack(side="left", padx=4)
        body = ttk.Panedwindow(root, orient="vertical")
        body.pack(fill="both", expand=True, padx=6)
        self.vbody = body
        paned = ttk.Panedwindow(body, orient="horizontal")
        self.paned = paned
        left = ttk.Frame(paned, width=520)
        paned.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("on", "title", "sid"),
                                 height=20, show="headings")
        self.tree.heading("on", text="On")
        self.tree.heading("title", text="Title")
        self.tree.heading("sid", text="Steam ID")
        self.tree.column("on", width=50, anchor="center", stretch=False)
        self.tree.column("title", width=380)
        self.tree.column("sid", width=90, stretch=False)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<<TreeviewSelect>>", self.show_desc)
        self.tip = ToolTip()
        self.tree.bind("<Motion>", self.on_hover)
        self.tree.bind("<Leave>", lambda e: self.tip.hide())
        panel = ttk.LabelFrame(paned, text="Conflict groups")
        paned.add(panel, weight=1)
        self.canvas = tk.Canvas(panel, width=520, highlightthickness=0)
        psb = ttk.Scrollbar(panel, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=psb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        psb.pack(side="left", fill="y")
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.panel = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.panel, anchor="nw",
                                  tags="panelwin")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig("panelwin", width=e.width))
        self.desc = tk.Text(body, height=6, wrap="word")
        body.add(paned, weight=1)
        body.add(self.desc, weight=0)
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=6, pady=4)
        ttk.Button(bottom, text="Move up", command=lambda: self.move_step(-1)).pack(side="left")
        ttk.Button(bottom, text="Move down", command=lambda: self.move_step(1)).pack(side="left", padx=4)
        ttk.Button(bottom, text="Auto-resolve all", command=self.auto_all).pack(side="left")
        ttk.Button(bottom, text="Unselect group mods", command=self.clear_groups).pack(side="left", padx=4)
        ttk.Button(bottom, text="Best fill groups", command=self.fill_best).pack(side="left")
        ttk.Button(bottom, text="Select all", command=self.select_all).pack(side="left", padx=4)
        ttk.Button(bottom, text="Apply", command=self.apply).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", padx=6)

        self.split_done = False
        root.bind("<Configure>", self.center_split, add=True)
        root.after(50, lambda: self.center_split_width(root.winfo_width()))
        self.reload()

    def center_split(self, event):
        self.center_split_width(event.width)

    def center_split_width(self, width):
        if self.split_done:
            return
        if width < 400:
            self.root.after(100, lambda: self.center_split_width(
                self.root.winfo_width()))
            return
        pw = self.paned
        target = max(width // 2, 300)
        pw.tk.call(pw._w, "sashpos", 0, target)
        try:
            got = int(pw.tk.call(pw._w, "sash", 0))
        except Exception:
            got = target
        if abs(got - target) <= 2:
            self.split_done = True
            self.root.unbind("<Configure>")
            try:
                pw.event_generate("<Configure>")
            except Exception:
                pass
            return
        self.split_left = getattr(self, "split_left", 8) - 1
        if self.split_left > 0:
            self.root.after(80, lambda: self.center_split_width(width))

    def reload(self):
        self.mods = load_mods(self.dirpath)
        self.order = load_order(self.dirpath, self.mods)
        by_id = dict((m["sid"], m) for m in self.mods)
        self.order = [s for s in self.order if s in by_id]
        self.order += sorted((m["sid"] for m in self.mods
                              if m["sid"] not in self.order), key=int)
        self.sel = dict((m["sid"], m["active"]) for m in self.mods)
        groups = conflict_groups(self.checked_mods())
        self.refresh_tree()
        self.refresh_groups()
        on = sum(1 for v in self.sel.values() if v)
        self.status.set("%d mods, %d on, %d conflict groups"
                        % (len(self.mods), on, len(groups)))
        self.center_split_width(self.root.winfo_width())

    def checked_mods(self):
        return [m for m in self.mods if self.sel.get(m["sid"])]

    def refresh_tree(self):
        needle = self.filter_var.get().lower()
        self.tree.delete(*self.tree.get_children())
        by_id = dict((m["sid"], m) for m in self.mods)
        for sid in self.order:
            m = by_id.get(sid)
            if m is None:
                continue
            text = "%s %s %s" % (m["title"], m["folder"], m["sid"])
            if needle and needle not in text.lower():
                continue
            self.tree.insert("", "end", iid=sid,
                             values=("x" if self.sel.get(sid) else "",
                                     m["title"], sid))

    def visible_groups(self):
        """Conflict groups among checked mods, multi-member only, numbered
        in a stable way (lowest steam id first) so the numbers survive a
        group disappearing next door."""
        groups = [g for g in conflict_groups(self.checked_mods())
                  if len(g) > 1]
        groups.sort(key=lambda g: min(int(x["sid"]) for x in g))
        return groups

    def group_of(self, sid):
        """Fresh (group, number) for a member sid, or (None, 0)."""
        for gi, g in enumerate(self.visible_groups()):
            if any(x["sid"] == sid for x in g):
                return g, gi + 1
        return None, 0

    def refresh_groups(self):
        groups = self.visible_groups()
        for child in self.panel.winfo_children():
            child.destroy()
        shown = 0
        for group in groups:
            shown += 1
            frame = ttk.LabelFrame(self.panel, text="Group %d (%d mods)"
                                   % (shown, len(group)))
            frame.pack(fill="x", pady=3)
            members = sorted(group, key=lambda x: x["title"])
            letters, idx = {}, {}
            for li, mm in enumerate(members):
                letters[mm["sid"]] = letter_for(li)
                idx[mm["sid"]] = li
            for mm in members:
                mark = "[x]" if self.sel.get(mm["sid"]) else "[ ]"
                row = ttk.Label(frame, text="%s. %s %s"
                                % (letters[mm["sid"]], mark, mm["title"]))
                row.pack(anchor="w")
                row.bind("<Button-1>",
                         lambda e, s=mm["sid"]: self.member_click(s))
                row.bind("<Enter>", lambda e, w=row, mm=mm: self.tip.show(
                    w, "%s\n%s" % (mm["title"], mm["desc"])))
                row.bind("<Leave>", lambda e: self.tip.hide())
            combos = best_sets(group)
            combos.sort(key=lambda c: min(idx.get(x["sid"], 999)
                                          for x in c))
            for ci, combo in enumerate(combos):
                chosen = sorted(combo, key=lambda x: idx.get(x["sid"], 999))
                tags = ",".join(letters.get(x["sid"], "?") for x in chosen)
                lbl = ttk.Label(frame, foreground="navy",
                                text="Combo %s: %s (%d)"
                                % (chr(65 + ci), tags, len(chosen)))
                lbl.pack(anchor="w")
                lbl.bind("<Button-1>",
                         lambda e, ch=tuple(chosen): self.combo_click(ch))
        if shown == 0:
            ttk.Label(self.panel, foreground="gray",
                      text="No conflicts among currently checked mods.\n"
                           "Tick mods in the list, then Auto-resolve all to "
                           "tidy them."
                      ).pack(anchor="w", pady=6)
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all") or (0, 0, 1, 1)
        self.canvas.configure(scrollregion=bbox)

    def member_click(self, sid):
        """Toggle one member, then show the fresh group it still touches."""
        group, _ = self.group_of(sid)
        if group is None:
            return
        mod = next(x for x in group if x["sid"] == sid)
        sids = set(x["sid"] for x in group)
        self.toggle_member(group, mod)
        shown, num = None, 0
        for gi, g2 in enumerate(self.visible_groups()):
            if any(x["sid"] in sids for x in g2):
                shown, num = g2, gi + 1
                break
        if shown is None:
            self.show_desc()
        else:
            self.show_group(shown, num)

    def combo_click(self, chosen):
        """Apply a captured combo against the fresh group state."""
        first = sorted(chosen, key=lambda x: x["title"])[0]["sid"]
        group, _ = self.group_of(first)
        self.apply_combo(group if group else list(chosen), chosen)
        group2, gnum2 = self.group_of(first)
        if group2 is None:
            self.show_desc()
        else:
            self.show_group(group2, gnum2)

    def on_wheel(self, event):
        step = int(-event.delta / 60) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    def show_desc(self, event=None):
        sel = self.tree.selection()
        sid = sel[0] if sel else ""
        mods = dict((m["sid"], m) for m in self.mods)
        m = mods.get(sid)
        self.desc.delete("1.0", "end")
        if m is None:
            return
        self.desc.insert("end", "%s [%s]\n%s\n" % (m["title"], sid, m["desc"]))
        hard = [o["title"] for o in self.mods
                if o["sid"] != sid and self.sel.get(o["sid"])
                and conflicts(m, o)]
        soft = [o["title"] for o in self.mods
                if o["sid"] != sid and self.sel.get(o["sid"])
                and not conflicts(m, o) and shared_paths(m, o)]
        self.desc.insert("end", "Hard conflicts: %s\n" % ", ".join(hard))
        self.desc.insert("end", "Overlaps (usually fine): %s" % ", ".join(soft))

    def show_group(self, group, gnum):
        """Selected members of one conflict group first, then unselected."""
        members = sorted(group, key=lambda x: x["title"])
        self.desc.delete("1.0", "end")
        for want in (True, False):
            head = "Selected" if want else "Unselected"
            self.desc.insert("end", "----- %s Items in Group %d -----\n"
                             % (head, gnum))
            for li, m in enumerate(members):
                if bool(self.sel.get(m["sid"])) is want:
                    self.desc.insert(
                        "end", "%s. %s\n%s\n\n"
                        % (letter_for(li), m["title"], m["desc"]))

    def on_hover(self, event):
        sid = self.tree.identify_row(event.y)
        m = next((x for x in self.mods if x["sid"] == sid), None)
        if m is None:
            self.tip.hide()
            return
        self.tip.show(self.tree, "%s\n%s" % (m["title"], m["desc"]))

    def move_step(self, step):
        sel = self.tree.selection()
        if not sel:
            return
        sid = sel[0]
        i = self.order.index(sid)
        j = min(max(i + step, 0), len(self.order) - 1)
        if i == j:
            return
        self.order[i], self.order[j] = self.order[j], self.order[i]
        self.refresh_tree()
        self.tree.selection_set(sid)

    def touch(self, sid):
        """Push a chosen mod to the end of load order: last loaded wins."""
        if sid in self.order:
            self.order.remove(sid)
            self.order.append(sid)

    def toggle_member(self, group, mod):
        if self.sel.get(mod["sid"]):
            self.sel[mod["sid"]] = False
        else:
            self.sel[mod["sid"]] = True
            self.touch(mod["sid"])
            for o in group:
                if o["sid"] != mod["sid"] and conflicts(mod, o):
                    self.sel[o["sid"]] = False
        self.refresh_tree()
        self.refresh_groups()

    def apply_combo(self, group, chosen):
        keep = set(m["sid"] for m in chosen)
        for m in group:
            self.sel[m["sid"]] = m["sid"] in keep
        for m in chosen:
            self.touch(m["sid"])
        self.refresh_tree()
        self.refresh_groups()

    def on_click(self, event):
        sid = self.tree.identify_row(event.y)
        if sid in self.sel:
            self.sel[sid] = not self.sel[sid]
            self.refresh_tree()
            self.tree.selection_set(sid)
            self.show_desc()
            self.refresh_groups()

    def auto_group(self, group):
        chosen = []
        pool = [m for m in sorted(group, key=lambda x: -len(x["assets"]))
                if self.sel.get(m["sid"])]
        for m in pool:
            if all(not conflicts(m, c) for c in chosen):
                chosen.append(m)
        for m in group:
            self.sel[m["sid"]] = m in chosen
        for m in chosen:
            self.touch(m["sid"])

    def auto_all(self):
        for group in conflict_groups(self.checked_mods()):
            if len(group) > 1:
                self.auto_group(group)
        self.refresh_tree()
        self.refresh_groups()

    def clear_groups(self):
        for group in conflict_groups(self.checked_mods()):
            if len(group) > 1:
                for m in group:
                    self.sel[m["sid"]] = False
        self.refresh_tree()
        self.refresh_groups()

    def select_all(self):
        """Check every known mod."""
        for m in self.mods:
            self.sel[m["sid"]] = True
        self.refresh_tree()
        self.refresh_groups()

    def fill_best(self):
        """Check the largest mutually compatible set per group, ignoring the
        current checks. Greedy: biggest mods first, keep it if compatible
        with everything already kept."""
        for group in conflict_groups(self.mods):
            if len(group) > 1:
                chosen = auto_pick(group)
                for m in group:
                    self.sel[m["sid"]] = m in chosen
                for m in chosen:
                    self.touch(m["sid"])
        self.refresh_tree()
        self.refresh_groups()

    def apply(self):
        changed = 0
        for m in self.mods:
            want = self.sel.get(m["sid"], m["active"])
            if want != m["active"]:
                if write_active(self.dirpath, m["sid"], want):
                    changed += 1
        count = write_modlist(self.dirpath, self.sel, self.order)
        self.reload()
        self.status.set("Applied %d changes, wrote %d mods to modlist.txt"
                        % (changed, count))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dirpath = args[0] if args else DEFAULT_DIR
    if not dirpath or not os.path.isdir(dirpath):
        print("No Workshop content dir found; pass one as argument.")
        return
    mods = load_mods(dirpath)
    groups = conflict_groups(mods)
    if "--smoke" in sys.argv:
        multi = [g for g in groups if len(g) > 1]
        print("%d mods, %d conflict groups" % (len(mods), len(multi)))
        for g in sorted(multi, key=len, reverse=True)[:5]:
            print("group x%d: %s" % (len(g), ", ".join(m["title"][:28] for m in g)))
        return
    root = tk.Tk()
    App(root, dirpath)
    root.mainloop()


if __name__ == "__main__":
    main()

