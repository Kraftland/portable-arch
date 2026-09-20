#!/usr/bin/bash

export LD_LIBRARY_PATH="/usr/share/chaoxing/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec \
	/usr/bin/electron \
	/usr/share/chaoxing/app.asar \
	"$@"