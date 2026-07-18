"""
patcher.py — standalone Leap Day mod patcher.

Asks for a mod file (.ldmod) and the user's own Leap Day package (.xapk/.apk),
applies the mod, and writes a signed, installable modded APK set. The mod file
contains only authored content, so this tool + a .ldmod carry no game data —
the user supplies their own copy of the game.

Runs as a windowed app (Tkinter, bundled with Python) by default. Pass
--mod/--xapk for a headless CLI build (used for testing the frozen binary).

Frozen with PyInstaller into a single executable; only a JRE is bundled
alongside for signing (no Python, no Android SDK, no JDK needed by the user).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

# make the sibling `core` package importable both from source and when frozen
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from core.project import Project
from core import modbuild, apkbuild


def run_build(xapk: str, mod: str, out_dir: str, install: bool, log):
    proj = Project.load(mod)
    log(f"Mod: {proj.name}")
    log(f"  custom levels: {len(proj.levels)}")
    if clone_package:
        pkg, label, _ = modbuild.clone_identity(proj)
        log(f"  install alongside original as: {label}  ({pkg})")
        if strip_store:
            log("  removing ad / billing / Play-store components")
    summary = modbuild.build(xapk, proj, out_dir, install=install,
                             clone_package=clone_package, strip_store=strip_store,
                             log=log)
    log("")
    log("✔ Done. Signed APKs in: " + summary.get("signed_dir", out_dir))
    for s in summary.get("signed", []):
        log("   • " + s)
    if summary.get("clone_package"):
        log("")
        log(f"This is a SEPARATE app ({summary['clone_package']}) — it installs")
        log("next to the original Leap Day instead of replacing it.")
    if not install:
        log("")
        log("To install on an emulator/phone (with adb):")
        log("   adb install-multiple -r <base>.apk <config.arm64_v8a>.apk")
    return summary


# ---------------- CLI ----------------
def cli(args):
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.xapk)), "leapday-mod-out")
    run_build(args.xapk, args.mod, out, args.install, print,
              clone_package=not args.no_clone, strip_store=not args.no_strip)


# ---------------- GUI ----------------
def gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title("Leap Day Mod Patcher")
    root.geometry("680x520")
    root.configure(bg="#1b1d23")
    FG, BG, AC = "#e8eaf0", "#1b1d23", "#56c271"

    state = {"mod": tk.StringVar(), "xapk": tk.StringVar(), "out": tk.StringVar(),
             "install": tk.BooleanVar(value=False),
             "clone": tk.BooleanVar(value=True), "strip": tk.BooleanVar(value=True)}

    def row(parent, label, var, kinds):
        f = tk.Frame(parent, bg=BG); f.pack(fill="x", padx=14, pady=6)
        tk.Label(f, text=label, bg=BG, fg=FG, width=14, anchor="w").pack(side="left")
        e = tk.Entry(f, textvariable=var, bg="#171a20", fg=FG, insertbackground=FG); e.pack(side="left", fill="x", expand=True, padx=6)
        def browse():
            p = filedialog.askopenfilename(filetypes=kinds) if kinds else filedialog.askdirectory()
            if p: var.set(p)
        tk.Button(f, text="Browse…", command=browse).pack(side="left")

    tk.Label(root, text="🐸  Leap Day Mod Patcher", bg=BG, fg=AC,
             font=("Helvetica", 16, "bold")).pack(pady=(14, 4))
    tk.Label(root, text="Apply a .ldmod to your own copy of the game → signed, installable APK.",
             bg=BG, fg="#9aa1b2").pack()

    row(root, "Mod file (.ldmod)", state["mod"], [("Leap Day mod", "*.ldmod")])
    row(root, "Game (.xapk/.apk)", state["xapk"], [("Leap Day package", "*.xapk *.apk")])
    row(root, "Output folder", state["out"], None)
    def _chk(text, var):
        tk.Checkbutton(root, text=text, variable=var, bg=BG, fg=FG,
                       selectcolor="#171a20", activebackground=BG,
                       activeforeground=FG).pack(anchor="w", padx=16)
    _chk("Install alongside the original game (rename to a separate app)", state["clone"])
    _chk("Remove ads / billing / Play-store connections", state["strip"])
    _chk("Install to connected device (needs adb on PATH)", state["install"])

    logbox = tk.Text(root, bg="#0d0f14", fg=FG, height=14, wrap="word",
                     font=("Menlo", 10), relief="flat")
    logbox.pack(fill="both", expand=True, padx=14, pady=10)

    def log(msg=""):
        logbox.insert("end", str(msg) + "\n"); logbox.see("end"); logbox.update_idletasks()

    def start():
        mod, xapk = state["mod"].get().strip(), state["xapk"].get().strip()
        if not mod or not xapk:
            log("⚠ Please choose both a .ldmod and your game .xapk/.apk."); return
        out = state["out"].get().strip() or os.path.join(os.path.dirname(xapk), "leapday-mod-out")
        state["out"].set(out)
        go_btn.config(state="disabled"); logbox.delete("1.0", "end")
        def work():
            try:
                run_build(xapk, mod, out, state["install"].get(), log,
                          clone_package=state["clone"].get(),
                          strip_store=state["strip"].get())
            except Exception as e:
                log(""); log("✖ ERROR: " + str(e))
            finally:
                root.after(0, lambda: go_btn.config(state="normal"))
        threading.Thread(target=work, daemon=True).start()

    go_btn = tk.Button(root, text="Build modded APK", command=start,
                       bg=AC, fg="#06210d", font=("Helvetica", 12, "bold"))
    go_btn.pack(pady=(0, 12))

    # check java availability up front
    import shutil
    if not (os.path.exists(apkbuild._java()) or shutil.which("java")):
        log("⚠ Java runtime not found. The bundled build includes one; if you")
        log("  see this from source, install a JRE or set LEAPDAY_JAVA.")
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Leap Day mod patcher")
    ap.add_argument("--mod"); ap.add_argument("--xapk")
    ap.add_argument("--out"); ap.add_argument("--install", action="store_true")
    ap.add_argument("--no-clone", action="store_true",
                    help="don't rename the package (build replaces the original "
                         "instead of installing alongside it)")
    ap.add_argument("--no-strip", action="store_true",
                    help="keep ad / billing / Play-store components")
    args = ap.parse_args()
    if args.mod and args.xapk:
        cli(args)
    else:
        gui()


if __name__ == "__main__":
    main()
