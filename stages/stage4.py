#!/usr/bin/env python3
"""Stage 4: Build RPM -- create an RPM package from the normalized app tree."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import tomli_w


def log(msg: str) -> None:
    print(f"[STAGE4] {msg}", flush=True)


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


def create_source_tarball(
    input_dir: Path, rpmbuild_dir: Path, app_id: str, version: str
) -> Path:
    log("Creating source tarball")
    tarball_path = rpmbuild_dir / "SOURCES" / f"{app_id}-{version}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="rpm-source-") as staging:
        source_dir = Path(staging) / f"{app_id}-{version}"
        shutil.copytree(input_dir, source_dir)

        run_cmd(
            f"tar --use-compress-program=pigz -cf '{tarball_path}' -C '{staging}' '{app_id}-{version}'"
        )
    log(f"Created tarball: {tarball_path}")
    return tarball_path


def generate_spec(
    rpmbuild_dir: Path,
    app_id: str,
    version: str,
    full_name: str,
    description: str,
    rpm_arch: str,
    has_bin: bool,
    has_hicolor: bool,
    has_pixmaps: bool,
) -> Path:
    spec_content = f"""%global debug_package %{{nil}}

Name:           {app_id}
Version:        {version}
Release:        1%{{?dist}}
Summary:        {full_name.replace('"', '\\"').replace("$", "\\$")}

License:        Proprietary
URL:            https://example.com
Source0:        {app_id}-{version}.tar.gz

BuildArch:      {rpm_arch}
BuildRequires:  coreutils
Requires:       ca-certificates
{'Requires:       hicolor-icon-theme' if has_hicolor else ''}

%description
{description.replace('"', '\\"').replace("$", "\\$")}

%prep
%setup -q

%build
# Nothing to build

%install
rm -rf $RPM_BUILD_ROOT
mkdir -p $RPM_BUILD_ROOT
cp -a * $RPM_BUILD_ROOT/

%files
{f'/usr/bin/{app_id}' if has_bin else ''}
/usr/lib64/{app_id}
/usr/share/applications/{app_id}.desktop
{'%dir /usr/share/icons' if has_hicolor or has_pixmaps else ''}
{'/usr/share/icons/hicolor' if has_hicolor else ''}
{'/usr/share/pixmaps' if has_pixmaps else ''}

%changelog
* %(date "+%a %b %d %Y") electron-repack <noreply@example.com> - {version}-1
- Automated build by electron-repack
"""

    spec_path = rpmbuild_dir / "SPECS" / f"{app_id}.spec"
    spec_path.write_text(spec_content)
    log(f"Created spec file: {spec_path}")
    return spec_path


def build_rpm(rpmbuild_dir: Path, spec_path: Path, rpm_arch: str) -> Path:
    log("Building RPM package")
    run_cmd(
        f"rpmbuild -bb --define '_topdir {rpmbuild_dir}' "
        f"--define '__gzip /usr/bin/pigz' '{spec_path}'"
    )

    rpms_dir = rpmbuild_dir / "RPMS" / rpm_arch
    rpm_file = next(rpms_dir.glob("*.rpm"), None) if rpms_dir.exists() else None

    if not rpm_file:
        error("RPM file not found after build")

    return rpm_file


def main() -> None:
    params: dict[str, object] = tomllib.loads(Path("params.toml").read_text())

    app_id: str = params["build"]["app_id"]
    arch: str = params["build"]["arch"]
    metadata: dict[str, object] = params.get("metadata", {})
    version: str = metadata.get("version", "1.0.0").replace("-", ".")
    full_name: str = metadata.get("full_name", app_id)
    description: str = metadata.get("description", f"{full_name} application")

    log(f"Building RPM for {app_id} {version} on {arch}")

    rpm_arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(arch)
    if not rpm_arch:
        error(f"Unknown architecture: {arch}")
        return  # unreachable

    input_dir = Path("input")
    if not input_dir.exists():
        error("Input directory not found")

    has_bin = (input_dir / f"usr/bin/{app_id}").exists()
    has_hicolor = (input_dir / "usr/share/icons/hicolor").exists()
    has_pixmaps = (input_dir / "usr/share/pixmaps").exists()

    # Create RPM build directories
    rpmbuild_dir = Path("/workspace/rpmbuild")
    for subdir in ["BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"]:
        (rpmbuild_dir / subdir).mkdir(parents=True, exist_ok=True)

    create_source_tarball(input_dir, rpmbuild_dir, app_id, version)
    spec_path = generate_spec(
        rpmbuild_dir,
        app_id,
        version,
        full_name,
        description,
        rpm_arch,
        has_bin,
        has_hicolor,
        has_pixmaps,
    )
    rpm_file = build_rpm(rpmbuild_dir, spec_path, rpm_arch)

    # Copy RPM to workspace
    shutil.copy2(rpm_file, rpm_file.name)
    log(f"RPM saved to: {rpm_file.name}")

    # Write result
    with open("result.toml", "wb") as f:
        tomli_w.dump(params, f)

    log("Stage 4 complete")


if __name__ == "__main__":
    main()
