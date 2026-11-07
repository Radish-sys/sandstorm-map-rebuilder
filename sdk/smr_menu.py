"""SMR menu for the Sandstorm SDK editor. Installed by the Sandstorm Map Rebuilder app into
Insurgency/Content/Python and loaded from init_unreal.py when the editor starts.

The menu lists the maps the app has extracted (from %APPDATA%/SandstormMapRebuilder/maps.json) and runs the
rebuild for one of them, so nobody has to type py commands.
"""
import json
import os
import subprocess
import sys

import unreal

MENU_VERSION = '0.9'
HERE = os.path.dirname(os.path.abspath(__file__))
MENU_NAME = 'LevelEditor.MainMenu.SMR'
MAX_MAPS = 30  # as many as the app remembers (smr/core.py remember_map)


def _maps():
    root = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    try:
        with open(os.path.join(root, 'SandstormMapRebuilder', 'maps.json')) as fh:
            maps = json.load(fh)
    except (OSError, ValueError):
        return []
    return [m for m in maps if os.path.exists(os.path.join(m.get('folder', ''), 'layout.json'))]


def _py(code):
    """A menu entry's Python command (runs in the editor's Python)."""
    return 'import smr_menu; smr_menu.%s' % code


def _entry(menu, section, name, label, tip, code):
    e = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
    e.set_label(label)
    e.set_tool_tip(tip)
    e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, '', _py(code))
    menu.add_menu_entry(section, e)


def register():
    """Build (or rebuild) the SMR menu in the level editor's main menu bar."""
    menus = unreal.ToolMenus.get()
    main = menus.find_menu('LevelEditor.MainMenu')
    if main is None:
        unreal.log_warning('SMR: level editor menu not found')
        return
    for i in range(MAX_MAPS):  # the per-map submenus are separate menus; clear them so a refresh doesn't stack entries
        menus.remove_menu('%s.map%d' % (MENU_NAME, i))
    menus.remove_menu(MENU_NAME)
    smr = main.add_sub_menu(main.get_name(), 'SMR', 'SMR', 'SMR', 'Sandstorm Map Rebuilder')
    maps = _maps()
    smr.add_section('Maps', 'Extracted maps' if maps else 'No extracted maps yet: extract one with the app')
    for i, m in enumerate(maps[:MAX_MAPS]):
        sub = smr.add_sub_menu(smr.get_name(), 'Maps', 'map%d' % i, m['name'],
                               '%s  (%s, extracted %s)' % (m['folder'], m.get('mod', ''), m.get('extracted', '')))
        sub.add_section('Run', 'Rebuild')
        f = repr(m['folder'])
        _entry(sub, 'Run', 'rebuild', 'Rebuild map',
               'Textures, materials, meshes, Blueprints, every level and its actors. Safe to run again.',
               'rebuild(%s)' % f)
        _entry(sub, 'Run', 'landscape', 'Rebuild landscape material only',
               'Only (re)builds the layered landscape material and its grass. Leaves actors alone.',
               'rebuild(%s, "landscape")' % f)
        _entry(sub, 'Run', 'holes', 'Finish landscape (holes, layers, phys)',
               'Run straight after importing the landscape by hand: cuts the original holes (stairwells, '
               'trenches, cellars) with its visibility mask, assigns each paint layer its LayerInfo and sets the '
               'phys material. Build Paths afterwards.',
               'rebuild(%s, "holes")' % f)
        _entry(sub, 'Run', 'slots', 'Fix mesh materials only',
               'Points every mesh the rebuild imported at its original materials (WorldGridMaterial fix) and '
               'repairs the stand-in materials if they render plain grey. Leaves the levels alone.',
               'rebuild(%s, "slots")' % f)
        _entry(sub, 'Run', 'foliage', 'Replace foliage only',
               'Removes the foliage the rebuild placed and puts every instance back as instanced meshes '
               '(needs a BP_SMR_Instances Blueprint anywhere in the project). Leaves other actors alone.',
               'rebuild(%s, "foliage")' % f)
        _entry(sub, 'Run', 'lods', 'Add LODs to imported meshes',
               'Generates 3 lower-detail LODs for every mesh the rebuild imported (they only have full detail). '
               'Big FPS win on large maps. Rebuild lighting afterwards.',
               'rebuild(%s, "lods")' % f)
        _entry(sub, 'Run', 'merge', 'Merge repeated props (FPS)',
               'Every mesh placed 3+ times as separate actors becomes one instanced component per level: '
               'far fewer draw calls, same look and collision. Running it again splits back any merged mesh whose material '
               'can\'t be instanced, and mirrored copies. Save a backup first. Rebuild lighting afterwards.',
               'rebuild(%s, "merge")' % f)
        _entry(sub, 'Run', 'roads', 'Place roads only',
               'Places the roads, curbs and fences that were landscape splines in the original. Leaves other actors '
               'alone.',
               'rebuild(%s, "roads")' % f)
        _entry(sub, 'Run', 'graft', 'Place original Blueprints',
               'Makes empty placeholders for the original mod\'s Blueprints that have logic (they can\'t be rebuilt) '
               'and places their actors. TODO.md lists what each placeholder needs. Run again after fixing them.',
               'rebuild(%s, "graft")' % f)
        _entry(sub, 'Run', 'graftpak', 'Build graft pak (after cooking)',
               'Packs the original compiled Blueprints, and the files they use, next to the cooked pak, '
               'so they replace the placeholders in game. Run after every cook, before uploading.',
               'rebuild(%s, "graftpak")' % f)
        _entry(sub, 'Run', 'scenarios', 'Re-link scenarios only',
               'Points the scenarios and World Settings at the objectives and spawn zones in the level as it is now. '
               'Leaves actors alone.',
               'rebuild(%s, "scenarios")' % f)
        sub.add_section('Files', 'Files')
        _entry(sub, 'Files', 'todo', 'Open TODO', 'What is left after the rebuild, in order, with exact values '
               '(opens in your browser; ticks are remembered there)',
               'open_file(%s, "TODO")' % f)
        _entry(sub, 'Files', 'report', 'Open REPORT.md', 'What was found and what is left to do by hand',
               'open_file(%s, "REPORT.md")' % f)
        _entry(sub, 'Files', 'log', 'Open rebuild_log.txt', 'The last rebuild\'s log and to-do list',
               'open_file(%s, "rebuild_log.txt")' % f)
        _entry(sub, 'Files', 'folder', 'Open folder', 'The extracted files (landscape PNGs, sounds...)',
               'open_file(%s)' % f)
    smr.add_section('Tools', 'Tools')
    _entry(smr, 'Tools', 'recompile', 'Recompile game master materials',
           'Fixes game materials showing the default gray texture after a texture change',
           'recompile_game_masters()')
    _entry(smr, 'Tools', 'refresh', 'Refresh map list', 'Pick up maps extracted since the editor started',
           'register()')
    menus.refresh_all_widgets()


def _busy():
    """A full rebuild places actors across editor frames, so the menu stays usable while it runs. Anything else
    started then would load other levels (or recompile materials) between its batches."""
    if getattr(sys, 'smr_running', False):
        unreal.EditorDialog.show_message('SMR', 'A rebuild is still running. Wait for its summary dialog, then try '
                                         'again.', unreal.AppMsgType.OK)
        return True
    return False


def rebuild(folder, *args):
    if _busy():
        return
    # always the script installed with this menu (the app's Reinstall updates both together). Maps extracted by
    # older versions have their own copy next to layout.json; that is only used if nothing is installed.
    script = os.path.join(HERE, 'smr_rebuild_map.py')
    if not os.path.exists(script):
        script = os.path.join(folder, 'rebuild_map.py')
    if not os.path.exists(os.path.join(folder, 'layout.json')):
        unreal.EditorDialog.show_message('SMR', 'No layout.json in %s.\nExtract the map again with the app.' % folder,
                                         unreal.AppMsgType.OK)
        return
    old = sys.argv
    sys.argv = [script, '--folder', folder] + list(args)
    try:
        g = {'__name__': '__main__', '__file__': script}
        with open(script, encoding='utf-8') as fh:
            exec(compile(fh.read(), script, 'exec'), g)
    finally:
        sys.argv = old


def open_file(folder, name=None):
    if name == 'TODO':  # the html version when the rebuild wrote one, else the markdown
        name = 'TODO.html' if os.path.exists(os.path.join(folder, 'TODO.html')) else 'TODO.md'
    path = os.path.join(folder, name) if name else folder
    if not os.path.exists(path):
        unreal.EditorDialog.show_message('SMR', '%s does not exist yet.' % path, unreal.AppMsgType.OK)
        return
    if sys.platform == 'win32':
        os.startfile(path)
    else:
        subprocess.Popen(['xdg-open', path])


def recompile_game_masters():
    if _busy():
        return
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    mats = [a.get_asset() for a in reg.get_assets_by_path('/Game/Environment/MaterialsMaster', True)
            if str(a.asset_class) == 'Material']
    with unreal.ScopedSlowTask(len(mats), 'Recompiling game master materials') as task:
        task.make_dialog(True)
        for m in mats:
            if task.should_cancel():
                break
            task.enter_progress_frame(1, m.get_name())
            unreal.MaterialEditingLibrary.recompile_material(m)
    unreal.log('SMR: recompiled %d game master materials' % len(mats))
