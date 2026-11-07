"""
Sandstorm Map Rebuilder - make a mod self-contained.

The SDK mounts every mod at once, so a mod can end up pointing at assets that live in ANOTHER mod (e.g. JunkYard's
clovers using ShootHouse's /WMMod/... materials). The editor shows them fine; the cooked game doesn't have them
unless the player also has that other mod, so they render black/grey.

This finds every asset in MOD that depends on another mod's asset and points it at a local one instead:
  - an asset with the same name and type already in MOD, if there is one;
  - otherwise a copy of the other mod's asset, made under /MOD/Deps/<other mod>/... (and fixed the same way).
Nothing in the other mod is changed.

Set MOD below, then File > Execute Python Script... > this file. Cook again afterwards.
"""
import os
import unreal

MOD = 'JunkYardRPK'

EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
REG = unreal.AssetRegistryHelpers.get_asset_registry()
ROOT = '/' + MOD
out = []
by_hand = []
mapping = {}       # other mod's package -> local package
fixed = set()


def say(msg):
    out.append(msg)
    unreal.log_warning('[SMR localize] ' + msg)


def short(p):
    return p.split('/')[-1].split('.')[0]


def pkg_of(obj):
    return obj.get_path_name().split('.')[0] if obj else None


def load(pkg):
    return unreal.load_object(None, pkg + '.' + short(pkg))


def cls_of(pkg):
    try:
        ad = REG.get_asset_by_object_path(pkg + '.' + short(pkg))
        return str(ad.asset_class) if ad and ad.is_valid() else ''
    except Exception:
        return ''


def other_mods():
    d = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir()), 'Mods')
    try:
        return {n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))} - {MOD}
    except OSError:
        return set()


OTHERS = other_mods()
OPTS = unreal.AssetRegistryDependencyOptions(include_soft_package_references=True,
                                             include_hard_package_references=True)


def external_deps(pkg):
    try:
        deps = REG.get_dependencies(pkg, OPTS) or []
    except Exception:
        return []
    return [str(d) for d in deps if str(d).strip('/').split('/')[0] in OTHERS]


LOCAL_BY_NAME = {}
for ad in REG.get_assets_by_path(ROOT, True):
    LOCAL_BY_NAME.setdefault(str(ad.asset_name), []).append(str(ad.package_name))


def localize(ext):
    """A package of this mod to use instead of the other mod's package ext."""
    if ext in mapping:
        return mapping[ext]
    kind = cls_of(ext)
    same = [p for p in LOCAL_BY_NAME.get(short(ext), []) if cls_of(p) == kind and kind]
    if same:
        mapping[ext] = same[0]
        say('%s -> this mod\'s own %s' % (ext, same[0]))
        return same[0]
    dest = ROOT + '/Deps' + ext
    if not EAL.does_asset_exist(dest):
        if not EAL.duplicate_asset(ext, dest):
            by_hand.append('%s: could not be copied into %s' % (ext, MOD))
            mapping[ext] = None
            return None
        say('%s copied to %s' % (ext, dest))
    mapping[ext] = dest
    fix(dest)  # the copy can point at the other mod too
    return dest


def fix(pkg):
    if pkg in fixed:
        return
    fixed.add(pkg)
    deps = external_deps(pkg)
    if not deps:
        return
    kind = cls_of(pkg)
    if kind == 'World':
        by_hand.append('level %s uses %s: check its actors\' materials/foliage' % (short(pkg), ', '.join(deps)))
        return
    obj = load(pkg)
    if obj is None:
        by_hand.append('%s could not be loaded' % pkg)
        return
    swap = {d: localize(d) for d in deps}
    swap = {d: load(n) for d, n in swap.items() if n}
    changed = False
    try:
        if isinstance(obj, unreal.StaticMesh):
            mats = obj.get_editor_property('static_materials')
            for i, sm in enumerate(mats):
                mi = sm.get_editor_property('material_interface')
                if pkg_of(mi) in swap:
                    obj.set_material(i, swap[pkg_of(mi)])
                    changed = True
        elif isinstance(obj, unreal.MaterialInstanceConstant):
            par = obj.get_editor_property('parent')
            if pkg_of(par) in swap:
                MEL.set_material_instance_parent(obj, swap[pkg_of(par)])
                changed = True
            for tp in obj.get_editor_property('texture_parameter_values'):
                t = tp.get_editor_property('parameter_value')
                if pkg_of(t) in swap:
                    name = tp.get_editor_property('parameter_info').get_editor_property('name')
                    MEL.set_material_instance_texture_parameter_value(obj, name, swap[pkg_of(t)])
                    changed = True
            if changed:
                MEL.update_material_instance(obj)
        elif 'FoliageType' in kind or 'GrassType' in kind:
            for prop in ('mesh', 'override_materials', 'grass_varieties'):
                try:
                    v = obj.get_editor_property(prop)
                except Exception:
                    continue
                if prop == 'mesh' and pkg_of(v) in swap:
                    obj.set_editor_property('mesh', swap[pkg_of(v)])
                    changed = True
                elif prop == 'override_materials':
                    new = [swap.get(pkg_of(m), m) for m in v]
                    if any(n is not m for n, m in zip(new, v)):
                        obj.set_editor_property('override_materials', new)
                        changed = True
                elif prop == 'grass_varieties':
                    hit = False
                    for gv in v:
                        m = gv.get_editor_property('grass_mesh')
                        if pkg_of(m) in swap:
                            gv.set_editor_property('grass_mesh', swap[pkg_of(m)])
                            hit = True
                    if hit:
                        obj.set_editor_property('grass_varieties', v)
                        changed = True
    except Exception as ex:
        by_hand.append('%s: %s' % (pkg, str(ex).splitlines()[0][:150]))
    if changed:
        EAL.save_loaded_asset(obj)
    left = external_deps(pkg)
    if left:
        by_hand.append('%s (%s) still uses %s' % (short(pkg), kind, ', '.join(left)))
    elif changed:
        say('fixed %s (%s)' % (short(pkg), kind))


def main():
    if not EAL.does_directory_exist(ROOT):
        say('STOPPED: no mod named %s' % MOD)
        return
    pkgs = [str(ad.package_name) for ad in REG.get_assets_by_path(ROOT, True)]
    hits = [p for p in dict.fromkeys(pkgs) if external_deps(p)]
    say('%d of %d assets in %s use other mods\' assets' % (len(hits), len(pkgs), MOD))
    with unreal.ScopedSlowTask(len(hits), 'Making %s self-contained' % MOD) as task:
        task.make_dialog(True)
        for p in hits:
            if task.should_cancel():
                break
            task.enter_progress_frame(1, short(p))
            fix(p)
    EAL.save_directory(ROOT, True, True)
    for b in by_hand:
        say('BY HAND: ' + b)
    say('done: %d assets now local. Cook again.' % len([v for v in mapping.values() if v]))
    unreal.EditorDialog.show_message('SMR localize', '\n'.join(out[-14:]), unreal.AppMsgType.OK)


main()
