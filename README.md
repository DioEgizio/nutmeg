# nutmeg

nutmeg is a Roblox bootstrapper for Linux. It downloads the macOS Roblox
client from Roblox's servers, keeps it up to date, and starts it through
[rosetta3](https://github.com/frolleks/rosetta3), which runs macOS programs on
Linux. It is written in Python with a GTK 4 interface.

rosetta3 is experimental, and so is running Roblox on it. nutmeg installs and
starts the client the way rosetta3's documentation describes; whether Roblox
then works depends on the rosetta3 build you use.

## Requirements

- Linux on x86-64 or AArch64.
- rosetta3, built from source as its README describes:
  `cargo build --release --bin rosetta3`, which produces
  `target/release/rosetta3`.
- SDL3 and a Vulkan driver, which rosetta3 uses at run time: it loads
  `libSDL3.so.0` for windows, and its Metal is backed by Vulkan.
- Python 3.10 or newer.
- PyGObject and GTK 4 (nutmeg is tested with GTK 4.14). On Debian and Ubuntu:
  `sudo apt install python3-gi gir1.2-gtk-4.0`. On Fedora:
  `sudo dnf install python3-gobject gtk4`. On Arch Linux:
  `sudo pacman -S python-gobject gtk4`.
- xdg-utils, so that nutmeg can open `roblox-player:` links from the browser.

## Installing

nutmeg runs straight from a checkout:

```sh
git clone https://github.com/frolleks/rostrap nutmeg
cd nutmeg
python3 -m nutmeg
```

To install the `nutmeg` command, use pipx with access to the system's
PyGObject:

```sh
pipx install --system-site-packages .
```

Nightly builds are on the Releases page, marked as pre-releases. The `.pyz`
file there runs as it is, with the system's Python and PyGObject:

```sh
python3 nutmeg-0.1.0.dev202609302341.pyz
```

## Setting up

1. Run `nutmeg` (or `python3 -m nutmeg`) to open the settings.
2. On the rosetta3 page, choose the rosetta3 program if it is not on `PATH`,
   then click Save.
3. On the Integration page, click Install. This adds "Roblox (nutmeg)" and
   "nutmeg Settings" to the application menu, and makes nutmeg the handler
   for `roblox-player:` and `roblox:` links.

After that, the Play button on roblox.com starts Roblox through nutmeg.

The first launch asks whether rosetta3 should install its Darwin base system
(MiniDarwin) into the prefix. rosetta3 downloads it from GitHub and checks it
against hashes built into rosetta3. Programs run without it, because rosetta3
links them against its own system libraries.

## Commands

```
nutmeg                      open the settings window
nutmeg launch [LINK]        install or update Roblox, then start it
nutmeg update               install or update Roblox without starting it
nutmeg install-desktop      add the menu entries and the link handler
nutmeg uninstall-desktop    remove the menu entries
```

`launch` and `update` show a progress window. With `--no-gui` they report on
the terminal instead, and ask their questions there; `--yes` answers every
question with its first choice.

## What nutmeg does

Each launch goes through these steps.

1. It asks `https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer`
   for the current macOS build, a name like `version-3bc33ee7ffad426f`.
2. If that build is not installed, it downloads
   `https://setup.rbxcdn.com/mac/<build>-RobloxPlayer.zip` (the x86_64 build)
   or `https://setup.rbxcdn.com/mac/arm64/<build>-RobloxPlayer.zip` (the arm64
   build), falling back to `setup-aws.rbxcdn.com` and `setup-ak.rbxcdn.com`.
   It unpacks the zip into
   `~/.local/share/nutmeg/versions/<build>-<arch>/Roblox.app`, refusing any
   path or link that would land outside that folder, and checks that the
   executable is a Mach-O file for the expected architecture.
3. It writes the FastFlags from the settings to
   `Roblox.app/Contents/MacOS/ClientSettings/ClientAppSettings.json`.
4. If Roblox is already running, it offers to close it. The macOS client runs
   one instance at a time (`LSMultipleInstancesProhibited` in its Info.plist).
5. It removes older builds that no Roblox process is using.
6. It runs

   ```sh
   ROSETTA3_PREFIX=~/.local/share/nutmeg/prefix \
     rosetta3 run ~/.local/share/nutmeg/versions/<build>-<arch>/Roblox.app/Contents/MacOS/RobloxPlayer \
     -protocolString <link>
   ```

   from the prefix's `root/` folder, which the client sees as `/`. The
   `-protocolString` argument is how rosetta3's `tools/install-url-handler.sh`
   passes a link, and the client reads it. Without a link, the client is
   started with no arguments, as it is when Roblox.app is opened on a Mac.

If Roblox's version service cannot be reached, nutmeg starts the build it
installed last.

On an x86-64 computer nutmeg installs the x86_64 build, and on AArch64 the
arm64 build, so that rosetta3 runs the client natively. On AArch64 the
settings can choose the x86_64 build instead, which rosetta3 recompiles with
its Microlift emulator. rosetta3 cannot emulate ARM, so the arm64 build is
refused on x86-64.

nutmeg installs Roblox's public release only. When nutmeg was written, the
version service answered HTTP 401 to requests for other channels.

## Settings

The settings are stored in `~/.config/nutmeg/config.json`. The runtime
options set rosetta3's environment variables, which rosetta3's
`docs/usage.md` describes:

| Setting | Variable |
| ------- | -------- |
| Prefix | `ROSETTA3_PREFIX` |
| VSync | `ROSETTA3_SWAP_INTERVAL` |
| Title bar | `ROSETTA3_DECORATIONS` |
| Menus | `ROSETTA3_MENU` |
| Control works as Command in menu shortcuts | `ROSETTA3_MENU_CTRL` |
| Keep the pointer inside the window | `ROSETTA3_CONFINE_POINTER` |
| Dead flag elimination | `ROSETTA3_DEAD_FLAGS` |
| Native call return | `ROSETTA3_CALL_RETURN` |
| Execution | `rosetta3 run --native` or `--emulated` |

A setting left at "Default" leaves its variable unset, and nutmeg removes
these variables from the environment it inherits, so the settings are the only
place they come from. Any other variable can be added under "Extra environment
variables".

## FastFlags

The FastFlags page of the settings lists one flag per row: its name, its type
(Boolean, Integer, String, or JSON for any other value) and its value. Add Flag
adds a row. While you type a new flag's name, its type follows the prefix:
`FFlag`, `DFFlag` and `SFFlag` are Boolean, `FInt` and `DFInt` are Integer,
`FString` and `DFString` are String. Choosing a type yourself stops that.

Import JSON takes a JSON object of flags, pasted or loaded from a file, and
either adds it to the list (replacing the values of flags already listed) or
replaces the list. Copy as JSON puts the list on the clipboard. The search box
filters the rows by name.

Save refuses a list with an empty name, a name listed twice, or a value that
does not fit its type, and says which flag is wrong. The flags are stored in
`config.json` as a JSON object, and nutmeg writes that object to
`Roblox.app/Contents/MacOS/ClientSettings/ClientAppSettings.json` before each
launch, or removes the file when the list is empty. Roblox decides which flags
it applies.

## Files

| Path | Contents |
| ---- | -------- |
| `~/.config/nutmeg/config.json` | settings |
| `~/.local/share/nutmeg/versions/` | installed Roblox builds |
| `~/.local/share/nutmeg/prefix/` | the rosetta3 prefix, including Roblox's own files under `root/Users/<name>/` |
| `~/.cache/nutmeg/downloads/` | downloads in progress |
| `~/.local/state/nutmeg/logs/` | one log per launch, with nutmeg's steps and everything rosetta3 and Roblox print |

Each path follows the matching `XDG_*_HOME` variable when it is set. The logs
hide the authentication ticket in `roblox-player:` links; the last 20 are kept.

## Nightly builds

`.github/workflows/nightly.yml` runs every day at 23:41 UTC. If the default
branch has commits that are not in a nightly release yet, it runs the test
suite under Xvfb, builds the release files with `scripts/build_nightly.py`,
and publishes them on the Releases page as a pre-release. A day without new
commits publishes nothing.

Each nightly has a wheel, a source archive, a `.pyz` and `SHA256SUMS`. Its
version is the one in `nutmeg/__init__.py` followed by `.devYYYYMMDDHHMM`, and
its tag is `nightly-YYYYMMDD-HHMM`, both from the build time in UTC. The notes
list the commits since the previous nightly.

The workflow can also be started from the Actions tab. Its `force` option
builds even when the newest commit already has a nightly.

To build the same files locally (the `build` package is needed):

```sh
python3 scripts/build_nightly.py --version 0.1.0.dev1 --out dist
```

## Development

The tests use a local web server in place of Roblox's servers and a script in
place of rosetta3:

```sh
python3 -m unittest discover -s tests
```

The window tests need a display. Without one, run them under Xvfb:

```sh
xvfb-run -a python3 -m unittest discover -s tests
```
