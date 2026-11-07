"""
Sandstorm Map Rebuilder - move a finished mod into a renamed mod, inside the SDK.

A rebuilt map that keeps the original mod's name collides with the original: players with both installed load
whichever the game finds first. This moves everything from /OLD into /NEW (an empty mod you created), renames the
map, its sublevels and scenarios to match, fixes every reference, and copies ModData across. Your landscape,
lighting and hand fixes come along; nothing is rebuilt.

Before running:
  1. Create the new mod in the SDK (e.g. JunkYardRPK). Leave it empty.
  2. Set OLD and NEW below.
  3. File > Execute Python Script... > this file.
Then open the renamed map, check it, cook, and upload as an update to the existing mod.io listing.
"""
import unreal

OLD = 'JunkYard'        # the mod as it is now
NEW = 'JunkYardRPK'     # the empty mod you created for it

BATCH = 200
EAL = unreal.EditorAssetLibrary
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
REG = unreal.AssetRegistryHelpers.get_asset_registry()
out = []
FIXUP_BY_HAND = []
LIGHTING_BY_HAND = []


def say(msg):
    out.append(msg)
    unreal.log_warning('[SMR rename] ' + msg)


def cls_of(path):
    """Asset class from the registry. EditorAssetLibrary refuses levels ("not accessible because it is of type
    Map/Level"), so it can't be used to find or load Worlds."""
    try:
        ad = REG.get_asset_by_object_path(path + '.' + short(path))
        if ad and ad.is_valid():
            return str(ad.asset_class)
    except Exception:
        pass
    return ''


def load(path):
    return unreal.load_object(None, path + '.' + short(path))


def short(path):
    return path.split('/')[-1].split('.')[0]


def _suffix():
    return NEW[len(OLD):] if NEW.lower().startswith(OLD.lower()) and len(NEW) > len(OLD) else None


def map_names(assets):
    """Persistent maps (their ORIGINAL names): the levels the mod's scenarios play on. On a second run the map may
    already be in the new mod under its new name, so that name is turned back into the original."""
    names = []
    sfx = _suffix()
    for p in assets:
        if 'Scenario' not in cls_of(p):
            continue
        sc = load(p)
        try:
            lvl = sc.get_editor_property('level')
            n = lvl.get_name() if lvl else None
            if n and lvl.get_path_name().startswith('/' + NEW + '/') and sfx and n.endswith(sfx):
                n = n[:-len(sfx)]
            if n and n not in names:
                names.append(n)
        except Exception:
            pass
    if not names:
        names = [short(p) for p in assets if cls_of(p) == 'World' and short(p).lower() == OLD.lower()]
    return names


def plan_names(maps):
    suffix = _suffix()
    ren = {m: (m + suffix if suffix else (NEW if len(maps) == 1 else m + '_' + NEW)) for m in maps}
    tail = suffix or '_' + NEW

    def new_name(name):
        if any(v.lower() in name.lower() for v in ren.values()) or (suffix and name.endswith(suffix)):
            return name  # already renamed (an earlier run, or by hand)
        for m in sorted(ren, key=len, reverse=True):
            i = name.lower().find(m.lower())
            if i >= 0:
                return name[:i] + ren[m] + name[i + len(m):]
        return name + tail
    return ren, new_name


def _repath(v):
    """Directory paths in ModData (Primary Asset Directories) point into the mod: /OLD/... -> /NEW/..."""
    try:
        items = list(v)
    except TypeError:
        return v
    for it in items:
        try:
            pth = str(it.get_editor_property('path'))
            if pth == '/' + OLD or pth.startswith('/' + OLD + '/'):
                it.set_editor_property('path', '/' + NEW + pth[len(OLD) + 1:])
        except Exception:
            pass
    return v


def copy_moddata(old_md, new_md, ren):
    copied = []
    for name in dir(old_md):
        if name.startswith('_') or name in ('game_name',):  # the new mod keeps its own name
            continue
        try:
            v = old_md.get_editor_property(name)
        except Exception:
            continue
        if callable(v):
            continue
        try:
            if isinstance(v, unreal.Map):
                m = unreal.Map(v.key_type, v.value_type)
                for k, val in v.items():
                    m[ren.get(str(k), k) if isinstance(k, (str, unreal.Name)) else k] = val
                v = m
            if isinstance(v, unreal.Array):
                v = _repath(v)
            new_md.set_editor_property(name, v)
            copied.append(name)
        except Exception:
            pass
    try:  # an earlier run copied the old name over
        if str(new_md.get_editor_property('game_name')) == OLD:
            new_md.set_editor_property('game_name', NEW)
    except Exception:
        pass
    return copied


def _batch_rename(moves, title):
    """moves: [(package, dest folder, dest name)] -> Unreal's own rename (fixes every reference)."""
    with unreal.ScopedSlowTask(len(moves), title) as task:
        task.make_dialog(True)
        for i in range(0, len(moves), BATCH):
            if task.should_cancel():
                say('cancelled; run again to finish')
                return False
            chunk = moves[i:i + BATCH]
            data = [unreal.AssetRenameData(load(src), folder, name) for src, folder, name in chunk
                    if load(src) is not None]
            if data and not TOOLS.rename_assets(data):
                say('%s: some assets could not be moved/renamed. If the Output Log says "Not checked-out or writable", '
                    'set Source Control to None (toolbar > Source Control > Change Source Control Settings) and run '
                    'this again.' % title)
            task.enter_progress_frame(len(chunk))
            unreal.SystemLibrary.collect_garbage()
    return True


def _fix_redirectors(root):
    reds = [EAL.load_asset(a.split('.')[0]) for a in EAL.list_assets(root, True, False)
            if cls_of(a.split('.')[0]) == 'ObjectRedirector']
    reds = [r for r in reds if r is not None]
    if not reds:
        return
    try:
        TOOLS.fixup_referencers(reds)
        say('fixed up %d redirectors in %s' % (len(reds), root))
    except AttributeError:  # not exposed to Python in this SDK
        FIXUP_BY_HAND.append(root)
        say('%d redirectors left in %s: right-click its Content folder > Fix Up Redirectors in Folder' % (len(reds), root))


def _pkgs(root, recursive=True):
    found = [str(ad.package_name) for ad in REG.get_assets_by_path(root, recursive)]
    return [a for a in dict.fromkeys(found) if cls_of(a) != 'ObjectRedirector']


def main():
    """Safe to run again, and works after a move done by hand: it moves what is still in /OLD, then renames
    whatever in /NEW still has the old names, then fixes scenarios and ModData."""
    old_root, new_root = '/' + OLD, '/' + NEW
    if not EAL.does_directory_exist(new_root):
        say('STOPPED: no mod named %s. Create it in the SDK first, then run this again.' % NEW)
        return
    # Nothing of either mod may be open, and the script must not switch maps itself: after levels are renamed,
    # unloading a map that uses them trips the editor's "World Memory Leaks" check and it closes.
    cur = unreal.EditorLevelLibrary.get_editor_world()
    cur = cur.get_path_name() if cur else ''
    if cur.startswith(old_root + '/') or cur.startswith(new_root + '/'):
        say('STOPPED: a map of %s is open. Restart the editor (or File > New Level > Empty Level), then run this '
            'again.' % (OLD if cur.startswith(old_root + '/') else NEW))
        unreal.EditorDialog.show_message('SMR rename', out[-1], unreal.AppMsgType.OK)
        return

    # 1. move what is still in the old mod (names unchanged; step 2 renames)
    left = [a for a in _pkgs(old_root) if not (a.count('/') == 2 and 'ModData' in cls_of(a))]
    if left:
        say('moving %d assets from %s to %s' % (len(left), old_root, new_root))
        if not _batch_rename([(a, new_root + a.rsplit('/', 1)[0][len(old_root):], a.rsplit('/', 1)[1]) for a in left],
                             'Moving %s to %s' % (OLD, NEW)):
            return
    _fix_redirectors(old_root)

    # 2. rename the maps, their sublevels and lighting data, and the scenarios
    assets = _pkgs(new_root)
    maps = map_names(assets)
    ren, new_name = plan_names(maps)
    say('maps: %s' % (', '.join('%s -> %s' % kv for kv in ren.items()) or 'none found (no scenario points at a level)'))
    renames = []
    for a in assets:
        kind = cls_of(a)
        # Only the maps players travel to and the scenarios are looked up by name across mods. Sublevels keep their
        # names: the map's streaming list, its lighting scenarios and every scenario's sublevel list refer to them
        # by name, and renaming them breaks those links ("Missing Level", "sublevel not found").
        if (kind == 'World' and a.rsplit('/', 1)[1] in ren) or 'Scenario' in kind:
            folder, name = a.rsplit('/', 1)
            nn = new_name(name)
            if nn != name:
                renames.append((a, folder, nn))
    if renames:
        say('renaming %d: %s' % (len(renames), ', '.join('%s -> %s' % (short(a), n) for a, _, n in renames)))
        if not _batch_rename(renames, 'Renaming %s assets' % NEW):
            return
    _fix_redirectors(new_root)

    # 3. scenarios: the Map name and sublevel names are plain text, not references
    short_ren = dict(ren)
    for p in _pkgs(new_root):
        if 'Scenario' not in cls_of(p):
            continue
        sc = EAL.load_asset(p)
        changed = []
        try:
            m = str(sc.get_editor_property('map'))
            if m in short_ren:
                sc.set_editor_property('map', short_ren[m])
                changed.append('Map %s -> %s' % (m, short_ren[m]))
        except Exception:
            pass
        try:
            subs = sc.get_editor_property('sublevels')
            hit = False
            for x in subs:
                n = str(x.get_editor_property('name'))
                if n in short_ren:
                    x.set_editor_property('name', short_ren[n])
                    hit = True
            if hit:
                sc.set_editor_property('sublevels', subs)
                changed.append('Sublevels')
        except Exception as ex:
            say('%s: sublevel names not updated (%s); check them by hand' % (short(p), str(ex).splitlines()[0][:120]))
        if changed:
            EAL.save_loaded_asset(sc)
            say('%s: %s' % (short(p), ', '.join(changed)))

    # 4. ModData: the game reads ModData_<mod>; fill the new mod's from the old one, then remove the old one
    mine = 'ModData_' + NEW
    mds = [a for a in _pkgs(old_root, False) + _pkgs(new_root, False) if 'ModData' in cls_of(a)]
    new_md = next((a for a in mds if short(a) == mine), None) or next((a for a in mds if a.startswith(new_root + '/')
                                                                         and OLD not in short(a)), None)
    olds = [a for a in mds if a != new_md]
    if new_md and olds:
        copied = copy_moddata(EAL.load_asset(olds[0]), EAL.load_asset(new_md), short_ren)
        EAL.save_loaded_asset(EAL.load_asset(new_md))
        say('ModData: %s filled from %s (%s)' % (short(new_md), short(olds[0]), ', '.join(copied) or 'nothing copied'))
        for o in olds:
            if o.startswith(new_root + '/') and EAL.delete_asset(o):
                say('ModData: removed %s from %s' % (short(o), NEW))
    elif new_md:
        say('ModData: %s kept (no old ModData found to copy from)' % short(new_md))
    else:
        say('ModData: %s not found; create it or fill it in by hand' % mine)

    EAL.save_directory(new_root, False, True)
    main_map = next(iter(ren.values()), NEW)
    say('done. Open /%s/Maps/%s, check World Settings and lighting, then cook. Travel: %s?Scenario=<scenario>'
        % (NEW, main_map, main_map))
    unreal.EditorDialog.show_message('SMR rename', '\n'.join(out[-10:]), unreal.AppMsgType.OK)

main()
