#!/usr/bin/env python3
"""Install the bundled plugin sources into an AstrBot data directory."""
import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path, help='AstrBot data directory on the host')
    parser.add_argument('--overwrite', action='store_true', help='overlay existing plugin directories')
    args = parser.parse_args()
    target = args.data_dir.resolve() / 'plugins'
    sources = sorted(p for p in (ROOT/'plugins').iterdir() if p.is_dir())
    existing = [p.name for p in sources if (target/p.name).exists()]
    if existing and not args.overwrite:
        parser.error('plugin directories already exist: '+', '.join(existing)+'; use --overwrite to overlay')
    target.mkdir(parents=True, exist_ok=True)
    for source in sources:
        shutil.copytree(source, target/source.name, dirs_exist_ok=args.overwrite,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
        print('installed',source.name)

if __name__ == '__main__':main()
