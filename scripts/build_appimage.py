#!/usr/bin/env python3
"""Build nutmeg's AppImage, which carries the Python, PyGObject and GTK 4 that
nutmeg runs on, so that a desktop needs none of them installed to run it.

    python3 scripts/build_appimage.py --version 0.2.0 --runtime runtime-x86_64 --out dist

It is built on Ubuntu 24.04 from the system's own packages:

    sudo apt-get install python3 python3-gi gir1.2-gtk-4.0 librsvg2-common \\
        libglib2.0-bin squashfs-tools patchelf

and so runs on systems with glibc 2.39 or newer. Run it with the system's
python3, the one PyGObject is installed for. --runtime is the AppImage type 2
runtime for this machine's architecture, from
https://github.com/AppImage/type2-runtime/releases. The nutmeg package in this
checkout is what is packaged, with --version stamped into its copy.

The AppImage holds every library the bundled programs load (as ldd finds
them), except those on the AppImage project's exclude list: glibc, the
graphics drivers and the other libraries an AppImage has to take from the
system it runs on. Each bundled library gets a RUNPATH that finds the
others, so that no LD_LIBRARY_PATH is needed, as it would reach Roblox and
every other program nutmeg starts.

With --test, nutmeg's tests are run on the bundled Python and GTK before the
AppImage is made, so that a module or library left out shows up then. The
tests need a display, such as xvfb-run's.
"""

from __future__ import annotations

import argparse
import compileall
import os
import platform
import py_compile
import shlex
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from build_nightly import PACKAGE, _VERSION, stamp_version  # noqa: E402
from nutmeg import desktop  # noqa: E402

# The AppImage project's exclude list,
# https://github.com/AppImageCommunity/pkg2appimage/blob/master/excludelist,
# with arm64's dynamic loader added.
EXCLUDED = frozenset(
    """
    ld-linux.so.2 ld-linux-x86-64.so.2 ld-linux-aarch64.so.1 libanl.so.1
    libBrokenLocale.so.1 libcidn.so.1 libc.so.6 libdl.so.2 libm.so.6 libmvec.so.1
    libnss_compat.so.2 libnss_dns.so.2 libnss_files.so.2 libnss_hesiod.so.2
    libnss_nisplus.so.2 libnss_nis.so.2 libpthread.so.0 libresolv.so.2 librt.so.1
    libthread_db.so.1 libutil.so.1 libstdc++.so.6 libGL.so.1 libEGL.so.1
    libGLdispatch.so.0 libGLX.so.0 libOpenGL.so.0 libdrm.so.2 libglapi.so.0
    libgbm.so.1 libxcb.so.1 libX11.so.6 libX11-xcb.so.1 libwayland-client.so.0
    libasound.so.2 libfontconfig.so.1 libfreetype.so.6 libharfbuzz.so.0
    libcom_err.so.2 libexpat.so.1 libgcc_s.so.1 libgpg-error.so.0 libICE.so.6
    libSM.so.6 libusb-1.0.so.0 libuuid.so.1 libz.so.1 libjack.so.0
    libpipewire-0.3.so.0 libxcb-dri3.so.0 libxcb-dri2.so.0 libfribidi.so.0
    libgmp.so.10
    """.split()
)

# The GObject namespaces nutmeg imports. The typelibs they depend on come too.
NAMESPACES = ["GLib-2.0", "GObject-2.0", "Gio-2.0", "Gdk-4.0", "Gtk-4.0"]

# Parts of the standard library nutmeg does not use.
STDLIB_LEFT_OUT = {
    "__pycache__", "dist-packages", "ensurepip", "idlelib", "lib2to3", "pydoc_data",
    "site-packages", "sitecustomize.py", "test", "tkinter", "turtle.py", "turtledemo",
}

# The machines the AppImage runtime is built for, by ELF machine number.
_ELF_MACHINES = {"x86_64": 62, "aarch64": 183}
_TYPELIB_MAGIC = b"GOBJ\nMETADATA\r\n\x1a"

# The variables the AppRun sets.
APPRUN_VARS = (
    "GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE", "GSETTINGS_SCHEMA_DIR", "GSETTINGS_BACKEND",
    "GIO_MODULE_DIR", "GIO_LAUNCH_DESKTOP", "GTK_EXE_PREFIX", "SSL_CERT_FILE",
)
# Runs the bundled Python. The variables point the bundled libraries at their
# files in the AppImage; they would break other programs, so the values they
# had are kept in NUTMEG_HOST_<name> (left unset where the variable was
# unset), and nutmeg puts them back for every program it starts
# (paths.host_env). The fields are filled in by write_apprun.
_APPRUN = """#!/bin/sh
# Runs nutmeg on the Python and GTK 4 in this AppImage.
set -e
here={here}
vars="{names}"

# Started by a nutmeg AppImage that passed its own values on: put the
# system's back first.
for name in ${{NUTMEG_APPIMAGE_VARS-}}; do
	case $name in *[!A-Z_]*) continue ;; esac
	if eval "[ -n \\"\\${{NUTMEG_HOST_$name+set}}\\" ]"; then
		eval "export $name=\\"\\$NUTMEG_HOST_$name\\""
		unset "NUTMEG_HOST_$name"
	else
		unset "$name"
	fi
done
for name in $vars; do
	if eval "[ -n \\"\\${{$name+set}}\\" ]"; then
		eval "export NUTMEG_HOST_$name=\\"\\$$name\\""
	fi
done
export NUTMEG_APPIMAGE_VARS="$vars"

export GI_TYPELIB_PATH="$here/usr/lib/girepository-1.0"
export GDK_PIXBUF_MODULE_FILE="$here/usr/lib/gdk-pixbuf-2.0/loaders.cache"
export GSETTINGS_SCHEMA_DIR="$here/usr/share/glib-2.0/schemas"
# GTK's settings, such as the file chooser's, are not kept: the system's
# dconf module is not loaded, as it was built for another GLib.
export GSETTINGS_BACKEND=memory
export GIO_MODULE_DIR="$here/usr/lib/gio/modules"
# What GIO starts programs from menu entries with, which is elsewhere on
# systems other than Debian's.
export GIO_LAUNCH_DESKTOP="$here/usr/bin/gio-launch-desktop"
# Keeps GTK from loading the system's GTK modules, built for another GTK.
export GTK_EXE_PREFIX="$here/usr"
# The bundled OpenSSL looks for certificates where Debian keeps them.
if [ -z "${{SSL_CERT_FILE-}}" ] && [ -z "${{SSL_CERT_DIR-}}" ]; then
	for file in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\
		/etc/ssl/ca-bundle.pem /etc/pki/tls/cacert.pem /etc/ssl/cert.pem; do
		if [ -f "$file" ]; then
			export SSL_CERT_FILE="$file"
			break
		fi
	done
fi

# -I: the system's PYTHONPATH, user site-packages and current folder stay out.
exec "$here/usr/bin/python3" -I {arguments}
"""


def system_lib_dirs(multiarch: str) -> list[Path]:
    return [Path(f"/usr/lib/{multiarch}"), Path(f"/lib/{multiarch}"), Path("/usr/lib"), Path("/lib")]


def find_library(soname: str, multiarch: str) -> Path:
    for directory in system_lib_dirs(multiarch):
        if (directory / soname).exists():
            return directory / soname
    raise SystemExit(f"{soname} is not installed")


def is_elf(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as f:
        return f.read(4) == b"\x7fELF"


def elf_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if is_elf(path))


def needed(path: Path) -> dict[str, Path]:
    """The libraries `path` loads, directly or not, by soname, and where ldd
    finds them."""
    result = subprocess.run(
        ["ldd", str(path)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise SystemExit(f"ldd {path} failed: {(result.stderr or result.stdout).strip()}")
    found = {}
    for line in result.stdout.splitlines():
        name, arrow, rest = line.strip().partition(" => ")
        if not arrow:
            continue  # the vDSO and the dynamic loader
        if rest.startswith("not found"):
            raise SystemExit(f"{path} needs {name}, which is not found")
        found[name] = Path(rest.rsplit(" (", 1)[0])
    return found


def typelib_header(path: Path) -> tuple[str, list[str], list[str]]:
    """A typelib's namespace, the namespaces it depends on and the libraries
    it is for, from its header (gitypelib-internal.h)."""
    data = path.read_bytes()
    if not data.startswith(_TYPELIB_MAGIC):
        raise SystemExit(f"{path} is not a typelib")
    dependencies, _size, namespace, nsversion, shared_library = struct.unpack_from("=5I", data, 36)

    def string(offset: int) -> str:
        return data[offset:data.index(b"\0", offset)].decode() if offset else ""

    return (
        f"{string(namespace)}-{string(nsversion)}",
        [n for n in string(dependencies).split("|") if n],
        [n for n in string(shared_library).split(",") if n],
    )


def copy_typelibs(appdir: Path, multiarch: str) -> list[Path]:
    """Copy the typelibs nutmeg needs, and return the libraries they are for."""
    sources = [Path(f"/usr/lib/{multiarch}/girepository-1.0"), Path("/usr/lib/girepository-1.0")]
    target = appdir / "usr/lib/girepository-1.0"
    target.mkdir(parents=True)
    libraries = []
    pending, done = list(NAMESPACES), set()
    while pending:
        name = pending.pop()
        if name in done:
            continue
        done.add(name)
        path = next((d / f"{name}.typelib" for d in sources if (d / f"{name}.typelib").exists()), None)
        if path is None:
            raise SystemExit(f"the typelib for {name} is not installed")
        namespace, dependencies, shared = typelib_header(path)
        if namespace != name:
            raise SystemExit(f"{path} is for {namespace}")
        shutil.copyfile(path, target / path.name)
        pending += dependencies
        libraries += [find_library(soname, multiarch) for soname in shared if soname not in EXCLUDED]
    return libraries


def copy_python(appdir: Path) -> list[Path]:
    """Copy this Python, its standard library and PyGObject. Returns the
    programs and modules in them."""
    if sys.prefix != "/usr" or sys.prefix != sys.base_prefix:
        raise SystemExit(f"run this with the system's python3, not {sys.executable}")
    import gi

    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib = Path(sysconfig.get_path("stdlib"))
    if stdlib != Path(f"/usr/lib/{version}"):
        raise SystemExit(f"expected the standard library in /usr/lib/{version}, not {stdlib}")
    python = appdir / "usr/bin/python3"
    python.parent.mkdir(parents=True)
    shutil.copy2(Path(sys.executable).resolve(), python)

    def ignore(folder: str, names: list[str]) -> set[str]:
        left_out = {n for n in names if n in STDLIB_LEFT_OUT}
        if Path(folder) == stdlib:
            left_out |= {n for n in names if n.startswith("config-")}
        return left_out

    shutil.copytree(stdlib, appdir / "usr/lib" / version, ignore=ignore)
    packages = appdir / "usr/lib/python3/dist-packages"
    shutil.copytree(Path(gi.__file__).parent, packages / "gi", ignore=shutil.ignore_patterns("__pycache__"))
    return [python] + elf_files(appdir / "usr/lib" / version) + elf_files(packages / "gi")


def copy_pixbuf_loader(appdir: Path, multiarch: str) -> Path:
    """Copy gdk-pixbuf's SVG loader, for icon themes' SVG icons (GTK reads PNG
    and JPEG itself), and write a loaders.cache that names it by file name
    only. gdk-pixbuf opens it with g_module_open, so the RUNPATH of the
    bundled GModule finds it next to the other libraries."""
    base = Path(f"/usr/lib/{multiarch}/gdk-pixbuf-2.0")
    loader = base / "2.10.0/loaders/libpixbufloader-svg.so"
    if not loader.exists():
        raise SystemExit(f"{loader} is not installed (librsvg2-common)")
    copied = appdir / "usr/lib" / loader.name
    shutil.copy2(loader, copied)
    cache = subprocess.run(
        [str(base / "gdk-pixbuf-query-loaders"), str(loader)], check=True, capture_output=True, text=True
    ).stdout
    if cache.count(f'"{loader}"') != 1:
        raise SystemExit(f"gdk-pixbuf-query-loaders did not list {loader}:\n{cache}")
    target = appdir / "usr/lib/gdk-pixbuf-2.0/loaders.cache"
    target.parent.mkdir(parents=True)
    target.write_text(cache.replace(f'"{loader}"', f'"{loader.name}"'), "utf-8")
    return copied


def copy_gio_launch_desktop(appdir: Path, multiarch: str) -> Path:
    """The helper GIO starts programs from menu entries with, as the log
    folder is opened."""
    source = Path(f"/usr/lib/{multiarch}/glib-2.0/gio-launch-desktop")
    copied = appdir / "usr/bin" / source.name
    shutil.copy2(source, copied)
    return copied


def copy_schemas(appdir: Path) -> None:
    """GTK's GSettings schemas, which its file chooser needs."""
    target = appdir / "usr/share/glib-2.0/schemas"
    target.mkdir(parents=True)
    schemas = sorted(Path("/usr/share/glib-2.0/schemas").glob("org.gtk.gtk4.*.gschema.xml"))
    if not schemas:
        raise SystemExit("GTK 4's GSettings schemas are not installed")
    for schema in schemas:
        shutil.copyfile(schema, target / schema.name)
    subprocess.run(["glib-compile-schemas", "--strict", str(target)], check=True)


def copy_nutmeg(appdir: Path, version: str) -> None:
    packages = appdir / "usr/lib/python3/dist-packages"
    shutil.copytree(ROOT / PACKAGE, packages / PACKAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    stamp_version(packages, version)


def write_apprun(path: Path, here: str, arguments: str) -> None:
    """Write an AppRun that finds the AppImage's files at `here`, a shell
    word, and runs the bundled Python with `arguments`."""
    path.write_text(_APPRUN.format(here=here, names=" ".join(APPRUN_VARS), arguments=arguments), "utf-8")
    path.chmod(0o755)


def direct_needs(path: Path) -> list[str]:
    """The sonames `path` itself asks for (its DT_NEEDED entries)."""
    result = subprocess.run(["patchelf", "--print-needed", str(path)], check=True, capture_output=True, text=True)
    return result.stdout.split()


def deploy_libraries(appdir: Path, programs: list[Path]) -> None:
    """Copy into usr/lib the libraries `programs` ask for, and the ones those
    ask for, but not excluded libraries: the system's copy of one finds what
    it needs on the system. Then give every program and library a RUNPATH to
    usr/lib."""
    lib = appdir / "usr/lib"
    pending = list(programs)
    while pending:
        path = pending.pop()
        found = needed(path)
        for soname in direct_needs(path):
            if soname not in EXCLUDED and not (lib / soname).exists():
                shutil.copy2(found[soname].resolve(), lib / soname)
                pending.append(lib / soname)
    for path in elf_files(appdir):
        relative = os.path.relpath(lib, path.parent)
        runpath = "$ORIGIN" if relative == "." else f"$ORIGIN/{relative}"
        subprocess.run(["patchelf", "--set-rpath", runpath, str(path)], check=True)


def check_libraries(appdir: Path) -> None:
    """Every library a bundled program or library asks for is found in the
    AppImage, or is excluded."""
    problems = []
    for path in elf_files(appdir):
        found = needed(path)
        for soname in direct_needs(path):
            if soname not in EXCLUDED and not found[soname].resolve().is_relative_to(appdir):
                problems.append(f"{path.relative_to(appdir)} loads {soname} from {found[soname]}")
    if problems:
        raise SystemExit("libraries from outside the AppImage:\n" + "\n".join(problems))


def compile_python(appdir: Path) -> None:
    # Unchecked hashes, so that the .pyc files are used whatever time stamps
    # the files get, and nothing is written to the read-only AppImage.
    for directory in sorted((appdir / "usr/lib").glob("python3*")):
        if not compileall.compile_dir(
            directory,
            quiet=1,
            workers=0,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        ):
            raise SystemExit(f"could not compile the Python files in {directory}")


def check_runtime(runtime: bytes, machine: str) -> None:
    # An AppImage of type 2 says so in the padding of its ELF header.
    if not runtime.startswith(b"\x7fELF") or runtime[8:11] != b"AI\x02":
        raise SystemExit("--runtime is not an AppImage type 2 runtime")
    elf_machine = struct.unpack_from("<H", runtime, 18)[0]
    if elf_machine != _ELF_MACHINES[machine]:
        raise SystemExit(f"--runtime is not for {machine}")


def check_version_output(command: list[str], version: str, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command + ["--version"], capture_output=True, text=True, env=env, cwd="/")
    expected = f"{PACKAGE} {version}"
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise SystemExit(
            f"{shlex.join(command)} --version printed {result.stdout.strip()!r} "
            f"(exit status {result.returncode}, stderr {result.stderr.strip()!r}); expected {expected!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="the version to stamp, such as 0.2.0")
    parser.add_argument("--runtime", required=True, type=Path, help="the AppImage type 2 runtime")
    parser.add_argument("--out", required=True, type=Path, help="the folder to write the AppImage to")
    parser.add_argument("--test", action="store_true", help="run nutmeg's tests on the bundled Python first")
    args = parser.parse_args()
    if not _VERSION.match(args.version):
        parser.error(f"{args.version!r} is not a version like 0.1.0 or 0.1.0.dev202609302341")
    machine = platform.machine()
    if machine not in _ELF_MACHINES:
        parser.error(f"AppImages are built for {' and '.join(_ELF_MACHINES)}, not {machine}")
    multiarch = sysconfig.get_config_var("MULTIARCH")
    if not multiarch:
        parser.error("this Python does not say its multiarch triplet; build on Debian or Ubuntu")
    runtime = args.runtime.read_bytes()
    check_runtime(runtime, machine)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{PACKAGE}-{args.version}-{machine}.AppImage"
    if target.exists():
        parser.error(f"{target} exists")

    with tempfile.TemporaryDirectory(prefix="nutmeg-appimage-") as scratch_name:
        scratch = Path(scratch_name)
        appdir = scratch / "AppDir"
        programs = copy_python(appdir)
        for library in copy_typelibs(appdir, multiarch):
            programs.append(appdir / "usr/lib" / library.name)
            shutil.copy2(library.resolve(), programs[-1])
        programs.append(copy_pixbuf_loader(appdir, multiarch))
        programs.append(copy_gio_launch_desktop(appdir, multiarch))
        # Empty, so that GIO loads none of the system's modules.
        (appdir / "usr/lib/gio/modules").mkdir(parents=True)
        copy_schemas(appdir)
        deploy_libraries(appdir, programs)
        check_libraries(appdir)
        copy_nutmeg(appdir, args.version)
        compile_python(appdir)

        # What desktops show for the AppImage: the settings, under nutmeg's icon.
        (appdir / desktop.SETTINGS_ID).write_text(desktop.settings_entry(["nutmeg"]), "utf-8")
        icon = f"{desktop.SETTINGS_ICON}.svg"
        (appdir / icon).write_bytes(desktop._settings_svg())
        (appdir / ".DirIcon").symlink_to(icon)
        write_apprun(appdir / "AppRun", '$(dirname "$(readlink -f "$0")")', '-m nutmeg "$@"')
        check_version_output([str(appdir / "AppRun")], args.version)

        if args.test:
            # The checkout's nutmeg, which the tests expect, on the bundled
            # Python and libraries.
            python = scratch / "python"
            write_apprun(python, shlex.quote(str(appdir)), '"$@"')
            run_tests = (
                "import sys, unittest; sys.path.insert(0, sys.argv[1]); "
                "unittest.main(module=None, argv=['unittest', 'discover', '-s', sys.argv[2]])"
            )
            result = subprocess.run([str(python), "-c", run_tests, str(ROOT), str(ROOT / "tests")])
            if result.returncode != 0:
                raise SystemExit("the tests failed on the bundled Python")

        image = scratch / "nutmeg.squashfs"
        subprocess.run(
            ["mksquashfs", str(appdir), str(image), "-root-owned", "-noappend", "-comp", "zstd", "-quiet", "-no-progress"],
            check=True,
        )
        with target.open("wb") as f:
            f.write(runtime)
            with image.open("rb") as squashfs:
                shutil.copyfileobj(squashfs, f)
        target.chmod(0o755)
    # Run it unpacked, which needs no FUSE.
    check_version_output([str(target)], args.version, env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"})
    print(f"{target.name}  {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
