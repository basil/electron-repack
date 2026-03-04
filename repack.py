#!/usr/bin/env python3
"""Electron app repack pipeline runner."""

import argparse
from dataclasses import dataclass
from enum import StrEnum
import docker
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import tomli_w

UBUNTU_IMAGE = "electron-repack-ubuntu:latest"
FEDORA_IMAGE = "electron-repack-fedora:latest"

IMAGE_DOCKERFILES: dict[str, str] = {
    UBUNTU_IMAGE: "Dockerfile.ubuntu",
    FEDORA_IMAGE: "Dockerfile.fedora",
}

STAGE_CONFIG: list[tuple[str, str]] = [
    ("Extract", UBUNTU_IMAGE),
    ("Normalize", FEDORA_IMAGE),
    ("Swap Electron", FEDORA_IMAGE),
    ("Patch", FEDORA_IMAGE),
    ("Build RPM", FEDORA_IMAGE),
]


def log(message: str) -> None:
    print(message, flush=True)


class Docker:
    def __init__(self, client: docker.client.DockerClient) -> None:
        self.client = client

    def build_image(self, tag: str, dockerfile: str) -> None:
        log(f"Building Docker image {tag}...")

        for chunk in self.client.api.build(
            path=".",
            dockerfile=dockerfile,
            tag=tag,
            rm=True,
            forcerm=True,
            decode=True,
            nocache=False,
            pull=False,
            cache_from=[tag],
        ):
            if "stream" in chunk:
                line = chunk["stream"].rstrip()
                if line:
                    print(line, flush=True)
            if "error" in chunk:
                raise RuntimeError(f"Docker build failed for {tag}: {chunk['error']}")

    def build_images(self, images: dict[str, str]) -> None:
        for tag, dockerfile in images.items():
            self.build_image(tag=tag, dockerfile=dockerfile)

    def run_stage(self, stage: "Stage") -> None:
        container = self.client.containers.create(
            image=stage.image,
            command=["python3", stage.script_name],
            volumes={
                str(stage.stage_dir.resolve()): {"bind": "/workspace", "mode": "z"}
            },
            working_dir="/workspace",
            environment={"HOME": "/tmp"},
            user=f"{os.getuid()}:{os.getgid()}" if os.name == "posix" else None,
            detach=True,
        )
        try:
            container.start()
            for chunk in container.logs(stream=True, follow=True):
                print(chunk.decode(), end="", flush=True)
            status = container.wait().get("StatusCode", 1)
            if status != 0:
                raise RuntimeError(
                    f"Stage failed (exit {status}):\n{container.logs().decode()}"
                )
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass


@dataclass
class Stage:
    app_id: str
    arch: str
    index: int
    name: str
    image: str
    workspace: Path
    prev: Stage | None = None

    @property
    def script_name(self) -> str:
        return f"stage{self.index}.py"

    @property
    def stage_dir(self) -> Path:
        return self.workspace / self.app_id / self.arch / f"stage{self.index}"

    def result_path(self) -> Path:
        return self.stage_dir / "result.toml"

    def is_complete(self) -> bool:
        return self.result_path().exists()

    def prepare(self, params: dict) -> None:
        if self.stage_dir.exists():
            shutil.rmtree(self.stage_dir)
        self.stage_dir.mkdir(parents=True)

        if self.prev is not None:
            prev_result = self.prev.result_path()
            if not prev_result.exists():
                raise RuntimeError(
                    f"Stage {self.prev.index} not complete for {self.app_id}/{self.arch}"
                )

            src = self.prev.stage_dir / "output"
            if not src.exists():
                raise RuntimeError(f"Missing directory: {src}")

            dst = self.stage_dir / "input"
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))

            params = tomllib.loads(prev_result.read_text(encoding="utf-8"))

        (self.stage_dir / "params.toml").write_text(
            tomli_w.dumps(params), encoding="utf-8"
        )
        shutil.copy2("config.toml", self.stage_dir / "config.toml")
        shutil.copy2(
            Path("stages") / self.script_name,
            self.stage_dir / self.script_name,
        )
        if self.name == "Patch":
            patches_dir = Path("patches") / self.app_id
            if patches_dir.exists():
                shutil.copytree(patches_dir, self.stage_dir / "patches")

    @staticmethod
    def clean_workspace(root: Path, app_id: str | None) -> None:
        if app_id:
            path = root / app_id
            if path.exists():
                log(f"Cleaning {path}")
                shutil.rmtree(path)
            return
        if root.exists():
            log("Cleaning entire workspace")
            shutil.rmtree(root)


def generate_stage_diff(stage_dir: Path) -> None:
    """Generate a diff between input/ and output/ in the stage workspace."""
    input_dir = stage_dir / "input"
    output_dir = stage_dir / "output"
    diff_path = stage_dir / "changes.diff"

    if not output_dir.exists():
        return

    if not input_dir.exists():
        # Stage 0 has no input directory
        diff_path.write_text("# No input directory (initial extraction stage)\n")
        return

    # Use git diff --no-index for rename/move detection
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--summary",
            "--patch",
            "--",
            "input",
            "output",
        ],
        cwd=stage_dir,
        capture_output=True,
    )
    # git diff --no-index exits 0 = identical, 1 = differences, 128 = error
    stdout = result.stdout.decode("utf-8", errors="replace")
    diff_path.write_text(stdout or "# No differences\n")
    log(f"Wrote diff: {diff_path} ({len(stdout)} bytes)")


class Pipeline:
    def __init__(self, docker_runtime: Docker, workspace: Path) -> None:
        self.docker_runtime = docker_runtime
        self.workspace = workspace

    @staticmethod
    def _build_stages(app_id: str, arch: str, workspace: Path) -> list[Stage]:
        stages = [
            Stage(
                app_id=app_id,
                arch=arch,
                index=index,
                name=name,
                image=image,
                workspace=workspace,
            )
            for index, (name, image) in enumerate(STAGE_CONFIG)
        ]
        for index, stage in enumerate(stages):
            if index > 0:
                stage.prev = stages[index - 1]
        return stages

    def run(self, app_id: str, arch: str, app_spec: dict) -> None:
        params: dict = {
            "build": {"app_id": app_id, "arch": arch},
            **app_spec,
        }

        for stage in self._build_stages(app_id, arch, self.workspace):
            log(f"--- Stage {stage.index}: {stage.name} ---")

            if stage.is_complete():
                log(f"Stage {stage.index} already complete")
                continue

            stage.prepare(params)
            self.docker_runtime.run_stage(stage)
            generate_stage_diff(stage.stage_dir)

            if not stage.is_complete():
                raise RuntimeError(
                    f"Stage {stage.index} failed: no result.toml produced"
                )


def load_apps(apps_dir: Path) -> dict[str, dict]:
    apps: dict[str, dict] = {}
    for path in sorted(apps_dir.glob("*.toml")):
        apps[path.stem] = tomllib.loads(path.read_text(encoding="utf-8"))
    return apps


class Arch(StrEnum):
    AMD64 = "amd64"
    ARM64 = "arm64"


def parse_architectures(s: str) -> list[Arch]:
    parts = [p.strip().lower() for p in s.split(",") if p.strip()]
    try:
        return [Arch(p) for p in parts]
    except ValueError as e:
        raise argparse.ArgumentTypeError("Unsupported arch") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repackage Electron apps for Fedora")
    parser.add_argument("--app", help="Single app id to process")
    parser.add_argument(
        "--arch",
        type=parse_architectures,
        default=[Arch.AMD64],
        help="Comma-separated architectures: amd64,arm64",
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--clean-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    os.chdir(Path(__file__).resolve().parent)
    args = parse_args()

    workspace = Path("workspace")
    if args.clean or args.clean_only:
        Stage.clean_workspace(workspace, args.app)
        if args.clean_only:
            log("Clean complete")
            return

    all_apps = load_apps(Path("apps"))
    if args.app:
        if args.app not in all_apps:
            raise RuntimeError(f"App not found: {args.app}")
        apps = {args.app: all_apps[args.app]}
    else:
        apps = all_apps

    log(f"Processing {len(apps)} app(s) for {len(args.arch)} architecture(s)...")

    log("Building Docker images...")
    docker_runtime = Docker(docker.from_env())
    docker_runtime.build_images(IMAGE_DOCKERFILES)

    pipeline = Pipeline(docker_runtime, workspace)
    for app_id, app_spec in apps.items():
        log(f"Processing app: {app_id}")
        for arch in args.arch:
            pipeline.run(app_id, arch, app_spec)


if __name__ == "__main__":
    main()
