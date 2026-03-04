# Electron App Repackager for Fedora

A pipeline-based system for converting upstream Linux Electron app distributions (`.deb`, AppImage) into Fedora RPM packages with a uniform installation layout and up-to-date Electron runtime.

## Problem Statement

Electron applications distributed for Linux often come in inconsistent formats:

- Debian packages (`.deb`) from various sources (direct URLs, APT repositories, GitHub releases)
- AppImage executables
- Outdated Electron runtimes bundled with the app
- Non-standard installation layouts (`/opt`, vendor-specific paths)

This tool standardizes these distributions into:

- Consistent RPM packages for Fedora
- Latest Electron runtime (when possible)
- Standard FHS-compliant layout:
  - `/usr/bin/{app}` - launcher
  - `/usr/lib64/{app}` - application files
  - `/usr/share/applications/{app}.desktop` - desktop entry
  - `/usr/share/icons/` - application icons
- Custom Electron flags for Wayland support and other optimizations

## Architecture

### Pipeline Overview

The system processes applications through a 5-stage pipeline. Each stage has:

- **Clear inputs/outputs**: Defined via TOML files (`params.toml` → `result.toml`)
- **Isolated workspace**: Files in `workspace/{app}/{arch}/stage{N}/`
- **Fail-fast behavior**: Errors stop processing immediately for debugging
- **Idempotent execution**: Completed stages are skipped on re-run
- **Docker isolation**: Stages run in Ubuntu or Fedora containers

### Pipeline Stages

```
┌──────────────┐
│  Stage 0     │  Extract upstream package (Ubuntu container)
│  Extract     │  - Download from source (DEB/AppImage/GitHub)
└──────┬───────┘  - Extract package contents
       │          - Detect metadata (version, name, description)
       │          - Check for launcher script and native modules
       ▼
┌──────────────┐
│  Stage 1     │  Normalize directory structure (Fedora container)
│  Normalize   │  - Move from /opt or squashfs-root to /usr layout
└──────┬───────┘  - Update paths in .desktop and launcher files
       │          - Organize icons, binaries, resources
       ▼
┌──────────────┐
│  Stage 2     │  Swap Electron runtime (Fedora container)
│  Swap        │  - Download latest Electron from GitHub
│  Electron    │  - Replace old Electron binaries
└──────┬───────┘  - Preserve app-specific resources
                  - Rebuild native modules against new ABI (if present)
       │
       ▼
┌──────────────┐
│  Stage 3     │  Apply patches and flags (Fedora container)
│  Patch       │  - Apply file patches (if any)
└──────┬───────┘  - Apply ASAR patches (extract, patch, repack)
       │          - Add custom Electron flags for Wayland support
       ▼
┌──────────────┐
│  Stage 4     │  Build RPM (Fedora container)
│  Build RPM   │  - Generate RPM spec file
└──────────────┘  - Run rpmbuild
                  - Validate package contents
```

### Stage Details

#### Stage 0: Extract

**Container**: Ubuntu (apt, dpkg, squashfs-tools)

**Inputs**:

- `apps/{app}.toml` - Application source configuration
- `config.toml` - Global configuration

**Source Types**:

1. **GitHub DEB**: Fetches latest release from GitHub, finds architecture-specific `.deb` asset
2. **APT Repository**: Adds apt source, downloads package (no installation)
3. **Direct DEB URLs**: Downloads from specified URL list
4. **AppImage**: Downloads with optional user-agent, extracts squashfs (uses last `hsqs` marker)

**Outputs**:

- `output/` - Unpacked application files
- `result.toml` - Detected metadata:
  - `version` - Application version
  - `full_name` - Display name (e.g., "Signal Desktop")
  - `description` - Application description
  - `has_launcher` - Whether app has a launcher shell script
  - `has_native_modules` - Whether app has native Node.js modules (`.node` files)
  - `native_modules` - List of native modules with name, version, and file paths

**Validation**:

- `.asar` file exists
- `.desktop` file exists
- Icons present
- Electron binary present

#### Stage 1: Normalize

**Container**: Fedora (patch, rpm-build, nodejs)

**Inputs**:

- `input/` (copy of Stage 0 `output/`)
- `params.toml` (Stage 0 result)

**Operations**:

1. Locate application root (searches `/opt`, `/usr/lib`, `squashfs-root`)
2. Copy application files to `/usr/lib64/{app}/`
3. Extract and update `.desktop` file:
   - Replace `/opt/*` paths with `/usr/lib64/{app}`
   - Standardize icon reference to `{app}`
   - Place in `/usr/share/applications/`
4. Copy icons to standard locations:
   - `hicolor/` tree → `/usr/share/icons/hicolor/`
   - Pixmaps → `/usr/share/pixmaps/`
5. Handle launcher:
   - If launcher script exists: Update paths in `/usr/bin/{app}`
   - If no launcher: Desktop file `Exec=` points directly to `/usr/lib64/{app}/{app}`

**Outputs**:

- `output/` - FHS-compliant directory tree

**Validation**:

- `/usr/lib64/{app}/` contains an executable electron binary
- `/usr/lib64/{app}/resources` exists
- `/usr/share/applications/{app}.desktop` exists
- `.asar` file present

#### Stage 2: Swap Electron

**Container**: Fedora

**Inputs**:

- `input/` (copy of Stage 1 `output/`)
- `electron_version` from `config.toml`
- `native_modules` manifest from Stage 0

**Operations**:

1. Download Electron zip from GitHub (`electron-v{version}-linux-{arch}.zip`)
2. Remove old Electron binaries: `electron`, `chrome-sandbox`, libs
3. Install new Electron binaries
4. Preserve app's `resources/` (skip `default_app.asar` from Electron)
5. Set executable permissions (`electron` → 755, `chrome-sandbox` → 4755)
6. If native modules present: rebuild against new Electron ABI using `@electron/rebuild`

**Outputs**:

- `output/` - Normalized tree with new Electron runtime

**Validation**:

- Electron binary exists and is executable

#### Stage 3: Patch

**Container**: Fedora (asar, patch)

**Inputs**:

- `input/` (copy of Stage 2 `output/`)
- `patches/{app}/` directory (optional)
- `electron.flags` from `config.toml`

**Operations**:

1. **Apply file patches** (`.patch` files):
   - Run `patch -p1 < {patch}` from patched root
2. **Apply ASAR patches** (`.asar.patch` files):
   - Extract ASAR with `asar extract`
   - Apply patch with `patch -p1`
   - Repack with `asar pack`
3. **Add custom Electron flags**:
   - If standard launcher script: Insert flags before `"$@"`
   - If non-standard/no launcher: Update `Exec=` line in `.desktop` file
   - Flags enable Wayland, Qt, IME support, etc.

**Outputs**:

- `output/` - Tree with patches and flags applied

**ASAR Iteration**:
To develop ASAR patches:

1. Run pipeline up to Stage 3
2. When Stage 3 fails, manually extract ASAR:

   ```bash
   cd workspace/{app}/{arch}/stage3/output/usr/lib64/{app}/resources
   asar extract app.asar /tmp/app-unpacked
   # Edit files in /tmp/app-unpacked
   diff -Naur original/ modified/ > patches/{app}/my-fix.asar.patch
   ```

3. Re-run `repack.py` - it will apply the patch

#### Stage 4: Build RPM

**Container**: Fedora (rpmbuild)

**Inputs**:

- `input/` (copy of Stage 3 `output/`)
- Metadata: `version`, `full_name`, `description`

**Operations**:

1. Create RPM build directories
2. Package `input/` tree as source tarball
3. Generate RPM spec file:
   - Name, version, summary from metadata
   - Architecture-specific (x86_64 or aarch64)
   - Requires `ca-certificates`, optionally `hicolor-icon-theme`
   - File list: `/usr/bin/{app}`, `/usr/lib64/{app}`, desktop/icon files
4. Run `rpmbuild -bb {spec}`
5. Validate RPM contents with `rpm -qlp`

**Outputs**:

- `{app}-{version}-1.{arch}.rpm`
- `result.toml` with RPM filename

**Validation**:

- Required paths exist in RPM
- All staged files packaged

## Usage

### Prerequisites

- Python 3.11+ (for `tomllib`)
- Docker installed and accessible
- Python packages: `pip install docker tomli-w`

### Basic Usage

```bash
# Build all apps for amd64 (default)
./repack.py

# Build specific app
./repack.py --app signal-desktop

# Build for specific architecture
./repack.py --arch arm64

# Build for both architectures
./repack.py --arch amd64,arm64

# Clean workspace before building
./repack.py --clean

# Clean workspace only (don't build)
./repack.py --clean-only

# Clean and rebuild specific app
./repack.py --clean --app claude-desktop
```

### Configuration Files

**`config.toml`** - Global configuration:

```toml
[electron]
version = "40.7.0"
flags = [
    "--enable-features=AllowQt,GlobalShortcutsPortal,WaylandWindowDecorations",
    "--enable-wayland-ime",
    "--ozone-platform=wayland",
    "--wayland-text-input-version=3",
]
```

**`apps/{app}.toml`** - Per-app configuration:

*GitHub DEB source:*

```toml
[source]
type = "deb"
github_repo = "owner/repo"
```

*APT repository source:*

```toml
[source]
type = "deb"
apt_repo_url = "https://packages.example.com/debian"
apt_dist = "stable"
apt_component = "main"
gpg_key_url = "https://packages.example.com/key.asc"
```

*Direct DEB URLs:*

```toml
[source]
type = "deb"
urls = [
    "https://example.com/app-amd64.deb",
    "https://example.com/app-arm64.deb"
]
```

*AppImage source:*

```toml
[source]
type = "appimage"
urls = ["https://example.com/download"]
user_agent = true  # Use arch-specific user agent
```

### Workspace Structure

```
workspace/
└── {app}/
    └── {arch}/
        ├── stage0/
        │   ├── params.toml       # Input parameters
        │   ├── result.toml       # Output metadata
        │   ├── output/           # Extracted package
        │   └── stage0.py         # Stage script
        ├── stage1/
        │   ├── params.toml
        │   ├── result.toml
        │   ├── input/            # Copy from stage0 output/
        │   ├── output/           # Normalized tree
        │   └── stage1.py
        ├── stage2/
        │   ├── params.toml
        │   ├── result.toml
        │   ├── input/            # Copy from stage1 output/
        │   ├── output/           # Copy of input/ with Electron swapped
        │   └── stage2.py
        ├── stage3/
        │   ├── params.toml
        │   ├── result.toml
        │   ├── input/            # Copy from stage2 output/
        │   ├── output/           # Copy of input/ with patches applied
        │   ├── patches/          # Copy from patches/{app}/
        │   └── stage3.py
        └── stage4/
            ├── params.toml
            ├── result.toml
            ├── input/            # Copy from stage3 output/
            ├── rpmbuild/         # RPM build tree
            ├── {app}-{version}.rpm  # Final RPM
            └── stage4.py
```

### Patching Applications

Create patches in `patches/{app}/`:

**Regular file patch** (`fix-path.patch`):

```bash
cd workspace/{app}/{arch}/stage3/output
# Make changes
diff -Naur original/ modified/ > /path/to/patches/{app}/fix-path.patch
```

**ASAR patch** (`fix-config.asar.patch`):

```bash
cd workspace/{app}/{arch}/stage3/output/usr/lib64/{app}/resources
asar extract app.asar /tmp/app-unpacked
# Edit files in /tmp/app-unpacked
cd /tmp
diff -Naur app-unpacked-original/ app-unpacked/ > /path/to/patches/{app}/fix-config.asar.patch
```

Re-run `./repack.py --app {app}` to apply patches.

### Debugging

1. **Stage fails**: Workspace directory preserved for inspection
2. **Inspect stage output**:

   ```bash
   cd workspace/{app}/{arch}/stage{N}/
   cat params.toml  # Stage inputs
   cat result.toml  # Stage outputs (if completed)
   ls -la input/ output/  # File trees
   ```

3. **Re-run after fixing**: Failed stage directory auto-deleted, re-runs from that stage
4. **Docker issues**: Images built automatically, rebuild with:

   ```bash
   docker rmi electron-repack-ubuntu:latest electron-repack-fedora:latest
   ./repack.py  # Rebuilds images
   ```

### Idempotent Execution

- **Completed stages skipped**: If `result.toml` exists, stage is not re-run
- **Failed stages cleaned**: If stage directory exists without `result.toml`, it's deleted before re-run
- **Full rebuild**: Use `--clean` to start fresh

## Design Decisions

### Why Docker Containers?

- **Isolation**: Ubuntu for DEB extraction (apt, dpkg), Fedora for RPM building
- **Reproducibility**: Consistent environment across builds
- **Multi-arch**: Build ARM64 packages on x86_64 host
- **Dependency management**: No host pollution

### Why Stage Isolation?

- **Debugging**: Inspect intermediate state
- **Fast iteration**: Re-run failed stages without redoing successful ones
- **Clear contracts**: `params.toml` → stage → `result.toml`
- **Parallel potential**: Stages could be parallelized per app/arch

### Why Swap Electron?

- **Security**: Upstream apps often ship outdated Electron with CVEs
- **Features**: Latest Electron has better Wayland, performance, codec support
- **Consistency**: All apps use same Electron version
- **Native modules**: Rebuilt against new Electron ABI using `@electron/rebuild`

### Why Custom Flags?

- **Wayland support**: Most apps default to X11
- **Qt integration**: Better theming on KDE/Qt desktops
- **IME support**: Proper input method handling for international users
- **Portals**: Sandboxing/permission integration

### Why Fail-Fast?

- **Correctness**: Invalid RPMs are dangerous
- **Clarity**: Errors are loud, not hidden
- **Trust**: User must fix issues, not auto-workaround
- **Maintainability**: Forces addressing root causes

## Limitations & Future Work

- **Source types**: Only DEB and AppImage supported (no Snap, Flatpak)
- **Testing**: No automated testing of built RPMs
- **Signing**: RPMs not signed
- **Repositories**: No auto-upload to RPM repo
- **ARM64 testing**: Cross-arch builds untested on real ARM64 hardware

## Contributing

To add a new application:

1. Create `apps/{app-name}.toml` with source configuration
2. Run `./repack.py --app {app-name}`
3. Debug any failures by inspecting workspace
4. Add patches to `patches/{app-name}/` if needed
5. Verify RPM installs and launches correctly

## License

This tooling is provided as-is. Packaged applications retain their original licenses.

## Examples

**Build Signal for amd64**:

```bash
./repack.py --app signal-desktop
# Output: workspace/signal-desktop/amd64/stage4/signal-desktop-{version}-1.x86_64.rpm
```

**Rebuild Element with clean workspace**:

```bash
./repack.py --clean --app element-desktop
```

**Build all apps for both architectures**:

```bash
./repack.py --arch amd64,arm64
# Builds 10 RPMs (5 apps x 2 arches)
```

**Clean only Obsidian**:

```bash
./repack.py --clean-only --app obsidian
```
