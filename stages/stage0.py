#!/usr/bin/env python3
"""Stage 0: Extract -- download and extract the source package."""

from github import Github
import magic
import os
from pathlib import Path
import re
import requests
import subprocess
import sys
import tempfile
import tomllib
import tomli_w

BROWSER_UA: dict[str, str] = {
    "arm64": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "amd64": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def log(msg: str) -> None:
    print(f"[STAGE0] {msg}", flush=True)


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


def download_file(
    url: str, dest: Path, headers: dict[str, str] | None = None
) -> requests.Response:
    log(f"Downloading: {url}")
    resp = requests.get(url, headers=headers or {}, allow_redirects=True)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return resp


def extract_deb(deb_path: Path, output_dir: Path) -> None:
    log(f"Extracting DEB: {deb_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(f"dpkg -x '{deb_path}' '{output_dir}'")


def extract_appimage(appimage_path: Path, output_dir: Path) -> None:
    log("Extracting AppImage")

    data = appimage_path.read_bytes()

    # Find all occurrences of 'hsqs' magic bytes
    positions: list[int] = []
    idx = 0
    while True:
        idx = data.find(b"hsqs", idx)
        if idx == -1:
            break
        positions.append(idx)
        idx += 1

    if not positions:
        error("No squashfs magic found in AppImage")

    # Use the last occurrence (after ELF stub)
    offset = positions[-1]
    log(f"Found squashfs at offset {offset}")

    with tempfile.TemporaryDirectory() as tmp:
        squashfs_path = Path(tmp) / "appimage.squashfs"
        squashfs_path.write_bytes(data[offset:])

        output_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(f"cd '{output_dir}' && unsquashfs -f '{squashfs_path}'")


def get_deb_metadata(deb_path: Path) -> tuple[str, str, str]:
    log("Extracting DEB metadata")

    metadata: dict[str, str] = {}
    for line in run_cmd(f"dpkg-deb -f '{deb_path}'").split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip().lower()] = value.strip()

    version = metadata.get("version", "unknown")
    description = metadata.get("description", "")
    full_name = metadata.get("package", "").replace("-", " ").title()

    return version, full_name, description


def get_desktop_metadata(desktop_path: Path) -> tuple[str, str]:
    log(f"Reading desktop file: {desktop_path}")

    full_name = ""
    description = ""

    for line in desktop_path.read_text().splitlines():
        if line.startswith("Name="):
            full_name = line.split("=", 1)[1]
        elif line.startswith("Comment="):
            description = line.split("=", 1)[1]

    return full_name, description


def detect_version(text: str) -> str:
    """Try to extract version number from a string (URL or filename)."""
    for pattern in [r"v?(\d+\.\d+\.\d+)", r"-(\d+\.\d+\.\d+)"]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return "unknown"


def has_launcher_script(root: Path) -> bool:
    for item in root.rglob("*"):
        if item.is_file() and os.access(item, os.X_OK):
            if magic.from_file(str(item), mime=True) == "text/x-shellscript":
                return True
    return False


def find_parent_package_json(node_file: Path) -> Path | None:
    """Walk up from a .node file to find the nearest package.json."""
    current = node_file.parent
    while current != current.parent:
        pkg = current / "package.json"
        if pkg.exists():
            return pkg
        current = current.parent
    return None


def collect_native_modules(root: Path) -> list[dict]:
    """For each .node file, walk up to find nearest package.json for name+version."""
    import json

    modules: dict[str, dict] = {}  # keyed by package name to deduplicate
    for node_file in root.rglob("*.node"):
        pkg_json = find_parent_package_json(node_file)
        if pkg_json:
            data = json.loads(pkg_json.read_text())
            name = data.get("name", "")
            version = data.get("version", "")
            key = name or str(node_file)
            if key not in modules:
                modules[key] = {"name": name, "version": version, "files": []}
            modules[key]["files"].append(str(node_file.relative_to(root)))
            log(f"Found native module: {name}@{version} ({node_file.name})")
        else:
            log(f"Found native module without package.json: {node_file.name}")
    return list(modules.values())


def find_github_deb_url(repo: str, arch: str) -> str:
    gh = Github()
    release = gh.get_repo(repo).get_latest_release()

    for asset in release.get_assets():
        name = asset.name.lower()
        if name.endswith(".deb"):
            if arch == "amd64" and (
                "amd64" in name or "x86_64" in name or "x64" in name
            ):
                return asset.browser_download_url
            elif arch == "arm64" and ("arm64" in name or "aarch64" in name):
                return asset.browser_download_url

    error(f"No .deb found for architecture {arch}")
    return ""  # unreachable, error() exits


def download_from_apt(app_id: str, arch: str, source: dict[str, object]) -> Path:
    apt_repo_url = source["apt_repo_url"]
    apt_dist = source["apt_dist"]
    apt_component = source["apt_component"]

    # Download GPG key if provided
    keyring_option = ""
    if "gpg_key_url" in source:
        gpg_key_url = source["gpg_key_url"]
        log(f"Downloading GPG key: {gpg_key_url}")
        run_cmd("sudo mkdir -p /etc/apt/keyrings")
        run_cmd(
            f"curl -fsSL '{gpg_key_url}' | sudo gpg --dearmor -o /etc/apt/keyrings/electron-repack-{app_id}.gpg"
        )
        keyring_option = f"signed-by=/etc/apt/keyrings/electron-repack-{app_id}.gpg"

    # Add APT source
    if keyring_option:
        sources_list = f"deb [arch={arch} {keyring_option}] {apt_repo_url} {apt_dist} {apt_component}"
    else:
        sources_list = f"deb [arch={arch}] {apt_repo_url} {apt_dist} {apt_component}"

    log(f"Writing sources.list: {sources_list}")
    run_cmd(
        f"echo '{sources_list}' | sudo tee /etc/apt/sources.list.d/electron-repack-{app_id}.list > /dev/null"
    )

    # Update and download package
    run_cmd("sudo apt-get update")
    run_cmd(f"apt-get download {app_id}")

    deb_files = list(Path(".").glob("*.deb"))
    if not deb_files:
        error("No .deb file downloaded")

    return deb_files[0]


def find_arch_url(urls: list[str], arch: str) -> str | None:
    for url in urls:
        if arch == "amd64" and ("amd64" in url or "x86_64" in url or "x64" in url):
            return url
        elif arch == "arm64" and ("arm64" in url or "aarch64" in url):
            return url
    return None


def extract_source_deb(
    app_id: str, arch: str, source: dict[str, object], output_dir: Path
) -> tuple[str, str, str]:
    """Download and extract a DEB source. Returns (version, full_name, description)."""
    if "github_repo" in source:
        repo: str = source["github_repo"]
        log(f"Fetching latest release from GitHub: {repo}")
        deb_path = Path("package.deb")
        download_file(find_github_deb_url(repo, arch), deb_path)

    elif "apt_repo_url" in source:
        deb_path = download_from_apt(app_id, arch, source)

    elif "urls" in source:
        urls: list[str] = source["urls"]
        deb_url = find_arch_url(urls, arch)
        if not deb_url:
            error(f"No URL found for architecture {arch}")
            return "", "", ""  # unreachable

        headers: dict[str, str] = {}
        if source.get("user_agent", False):
            headers["User-Agent"] = BROWSER_UA.get(arch, BROWSER_UA["amd64"])

        deb_path = Path("package.deb")
        download_file(deb_url, deb_path, headers=headers)

    else:
        error("DEB source has no github_repo, apt_repo_url, or urls")
        return "", "", ""  # unreachable

    version, full_name, description = get_deb_metadata(deb_path)
    extract_deb(deb_path, output_dir)
    return version, full_name, description


def extract_source_appimage(
    app_id: str, arch: str, source: dict[str, object], output_dir: Path
) -> tuple[str, str, str]:
    """Download and extract an AppImage source. Returns (version, full_name, description)."""
    urls: list[str] = source["urls"]
    if not urls:
        error("No AppImage URL configured in source.urls")

    headers: dict[str, str] = {}
    if source.get("user_agent", False):
        headers["User-Agent"] = BROWSER_UA.get(arch, BROWSER_UA["amd64"])

    appimage_path = Path("app.appimage")

    response: requests.Response | None = None
    downloaded_url: str | None = None
    for candidate_url in [
        url
        for url in urls
        if (arch == "amd64" and ("amd64" in url or "x86_64" in url or "x64" in url))
        or (arch == "arm64" and ("arm64" in url or "aarch64" in url))
    ] or urls:
        try:
            response = download_file(candidate_url, appimage_path, headers)
            downloaded_url = candidate_url
            break
        except Exception as e:
            log(f"Failed to download from {candidate_url}: {e}")

    if not response or not downloaded_url:
        error("Failed to download AppImage from any configured source.urls entry")
        return "", "", ""  # unreachable

    # Try to get version from content-disposition header
    content_disp = response.headers.get("Content-Disposition", "")
    if "filename=" in content_disp:
        filename = content_disp.split("filename=")[1].strip('"')
        version = detect_version(filename)
    else:
        version = detect_version(downloaded_url)

    # Make executable and extract
    appimage_path.chmod(0o755)
    extract_appimage(appimage_path, output_dir)

    # Get metadata from desktop file
    desktop_file = next(output_dir.rglob("*.desktop"), None)
    if desktop_file:
        full_name, description = get_desktop_metadata(desktop_file)
    else:
        full_name = app_id.replace("-", " ").title()
        description = f"{full_name} application"

    return version, full_name, description


def validate_extracted(app_id: str, output_dir: Path) -> None:
    electron_binary = next(output_dir.rglob("electron"), None)
    if not electron_binary:
        electron_binary = next(output_dir.rglob(app_id), None)
    if not electron_binary:
        electron_binary = next(output_dir.rglob(app_id.replace("-", "")), None)

    if not next(output_dir.rglob("*.asar"), None):
        error("No .asar file found in extracted package")
    if not next(output_dir.rglob("*.desktop"), None):
        error("No .desktop file found in extracted package")
    if not next(output_dir.rglob("*.png"), None):
        error("No icon file found in extracted package")
    if not electron_binary:
        error(
            f"No electron binary found in extracted package (looked for: electron, {app_id})"
        )

    log("Validation passed - all required files present")


def main() -> None:
    params: dict[str, object] = tomllib.loads(Path("params.toml").read_text())

    app_id: str = params["build"]["app_id"]
    arch: str = params["build"]["arch"]
    source: dict[str, object] = params.get("source", {})
    source_type: str = source.get("type", "")

    log(f"Extracting {app_id} for {arch}")

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    if source_type == "deb":
        version, full_name, description = extract_source_deb(
            app_id, arch, source, output_dir
        )
    elif source_type == "appimage":
        version, full_name, description = extract_source_appimage(
            app_id, arch, source, output_dir
        )
    else:
        error(f"Unknown source type: {source_type}")
        return  # unreachable

    # Detect launcher and native modules
    launcher_present = has_launcher_script(output_dir)
    native_modules = collect_native_modules(output_dir)
    native = len(native_modules) > 0

    log(
        f"Detected: version={version}, launcher={launcher_present}, native_modules={native}"
    )

    validate_extracted(app_id, output_dir)

    # Write result
    result = {
        "build": {"app_id": app_id, "arch": arch},
        "metadata": {
            "version": version,
            "full_name": full_name,
            "description": description,
            "has_launcher": launcher_present,
            "has_native_modules": native,
            "native_modules": native_modules,
        },
    }

    with open("result.toml", "wb") as f:
        tomli_w.dump(result, f)

    log("Stage 0 complete")


if __name__ == "__main__":
    main()
