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
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

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


def games_cache_path():
    return os.path.join(support_dir(), "steam-workshop-games.txt")


def load_games_cache():
    out = {}
    try:
        with open(games_cache_path(), encoding="utf-8-sig") as fh:
            for line in fh:
                a, _, t = line.rstrip("\n").partition("\t")
                if a.strip().isdigit():
                    out[a.strip()] = t.strip()
    except OSError:
        pass
    return out


def save_games_cache(titles):
    try:
        with open(games_cache_path(), "w", encoding="utf-8") as fh:
            for a in sorted(titles, key=int):
                fh.write("%s\t%s\n" % (a, titles[a]))
    except OSError:
        pass


def read_app_titles(steamapps_dir):
    """appid -> product name pairs from Steam's per-app acf manifests."""
    out = {}
    try:
        names = sorted(os.listdir(steamapps_dir))
    except OSError:
        return out
    for nme in names:
        if not nme.startswith("appmanifest_") or not nme.endswith(".acf"):
            continue
        appid = nme[len("appmanifest_"):-len(".acf")]
        if not appid.isdigit():
            continue
        try:
            raw = open(os.path.join(steamapps_dir, nme), "rb").read()
        except OSError:
            continue
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:40]:
            text = raw.decode("utf-16", "replace")
        else:
            text = raw.decode("utf-8", "replace")
        mm = re.search(r'"name"\s+"([^"]+)"', text)
        if mm:
            out[appid] = mm.group(1)
    return out


def game_label(appid, title):
    return "%s - %s" % (appid, title) if title else appid


def discover_games(base):
    """Installed Workshop games under content/: labels 'steam id - title'.
    Names come from the games cache file first, Steam's appmanifest.vdf
    second; the merged result is written back to the cache."""
    titles = load_games_cache()
    if not base or not os.path.isdir(base):
        return []
    found = []
    for appid in sorted((d for d in os.listdir(base) if d.isdigit()),
                        key=int):
        try:
            kids = os.listdir(os.path.join(base, appid))
        except OSError:
            continue
        if any(k.isdigit() for k in kids):
            found.append(appid)
    if any(not titles.get(a) for a in found):
        titles.update(dict((k, v) for k, v in read_app_titles(
            os.path.dirname(os.path.dirname(base))).items() if v))
    merged = dict((a, titles.get(a, "")) for a in found)
    save_games_cache(merged)
    return [game_label(a, merged[a]) for a in found]


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


def api_details(sid):
    """Official Steam Web API JSON for one item (anonymous POST)."""
    url = ("https://api.steampowered.com/ISteamRemoteStorage/"
           "GetPublishedFileDetails/v1/")
    data = urllib.parse.urlencode(
        {"itemcount": 1, "publishedfileids[0]": sid}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Accept": "application/json",
                                 "User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            body = urllib.request.urlopen(req, timeout=12).read().decode(
                "utf-8", "replace")
            j = json.loads(body)
            lst = j.get("response", {}).get("publishedfiledetails") or []
            return lst[0] if lst else {}
        except Exception:
            time.sleep(1)
    return {}


def clean_bb(text):
    """Flatten BBCode-ish markup the API puts in descriptions."""
    text = re.sub(r"\[/?(?:b|i|u|h[12]|br|list|olist|li|url(?:=[^\]]*)?"
                  r"|img(?:=[^\]]*)?|spoiler|table|trow|th|td)\]", " ", text)
    return " ".join(html.unescape(text).split())


def fetch_page(sid):
    """Item page HTML, with retries (Workshop rate-limits bursts)."""
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
    return page


def fetch_desc(sid, cid=""):
    """Description + last-update epoch from the Steam Web API JSON; creator
    name from the item page anchor matching the creator id (persona text,
    vanity slug only as fallback). Page fetched only when needed."""
    j = api_details(sid)
    desc = clean_bb(str(j.get("description") or "")) if j else ""
    updated = str(j.get("time_updated") or "")
    name = ""
    page = fetch_page(sid) if cid or not desc else ""
    if page:
        if not desc:
            m = re.search('id="highlightContent">(.+?)</div>', page, re.S)
            if m:
                text = re.sub(r"<[^>]+>", " ", m.group(1))
                desc = " ".join(html.unescape(text).split())
        pats = []
        if cid:
            pats.append('href="[^"]*/(?:profiles|online)/%s[^"]*"[^>]*>'
                        '([^<]{2,60})<' % re.escape(cid))
        pats.append(
            r'href="[^"]*/(?:id|profiles)/[^"]+"[^>]*>([^<]{2,60})<')
        for pat in pats:
            mm = re.search(pat, page)
            if mm:
                name = html.unescape(mm.group(1)).strip()
                if name:
                    break
        if not name:
            mm = re.search(r'href="[^"]*/id/([^"/]+)"', page)
            if mm:
                name = mm.group(1).strip()
        if name.endswith("'s Workshop"):
            name = name[:-len("'s Workshop")]
    if not updated and page:
        epochs = re.findall(r"\b1[67]\d{8}\b", page)
        cutoff = int(time.time()) - 3 * 86400
        past = [e for e in epochs if int(e) <= cutoff]
        pool = past or epochs
        updated = max(pool, key=int) if pool else ""
    return desc, name, updated


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
        cid = str(info.get("creator") or "").strip()
        author = " ".join(str(info.get("author") or "").split())
        try:
            upd = str(int(os.path.getmtime(os.path.join(
                dirpath, sid, "modinfo.json"))))
        except Exception:
            upd = ""
        mods.append({"sid": sid,
                     "title": info.get("title") or "(untitled)",
                     "folder": info.get("folderName") or sid,
                     "desc": desc,
                     "cid": cid,
                     "creator": author or cid,
                     "active": bool(info.get("active", True)),
                     "edited": edited,
                     "assets": assets,
                     "stems": stems,
                     "hashes": hs,
                     "upd": upd})
    apply_cache(load_desc_cache(dirpath), mods)
    return mods


def apply_cache(cache, mods):
    """Fill short descriptions and creator names from the local cache."""
    for m in mods:
        short = len(m["desc"].split()) < 10
        ent = cache.get(m["sid"])
        if isinstance(ent, str):
            ent = {"desc": ent}
        cname = cache.get("by:" + m["cid"]) if m["cid"] else None
        if not cname and isinstance(ent, dict):
            cname = ent.get("creator") or None
        if short and (ent or {}).get("desc"):
            m["desc"] = ent["desc"]
        if cname:
            m["creator"] = cname
        if isinstance(ent, dict) and ent.get("updated"):
            m["upd"] = str(ent["updated"])


def upd_stamp(mod):
    """Short 'last touched' date: cached epoch when present, else the time
    Steam wrote the modinfo.json (its download stamp)."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(mod["upd"])))
    except Exception:
        return ""


def fetch_missing(dirpath, mods):
    """Slow pass (worker thread): top up short descriptions and missing
    creator names from the Steam Workshop into the cache, deduped per
    creator id. Returns True if new data arrived."""
    cache = load_desc_cache(dirpath)
    changed = False
    tried, tries = set(), 0
    for m in mods:
        short = len(m["desc"].split()) < 10
        cid = m["cid"]
        ent = cache.get(m["sid"])
        if isinstance(ent, str):
            ent = {"desc": ent}
        ent = dict(ent or {})
        name_key = ("by:" + cid) if cid else ""
        cname = cache.get(name_key) if name_key else None
        if cname is None and isinstance(ent, dict) and ent.get("creator"):
            cname = ent["creator"]
            if name_key:
                cache[name_key] = cname
                changed = True
        need_desc = short and not ent.get("desc")
        need_name = bool(cid) and cname is None and cid not in tried
        if (need_desc or need_name) and tries < 60:
            tries += 1
            tried.add(cid)
            note, pname, pup = fetch_desc(m["sid"],
                                          "" if cname else cid)
            changed = True
            if note and not ent.get("desc"):
                ent["desc"] = note
            if pname and name_key:
                cache[name_key] = pname
            if pup and not ent.get("updated"):
                ent["updated"] = pup
            if ent.get("desc") or ent.get("updated"):
                cache[m["sid"]] = ent
    if changed:
        save_desc_cache(dirpath, cache)
    apply_cache(cache, mods)
    return changed


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
    appid = os.path.basename(dirpath.rstrip(os.sep)) or APP_ID
    label = game_label(appid, load_games_cache().get(appid, ""))
    for ch in '/\\:*?"<>|':
        label = label.replace(ch, " ")
    home = os.path.join(support_dir(), "modlists", label)
    backups = os.path.join(home, "backups")
    if not os.path.isdir(home):
        os.makedirs(backups, exist_ok=True)
    list_file = os.path.join(home, "modlist.txt")
    if os.path.isfile(list_file):
        n = 0
        while os.path.isfile(os.path.join(backups, "modlist-bak%d.txt" % n)):
            n += 1
        os.replace(list_file, os.path.join(backups,
                                           "modlist-bak%d.txt" % n))
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
        lines.append("%s\t%s\t%s\t%s"
                     % (sid, on, shown_title(m), m["desc"]))
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

def shown_title(mod):
    """Title plus creator in parentheses, for list and report display."""
    if mod["creator"]:
        return "%s (%s)" % (mod["title"], mod["creator"])
    return mod["title"]


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
        filt_lbl = ttk.Label(top, text="Filter:")
        cur = os.path.basename((dirpath or "").rstrip(os.sep))
        self.base = os.path.dirname((dirpath or "").rstrip(os.sep))
        labels = discover_games(self.base)
        self.game_box = ttk.Combobox(top, width=42, state="readonly")
        self.game_box["values"] = labels
        self.game_box.set(next((l for l in labels if l == cur
                                or l.split(" - ")[0] == cur), cur))
        self.game_box.bind("<<ComboboxSelected>>", self.pick_game)
        self.game_box.pack(side="left", padx=(0, 6))
        filt_lbl.pack(side="left")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.filter_var, width=40)
        ent.pack(side="left", padx=4)
        ent.bind("<KeyRelease>", lambda e: self.refresh_tree())
        ttk.Button(top, text="Reload", command=self.reload).pack(side="left", padx=4)
        ttk.Button(top, text="Select all", command=self.select_all).pack(side="left", padx=4)
        ttk.Button(top, text="Unselect all", command=self.unselect_all).pack(side="left", padx=4)
        ttk.Button(top, text="Reset", command=self.reload).pack(side="left", padx=4)
        ttk.Button(top, text="Apply", command=self.apply).pack(side="left", padx=4)
        body = ttk.Panedwindow(root, orient="vertical")
        body.pack(fill="both", expand=True, padx=6)
        self.vbody = body
        paned = ttk.Panedwindow(body, orient="horizontal")
        self.paned = paned
        left = ttk.Frame(paned, width=520)
        paned.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("on", "title", "author",
                                                "sid"),
                                 height=20, show="headings")
        self.tree.heading("on", text="On")
        self.tree.heading("title", text="Title")
        self.tree.heading("author", text="Author")
        self.tree.heading("sid", text="Steam ID")
        self.tree.column("on", width=50, anchor="center", stretch=False)
        self.tree.column("title", width=300)
        self.tree.column("author", width=130, stretch=False)
        self.tree.column("sid", width=90, stretch=False)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self._drag = None
        self._drag_moved = False
        self.tree.bind("<ButtonPress-1>", self.press_row)
        self.tree.bind("<B1-Motion>", self.drag_row)
        self.tree.bind("<ButtonRelease-1>", self.release_row)
        self.tree.bind("<<TreeviewSelect>>", self.show_desc)
        self.tip = ToolTip()
        self.tree.bind("<Motion>", self.on_hover)
        self.tree.bind("<Leave>", lambda e: self.tip.hide())
        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self.authors = ttk.LabelFrame(right, text="Authors")
        self.authors.pack(side="top", fill="x", pady=(0, 4))
        panel = ttk.LabelFrame(right, text="Conflict groups")
        panel.pack(side="top", fill="both", expand=True)
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
        ttk.Button(bottom, text="Auto-resolve all", command=self.auto_all).pack(side="left")
        ttk.Button(bottom, text="Best fill groups", command=self.fill_best).pack(side="left", padx=4)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", padx=6)

        self.split_done = False
        root.bind("<Configure>", self.center_split, add=True)
        root.after(50, lambda: self.center_split_width(root.winfo_width()))
        self.reload()
        self.start_bg()

    def start_bg(self):
        """Top up descriptions/names on a worker thread; non-blocking, the
        list repaints itself once new data lands in the cache."""
        def work():
            try:
                fetch_missing(self.dirpath, self.mods)
            except Exception:
                pass
            try:
                self.root.after(0, self.after_bg)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def after_bg(self):
        keep = self.tree.selection()
        self.refresh_tree()
        if keep and keep[0] in self.tree.get_children():
            self.tree.selection_set(keep[0])
            self.show_desc()

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

    def pick_game(self, event=None):
        """Switch to another Workshop game's content dir and resync."""
        appid = self.game_box.get().split(" - ", 1)[0].strip()
        if appid.isdigit():
            self.dirpath = os.path.join(self.base, appid)
            self.reload()

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
        checked = [s for s in self.order
                   if by_id.get(s) and self.sel.get(s)]
        rest = [s for s in self.order
                if by_id.get(s) and not self.sel.get(s)]
        for sid in checked + rest:
            m = by_id.get(sid)
            text = "%s %s %s %s" % (m["title"], m["folder"], m["sid"],
                                    m["creator"])
            if needle and needle not in text.lower():
                continue
            on, orig = bool(self.sel.get(sid)), bool(m["active"])
            if on == orig:
                mark = "\u2713" if on else ""
            elif on:
                mark = "\U0001F5F9"
            else:
                mark = "\u2717"
            self.tree.insert("", "end", iid=sid,
                             values=(mark, m["title"], m["creator"], sid))

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
                base = "%s. %s %s" % (letters[mm["sid"]], mark,
                                      shown_title(mm))
                stamp = upd_stamp(mm)
                if stamp:
                    base += " (%s)" % stamp
                row = ttk.Label(frame, text=base)
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
        self.build_authors()

    def build_authors(self):
        """One row per creator above the groups pane: checkbox + steam id +
        name. The checkbox switches that whole author's mods on/off."""
        for child in self.authors.winfo_children():
            child.destroy()
        heads = {}
        for m in self.mods:
            if m["cid"] and m["cid"] not in heads:
                heads[m["cid"]] = m["creator"] or m["cid"]
        for cid, name in sorted(heads.items(), key=lambda x: x[1].lower()):
            on = any(bool(self.sel.get(x["sid"])) for x in self.mods
                     if x["cid"] == cid)
            var = tk.BooleanVar(value=bool(on))
            row = ttk.Frame(self.authors)
            row.pack(anchor="w")
            ttk.Checkbutton(row, variable=var,
                            command=lambda c=cid, v=var: self.author_toggle(
                                c, v.get())).pack(side="left")
            ttk.Label(row, text=cid).pack(side="left", padx=4)
            ttk.Label(row, text=name).pack(side="left")

    def author_toggle(self, cid, want):
        for m in self.mods:
            if m["cid"] == cid:
                self.sel[m["sid"]] = bool(want)
        self.refresh_tree()
        self.refresh_groups()

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

    def press_row(self, event):
        self._drag = self.tree.identify_row(event.y)
        self._drag_moved = False

    def drag_row(self, event):
        """Reorder rows by dragging: the row follows the pointer."""
        sid, target = self._drag, self.tree.identify_row(event.y)
        if not sid or not target or sid == target:
            return
        self._drag_moved = True
        down = self.order.index(sid) < self.order.index(target)
        self.order.remove(sid)
        ti = self.order.index(target)
        self.order.insert(ti + 1 if down else ti, sid)
        self.refresh_tree()
        self.tree.selection_set(sid)

    def release_row(self, event):
        sid, moved = self._drag, self._drag_moved
        self._drag = None
        if sid is None:
            return
        if sid in self.sel and not moved:
            self.sel[sid] = not self.sel[sid]
            self.refresh_tree()
            self.tree.selection_set(sid)
            self.show_desc()
            self.refresh_groups()
        elif moved:
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

    def select_all(self):
        """Check every known mod."""
        for m in self.mods:
            self.sel[m["sid"]] = True
        self.refresh_tree()
        self.refresh_groups()

    def unselect_all(self):
        """Clear every checkbox."""
        for m in self.mods:
            self.sel[m["sid"]] = False
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
        turns_on = sum(1 for m in self.mods
                       if self.sel.get(m["sid"]) and not m["active"])
        turns_off = sum(1 for m in self.mods
                        if not self.sel.get(m["sid"]) and m["active"])
        if turns_on or turns_off:
            msg = ("Apply changes to Bellwright mods?\n\n"
                   "Switching on:  %d mods\nSwitching off: %d mods\n\n"
                   "Disabled mods move to the bottom of the game's mod "
                   "list.\nEnabled mods keep their relative order."
                   % (turns_on, turns_off))
            if not messagebox.askokcancel("Apply changes", msg):
                return
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

