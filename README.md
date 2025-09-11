# Portable for Arch

This repository is intended for Arch Linux PKGBUILD scripts.

---

Usage:

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

(Note that SkipReview skips the paru review functionality, if you wish to have that please comment it out.)

- Sync PKGBUILDs: `paru -Syu --pkgbuilds`

# Contributing

We are currently only accepting `x86_64` packages. Please place your PKGBUILD and up-to-date .SRCINFO under `x86_64/${pkgname}` and create pull requests. If you are in need of other architectures, let me know.

# Background

This repository was created because of AUR's opaque moderation rules, see [An open letter to the AUR community](https://blog.kimiblock.top/2025/09/08/letter-to-aur/).