# GTK WiFi Scanner

A lightweight GTK4 application for scanning and displaying WiFi networks on Linux Wayland. This application works without requiring root privileges by using NetworkManager's D-Bus API.

## Features

- **Wayland Native**: Works natively on Wayland without requiring XWayland
- **No Root Required**: Uses NetworkManager's D-Bus API, accessible to regular users
- **Sorted by Signal Strength**: Networks are automatically sorted by signal strength (strongest first)
- **Modern UI**: Built with GTK4 and libadwaita for a modern, native look

## Requirements

- Linux with Wayland
- NetworkManager (usually pre-installed on most Linux distributions)
- Python 3.8+
- PyGObject (GTK4 bindings)
- libadwaita (for Adw widgets)

## Installation

### Install System Dependencies

On Arch Linux:
```bash
sudo pacman -S python-gobject gtk4 libadwaita networkmanager
```

On Ubuntu/Debian:
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-dbus network-manager
```

On Fedora:
```bash
sudo dnf install python3-gobject gtk4 libadwaita NetworkManager
```

### Install Python Dependencies

```bash
pip install -r requirements.txt
```

Or install directly:
```bash
pip install PyGObject
```

## Usage

Make the script executable:
```bash
chmod +x wifi_scanner.py
```

Run the application:
```bash
./wifi_scanner.py
```

Or:
```bash
python3 wifi_scanner.py
```

## Building a Standalone Executable with Nuitka

Nuitka can compile this application into a standalone executable that includes all dependencies.

### Prerequisites for Building

Install Nuitka:
```bash
pip install nuitka
```

You'll also need the C compiler and development tools:
- **Arch Linux**: `sudo pacman -S base-devel gcc`
- **Ubuntu/Debian**: `sudo apt install build-essential gcc`
- **Fedora**: `sudo dnf install gcc gcc-c++ make`

### Build Commands

#### Basic Build (Standalone Directory)
```bash
nuitka --standalone --enable-plugin=gtk wifi_scanner.py
```

This creates a `wifi_scanner.dist/` directory containing the executable and all dependencies.

#### Single File Executable
```bash
nuitka --onefile --enable-plugin=gtk wifi_scanner.py
```

This creates a single `wifi_scanner.bin` executable file.

#### Recommended Build (Optimized Single File)
```bash
nuitka --onefile \
       --enable-plugin=gtk \
       --include-module=gi \
       --include-module=gi.repository.Gtk \
       --include-module=gi.repository.Adw \
       --include-module=gi.repository.NM \
       --include-module=gi.repository.GLib \
       --include-module=gi.repository.Gio \
       --include-module=gi.repository.Gdk \
       --linux-icon=/path/to/icon.png \
       wifi_scanner.py
```

### Running the Built Executable

After building:
- **Standalone directory**: Run `./wifi_scanner.dist/wifi_scanner`
- **Single file**: Run `./wifi_scanner.bin`

### Notes

- The first build may take several minutes as Nuitka compiles Python to C++
- The executable will be large (typically 50-100MB) as it includes Python runtime and GTK libraries
- The single-file executable extracts to a temporary directory when run, so ensure you have sufficient disk space
- NetworkManager and GTK4 libraries must still be available on the target system (they're typically pre-installed on Linux)

## How It Works

The application uses NetworkManager's D-Bus interface through the `libnm` library (via PyGObject) to:
1. Detect WiFi devices
2. Request a network scan
3. Retrieve available access points
4. Display them sorted by signal strength

All of this is done through the user's D-Bus session, so no root privileges are required.

## Network Information Displayed

For each network, the application shows:
- **SSID** (Network name)
- **Signal Strength** (as percentage and icon)
- **Frequency** (in MHz)
- **Security** (WPA2, WPA, WEP, Open, etc.)

## Troubleshooting

If you see "No WiFi device found":
- Make sure NetworkManager is running: `systemctl status NetworkManager`
- Ensure your WiFi adapter is enabled
- Check that NetworkManager can see your device: `nmcli device status`

If you get permission errors:
- Make sure you're in the `network` group (some distributions require this)
- Check that NetworkManager D-Bus service is accessible: `dbus-send --system --print-reply --dest=org.freedesktop.NetworkManager /org/freedesktop/NetworkManager org.freedesktop.DBus.Properties.Get string:org.freedesktop.NetworkManager string:AllDevices`

## Hyprland rules:
```
# SMOL WIFI MGR
windowrulev2 = float, class:(^com\.smol.*)
windowrulev2 = move 2025 48, class:(^com\.smol.*)
# windowrulev2 = opacity 0.9, class:(^com\.smol.*)
```