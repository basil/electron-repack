#!/usr/bin/env python3
"""Stage 2: Swap Electron -- replace bundled Electron with a chosen version."""

import json
import os
from pathlib import Path
import requests
import shutil
import subprocess
import sys
import tempfile
import tomllib
import tomli_w
import zipfile


def log(msg: str) -> None:
    print(f"[STAGE2] {msg}", flush=True)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def download_file(url: str, dest: Path) -> None:
    log(f"Downloading: {url}")
    resp = requests.get(
        url, headers={"User-Agent": "electron-repack"}, allow_redirects=True
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def download_and_extract_electron(
    electron_version: str, arch: str, work_dir: Path
) -> Path:
    """Download and extract the official Electron zip. Returns the extract directory."""
    electron_arch = {"amd64": "x64", "arm64": "arm64"}.get(arch)
    if not electron_arch:
        error(f"Unknown architecture: {arch}")

    electron_zip = work_dir / "electron.zip"
    download_file(
        f"https://github.com/electron/electron/releases/download/v{electron_version}/electron-v{electron_version}-linux-{electron_arch}.zip",
        electron_zip,
    )

    extract_dir = work_dir / "extracted"
    extract_dir.mkdir()

    log("Extracting Electron zip")
    with zipfile.ZipFile(electron_zip, "r") as zf:
        zf.extractall(extract_dir)

    return extract_dir


def copy_app_resources(old_lib_dir: Path, new_lib_dir: Path) -> None:
    """Copy app-specific resources from the old app dir into the new electron dir."""
    old_resources = old_lib_dir / "resources"
    new_resources = new_lib_dir / "resources"

    if not old_resources.exists():
        error(f"No resources directory in {old_lib_dir}")

    # Remove electron's placeholder app
    default_asar = new_resources / "default_app.asar"
    if default_asar.exists():
        default_asar.unlink()
        log("Removed default_app.asar from new electron")

    for item in old_resources.iterdir():
        dst = new_resources / item.name
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
        log(f"Copied app resource: {item.name}")


def check_unsalvaged(
    app_lib_dir: Path, new_electron_dir: Path, output_dir: Path, app_id: str
) -> None:
    """Verify nothing important is being lost when we replace the old app dir.

    Every item in the old lib dir should be either:
      - 'resources/' (already copied to new electron)
      - present in the new electron dir (it's an electron file being replaced)
      - already placed elsewhere in the output tree by stage 1 (desktop, icons)
    """
    new_electron_names = {item.name for item in new_electron_dir.iterdir()}
    share_dir = output_dir / "usr" / "share"

    unsalvaged: list[str] = []
    for item in app_lib_dir.iterdir():
        name = item.name
        if name == "resources":
            continue
        if name in new_electron_names:
            continue
        # The app-named binary is the old electron, will be replaced by renamed new one
        if name in (app_id, app_id.replace("-", "")):
            continue
        # Check if stage 1 already placed this file type in /usr/share/
        if name.endswith(".desktop"):
            if list(share_dir.rglob(f"{app_id}.desktop")):
                continue
        if name.endswith((".png", ".svg", ".ico")):
            if list(share_dir.rglob(f"{app_id}.*")):
                continue
        unsalvaged.append(name)

    if unsalvaged:
        error(
            f"Unsalvaged files in {app_lib_dir} would be lost during electron swap: "
            + ", ".join(sorted(unsalvaged))
        )


def swap_electron(
    app_lib_dir: Path,
    electron_version: str,
    arch: str,
    app_id: str,
    output_dir: Path,
) -> None:
    log(f"Swapping Electron to version {electron_version}")

    with tempfile.TemporaryDirectory(prefix="electron-") as work_dir:
        new_electron_dir = download_and_extract_electron(
            electron_version, arch, Path(work_dir)
        )
        copy_app_resources(app_lib_dir, new_electron_dir)
        check_unsalvaged(app_lib_dir, new_electron_dir, output_dir, app_id)

        # Rename electron binary to match the app name
        electron_bin = new_electron_dir / "electron"
        if electron_bin.exists():
            electron_bin.rename(new_electron_dir / app_id)
            log(f"Renamed electron -> {app_id}")

        # Replace old app dir contents with new electron + app resources
        shutil.rmtree(app_lib_dir)
        shutil.copytree(new_electron_dir, app_lib_dir)

    # Set permissions
    app_bin = app_lib_dir / app_id
    if app_bin.exists():
        app_bin.chmod(0o755)
        log(f"Set {app_id} executable")

    chrome_sandbox = app_lib_dir / "chrome-sandbox"
    if chrome_sandbox.exists():
        chrome_sandbox.chmod(0o4755)
        log("Set chrome-sandbox permissions")


def validate_electron(app_lib_dir: Path, app_id: str) -> None:
    app_bin = app_lib_dir / app_id
    if not app_bin.exists():
        error(f"{app_id} binary missing after swap")
    if not os.access(app_bin, os.X_OK):
        error(f"{app_id} binary not executable")

    asar = next(app_lib_dir.rglob("*.asar"), None)
    if not asar:
        error("No .asar file found after swap")

    log("Validation passed")


def run_cmd(
    cmd: str,
    cwd: str | None = None,
    fatal: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str]:
    """Run a shell command. Returns (success, stdout, stderr)."""
    log(f"Running: {cmd}")
    run_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, cwd=cwd, env=run_env
    )
    if result.returncode != 0:
        if fatal:
            error(
                f"Command failed: {cmd}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
            )
        else:
            log(f"Command failed (non-fatal): {cmd}\nSTDERR: {result.stderr}")
    return result.returncode == 0, result.stdout, result.stderr


def rebuild_native_modules(
    native_modules: list[dict],
    electron_version: str,
    arch: str,
    app_lib_dir: Path,
) -> None:
    """Rebuild native modules against the new Electron's ABI.

    Uses a two-phase approach:
    1. Try @electron/rebuild --module-dir on the app's own node_modules (for modules with source)
    2. For modules not rebuilt in phase 1, try npm install + rebuild (for public npm packages)

    If both fail for a module, the original .node files are kept (N-API modules are ABI-stable).
    """
    log(f"Rebuilding {len(native_modules)} native module(s)")

    electron_arch = {"amd64": "x64", "arm64": "arm64"}.get(arch)
    if not electron_arch:
        error(f"Unknown architecture: {arch}")

    # Find the app's node_modules directory (inside app.asar.unpacked)
    unpacked_dirs = list(app_lib_dir.rglob("app.asar.unpacked"))
    if not unpacked_dirs:
        log("Warning: no app.asar.unpacked directory found, skipping rebuild")
        return

    modules_dir = (unpacked_dirs[0] / "node_modules").resolve()
    if not modules_dir.exists():
        log("Warning: no node_modules in app.asar.unpacked, skipping rebuild")
        return

    # Phase 1: Try @electron/rebuild on the in-place node_modules
    # This works for modules that have binding.gyp source included
    log("Phase 1: Attempting in-place rebuild with @electron/rebuild")

    # Install @electron/rebuild into a temp dir and run it pointing at the app's modules
    with tempfile.TemporaryDirectory(prefix="rebuild-") as work_dir:
        work = Path(work_dir)

        # Install @electron/rebuild tool
        ok, _, _ = run_cmd("npm install @electron/rebuild", cwd=str(work), fatal=False)
        if not ok:
            log("Warning: failed to install @electron/rebuild, trying phase 2")
        else:
            # @electron/rebuild expects a package.json in the module dir; create a stub if missing
            stub_pkg = modules_dir.parent / "package.json"
            created_stub = False
            if not stub_pkg.exists():
                stub_pkg.write_text(json.dumps({"name": "app", "version": "1.0.0"}))
                created_stub = True

            # Install build deps (like node-addon-api) into a separate dir
            # and make them available via NODE_PATH to avoid clobbering app's node_modules
            build_deps_dir = work / "build_deps" / "node_modules"
            build_deps_dir.mkdir(parents=True)
            run_cmd(
                f"npm install --prefix '{work / 'build_deps'}' node-addon-api",
                fatal=False,
            )

            # -m expects the directory *containing* node_modules, not node_modules itself
            ok, stdout, stderr = run_cmd(
                f"npx @electron/rebuild -v {electron_version} -a {electron_arch} -m '{modules_dir.parent}'",
                cwd=str(work),
                fatal=False,
                env={"NODE_PATH": str(build_deps_dir)},
            )

            if created_stub:
                stub_pkg.unlink(missing_ok=True)
            if ok:
                log("In-place rebuild succeeded")
                return
            else:
                log("In-place rebuild failed, trying phase 2")

    # Phase 2: For public npm packages, install fresh copies and rebuild
    log("Phase 2: Attempting npm install + rebuild for public packages")

    with tempfile.TemporaryDirectory(prefix="rebuild-") as work_dir:
        work = Path(work_dir)

        deps = {}
        for mod in native_modules:
            name = mod.get("name", "")
            version = mod.get("version", "")
            if name and version:
                deps[name] = version

        pkg = {"name": "rebuild-workspace", "version": "1.0.0", "dependencies": deps}
        (work / "package.json").write_text(json.dumps(pkg, indent=2))
        log(f"Created package.json with deps: {deps}")

        ok, _, _ = run_cmd("npm install --ignore-scripts", cwd=str(work), fatal=False)
        if not ok:
            log(
                "Warning: npm install failed — modules may be private/unlisted. Keeping original .node files."
            )
            return

        ok, _, _ = run_cmd(
            f"npx --yes @electron/rebuild -v {electron_version} -a {electron_arch}",
            cwd=str(work),
            fatal=False,
        )
        if not ok:
            log("Warning: @electron/rebuild failed. Keeping original .node files.")
            return

        # Copy rebuilt .node files back to their original paths
        for mod in native_modules:
            name = mod.get("name", "")
            files = mod.get("files", [])
            if not name:
                continue

            rebuilt_nodes = list((work / "node_modules" / name).rglob("*.node"))
            if not rebuilt_nodes:
                log(f"Warning: no rebuilt .node files found for {name}")
                continue

            for original_rel in files:
                original_abs = Path("output") / original_rel
                original_name = Path(original_rel).name
                match = next(
                    (r for r in rebuilt_nodes if r.name == original_name), None
                )
                if match:
                    shutil.copy2(match, original_abs)
                    log(f"Replaced {original_rel} with rebuilt version")
                else:
                    log(f"Warning: no rebuilt match for {original_name}")

    log("Native module rebuild complete")


def main() -> None:
    params: dict[str, object] = tomllib.loads(Path("params.toml").read_text())
    config: dict[str, object] = tomllib.loads(Path("config.toml").read_text())

    app_id: str = params["build"]["app_id"]
    arch: str = params["build"]["arch"]
    electron_version: str = config["electron"]["version"]

    log(f"Processing {app_id} for {arch}")

    shutil.copytree(
        Path("input"), Path("output"), ignore=shutil.ignore_patterns(".git")
    )

    app_lib_dir = Path("output") / "usr" / "lib64" / app_id

    swap_electron(app_lib_dir, electron_version, arch, app_id, Path("output"))
    validate_electron(app_lib_dir, app_id)

    native_modules = params.get("metadata", {}).get("native_modules", [])
    if native_modules:
        rebuild_native_modules(native_modules, electron_version, arch, app_lib_dir)

    # Write result (same as params)
    with open("result.toml", "wb") as f:
        tomli_w.dump(params, f)

    log("Stage 2 complete")


if __name__ == "__main__":
    main()
