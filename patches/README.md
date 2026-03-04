# Patches Directory

Place application-specific patches here in subdirectories named after the app ID.

## Structure

```
patches/
├── signal-desktop/
│   ├── fix-something.patch
│   └── update-config.asar.patch
├── element-desktop/
│   └── custom-theme.asar.patch
└── README.md (this file)
```

## Patch Types

### Regular File Patches (`.patch`)

These are applied to the normalized file tree with `patch -p1`.

**Creating a patch**:
```bash
cd workspace/{app}/{arch}/stage3/output
# Make your changes
diff -Naur original/ modified/ > patches/{app}/my-fix.patch
```

### ASAR Patches (`.asar.patch`)

These are applied to the contents of `.asar` archives (which are then repacked).

**Creating an ASAR patch**:
```bash
cd workspace/{app}/{arch}/stage3/output/usr/lib64/{app}/resources
asar extract app.asar /tmp/app-unpacked

# Make a copy for diffing
cp -r /tmp/app-unpacked /tmp/app-unpacked-original

# Edit files in /tmp/app-unpacked
vim /tmp/app-unpacked/main.js

# Create patch
cd /tmp
diff -Naur app-unpacked-original/ app-unpacked/ > patches/{app}/my-asar-fix.asar.patch
```

## Patch Application Order

1. All `.patch` files are applied first (in alphabetical order)
2. All `.asar.patch` files are applied second (in alphabetical order)
3. Custom Electron flags are added last

## Tips

- Name patches descriptively: `fix-wayland-support.patch`
- Keep patches focused on one issue
- Test patches by re-running: `./repack.py --app {app}`
- Document complex patches with comments
