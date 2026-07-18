"""
cli.py — headless build / playtest (no GUI).

Build a signed mod from a .ldmod:
    python3 studio/cli.py --xapk LEAPDAY.xapk --mod my.ldmod [--install]

Playtest an ordered chunk SEQUENCE once (stack -> flood today's run -> launch):
    python3 studio/cli.py --xapk LEAPDAY.xapk --demo "s9_jk_blob_01,s17_adam_cactustornado,s6_..."

Continuously overwrite the demo level as you edit the .ldmod's `demo` list:
    python3 studio/cli.py --xapk LEAPDAY.xapk --mod my.ldmod --watch
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.project import Project
from core import modbuild, playtest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "tiles", "catalog.json")


def _catalog():
    return json.load(open(CATALOG)) if os.path.exists(CATALOG) else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xapk", required=True)
    ap.add_argument("--mod", help="a .ldmod to build or watch")
    ap.add_argument("--demo", help="comma-separated chunk names to stack + playtest once")
    ap.add_argument("--out", default=os.path.join(ROOT, "build"))
    ap.add_argument("--scope", choices=["mid", "all", "easy"], default="mid",
                    help="which chunks to flood: mid gameplay (default, reliable), "
                         "all, or just the easy first-section")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--force-date", help="lock the daily level to this date (YYYY-MM-DD), "
                    "e.g. 2026-01-01 — the game then always loads that day's level")
    ap.add_argument("--watch", action="store_true",
                    help="with --mod: re-playtest its `demo` list whenever the file changes")
    ap.add_argument("--clone", action="store_true",
                    help="rename the package (per-mod id) so the build installs "
                         "ALONGSIDE the original game instead of replacing it")
    ap.add_argument("--no-strip", action="store_true",
                    help="with --clone: keep ad / billing / Play-store components")
    a = ap.parse_args()

    # --- continuous demo playtest -----------------------------------------
    if a.watch:
        if not a.mod:
            ap.error("--watch requires --mod")
        playtest.watch_demo(a.xapk, a.mod, _catalog(),
                            os.path.join(a.out, "playtest"), scope=a.scope)
        return

    # --- one-shot sequence playtest ---------------------------------------
    if a.demo:
        names = [n.strip() for n in a.demo.split(",") if n.strip()]
        project = Project.load(a.mod) if a.mod else Project()
        bundle = playtest._bundle_from_xapk(a.xapk, os.path.join(a.out, "playtest"))
        summary = playtest.playtest_sequence(
            a.xapk, names, project, bundle, _catalog(),
            os.path.join(a.out, "playtest"), scope=a.scope)
        print("playtest:", {k: summary[k] for k in ("flooded", "sequence") if k in summary})
        return

    # --- plain build (optionally just a force-date lock) ------------------
    if not a.mod and not a.force_date:
        ap.error("need --mod (build), --demo (playtest), --force-date, or --mod --watch")
    proj = Project.load(a.mod) if a.mod else Project(name="date-lock")
    if a.force_date:
        proj.force_date = a.force_date
    print(f"mod: {proj.name}  levels={len(proj.levels)} sequences={len(proj.sequences)}"
          + (f"  force_date={proj.force_date}" if proj.force_date else ""))
    summary = modbuild.build(a.xapk, proj, a.out, install=a.install,
                             clone_package=a.clone,
                             strip_store=(a.clone and not a.no_strip))
    print("summary:", summary)


if __name__ == "__main__":
    main()
