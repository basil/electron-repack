#!/usr/bin/env python3
"""Stage 1: Normalize -- reorganize extracted files into a standard FHS layout."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import tomli_w


def log(msg: str) -> None:
    print(f"[STAGE1] {msg}", flush=True)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def find_file(root: Path, pattern: str) -> Path | None:
    return next(root.rglob(pattern), None)


def find_dir(root: Path, pattern: str) -> Path | None:
    for p in root.rglob(pattern):
        if p.is_dir():
            return p
    return None


def find_app_root(tree: Path) -> Path:
    """Locate the application root directory inside the extracted tree."""
    # Check for squashfs-root (AppImage)
    squashfs_root = tree / "squashfs-root"
    if squashfs_root.exists():
        log("Found squashfs-root (AppImage)")
        return squashfs_root

    # Check for /opt
    opt_dir = tree / "opt"
    if opt_dir.exists():
        subdirs = [d for d in opt_dir.iterdir() if d.is_dir()]
        if subdirs:
            log(f"Found app in /opt: {subdirs[0].name}")
            return subdirs[0]

    # Check for /usr/lib
    usr_lib = tree / "usr" / "lib"
    if usr_lib.exists():
        for subdir in usr_lib.iterdir():
            if subdir.is_dir() and (
                (subdir / "resources").exists() or next(subdir.rglob("*.asar"), None)
            ):
                log(f"Found app in /usr/lib: {subdir.name}")
                return subdir

    error("Could not find application root directory")
    return Path()  # unreachable


def move_app_files(app_root: Path, app_lib_dir: Path) -> None:
    """Move application files from their original location to /usr/lib64/{app_id}/."""
    log(f"Moving application files from {app_root} to {app_lib_dir}")
    app_lib_dir.mkdir(parents=True, exist_ok=True)
    for item in app_root.iterdir():
        shutil.move(str(item), str(app_lib_dir / item.name))


def normalize_desktop_file(output_dir: Path, app_id: str) -> None:
    desktop_file = find_file(output_dir, "*.desktop")
    if not desktop_file:
        error("No .desktop file found")

    log(f"Found desktop file: {desktop_file}")
    content = desktop_file.read_text()

    # Update paths in desktop file
    content = re.sub(r"/opt/[^/\s]+", f"/usr/lib64/{app_id}", content)
    content = re.sub(r"/usr/lib/[^/\s]+", f"/usr/lib64/{app_id}", content)

    # Replace AppImage-style Exec lines -- point to lib dir binary directly
    # (AppImage apps don't ship /usr/bin launchers)
    content = re.sub(
        r"^Exec=AppRun\s+",
        f"Exec=/usr/lib64/{app_id}/{app_id} ",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"^Exec=\./[^\s]+\s+",
        f"Exec=/usr/lib64/{app_id}/{app_id} ",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        rf"^Exec={re.escape(app_id)}\s+",
        f"Exec=/usr/lib64/{app_id}/{app_id} ",
        content,
        flags=re.MULTILINE,
    )

    # Strip AppImage sandbox workaround (system-installed chrome-sandbox handles it)
    content = re.sub(r" --no-sandbox", "", content)

    # Replace Icon path if it's absolute
    content = re.sub(r"Icon=/.*", f"Icon={app_id}", content)

    # Move desktop file to standard location
    desktop_dest = output_dir / "usr" / "share" / "applications" / f"{app_id}.desktop"
    desktop_dest.parent.mkdir(parents=True, exist_ok=True)
    if desktop_file != desktop_dest:
        desktop_file.unlink()
    desktop_dest.write_text(content)
    log(f"Wrote desktop file: {desktop_dest}")


def move_icons(output_dir: Path, app_root: Path, app_id: str) -> None:
    icon_dirs: list[Path] = []
    hicolor_dir = find_dir(output_dir, "hicolor")
    if hicolor_dir:
        icon_dirs.append(hicolor_dir)

    pixmaps_dir = output_dir / "usr" / "share" / "pixmaps"
    if pixmaps_dir.exists():
        icon_dirs.append(pixmaps_dir)

    # Also check in app root for icon
    app_icon = find_file(app_root, "*.png")
    if app_icon and app_icon not in icon_dirs:
        pixmap_dest = output_dir / "usr" / "share" / "pixmaps"
        pixmap_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(app_icon, pixmap_dest / f"{app_id}.png")
        log(f"Copied app icon to pixmaps: {app_id}.png")

    for icon_dir in icon_dirs:
        if "hicolor" in str(icon_dir):
            dest = output_dir / "usr" / "share" / "icons" / "hicolor"
            if icon_dir.resolve() != dest.resolve():
                dest.mkdir(parents=True, exist_ok=True)
                for item in icon_dir.iterdir():
                    dst = dest / item.name
                    if item.is_dir():
                        if dst.exists():
                            shutil.rmtree(dst)
                        shutil.copytree(item, dst)
                    else:
                        shutil.copy2(item, dst)
                log("Moved hicolor icons")
            else:
                log("Hicolor icons already in correct location")
        elif "pixmaps" in str(icon_dir):
            dest = output_dir / "usr" / "share" / "pixmaps"
            if icon_dir.resolve() != dest.resolve():
                dest.mkdir(parents=True, exist_ok=True)
                for item in icon_dir.iterdir():
                    shutil.copy2(item, dest / item.name)
                log("Moved pixmap icons")
            else:
                log("Pixmap icons already in correct location")


def cleanup_old_dirs(output_dir: Path) -> None:
    """Remove original source directories that are no longer needed."""
    for name in ["opt", "squashfs-root"]:
        path = output_dir / name
        if path.exists():
            log(f"Removing old directory: {name}")
            shutil.rmtree(path)

    # Remove /usr/lib if it's now empty or only had the app
    usr_lib = output_dir / "usr" / "lib"
    if usr_lib.exists():
        log("Removing usr/lib (contents moved to usr/lib64)")
        shutil.rmtree(usr_lib)

    # Remove Debian-specific directories that don't belong in the RPM
    for rel in ["usr/share/doc", "usr/share/man", "usr/share/lintian"]:
        path = output_dir / rel
        if path.exists():
            log(f"Removing Debian artifact: {rel}")
            shutil.rmtree(path)


def cleanup_app_lib_dir(app_lib_dir: Path, app_id: str) -> None:
    """Remove non-electron, non-resource junk from the app lib dir.

    After moving files from squashfs-root or /opt, the lib dir may contain
    AppImage artifacts, old license files, stale directories, etc. that
    don't belong in the final package.
    """
    # The app-named binary is the old electron binary; stage 2 will provide a new one
    # (or skip the swap entirely for native-module apps, in which case we keep it)
    junk = [".DirIcon", "AppRun", "LICENSE.electron.txt"]
    for name in junk:
        path = app_lib_dir / name
        if path.exists():
            log(f"Removing junk from lib dir: {name}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    # squashfs-root may have had its own usr/ tree inside it
    usr_in_lib = app_lib_dir / "usr"
    if usr_in_lib.exists():
        log("Removing stale usr/ from lib dir")
        shutil.rmtree(usr_in_lib)


def validate_output(output_dir: Path, app_lib_dir: Path, app_id: str) -> None:
    if not (output_dir / f"usr/share/applications/{app_id}.desktop").exists():
        error(
            f"Required file missing after normalization: usr/share/applications/{app_id}.desktop"
        )

    # Check for resources directory (may be nested)
    if not (app_lib_dir / "resources").exists() and not next(
        app_lib_dir.rglob("resources"), None
    ):
        error(f"No resources directory found in {app_lib_dir}")

    # Check for electron binary (could be named electron, app_id, etc.)
    for candidate_name in ["electron", app_id, app_id.replace("-", "")]:
        candidate = app_lib_dir / candidate_name
        if candidate.exists() and os.access(candidate, os.X_OK):
            log(f"Validated electron binary: {candidate_name}")
            break
    else:
        # Search recursively
        found = False
        for candidate in app_lib_dir.rglob("electron"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                log(
                    f"Validated electron binary (nested): {candidate.relative_to(app_lib_dir)}"
                )
                found = True
                break
        if not found:
            error(f"No electron binary found in {app_lib_dir}")

    # Check for asar
    if not find_file(app_lib_dir, "*.asar"):
        error("No .asar file found after normalization")

    log("Validation passed")


def main() -> None:
    params: dict[str, object] = tomllib.loads(Path("params.toml").read_text())

    app_id: str = params["build"]["app_id"]

    log(f"Normalizing {app_id} for {params['build']['arch']}")

    input_dir = Path("input")
    output_dir = Path("output")

    if not input_dir.exists():
        error("Input directory not found")

    # Copy input to output verbatim, then restructure in-place
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(input_dir, output_dir, ignore=shutil.ignore_patterns(".git"))

    # Find app root in output and restructure in-place
    app_root = find_app_root(output_dir)
    log(f"Application root: {app_root}")

    app_lib_dir = output_dir / "usr" / "lib64" / app_id
    move_app_files(app_root, app_lib_dir)
    normalize_desktop_file(output_dir, app_id)
    move_icons(output_dir, app_root, app_id)
    cleanup_old_dirs(output_dir)
    cleanup_app_lib_dir(app_lib_dir, app_id)
    validate_output(output_dir, app_lib_dir, app_id)

    # Write result (same as params)
    with open("result.toml", "wb") as f:
        tomli_w.dump(params, f)

    log("Stage 1 complete")


if __name__ == "__main__":
    main()
