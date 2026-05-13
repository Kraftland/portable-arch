#!/usr/bin/bash

export PORTABLE_CONF="im.qq.app"

if [[ "$@" = "--actions opendir" ]]; then
	portable --actions opendir
elif [[ "$@" = "--actions share-files" ]]; then
	portable --actions share-files
elif [[ "$@" = "--actions quit" ]]; then
	portable --actions quit
else
	portable -- "$@"
fi