#!/usr/bin/bash
install \
	-vDm755 \
	/usr/share/discord-bwrap/settings.json \
	~/".config/discord/settings.json"
/opt/discord/Discord --ozone-platform-hint=auto --enable-features=AcceleratedVideoEncoder,AcceleratedVideoDecoder,AcceleratedVideoDecodeLinuxGL,VaapiIgnoreDriverChecks  --wayland-text-input-version=3 --enable-wayland-ime --no-sandbox $@