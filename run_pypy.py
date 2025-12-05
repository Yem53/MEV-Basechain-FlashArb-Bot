#!/usr/bin/env python3
"""
=========================================================
     ⚡ FlashArb V3 - PyPy JIT Launcher
=========================================================

This script provides instructions and a launcher for running
the arbitrage bot with PyPy instead of CPython.

⚡ PERFORMANCE BENEFITS:
- PyPy uses JIT (Just-In-Time) compilation
- Math operations: 5-10x faster
- Loop execution: 3-5x faster
- Overall bot performance: 2-4x improvement

📦 INSTALLATION:

Windows:
    1. Download PyPy from: https://www.pypy.org/download.html
       (Choose: PyPy3.10 for Windows 64-bit)
    2. Extract to C:\\pypy3
    3. Add to PATH: C:\\pypy3

Linux/macOS:
    # Ubuntu/Debian
    sudo apt install pypy3 pypy3-dev

    # macOS
    brew install pypy3

    # Or download from pypy.org

🚀 USAGE:

    # Install dependencies for PyPy
    pypy3 -m pip install -r requirements.txt

    # Run the bot with PyPy
    pypy3 main.py

    # Or use this launcher script
    python run_pypy.py

⚠️ COMPATIBILITY NOTES:
- PyPy has excellent compatibility with pure Python code
- Most libraries work (web3.py, aiohttp, requests)
- Some C-extension libraries may need pypy-compatible versions
- orjson works with PyPy but may need pypy-specific install

🔧 FALLBACK:
If PyPy is not available, this script falls back to CPython.
"""

import os
import sys
import shutil
import subprocess


def find_pypy():
    """Find PyPy executable on the system."""
    # Check common names
    pypy_names = ['pypy3', 'pypy3.10', 'pypy3.9', 'pypy']
    
    for name in pypy_names:
        path = shutil.which(name)
        if path:
            return path
    
    # Check common installation directories (Windows)
    windows_paths = [
        r'C:\pypy3\pypy3.exe',
        r'C:\pypy\pypy3.exe',
        os.path.expanduser(r'~\pypy3\pypy3.exe'),
    ]
    
    for path in windows_paths:
        if os.path.exists(path):
            return path
    
    return None


def check_pypy_version(pypy_path):
    """Check PyPy version and compatibility."""
    try:
        result = subprocess.run(
            [pypy_path, '--version'],
            capture_output=True,
            text=True
        )
        version_info = result.stdout.strip()
        print(f"✅ Found PyPy: {version_info}")
        return True
    except Exception as e:
        print(f"❌ PyPy check failed: {e}")
        return False


def install_dependencies(pypy_path):
    """Install required dependencies for PyPy."""
    print("\n📦 Installing dependencies for PyPy...")
    
    try:
        subprocess.run(
            [pypy_path, '-m', 'pip', 'install', '-r', 'requirements.txt'],
            check=True
        )
        print("✅ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Some dependencies may have failed: {e}")
        return False


def run_bot(python_path, use_pypy=False):
    """Run the arbitrage bot."""
    engine = "PyPy (JIT)" if use_pypy else "CPython"
    print(f"\n🚀 Starting FlashArb V3 with {engine}...")
    print("=" * 60)
    
    # Get the directory of this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(script_dir, 'main.py')
    
    try:
        subprocess.run([python_path, main_script], cwd=script_dir)
    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return False
    
    return True


def main():
    """Main launcher function."""
    print("=" * 60)
    print("     ⚡ FlashArb V3 - PyPy JIT Launcher")
    print("=" * 60)
    
    # Try to find PyPy
    pypy_path = find_pypy()
    
    if pypy_path:
        print(f"\n✅ PyPy found at: {pypy_path}")
        
        if check_pypy_version(pypy_path):
            # Ask user if they want to use PyPy
            print("\n🔥 PyPy provides 2-10x performance improvement!")
            
            # Check if running interactively
            if sys.stdin.isatty():
                choice = input("Use PyPy for this session? [Y/n]: ").strip().lower()
                use_pypy = choice != 'n'
            else:
                use_pypy = True
            
            if use_pypy:
                # Check if dependencies are installed
                print("\n🔍 Checking PyPy dependencies...")
                try:
                    subprocess.run(
                        [pypy_path, '-c', 'import web3; import aiohttp'],
                        capture_output=True,
                        check=True
                    )
                    print("✅ Dependencies OK")
                except subprocess.CalledProcessError:
                    print("⚠️ Dependencies not installed for PyPy")
                    if sys.stdin.isatty():
                        install = input("Install now? [Y/n]: ").strip().lower()
                        if install != 'n':
                            install_dependencies(pypy_path)
                
                run_bot(pypy_path, use_pypy=True)
                return
    else:
        print("\n⚠️ PyPy not found on this system")
        print("\n📝 To install PyPy:")
        print("   Windows: Download from https://www.pypy.org/download.html")
        print("   Linux:   sudo apt install pypy3")
        print("   macOS:   brew install pypy3")
    
    # Fallback to CPython
    print("\n🔄 Falling back to CPython...")
    run_bot(sys.executable, use_pypy=False)


if __name__ == "__main__":
    main()

