"""Checks run on a fresh extract, before anything is rebuilt: the map quirks that otherwise only show up in the
editor (or in game) after a full rebuild. Each check reads layout.json only.

Result (layout.json 'preflight'):
  {'tier': 'easy' | 'landscape' | 'advanced', 'checks': [{'code', 'level', 'msg'}, ...]}
level is 'warn' (needs a decision or a test before rebuilding) or 'info' (expected extra work).
Each check has a short code that names it.
"""
from .core import travel_maps

TIERS = {
    'easy': 'Easy: no landscape and no scripted Blueprints. The rebuild plus the TODO list covers it.',
    'landscape': 'Landscape: one guided step in the editor (the landscape import), listed in the TODO.',
    'advanced': 'Advanced: the map has scripted Blueprints or level logic, which need editor work by hand.',
}


def _short(p):
    return (p or '').split('/')[-1].split('.')[0]


def _lighting(level):
    """World Settings lighting scenarios: {name: level}."""
    v = (level.get('world_settings') or {}).get('LightingScenarios')
    return v if isinstance(v, dict) else {}


def _streamed_from(levels, root):
    """Short names of every level the map streams in, directly or through its sublevels, plus its lighting
    scenario levels."""
    out, todo = set(), [root]
    while todo:
        pkg = todo.pop()
        d = levels.get(pkg) or {}
        for s in d.get('streaming') or []:
            lp = s.get('level') if isinstance(s, dict) else s
            if lp and _short(lp) not in out:
                out.add(_short(lp))
                todo.append(lp.split('.')[0])
        for lv in _lighting(d).values():
            if isinstance(lv, str):
                out.add(_short(lv))
    return out


def run(L):
    checks = []

    def add(code, level, msg):
        checks.append({'code': code, 'level': level, 'msg': msg})

    levels = L.get('levels') or {}
    scenarios = L.get('scenarios') or {}
    bps = L.get('blueprints') or {}
    by_short = {_short(k): k for k in levels}
    old = (L.get('mods') or [None])[0]

    if not scenarios:
        add('no_scenarios', 'warn', 'The mod has no scenarios, so the rebuilt map can only be opened in the editor, '
                                    'not played. Make one after the rebuild, or check that every pak was given.')
    if old and L.get('target_mod') == old:
        add('same_name', 'warn', 'The rebuild uses the original mod\'s name (%s). Players who have the original '
                                 'subscribed will load the original instead.' % old)
    for w in L.get('pak_warnings') or []:
        add('pak_missing_assets', 'warn', w)
    if L.get('mesh_errors'):
        add('mesh_errors', 'warn', '%d mesh(es) could not be read and will be missing: %s. Export them with FModel, '
                                   'or swap the actors that use them.' % (
                                       len(L['mesh_errors']), ', '.join(_short(k) for k in sorted(L['mesh_errors']))))

    # scenarios: sublevels the map never streams, sublevels that don't exist, lighting that isn't set up
    for tm in travel_maps(L):
        streamed = _streamed_from(levels, tm)
        lighting = _lighting(levels.get(tm) or {})
        unstreamed, absent, bad_light = {}, {}, {}
        for pkg, s in sorted(scenarios.items()):
            if not isinstance(s, dict) or (s.get('Level') or '').split('.')[0] != tm:
                continue
            for sub in s.get('Sublevels') or []:
                name = sub.get('Name') if isinstance(sub, dict) else sub
                if not name or name == _short(tm):
                    continue
                if name not in by_short:
                    absent.setdefault(name, []).append(_short(pkg))
                elif name not in streamed:
                    unstreamed.setdefault(name, []).append(_short(pkg))
            dl = s.get('DefaultLightingScenario')
            if dl and lighting and dl not in lighting:
                bad_light.setdefault(dl, []).append(_short(pkg))
        for name, scs in sorted(absent.items()):
            add('sublevel_absent', 'warn', '%s: %d scenario(s) load the sublevel %s, which is in none of the paks '
                                           '(%s). Those modes will be missing whatever it held.' % (
                                               _short(tm), len(scs), name, ', '.join(scs)))
        if unstreamed:
            names = sorted(unstreamed)
            scs = sorted({x for v in unstreamed.values() for x in v})
            add('sublevel_unstreamed', 'warn',
                '%s does not stream %s, but %d scenario(s) load %s (%s). Play one of those modes on the original '
                'first: if it looks right there, nothing is needed; if that part of the map is missing, the original '
                'was already broken and adding %s to %s in the Levels panel fixes the rebuild.' % (
                    _short(tm), ', '.join(names), len(scs), 'it' if len(names) == 1 else 'them', ', '.join(scs),
                    'it' if len(names) == 1 else 'them', _short(tm)))
        for dl, scs in sorted(bad_light.items()):
            add('lighting_unknown', 'warn', '%s: %d scenario(s) ask for the lighting scenario %s, which the map\'s '
                                            'World Settings do not define (%s; defined: %s).' % (
                                                _short(tm), len(scs), dl, ', '.join(scs), ', '.join(sorted(lighting))))

    # game modes that are the author's own Blueprints
    for pkg, s in sorted(scenarios.items()):
        gm = s.get('GameMode') if isinstance(s, dict) else None
        if not isinstance(gm, str) or not gm.startswith('/') or gm.startswith('/Script/'):
            continue
        info = bps.get(gm.split('.')[0])
        info = info if isinstance(info, dict) else {}
        if info.get('has_logic'):
            add('custom_gamemode_logic', 'warn', 'Scenario %s uses the author\'s own game mode %s, which has '
                                                 'Blueprint logic and cannot be rebuilt. Expect that mode to need '
                                                 'the graft, or a stock game mode instead.' % (_short(pkg), _short(gm)))

    # editor work that is expected, but decides how hard the map is
    logic = sorted(_short(k) for k, v in bps.items() if isinstance(v, dict) and v.get('has_logic'))
    persistent_scripts = sorted(_short(k) for k in travel_maps(L) if (levels.get(k) or {}).get('level_script'))
    if logic:
        add('scripted_blueprints', 'info', '%d scripted Blueprint(s) (%s): the graft ships the originals, and each '
                                           'placeholder needs fixing by hand in the editor.' % (len(logic), ', '.join(logic)))
    if persistent_scripts:
        add('level_script', 'info', 'The level Blueprint of %s has logic, which has to be rebuilt by hand.' %
            ', '.join(persistent_scripts))
    lss = L.get('landscapes') or []
    if lss:
        add('landscape', 'info', '%d landscape(s) to import by hand (%s).' % (
            len(lss), ', '.join(_short(x['level']) for x in lss)))

    tier = 'advanced' if logic or persistent_scripts else 'landscape' if lss else 'easy'
    return {'tier': tier, 'checks': checks}


def lines(pf):
    """Plain-text lines for the app's log and the command line."""
    out = ['Difficulty: ' + TIERS[pf['tier']]]
    for c in pf['checks']:
        out.append(('  ! ' if c['level'] == 'warn' else '  - ') + c['msg'])
    return out


def report_section(pf):
    """The REPORT.md section, shown first."""
    out = ['## Before you start', '', '**%s**' % TIERS[pf['tier']], '']
    warns = [c for c in pf['checks'] if c['level'] == 'warn']
    infos = [c for c in pf['checks'] if c['level'] != 'warn']
    if warns:
        out += ['Check these before rebuilding:', '']
        out += ['- **Warning:** ' + c['msg'] for c in warns]
        out.append('')
    if infos:
        out += ['- ' + c['msg'] for c in infos]
        out.append('')
    if not warns:
        out += ['No problems found.', '']
    return out
