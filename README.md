# printo

A small, fast GTK4 / libadwaita GUI for CUPS. Drop a file, pick a printer,
press Print. Pause, resume and cancel jobs from the queue page.

Status: **v0.1** — print files and manage the queue.

## Features (v0.1)

- Print one or many files (choose, or drag and drop onto the window)
- Copies, one/two-sided, paper size, orientation, page ranges
- Live queue: hold, resume, cancel
- `printo file.pdf` opens with the file already chosen; shows in "Open with"

## Roadmap

| Version | Plan |
|---|---|
| v0.1 | Print files, queue (done) |
| v0.2 | Add/remove printers, presets, tray icon |
| v0.3 | Print preview, PDF tools (merge/split/rotate), Thai translation |

## Install

Arch (AUR, once published):

```bash
yay -S printo
```

Debian / Ubuntu / Mint: build the `.deb` yourself (see
[docs/PACKAGING.md](docs/PACKAGING.md)):

```bash
sudo apt install ./printo_0.1.0_all.deb
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
- `printo/app.py` — GTK4 window; CUPS calls run in worker threads

Printing needs no special rights. Adding/removing printers (v0.2) needs your
user in the `lpadmin` group (`sys` or `wheel` on some distros).

## License

MIT
