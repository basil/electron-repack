# Contributing Guide

## Adding a New Application

### Step 1: Create App Configuration

Create `apps/{app-name}.toml` with the source configuration.

Choose the appropriate source type:

#### GitHub Release (DEB)

```toml
[source]
type = "deb"
github_repo = "owner/repository"
```

The system will:

- Fetch latest release from GitHub API
- Find `.deb` asset matching architecture (amd64/arm64)
- Download and extract

#### APT Repository (DEB)

```toml
[source]
type = "deb"
apt_repo_url = "https://packages.example.com/debian"
apt_dist = "stable"
apt_component = "main"
gpg_key_url = "https://packages.example.com/key.asc"  # Optional
```

#### Direct DEB URLs

```toml
[source]
type = "deb"
urls = [
    "https://example.com/downloads/app-1.0.0-amd64.deb",
    "https://example.com/downloads/app-1.0.0-arm64.deb"
]
```

URLs should include architecture indicators (`amd64`, `x64`, `arm64`, `aarch64`).

#### AppImage

```toml
[source]
type = "appimage"
urls = ["https://example.com/download"]
user_agent = false  # Set to true if download requires browser user-agent
```

### Step 2: Test Initial Build

```bash
./repack.py --app {app-name}
```

Common first-run issues:

**Stage 0 fails**:

- Check source URL is correct
- Verify GitHub repo name
- Test APT repository manually
- For AppImage, try downloading URL directly

**Stage 1 fails**:

- Check extracted directory structure
- App may be in unexpected location
- Desktop file or icons may be missing

**Stage 2 fails**:

- Check internet connection for Electron download
- If app has native modules, rebuild may fail (check build toolchain)

**Stage 3 fails**:

- May need custom patches
- Launcher script may be non-standard

### Step 3: Inspect and Debug

```bash
# Check what was extracted
ls -la workspace/{app-name}/amd64/stage0/output/

# Check normalized structure
tree workspace/{app-name}/amd64/stage1/output/

# Read metadata
cat workspace/{app-name}/amd64/stage0/result.toml
cat workspace/{app-name}/amd64/stage1/result.toml
```

### Step 4: Create Patches (if needed)

#### File System Patches

If files need modification:

```bash
cd workspace/{app-name}/amd64/stage3/output

# Make a copy for comparison
cp -r . /tmp/original

# Make your changes
vim usr/share/applications/{app-name}.desktop

# Create patch
diff -Naur /tmp/original . > patches/{app-name}/my-fix.patch
```

#### ASAR Patches

If app.asar contents need modification:

```bash
cd workspace/{app-name}/amd64/stage3/output/usr/lib64/{app-name}/resources

# Extract ASAR
asar extract app.asar /tmp/app-unpacked
cp -r /tmp/app-unpacked /tmp/app-original

# Make changes
vim /tmp/app-unpacked/main.js

# Create patch
cd /tmp
diff -Naur app-original/ app-unpacked/ > patches/{app-name}/fix-main.asar.patch
```

### Step 5: Verify RPM

```bash
# List RPM contents
rpm -qlp workspace/{app-name}/amd64/stage4/{app-name}-*.rpm

# Get RPM info
rpm -qip workspace/{app-name}/amd64/stage4/{app-name}-*.rpm

# Test install (in VM or container recommended)
sudo dnf install workspace/{app-name}/amd64/stage4/{app-name}-*.rpm

# Test launch
{app-name}
```

### Step 6: Test Both Architectures

```bash
# Build for ARM64
./repack.py --app {app-name} --arch arm64

# Build for both
./repack.py --app {app-name} --arch amd64,arm64
```

## Customization Options

### Override Detected Metadata

If auto-detection fails, you can override in the app config:

```toml
[source]
type = "deb"
github_repo = "owner/repo"

[metadata]
full_name = "My Custom App Name"
description = "A better description than auto-detected"
version = "1.2.3"  # Override version detection
```

(Note: This requires modifying the extraction stage to read metadata section)

### App-Specific Electron Flags

Some apps may need different flags. You can patch the launcher or desktop file accordingly.

### Skip Electron Swap

If the app breaks with newer Electron but doesn't have native modules, create a file:

```bash
echo "skip_electron_swap = true" >> apps/{app-name}.toml
```

(Note: This requires modifying stage 2 to read this flag)

## Testing Checklist

Before submitting a new app configuration:

- [ ] Builds successfully for amd64
- [ ] Builds successfully for arm64 (if applicable)
- [ ] RPM installs without errors
- [ ] Application launches
- [ ] Desktop entry appears in application menu
- [ ] Icon displays correctly
- [ ] Application functions as expected
- [ ] No errors in journal: `journalctl -f` while launching

## Common Patterns

### App in /opt

Most DEB packages install to `/opt/{vendor}/{app}`. Stage 1 handles this automatically.

### Multiple Icon Sizes

Apps should provide hicolor icon theme with multiple sizes. Stage 1 copies these automatically.

### Launcher Scripts

Apps with launcher scripts are detected automatically. The launcher is updated to reference `/usr/lib64/{app}` instead of `/opt`.

### ASAR Encryption

Some apps encrypt their ASAR. These cannot be patched with `.asar.patch`. Use regular patches on the app's behavior instead.

## Troubleshooting

### "No .asar found"

Check if app uses a different archive format or if ASAR is in unexpected location:

```bash
find workspace/{app}/amd64/stage0/output -name "*.asar"
```

### "Electron binary not found"

App may use a different binary name:

```bash
find workspace/{app}/amd64/stage0/output -name "electron" -o -name "*electron*"
```

### Version Detection Fails

Add manual version detection logic or use `metadata.version` override.

### Desktop File Missing Categories

Edit desktop file with a patch to add proper categories:

```diff
--- a/usr/share/applications/app.desktop
+++ b/usr/share/applications/app.desktop
@@ -5,3 +5,4 @@
 Exec=/usr/bin/app
 Icon=app
 Type=Application
+Categories=Network;InstantMessaging;
```

## Advanced: Modifying Stages

If an app requires special handling not covered by patches:

1. **Identify which stage** needs modification
2. **Edit `stages/stage{N}.py`** to add special case
3. **Use app_id check** to scope changes:

   ```python
   if app_id == "special-app":
       # Custom logic
   ```

4. **Test thoroughly** with all apps to ensure no regressions
5. **Document** the special case

## Code Style

- Use fail-fast approach - don't silently work around errors
- Log all significant operations
- Validate assumptions with assertions
- Keep stages independent and isolated
- Preserve existing patterns (consistency > cleverness)

## Submitting

When contributing a new app:

1. Create `apps/{app-name}.toml`
2. Create any necessary patches in `patches/{app-name}/`
3. Test building for both architectures
4. Verify RPM installs and works
5. Submit with:
   - App config file
   - Any patches
   - Brief description of app
   - Any known issues or limitations

## Getting Help

- Check existing app configs for examples
- Inspect workspace directories to understand what's happening
- Read stage scripts to see processing logic
- Ask questions with specific error messages and context
