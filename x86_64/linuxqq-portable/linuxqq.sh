#!/bin/bash

if [ -d ~/.config/QQ/versions ]; then
	find ~/.config/QQ/versions -name sharp-lib -type d -exec rm -r {} \; 2>/dev/null
	find ~/.config/QQ/versions -name libssh2.so.1 -type f -exec rm {} \; 2>/dev/null
fi

rm -rf ~/.config/QQ/crash_files/*
if [[ "${XDG_SESSION_TYPE}" = "wayland" ]]; then
	flag="--ozone-platform=wayland"
fi

/opt/QQ/qq --no-sandbox ${flag} --enable-features=WebRTCPipeWireCapturer --wayland-text-input-version=3 --enable-wayland-ime "$@"