#!/usr/bin/env bash
# Record one Forest Care field mission: the raw sensor data (rosbag2, MCAP) plus mission notes.
#
#   ros2 run forestcare_rover record_mission.sh --site "Kottenforst ride 7" --operator "Name" \
#       [--weather "overcast, dry"] [--notes "..."] [--rover rover-01] [--out ~/forestcare_missions]
#       [--topics FILE] [--gateway-params FILE] [--split-minutes 10] [--no-preflight] [--force]
#
# Creates <out>/<UTC start>_<site>/ with
#   mission_info.yaml   operator, site, rover, weather, notes, start/end time, preflight result
#   preflight.txt       the sensor check made before recording
#   config/             copies of the configuration in use (topics, mounts, drivers, gateway)
#   bag/                the rosbag2: MCAP, zstd-compressed chunks, a new file every N minutes
# Stop with Ctrl-C: the bag is closed properly, then the next steps are printed.
set -uo pipefail

SITE=""; OPERATOR="${USER:-operator}"; ROVER="${FORESTCARE_ROVER:-rover-01}"
OUT="${FORESTCARE_MISSIONS:-$HOME/forestcare_missions}"; NOTES=""; WEATHER=""
PREFLIGHT=1; FORCE=0; SPLIT_MIN=10; MIN_FREE_GB=20
command -v ros2 >/dev/null || { echo "ros2 not found: source your ROS 2 setup first"; exit 1; }
SHARE="$(ros2 pkg prefix forestcare_rover)/share/forestcare_rover"
TOPICS_FILE="$SHARE/config/record_topics.txt"
GATEWAY_PARAMS="$SHARE/config/forestcare_gateway.yaml"

usage() { sed -n '2,15p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE="$2"; shift 2 ;;
    --operator) OPERATOR="$2"; shift 2 ;;
    --rover) ROVER="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --notes) NOTES="$2"; shift 2 ;;
    --weather) WEATHER="$2"; shift 2 ;;
    --topics) TOPICS_FILE="$2"; shift 2 ;;
    --gateway-params) GATEWAY_PARAMS="$2"; shift 2 ;;
    --split-minutes) SPLIT_MIN="$2"; shift 2 ;;
    --no-preflight) PREFLIGHT=0; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1"; usage ;;
  esac
done
[ -n "$SITE" ] || { echo "--site is required"; usage; }

quote() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
SLUG=$(printf '%s' "$SITE" | tr -cs 'A-Za-z0-9' '_' | sed 's/^_//; s/_$//')
MISSION="$OUT/$(date -u +%Y%m%dT%H%M%SZ)_${SLUG}"
mkdir -p "$MISSION/config"

FREE_GB=$(df -P -BG "$MISSION" | awk 'NR == 2 { gsub("G", "", $4); print $4 }')
if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
  echo "Only ${FREE_GB} GB free on $(df -P "$MISSION" | awk 'NR == 2 { print $6 }'); a 720p camera needs ~5 GB per hour, 3D LiDAR adds 10-20 GB."
  [ "$FORCE" -eq 1 ] || { echo "Free some space, or record anyway with --force."; exit 1; }
fi

cp "$TOPICS_FILE" "$MISSION/config/record_topics.txt"
cp "$SHARE/config/mounts.yaml" "$SHARE/config/drivers.yaml" "$MISSION/config/" 2>/dev/null || true
cp "$GATEWAY_PARAMS" "$MISSION/config/gateway.yaml" 2>/dev/null || true

PREFLIGHT_RESULT="skipped"
if [ "$PREFLIGHT" -eq 1 ]; then
  echo "Checking the sensors for 8 s ..."
  # run the check under the gateway's node name so it reads the gateway's topic settings
  if ros2 run forestcare_gateway preflight --ros-args -r __node:=forestcare_gateway \
       --params-file "$GATEWAY_PARAMS" -p duration_s:=8.0 -p bag_dir:="$OUT" | tee "$MISSION/preflight.txt"; then
    PREFLIGHT_RESULT="ready"
  else
    PREFLIGHT_RESULT="NOT ready"
    [ "$FORCE" -eq 1 ] || { echo "The sensor check failed (see above). Fix it, or record anyway with --force."; exit 1; }
  fi
fi

{
  echo "operator: $(quote "$OPERATOR")"
  echo "site: $(quote "$SITE")"
  echo "area_name: $(quote "$SITE")"
  echo "rover: $(quote "$ROVER")"
  echo "weather: $(quote "$WEATHER")"
  echo "notes: $(quote "$NOTES")"
  echo "started_utc: \"$(date -u +%FT%TZ)\""
  echo "ros_distro: $(quote "${ROS_DISTRO:-}")"
  echo "host: $(quote "$(hostname)")"
  echo "preflight: $(quote "$PREFLIGHT_RESULT")"
} > "$MISSION/mission_info.yaml"

TOPICS=$(grep -v '^[[:space:]]*#' "$TOPICS_FILE" | awk 'NF { print $1 }')
echo
echo "Recording into $MISSION/bag"
echo "Drive the route. Press a mark button at each plant; Ctrl-C when the route is done."
trap 'echo; echo "Stopping the recorder ..."' INT TERM
# shellcheck disable=SC2086
ros2 bag record -s mcap --storage-preset-profile zstd_fast --max-bag-duration $((SPLIT_MIN * 60)) \
  -o "$MISSION/bag" $TOPICS
STATUS=$?
trap - INT TERM
{
  echo "ended_utc: \"$(date -u +%FT%TZ)\""
  echo "recorder_exit_code: $STATUS"
} >> "$MISSION/mission_info.yaml"

echo
ros2 bag info "$MISSION/bag" 2>/dev/null | sed -n '1,8p'
cat <<EOF

Mission folder: $MISSION
Next steps (no ROS needed, e.g. on the field laptop or back in the office):
  python -m forestcare_gateway inspect "$MISSION/bag"
  python -m forestcare_gateway convert "$MISSION/bag" --config "$MISSION/config/gateway.yaml"
  python -m forestcare_gateway upload  --api http://<forest-care-server>:8000
EOF
