#!/usr/bin/env bash

. ~/conda/etc/profile.d/conda.sh
. ~/conda/etc/profile.d/mamba.sh

mamba activate ros-ws

export COLCON_DEFAULTS_FILE=/root/colcon_defaults.yaml
export PATH=/root/buildcache/bin:$PATH
export COLCON_DEFAULTS_FILE=/root/defaults.yaml
