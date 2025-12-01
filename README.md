# Portable for Arch

This repository is intended for Arch Linux PKGBUILD scripts.

---

# Manual

## Installation:

1. moeOS systems already have this enabled on latest updates
2. Ordinary Arch systems:

- Install `paru`

Edit `/etc/paru.conf`, add the following to include this repository:

```
[portable]
Url = https://github.com/Kraftland/portable-arch.git
Depth = 10
SkipReview
```

**And add or uncomment Chroot**

Warning! Please use chroot to build these packages.

(Note that SkipReview skips the paru review functionality, if you wish to have that please comment it out.)

- Sync PKGBUILDs: `paru -Syu --pkgbuilds`

## Usage:

- `paru -Syu` upgrades your system as well as Portable packages
- `paru -Sl portable` to list all packages
- `paru -S [package name]` to install a package

# Contributing

We are currently only accepting `x86_64` and `any` packages. Please place your PKGBUILD and up-to-date .SRCINFO under `x86_64/${pkgname}` or `any/${pkgname}` and create pull requests. Or become a maintainer by sending Kimiblock Moe an email. If you are in need of other architectures, let me know.

The pre-commit hook is capable of generating .SRCINFO automatically. To make use of it, run `ln -srf pre-commit .git/hooks/pre-commit`

# Background

This repository was created because of AUR's opaque moderation rules, see [An open letter to the AUR community](https://blog.kimiblock.top/2025/09/08/letter-to-aur/).