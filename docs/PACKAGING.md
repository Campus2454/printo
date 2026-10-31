# Packaging printo

Before any package: make a **release tag** on GitHub. Packages download the
source tarball of the tag.

```bash
git tag -a v0.1.0 -m "printo 0.1.0"
git push origin v0.1.0
```

Version lives in three places; keep them equal when you bump:
`pyproject.toml`, `printo/__init__.py`, `packaging/arch/PKGBUILD` (+ a new
entry at the top of `debian/changelog`).

## Debian / Ubuntu / Mint (.deb)

```bash
sudo apt install debhelper dh-python pybuild-plugin-pyproject \
                 python3-setuptools python3-wheel python3-all fakeroot
dpkg-buildpackage -us -uc -b
sudo apt install ../printo_0.1.0_all.deb
```

Edit the maintainer name/email in `debian/control` and `debian/changelog`
first.

## Arch / Manjaro / EndeavourOS (pacman)

Test locally:

```bash
cd packaging/arch
updpkgsums            # pacman-contrib; writes the real sha256
makepkg -si           # build and install
```

### Publish to the AUR (so `yay -S printo` works)

1. Make an account at <https://aur.archlinux.org> and add your SSH public key
   in *My Account*.
2. Check the name is free: <https://aur.archlinux.org/packages?K=printo>
3. Clone the empty AUR repo, copy the PKGBUILD in, generate `.SRCINFO`, push:

```bash
git clone ssh://aur@aur.archlinux.org/printo.git aur-printo
cd aur-printo
cp ../printo/packaging/arch/PKGBUILD .
updpkgsums
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m "printo 0.1.0"
git push
```

On each new release: bump `pkgver`, reset `pkgrel=1`, run `updpkgsums`,
regenerate `.SRCINFO`, commit, push.

The AUR is source-only and community-run. Getting into the **official**
Arch repos (`extra`) needs a Trusted User/Package Maintainer to sponsor it;
people usually start on the AUR and move later if the package is popular.

Lint before pushing: `namcap PKGBUILD` and `namcap *.pkg.tar.zst`.

## Later

- Fedora `.rpm` (`.spec` file)
- Flatpak (needs `--socket=cups`... or talk to the host CUPS over its socket)
