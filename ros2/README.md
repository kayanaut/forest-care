# Forest Care robotics (ROS 2)

Two ROS 2 packages connect a rover to Forest Care. The rover is a **data source**: it
records what it senses and what the operator marks. Forest Care and its human reviewers
decide what a plant is.

| Package | What it does |
|---|---|
| `forestcare_gateway` | Turns rover data (live topics or a recorded rosbag) into a Forest Care mission, keeps it in an outbox and uploads it. Also: a simulated rover, and the localization experiment. |
| `forestcare_rover` | Brings up a teleoperated rover: drivers, sensor mounts, gamepad, mark buttons, and `record_mission.sh`. |

Start with the documents:

1. [docs/ROS2_GATEWAY.md](../docs/ROS2_GATEWAY.md): topics, message flow, configuration,
   how images and positions are associated.
2. [docs/FIELD_DATA_COLLECTION.md](../docs/FIELD_DATA_COLLECTION.md): sensors, recording,
   teleoperation, field checklist.
3. [docs/LOCALIZATION_VALIDATION.md](../docs/LOCALIZATION_VALIDATION.md): is the rover's
   position good enough to find the same plant stand again?

## Two ways to run it

**Without ROS.** Conversion, upload, the simulator and the localization analysis are plain
Python. They work on any laptop from the repository root:

```bash
uv sync
uv run python -m forestcare_gateway --help
uv run python -m forestcare_gateway demo-bag /tmp/demo_bag            # a simulated mission as a real rosbag2
uv run python -m forestcare_gateway convert /tmp/demo_bag --outbox /tmp/outbox
uv run python -m forestcare_gateway loc-experiment --out /tmp/locexp   # the localization experiment, simulated
```

**With ROS 2** (Jazzy or Kilted), for the live nodes and the rover:

```bash
cd ros2 && colcon build --symlink-install && source install/setup.bash
ros2 launch forestcare_gateway sim_demo.launch.py      # a simulated rover drives; the gateway records and uploads
ros2 launch forestcare_rover sim_teleop.launch.py      # you drive the simulated rover
ros2 launch forestcare_rover rover.launch.py           # the real rover (after configuring it)
```

With ROS 2 installed, `fc_gateway` is the same command as `python -m forestcare_gateway`.

## Commands (`fc_gateway …`)

| Command | Purpose |
|---|---|
| `inspect BAG` | Check a bag before converting: rates, GNSS fix and accuracy, clock offsets |
| `convert BAG` | Bag → mission package in the outbox (`--method` picks the localization) |
| `upload` | Send finished packages; resumable, never duplicates |
| `status` | List the outbox |
| `recover PKG` | Rebuild an interrupted package |
| `demo-bag OUT` | Write a simulated mission bag (`--world loc --rtk --lidar-odom --tag-marks` for the localization world) |
| `loc-eval EXPERIMENT.yaml --out DIR` | Evaluate a repeated-run localization experiment |
| `loc-experiment --out DIR` | Simulate one and evaluate it |

## Nodes

| Node (`ros2 run forestcare_gateway …`) | Purpose |
|---|---|
| `gateway` | Records a mission live; services `~/start_mission`, `~/stop_mission`, `~/status`; uploads at the end |
| `mark` | Gamepad buttons → operator marks on `/forestcare/mark` |
| `mark_console` | Type marks: Enter, `ref R3`, `plant P2 <label>`, or any text |
| `sim_rover` | Simulated rover with the real topic names; drives itself or follows `/cmd_vel` |
| `preflight` | 8-second sensor check before recording |

## Where things are

```
forestcare_gateway/forestcare_gateway/
  config.py       every setting, with defaults and FIELD markers for values to measure
  messages.py     ROS messages -> small Python records (NavSatFix, Odometry, Imu, images, marks, detections)
  recorder.py     one mission: writes append-only streams into the package, snapshots each mark
  package.py      the mission package on disk (outbox): JSONL streams, frames, state
  localize.py     trajectory from GNSS (and, via loc/estimators.py, fused with odometry/IMU)
  assemble.py     package -> Forest Care mission: track, frames matched to positions, marks, detections
  uploader.py     batched, resumable upload
  bagio.py        rosbag2 reading/writing (rosbags library, no ROS needed)
  health.py       inspect/preflight rules
  sim.py, demo.py simulated world, sensors and operator; demo bags
  cli.py          the fc_gateway command
  nodes/          the ROS 2 nodes (the only files that import rclpy)
  loc/            localization experiment: estimators, stand rule, evaluation, report
forestcare_rover/
  config/         drivers.yaml, mounts.yaml, teleop_joy.yaml, forestcare_gateway.yaml, record_topics.txt
  launch/         rover.launch.py, sim_teleop.launch.py
  scripts/        record_mission.sh
```

## Extending it

- **A new sensor.** Add its topic to `topics` in `config.py`, a parser in `messages.py`, a
  stream in `recorder.py`, and use it in `assemble.py`. Record it in
  `forestcare_rover/config/record_topics.txt`.
- **A detector.** Publish `vision_msgs/Detection2DArray` on `/forestcare/detections`, with
  the image's header stamp. Map its class names to Forest Care's in
  `detections.class_map`. The gateway matches each detection to its frame.
- **A localization method.** Add it to `METHODS` in `localize.py` and `SOURCES` in
  `loc/estimators.py`, then to `ORDER`, `HARDWARE` and `NEEDS` in `loc/evaluate.py`. The
  experiment then compares it with the others on the same data.
- **Another rover.** Only `forestcare_rover/config/` changes: drivers and remaps, mounts,
  gamepad, gateway settings.

## Tests

```bash
uv run pytest ros2/forestcare_gateway/test      # gateway, no ROS needed (a few seconds)
uv run pytest tests/test_gateway_journey.py tests/test_localization_journey.py   # through the Forest Care API
uv run pytest -m ros                            # live ROS 2 nodes; needs ROS 2 (Kilted) and colcon build
```

## Status

- **Tested** with the simulated rover and with real ROS 2 (Kilted) tooling: rosbag2 MCAP
  files, `ros2 bag play`, the live node, teleoperation, recording, conversion, upload, and
  the dashboard.
- **Not yet done:** any drive with physical hardware. Driver settings, mounting offsets,
  clock offsets and the localization accuracy must be measured on the real rover.
