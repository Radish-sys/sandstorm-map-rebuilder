"""Shared logic for the app and the command line: extraction, the list of extracted maps the editor menu reads,
and finding / setting up the Sandstorm SDK."""
import glob
import json
import os
import re
import shutil
import string
import sys
import time

APP_NAME = 'SandstormMapRebuilder'


# ----------------------------------------------------------------------------- locations

def app_dir():
    """Folder of the exe (or of the repo when run from source)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource(*parts):
    """Bundled file: inside the PyInstaller bundle when frozen, else in the repo."""
    base = getattr(sys, '_MEIPASS', None) or app_dir()
    return os.path.join(base, *parts)


def config_dir():
    root = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _load(name, default):
    try:
        with open(os.path.join(config_dir(), name), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(name, data):
    path = os.path.join(config_dir(), name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)  # ASCII (non-ASCII paths are \u-escaped), so the editor menu reads it either way
    os.replace(tmp, path)


def settings():
    return _load('settings.json', {})


def save_settings(s):
    _save('settings.json', s)


# ----------------------------------------------------------------------------- extracted maps (read by the editor menu)

def maps_file():
    return os.path.join(config_dir(), 'maps.json')


def extracted_maps():
    return [m for m in _load('maps.json', []) if os.path.exists(os.path.join(m.get('folder', ''), 'layout.json'))]


def remember_map(name, folder, mod):
    # one entry per map name: re-extracting into another folder (e.g. a newer copy of the app) replaces the old one
    maps = [m for m in _load('maps.json', []) if os.path.normcase(m.get('folder', '')) != os.path.normcase(folder)
            and m.get('name') != name]
    maps.insert(0, {'name': name, 'folder': folder, 'mod': mod, 'extracted': time.strftime('%Y-%m-%d %H:%M')})
    _save('maps.json', maps[:30])  # the editor menu lists all of them (MAX_MAPS in sdk/smr_menu.py)


def forget_map(folder):
    _save('maps.json', [m for m in _load('maps.json', [])
                        if os.path.normcase(m.get('folder', '')) != os.path.normcase(folder)])


# ----------------------------------------------------------------------------- extraction

def find_paks(paths):
    """Files and folders -> every .pak, main pak first (the extractor wants the map's own chunk first)."""
    paks = []
    for p in paths:
        p = p.strip().strip('"')
        if os.path.isdir(p):
            paks += glob.glob(os.path.join(p, '**', '*.pak'), recursive=True)
        elif p.lower().endswith('.pak') and os.path.isfile(p):
            paks.append(p)
    seen, out = set(), []
    for p in paks:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            out.append(os.path.abspath(p))

    def key(p):
        n = os.path.basename(p).lower()
        return ('optional' in n, 'sound' in n, not re.search(r'pakchunk0', n), n)
    return sorted(out, key=key)


def map_name(paks):
    n = os.path.splitext(os.path.basename(paks[0]))[0]
    return re.split(r'pakchunk', n, flags=re.I)[0] or n


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, '.smr_write_test')
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def output_root():
    """Where extracted maps go: the folder picked in the app, else SMR Maps next to the app, else (the app sits
    somewhere read-only, e.g. Program Files) SMR Maps in Documents."""
    chosen = settings().get('output_root')
    if chosen:
        return chosen
    # 'SMR Maps', not 'SMR': on Windows a folder named SMR next to the source tree IS the smr package folder
    beside = os.path.join(app_dir(), 'SMR Maps')
    return beside if _writable(beside) else os.path.join(os.path.expanduser('~'), 'Documents', 'SMR Maps')


def default_output(paks):
    return os.path.join(output_root(), map_name(paks))


RENAME_SUFFIX = 'RPK'


def original_mod(paks):
    """The original mod's name, read from the paks' file lists (instant, nothing is extracted): the same mod the
    extraction will rebuild, so the name the app suggests matches the one the extractor uses."""
    from .extract import ModFS, order_mods
    try:
        with ModFS(paks) as fs:
            mods = order_mods(fs, paks)
    except Exception:
        mods = []
    return mods[0] if mods else (clean_mod_name(map_name(paks)) or 'Map')


def default_target(mod):
    """The mod name a rebuild gets unless the user picks one: never the original's, or both mods load into the same
    paths and the game picks whichever it finds first (players with the original subscribed get the old, broken
    one, reported as 'Corrupt data found')."""
    return (mod or 'Map') + RENAME_SUFFIX


def clean_mod_name(name):
    """A mod (plugin) name the SDK accepts: letters, digits and underscores, starting with a letter."""
    n = re.sub(r'[^A-Za-z0-9_]', '', name or '')
    return n if n[:1].isalpha() else ''


def travel_maps(L):
    """Packages of the maps players travel to: the levels the mod's scenarios play on. Other Worlds the extract
    lists as persistent (TORO's Overview, TORO_Lights, Assault_Backup_Gamemodes...) are sublevels or leftovers and
    keep their names. Without scenarios: the level named like the mod, else every persistent level."""
    old = (L.get('mods') or [None])[0]
    pers = list(L.get('persistent_levels') or [])
    maps = []
    for s in (L.get('scenarios') or {}).values():
        lvl = (s.get('Level') or '').split('.')[0] if isinstance(s, dict) else ''
        if lvl and lvl not in maps:
            maps.append(lvl)
    if not maps:
        maps = [p for p in pers if old and p.rsplit('/', 1)[-1].lower() == old.lower()] or pers
    return maps


def rename_plan(L, target):
    """Old package -> new package for the mod's maps and scenarios, so the rebuilt map and its scenarios get names
    the original doesn't use (the game finds maps and scenarios by name, not by path).
    The map name follows the mod name: JunkYard -> JunkYardRPK makes the map JunkYardRPK and its scenarios
    Scenario_JunkYardRPK_*. Returns (renames, map_renames); both empty when the name is unchanged."""
    old = (L.get('mods') or [None])[0]
    if not old or not target or target == old:
        return {}, {}
    suffix = target[len(old):] if target.lower().startswith(old.lower()) and len(target) > len(old) else None
    travel = travel_maps(L)
    names = [p.rsplit('/', 1)[-1] for p in travel]
    maps = {}
    for m in names:
        maps[m] = m + suffix if suffix else (target if len(names) == 1 else m + '_' + target)
    tail = suffix or '_' + target

    def newname(name):
        for m in sorted(maps, key=len, reverse=True):
            i = name.lower().find(m.lower())
            if i >= 0:
                return name[:i] + maps[m] + name[i + len(m):]
        return name + tail
    renames = {}
    # only the maps players travel to and the scenarios: sublevels are found by name from the map's streaming list,
    # its lighting scenarios and the scenarios' sublevel lists, and keep their original names
    for pkg in travel + list(L.get('scenarios') or {}):
        if pkg.startswith('/' + old + '/'):
            folder, name = pkg.rsplit('/', 1)
            renames[pkg] = folder + '/' + newname(name)
    return renames, maps


def set_target(L, target):
    """Record the mod name the rebuild goes into (layout.json: target_mod, renames, map_renames)."""
    old = (L.get('mods') or [None])[0]
    target = clean_mod_name(target) or default_target(old)
    renames, maps = rename_plan(L, target)
    L['target_mod'] = target
    L['renames'] = renames
    L['map_renames'] = maps
    if renames:
        print('  rebuild goes into mod %s (original: %s); maps renamed: %s' % (
            target, old, ', '.join('%s -> %s' % kv for kv in maps.items()) or 'none'))
    return target


def extract(paks, out, target=None):
    """Run the extractor. Prints progress (the app redirects stdout into its log). Returns a summary dict.
    target: the mod name to rebuild into (default: the original's name + RENAME_SUFFIX)."""
    from .extract import run, write_report
    from . import preflight
    t0 = time.time()
    L = run(paks, out)
    borrow_meshes(L, out)
    target = set_target(L, target)
    L['preflight'] = preflight.run(L)
    print('\n'.join(preflight.lines(L['preflight'])))
    with open(os.path.join(out, 'layout.json'), 'w', encoding='utf-8') as fh:
        json.dump(L, fh, indent=1, default=str)
    write_report(L, os.path.join(out, 'REPORT.md'))
    mod = (L.get('mods') or [map_name(paks)])[0]
    remember_map(map_name(paks), os.path.abspath(out), mod)
    s = {
        'preflight': L['preflight'],
        'map': map_name(paks), 'mod': mod, 'target': target,
        'new_map': next(iter((L.get('map_renames') or {}).values()), None),
        'folder': os.path.abspath(out), 'seconds': round(time.time() - t0),
        'levels': len(L.get('levels') or {}),
        'actors': sum(len(l.get('actors') or []) for l in (L.get('levels') or {}).values()),
        'meshes': len(L.get('meshes') or {}), 'mesh_errors': len(L.get('mesh_errors') or {}),
        'stand_ins': len([m for m in (L.get('materials') or {}).values() if m.get('substitute')]),
        'textures': len([t for t in L.get('textures') or [] if 'file' in t]),
        'landscapes': len(L.get('landscapes') or []),
        'scenarios': len(L.get('scenarios') or {}),
        'sounds': len([x for x in L.get('sounds') or [] if 'file' in x]),
    }
    return s


def _rest(path):
    """'/JunkYard/Brushify/Meshes/S_Grass' -> 'Brushify/Meshes/S_Grass' (the path inside its mod)."""
    return path.strip('/').split('/', 1)[-1]


def borrow_meshes(L, out):
    """Meshes this pak couldn't provide (geometry in a pak the mod doesn't ship) are often marketplace assets
    another extracted map has too, at the same path inside its mod. Copy that FBX in as this mod's own mesh."""
    errors = L.get('mesh_errors') or {}
    if not errors:
        return False
    want = {_rest(p): p for p in errors}
    borrowed = 0
    for m in extracted_maps():
        if os.path.normcase(os.path.abspath(m['folder'])) == os.path.normcase(os.path.abspath(out)):
            continue
        try:
            with open(os.path.join(m['folder'], 'layout.json'), encoding='utf-8') as fh:
                other = json.load(fh)
        except (OSError, ValueError):
            continue
        for opath, mesh in (other.get('meshes') or {}).items():
            mine = want.get(_rest(opath))
            if not mine or mine not in errors:
                continue
            src = os.path.join(m['folder'], *mesh['file'].split('/'))
            rel = mine.strip('/') + '.fbx'
            dst = os.path.join(out, 'meshes', *rel.split('/'))
            if not os.path.exists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(src, dst)
            entry = dict(mesh, file='meshes/' + rel, borrowed_from=m['name'])
            mats = []
            my_root = mine.strip('/').split('/')[0]
            for slot in mesh.get('materials') or []:
                mp = (slot.get('material') or '').split('.')[0]
                if not mp or mp.startswith(('/Game/', '/Engine/')):
                    mats.append(slot)  # base-game material: the same in every mod
                    continue
                # the other mod's material -> this mod's copy at the same path, if it has one
                cand = '/%s/%s' % (my_root, _rest(mp))
                mats.append(dict(slot, material=cand + '.' + cand.split('/')[-1]
                                 if cand in (L.get('materials') or {}) else None))
            entry['materials'] = mats
            L.setdefault('meshes', {})[mine] = entry
            del errors[mine]
            borrowed += 1
            print('  borrowed %s from the %s extraction (same marketplace asset)' % (mine.split('/')[-1], m['name']))
    return borrowed > 0


# ----------------------------------------------------------------------------- SDK

def _steam_libraries():
    roots = []
    for base in (r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam'):
        vdf = os.path.join(base, 'steamapps', 'libraryfolders.vdf')
        if os.path.exists(vdf):
            roots.append(base)
            try:
                with open(vdf, encoding='utf-8', errors='replace') as fh:  # Steam writes it as UTF-8
                    roots += [p.replace('\\\\', '\\') for p in re.findall(r'"path"\s+"([^"]+)"', fh.read())]
            except OSError:
                pass
    return roots


def is_sdk(folder):
    """The SDK root is the folder that holds Insurgency\\Insurgency.uproject and the editor binaries."""
    return bool(folder) and os.path.isfile(os.path.join(folder, 'Insurgency', 'Insurgency.uproject'))


def find_sdk():
    cands = []
    for lib in _steam_libraries():
        cands += glob.glob(os.path.join(lib, 'steamapps', 'common', '*'))
    for drive in string.ascii_uppercase[2:8]:
        for base in (r'%s:\Program Files\Epic Games' % drive, r'%s:\Epic Games' % drive):
            cands += glob.glob(os.path.join(base, '*'))
    for c in cands:
        if is_sdk(c):
            return c
    return None


MENU_MARK = '# --- Sandstorm Map Rebuilder menu (managed by the app) ---'


def _read_init(path):
    """init_unreal.py as text; '' if there is none. Read as UTF-8 (what the editor's Python uses), with any bytes
    that aren't kept as they are, so another tool's lines survive the rewrite on Remove byte for byte."""
    try:
        with open(path, encoding='utf-8', errors='surrogateescape') as fh:
            return fh.read()
    except OSError:
        return ''


def sdk_python_dir(sdk):
    return os.path.join(sdk, 'Insurgency', 'Content', 'Python')


def menu_installed(sdk):
    """Installed version of the editor menu, or None."""
    try:
        with open(os.path.join(sdk_python_dir(sdk), 'smr_menu.py'), encoding='utf-8', errors='replace') as fh:
            txt = fh.read()
        m = re.search(r"MENU_VERSION = '([^']+)'", txt)
        return m.group(1) if m else '?'
    except OSError:
        return None


def installed_build(sdk):
    """Build of the installed rebuild script (r31...), or None. Test builds share a version number, so the version
    alone can't tell an old script from the current one."""
    try:
        with open(os.path.join(sdk_python_dir(sdk), 'smr_rebuild_map.py'), encoding='utf-8', errors='replace') as fh:
            txt = fh.read()
        m = re.search(r"BUILD = '([^']+)'", txt)
        return m.group(1) if m else None
    except OSError:
        return None


def install_menu(sdk):
    """Copy the editor menu + rebuild script into the SDK's Content/Python and hook init_unreal.py.
    An existing init_unreal.py is kept; one marked line is added to it."""
    if not is_sdk(sdk):
        raise ValueError('%s is not the Sandstorm SDK folder (no Insurgency\\Insurgency.uproject in it)' % sdk)
    d = sdk_python_dir(sdk)
    os.makedirs(d, exist_ok=True)
    shutil.copy(resource('sdk', 'smr_menu.py'), os.path.join(d, 'smr_menu.py'))
    shutil.copy(resource('sdk', 'rebuild_map.py'), os.path.join(d, 'smr_rebuild_map.py'))
    init = os.path.join(d, 'init_unreal.py')
    hook = MENU_MARK + '\ntry:\n    import smr_menu\n    smr_menu.register()\nexcept Exception as _smr_ex:\n' \
                       '    import unreal\n    unreal.log_warning("SMR menu failed to load: %s" % _smr_ex)\n'
    old = _read_init(init)
    if MENU_MARK not in old:
        with open(init, 'a', encoding='utf-8', errors='surrogateescape') as fh:
            fh.write(('\n' if old and not old.endswith('\n') else '') + hook)
    return d


def uninstall_menu(sdk):
    d = sdk_python_dir(sdk)
    for f in ('smr_menu.py', 'smr_rebuild_map.py'):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    init = os.path.join(d, 'init_unreal.py')
    if os.path.exists(init):
        txt = _read_init(init)
        if MENU_MARK in txt:
            # remove just the hook block: the mark through its "SMR menu failed to load" line, so another tool's
            # try/except written after it stays. A hook without that line falls back to the mark plus its try/except
            hook = re.escape(MENU_MARK) + r'\n(?:.*\n){0,8}?.*SMR menu failed to load.*(?:\n|$)'
            if re.search(hook, txt):
                txt = re.sub(hook, '', txt, count=1)
            else:
                txt = re.sub(re.escape(MENU_MARK) + r'\ntry:\n(?:    .*\n?)*(?:except.*\n?(?:    .*\n?)*)?', '', txt)
            txt = txt.rstrip() + '\n'
            if txt.strip():
                with open(init, 'w', encoding='utf-8', errors='surrogateescape') as fh:
                    fh.write(txt)
            else:
                os.remove(init)
