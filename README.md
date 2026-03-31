# Portable for Arch

This repository is intended for Arch Linux PKGBUILD scripts.

---

# Manual

## Installation:

Note that we are currently in progress of migrating to StashPak, of which the progress is tracked [here](https://github.com/Kraftland/portable-arch/issues/24). You may have to clone the repo and build packages manually for some packages.

1. moeOS systems already have this enabled on latest updates
2. Ordinary Arch systems:

- Install `stashpak`

## Usage:

- `stashpak get <packages>` gets packages from this repository

# Contributing

We are currently only accepting `x86_64` and `any` packages. Please place your PKGBUILD and up-to-date .SRCINFO under `x86_64/${pkgname}` or `any/${pkgname}` and create pull requests. Or become a maintainer by sending Kimiblock Moe an email. If you are in need of other architectures, let me know.

The main branch is protected, please always push to another branch and merge via pull request. Please do not change packages which do not belong to you.

# Background

This repository was created because of AUR's opaque moderation rules, see [An open letter to the AUR community](https://blog.kimiblock.top/2025/09/08/letter-to-aur/).

