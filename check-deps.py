#!/usr/bin/env python3
"""Check if all dependencies are available."""

import sys

print("Checking dependencies...")
print()

# Check Python version
print(f"Python version: {sys.version}")
if sys.version_info < (3, 11):
    print("ERROR: Python 3.11+ required (for tomllib)")
    sys.exit(1)
print("✓ Python version OK")
print()

# Check tomllib
try:
    import tomllib

    print("✓ tomllib available")
except ImportError:
    print("ERROR: tomllib not found - Python 3.11+ required")
    sys.exit(1)
print()

# Check tomli_w
try:
    import tomli_w

    print("✓ tomli_w available")
except ImportError:
    print("ERROR: tomli_w not found")
    print("Install with: pip install tomli-w")
    sys.exit(1)
print()

# Check docker
try:
    import docker

    print("✓ docker package available")

    # Try to connect to Docker daemon
    try:
        client = docker.from_env()
        print("✓ Docker daemon accessible")
        version = client.version()
        print(f"  Docker version: {version.get('Version', 'unknown')}")
    except Exception as e:
        print(f"ERROR: Cannot connect to Docker daemon: {e}")
        print("Make sure Docker is running and you have permission to access it")
        sys.exit(1)
except ImportError:
    print("ERROR: docker package not found")
    print("Install with: pip install docker tomli-w")
    sys.exit(1)
print()

print("=" * 60)
print("All dependencies satisfied!")
print("Ready to run: ./repack.py")
print("=" * 60)
