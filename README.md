# printo

A small, fast GTK4 / libadwaita GUI for CUPS. Drop a file, pick a printer,
press Print. Pause, resume and cancel jobs from the queue page.

Status: **v0.2** — printing, queue, printer management, presets, tray.

## Features

- Print one or many files (choose, or drag and drop onto the window)
- Copies, one/two-sided, paper size, orientation, page ranges
- Live queue: hold, resume, cancel
- `printo file.pdf` opens with the file already chosen; shows in "Open with"
- **Printers page**: scan the network/USB or type an address, add (driverless
  or pick a driver), remove, set default, print a test page
- **Presets**: save "A4, both sides" style option sets and reuse them
- **Tray icon + notifications**: told when a job finishes or fails, or a
  printer stops. Closing the window keeps printo in the tray

## Roadmap

| Version | Plan |
|---|---|
| v0.1 | Print files, queue (done) |
| v0.2 | Add/remove printers, presets, tray icon (done) |
| v0.3 | Print preview, PDF tools (merge/split/rotate), Thai translation |

## Install

Arch (AUR, once published):

```bash
yay -S printo
```

Debian / Ubuntu / Mint: build the `.deb` yourself (see
[docs/PACKAGING.md](docs/PACKAGING.md)):

```bash
sudo apt install ./printo_0.2.0_all.deb
```

From source:

```bash
# Debian/Ubuntu:  sudo apt install python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1
# Arch:           sudo pacman -S python-gobject python-pycups gtk4 libadwaita
git clone https://github.com/Campus2454/printo
cd printo
python3 -m printo
```

Needs GTK 4.10+, libadwaita 1.4+, and a running CUPS service.

## Develop

```bash
python3 -m unittest discover -s tests   # backend tests, no GUI needed
```

- `printo/backend.py` — all CUPS logic, no GUI imports, unit tested
- `printo/presets.py`, `printo/watch.py` — presets file and "what changed since
  the last poll" logic, also GUI-free and unit tested
- `printo/app.py`, `printo/printers_ui.py`, `printo/tray.py` — GTK4 windows and
  the tray icon; CUPS calls run in worker threads

Printing needs no special rights. Adding/removing printers needs your user
in the `lpadmin` group (`sys` or `wheel` on some distros); printo tells you
if you are missing it.

### Tray and autostart

The tray uses the StatusNotifierItem protocol: it works on KDE, XFCE,
Cinnamon, MATE, and GNOME with the AppIndicator extension. With no tray
available, printo behaves like a normal window app (closing quits).

Start hidden in the tray at login:

```bash
mkdir -p ~/.config/autostart
sed 's/^Exec=printo %F/Exec=printo --background/' \
  /usr/share/applications/io.github.campus2454.printo.desktop \
  > ~/.config/autostart/io.github.campus2454.printo.desktop
```

Presets are stored in `~/.config/printo/presets.json`.

## License

MIT
