#!/usr/bin/env python3
"""Stage 3: Patch -- apply file patches, ASAR patches, and electron flags."""

import magic
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import tomli_w


def log(msg: str) -> None:
    print(f"[STAGE3] {msg}", flush=True)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run_cmd(cmd: str) -> str:
    log(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        error(
            f"Command failed: {cmd}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    return result.stdout


def apply_file_patches(patches_dir: Path, output_dir: Path) -> None:
    for patch_file in sorted(
        p
        for p in patches_dir.iterdir()
        if p.suffix == ".patch" and not p.name.endswith(".asar.patch")
    ):
        log(f"Applying patch: {patch_file.name}")
        run_cmd(f"cd '{output_dir}' && patch -p1 < '{patch_file}'")


def apply_asar_patches(patches_dir: Path, app_lib_dir: Path) -> None:
    for asar_patch in sorted(
        p for p in patches_dir.iterdir() if p.name.endswith(".asar.patch")
    ):
        log(f"Applying ASAR patch: {asar_patch.name}")

        asar_file = next(app_lib_dir.rglob("*.asar"), None)
        if not asar_file:
            error("No .asar file found for ASAR patch")

        with tempfile.TemporaryDirectory(prefix="asar-extracted-") as tmp:
            asar_extract_dir = Path(tmp)

            log(f"Extracting ASAR: {asar_file}")
            run_cmd(f"asar extract '{asar_file}' '{asar_extract_dir}'")

            log("Applying patch to extracted ASAR")
            run_cmd(f"cd '{asar_extract_dir}' && patch -p1 < '{asar_patch.resolve()}'")

            log("Repacking ASAR")
            run_cmd(f"asar pack '{asar_extract_dir}' '{asar_file}'")

            log(f"ASAR patch applied: {asar_patch.name}")


def apply_patches(patches_dir: Path, output_dir: Path, app_lib_dir: Path) -> None:
    if not patches_dir.exists():
        return

    apply_file_patches(patches_dir, output_dir)
    apply_asar_patches(patches_dir, app_lib_dir)


def inject_flags_into_launcher(launcher_path: Path, flags_str: str) -> bool:
    """Try to inject flags into an existing shell launcher. Returns True on success."""
    if not launcher_path.exists():
        return False

    if magic.from_file(str(launcher_path), mime=True) != "text/x-shellscript":
        return False

    content = launcher_path.read_text()
    if "exec" not in content.lower() or "electron" not in content:
        log("Non-standard launcher, updating desktop file")
        return False

    log("Updating launcher script with flags")

    new_lines: list[str] = []

    for line in content.split("\n"):
        if "exec" in line.lower() and "electron" in line:
            if '"$@"' in line:
                line = line.replace('"$@"', f'{flags_str} "$@"')
            elif "$@" in line:
                line = line.replace("$@", f"{flags_str} $@")
            else:
                line = line.rstrip() + f" {flags_str}"

        new_lines.append(line)

    launcher_path.write_text("\n".join(new_lines))
    log("Launcher script updated")
    return True


def inject_flags_into_desktop(desktop_path: Path, flags_str: str) -> None:
    log("Updating desktop file with flags")

    new_lines: list[str] = []

    for line in desktop_path.read_text().split("\n"):
        if line.startswith("Exec="):
            parts = line.split(" ", 1)
            if len(parts) == 1:
                line = f"{parts[0]} {flags_str}"
            else:
                line = f"{parts[0]} {flags_str} {parts[1]}"

        new_lines.append(line)

    desktop_path.write_text("\n".join(new_lines))
    log("Desktop file updated")


def add_electron_flags(
    electron_flags: list[str], app_lib_dir: Path, output_dir: Path, app_id: str
) -> None:
    if not electron_flags:
        return

    log(f"Adding {len(electron_flags)} custom electron flags")
    flags_str = " ".join(electron_flags)
    log(f"Flags: {flags_str}")

    launcher_path = app_lib_dir / app_id
    desktop_path = output_dir / "usr" / "share" / "applications" / f"{app_id}.desktop"

    if not inject_flags_into_launcher(launcher_path, flags_str):
        inject_flags_into_desktop(desktop_path, flags_str)


def main() -> None:
    params: dict[str, object] = tomllib.loads(Path("params.toml").read_text())
    config: dict[str, object] = tomllib.loads(Path("config.toml").read_text())

    app_id: str = params["build"]["app_id"]
    electron_flags: list[str] = config.get("electron", {}).get("flags", [])

    log(f"Patching {app_id} for {params['build']['arch']}")

    shutil.copytree(
        Path("input"), Path("output"), ignore=shutil.ignore_patterns(".git")
    )

    output_dir = Path("output")
    app_lib_dir = output_dir / "usr" / "lib64" / app_id

    apply_patches(Path("patches"), output_dir, app_lib_dir)
    add_electron_flags(electron_flags, app_lib_dir, output_dir, app_id)

    log("Patching complete")

    # Write result (same as params)
    with open("result.toml", "wb") as f:
        tomli_w.dump(params, f)

    log("Stage 3 complete")


if __name__ == "__main__":
    main()
