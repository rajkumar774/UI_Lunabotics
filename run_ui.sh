#!/usr/bin/env bash
# Launch Mission Control with an environment that actually works.
#
# Three things here are load-bearing; each one has silently broken the UI:
#
#  1. PYTHONPATH is APPENDED. Writing PYTHONPATH=src clobbers what ROS's
#     setup.bash just set, rclpy vanishes, and the app quietly falls back to
#     MOCK MODE while looking completely normal.
#  2. QT_QPA_PLATFORM=xcb. Under native Wayland, Qt's GTK platform theme asks
#     GNOME for a settings key that does not exist and GLib aborts the process.
#  3. The DDS vars must match the Jetson exactly, or the UI connects to nothing.
cd "$(dirname "$(readlink -f "$0")")"

# ROS setup scripts reference unset variables, so `set -u` must stay off here.
source /opt/ros/humble/setup.bash

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-17}"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
export CYCLONEDDS_URI="${CYCLONEDDS_URI:-file://$HOME/.ros/cyclonedds.xml}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export QT_QPA_PLATFORMTHEME=
export PYTHONPATH="src:${PYTHONPATH:-}"

python3 -c 'import rclpy' 2>/dev/null || {
    echo "ERROR: rclpy is not importable -- the UI would fall back to MOCK MODE."
    echo "       PYTHONPATH=$PYTHONPATH"
    exit 1
}
echo "ROS_DOMAIN_ID=$ROS_DOMAIN_ID  RMW=$RMW_IMPLEMENTATION"
echo "rclpy OK -- starting Mission Control (real ROS 2 backend)"
exec python3 -m mission_control.main "$@"
