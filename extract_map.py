#!/usr/bin/env python3
r"""Command-line extractor (the app does the same thing with buttons).

Usage: python extract_map.py <paks or folder> [more ...] [-o OUTPUT_DIR] [--mod-name NAME]

Give it the mod's .pak files or the folder they're in. Output defaults to SMR Maps\<map name> next to this script (in Documents if this
folder is read-only).
--mod-name is the mod you will create in the SDK (default: the original's name + RPK); it must differ from the
original's, or players with both installed load the original instead.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smr import __version__, core  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paks', nargs='+', help='.pak files, or a folder containing them')
    ap.add_argument('-o', '--out', help='output folder (default: SMR Maps\\<map name> next to this script, or in Documents)')
    ap.add_argument('--mod-name', help='mod name to rebuild into (default: original name + RPK)')
    a = ap.parse_args()
    paks = core.find_paks(a.paks)
    if not paks:
        sys.exit('no .pak files found in: %s' % ', '.join(a.paks))
    print('Sandstorm Map Rebuilder v%s\npaks:\n  %s' % (__version__, '\n  '.join(paks)))
    s = core.extract(paks, a.out or core.default_output(paks), a.mod_name)
    print('\nwrote %s in %ds: %d levels, %d actors, %d meshes, %d textures' % (
        s['folder'], s['seconds'], s['levels'], s['actors'], s['meshes'], s['textures']))
    print('next: in the SDK, create a mod named %s, then SMR menu > %s > Rebuild map' % (s['target'], s['map']))


if __name__ == '__main__':
    main()
