# GUR-166: Gazebo, TurtleBot3, and Nav2 headless on a CPU-only container host

**Question.** Can Gazebo with the TurtleBot3 Waffle and Burger simulations run
headless, alongside Nav2, in an unprivileged Docker container on a CPU-only
amd64 host? Can that setup finish a short there-and-back navigation mission in
`turtlebot3_world` within 2700 s? Gazebo Harmonic is the version paired with
ROS 2 Jazzy.

**Status.** This is desk research checked against upstream sources, package
indexes, and source code on 2026-10-16. No simulation was run. This note
records options, costs, and unknowns. It does not choose a design.

## Short answer

- **Probably yes, but no measurement confirms it on our hosts.** Nav2's own
  Jazzy CI runs a headless TurtleBot3 Waffle in Gazebo Harmonic with Nav2 and
  drives one navigate-to-pose goal in the TurtleBot3 world. Each test has a
  180 s timeout. The test runs in a Docker executor that has no GPU (4 vCPU,
  8 GB). The 2700 s budget is about 15 times that timeout.
- **Rendering cannot be avoided, even for the Burger.** The TurtleBot3 lidar is
  a `gpu_lidar` sensor that ogre2 renders. On a host without a GPU, Mesa
  llvmpipe does the rendering in software.
- **The main cost and risk is the camera.** The ROBOTIS `turtlebot3_waffle`
  model renders a 1920×1080 camera at 30 Hz. Gazebo blocks the simulation step
  while a sensor frame renders. Use the `waffle_pi` model (640×480) or the Nav2
  minimal model (320×240 depth at 5 Hz) instead.
- **The stock ROBOTIS launch file does not run headless.** It always starts
  the GUI client and also loads ground-plane and sun models from Gazebo Fuel
  over the network.

## 1. Packages and upstream images (exact versions)

Versions come from the `packages.ros.org` index for `noble/main/binary-amd64`,
read on 2026-10-16.

| Package (`ros-jazzy-…`) | Version | Contents |
|---|---|---|
| `turtlebot3-gazebo` | `2.3.7-1noble.20261006.041329` | ROBOTIS worlds, models, launch files, and bridge configuration |
| `turtlebot3-simulations` | `2.3.7-1noble.20261006.043915` | Metapackage |
| `turtlebot3-description` | `2.3.6-1noble.20260911.051522` | The same version the `turtlebot3-bringup` service pins |
| `ros-gz` | `1.0.24-1noble.20261006.041805` | Metapackage: `ros-gz-bridge`, `ros-gz-image`, `ros-gz-sim`, `ros-gz-sim-demos` (each `1.0.24`) |
| `gz-sim-vendor` | `0.0.13-1noble.20260905.073557` | Vendors **gz-sim8 8.15.0** (Harmonic) |
| `gz-rendering-vendor` | `0.0.7-1noble.20260905.065113` | Vendors **gz-rendering8 8.2.3**. Depends on `libegl1`, `libglx0`, and `libopengl0` |
| `gz-sensors-vendor` | `0.0.6-1noble.20260905.071631` | gz-sensors8 |
| `gz-ogre-next-vendor` | `0.0.5-1noble.20260225.232146` | Ogre-next, the ogre2 engine |
| `nav2-minimal-tb3-sim` | `1.0.1-1noble.20261006.041307` | Nav2's TurtleBot3 simulation. The `jazzy` branch head is already `1.0.3` |
| `nav2-bringup` / `navigation2` | `1.3.13` | Nav2 launch files, parameters, and the `tb3_sandbox` map |
| `simulation` / `desktop-full` | `0.11.0` | REP-2001 variants. Both include `ros_gz`. Neither includes `turtlebot3_gazebo` or Nav2 |

Gazebo's ROS installation page lists Harmonic as the recommended version for
Jazzy, delivered as ROS vendor packages, and installs it with
`apt-get install ros-jazzy-ros-gz`.
[gazebosim.org/docs/harmonic/ros_installation](https://gazebosim.org/docs/harmonic/ros_installation/)
The `turtlebot3_gazebo` 2.3.7 package depends on `gz_sim_vendor`,
`ros_gz_bridge`, `ros_gz_image`, and `ros_gz_sim`. This is its `package.xml` on
the `jazzy` branch at commit `45633014a14e8f438495b532a723e4ad45cbbd31`. The
ROBOTIS e-Manual Jazzy tab documents only a source build
(`git clone -b jazzy …turtlebot3_simulations`). The apt package exists anyway.
[emanual.robotis.com simulation](https://emanual.robotis.com/docs/en/platform/turtlebot3/simulation/)

**Upstream images.** Docker Hub was read on 2026-10-16. Sizes are compressed
amd64 sizes.

| Image | Size | Contents |
|---|---|---|
| `osrf/ros:jazzy-simulation` | ~732 MB | `ros_base` and `ros_gz` (Harmonic) |
| `osrf/ros:jazzy-desktop-full` | ~1.42 GB | Adds RViz, perception, and `ros_gz_sim_demos` |
| `ros:jazzy-ros-base` | ~298 MB | The official library image. Has no Gazebo |
| `ghcr.io/ros-navigation/navigation2:jazzy` | Not measured | Nav2's CI image. It is a development build image, not a runtime image |

None of these images includes `turtlebot3_gazebo`, so a recipe would install it
with apt on top. The Docker Hub `gazebo` library image stops at Gazebo Classic
`gzserver11` (last updated 2025-04), and no official Harmonic image exists.

## 2. Headless operation and camera rendering without a GPU

### How Gazebo renders without a display

- `gz sim -s` runs only the server, and `--headless-rendering` selects EGL.
  This is the gz-sim 8.15.0 `cmdsim.rb.in` help text.
- The Gazebo headless-rendering page says EGL is "only available with OGRE2"
  and that "it is possible to use a machine without a GPU, in which case OGRE
  will revert to software rendering". It recommends a GPU when sensors need a
  render engine.
  [gazebosim.org/api/sim/8/headless_rendering](https://gazebosim.org/api/sim/8/headless_rendering.html)
- **The server falls back to EGL automatically.** In gz-rendering 8.2.3,
  `Ogre2RenderEngine::CreateContext()` calls `XOpenDisplay`. When that fails,
  it logs "Unable to open display … Trying to run in headless mode" and then
  uses Ogre's "Headless EGL / PBuffer" interface (`ogre2/src/Ogre2RenderEngine.cc`
  around lines 536–553 and 829–836). A container without `DISPLAY` therefore
  takes the EGL path without the flag.
- The Gazebo HPC guide states that EGL "works with devices `/dev/dri/card?` and
  `/dev/dri/renderD???`". It also states that if `EGL_MESA_device_software` or
  `llvmpipe` appears, "EGL works, but is not HW-accelerated", and that
  "`llvmpipe` is software rendering (slow)". The guide also covers GLX under
  Xvfb as an alternative, where "sensor rendering happens in off-screen
  buffers, so sensors are not affected".
  [gazebosim.org/api/sim/10/hpc_clusters](https://gazebosim.org/api/sim/10/hpc_clusters.html)

### What this means for an unprivileged container

llvmpipe needs no device nodes, so `--privileged` and `/dev/dri` are not
required in principle. Two routes exist:

- **(a) EGL with Mesa's software device.** No X server is needed.
- **(b) Xvfb with GLX on llvmpipe.** This is the older, widely used route.

The image needs a Mesa EGL vendor and DRI drivers: `libegl-mesa0` and
`libgl1-mesa-dri`. `gz-rendering-vendor` depends only on the glvnd front ends
(`libegl1`, `libglx0`). A `--no-install-recommends` image might therefore lack
a working EGL or GL implementation. **Unknown:** whether the Jazzy debs pull in
Mesa transitively. Check with `eglinfo` or `glxinfo -B` inside the built image.

### Which sensors need rendering

The sensor list comes from the model SDFs in `turtlebot3_gazebo` 2.3.7.

| Model | Lidar | Camera |
|---|---|---|
| `turtlebot3_burger` | `gpu_lidar`, 360 samples, **5 Hz** | None |
| `turtlebot3_burger_cam` | `gpu_lidar`, 5 Hz | 320×240 at 30 Hz |
| `turtlebot3_waffle` | `gpu_lidar`, 360 samples, **10 Hz** | **1920×1080 R8G8B8 at 30 Hz** |
| `turtlebot3_waffle_pi` | `gpu_lidar`, 10 Hz | 640×480 at 30 Hz |
| Nav2 `gz_waffle.sdf.xacro` | `gpu_lidar`, 5 Hz | Depth camera, 320×240 at 5 Hz. Not bridged |

Every model has a `gpu_lidar`, so the Sensors system must start ogre2 even for
the Burger. In gz-sim 8.15.0, `Sensors::PostUpdate` starts rendering whenever
a `GpuLidar` or camera component exists. The gz-sim 8.15.0 `src/systems`
directory has no system that produces scans without rendering. The world files
pin `<render_engine>ogre2</render_engine>`.

### Cost of camera rendering

No measured numbers were found in primary sources. The cost structure is
documented in the code:

- **Rendering throttles physics.** The gz-sim 8.15.0 `Sensors.cc` comment says
  "The caller of PostUpdate will be blocked if there is a rendering operation
  currently ongoing, until that completes". Rendering time per simulated
  second therefore caps the real-time factor directly.
- **The Waffle camera is the expensive case.** Per simulated second, it needs
  30 software-rendered 2.07 MP frames plus 10 lidar renders. The image bridge
  then moves about 6.2 MB per frame (about 187 MB/s at RTF 1) through
  `ros_gz_image`, `image_bridge`, and DDS.
- **The Waffle Pi camera is far cheaper.** At 0.3 MP it costs about 1/6.75 of
  the Waffle's pixel work and bridge traffic. Its 640×480 resolution matches
  the `v4l2-camera` service's default.
- **The camera has no frame-rate setting outside the model.** Lowering it means
  editing the model SDF (`<update_rate>`, `<width>`, `<height>`) or using a
  different model.

## 3. Achievable real-time factor and its limits

**Evidence that it works.** In navigation2 1.3.13, the `.circleci/config.yml`
`release_test` job runs on `ghcr.io/ros-navigation/navigation2:jazzy` with a
Docker executor of `resource_class: large`. In the default `build_and_test`
workflow, no regex skips `nav2_system_tests`. CircleCI documents the Docker
`large` class as 4 vCPU and 8 GB RAM.
[circleci.com docs: Docker execution environment](https://circleci.com/docs/guides/execution-managed/using-docker/)

The system test is `nav2_system_tests/src/system/test_system_launch.py`. It
works as follows:

1. It starts `gz sim -r -s tb3_sandbox.sdf.xacro`.
2. It spawns `gz_waffle.sdf.xacro`.
3. It starts the full Nav2 stack with `use_sim_time`.
4. It navigates from (−2.0, −0.5) to (0.0, 2.0) in the TurtleBot3 world.

Each test variant has a 180 s timeout, set in `nav2_system_tests/src/system/CMakeLists.txt`.
Variants include DWB with NavFn and Regulated Pure Pursuit with A*. This is the
closest primary evidence that the whole stack completes a navigation mission on
a CPU-only container. **Not verified:** the current pass rate of that CI job.

**Required real-time factor.** This is an estimate, not a measurement.

- The `nav2-navigation` service caps DWB at `max_vel_x: 0.3`, and Nav2's
  default MPPI caps at `vx_max: 0.5`.
- A there-and-back mission across the roughly 4 m × 4 m TurtleBot3 world is
  about 8–10 m of path plus turns.
- That is on the order of 60–150 s of simulated time.
- After 60–120 s of wall-clock startup, 2700 s of wall time leaves room for an
  RTF as low as about 0.05–0.1.
- The Nav2 CI evidence implies an RTF well above that floor, at least for the
  Nav2 minimal sensor set.

**What limits the real-time factor:**

1. **Sensor rendering on llvmpipe.** It blocks the simulation step, as
   described above. It dominates with the Waffle 1080p camera and is modest
   with the 5–10 Hz lidar and small cameras.
2. **The physics step rate.** ROBOTIS `turtlebot3_world.world` uses
   `max_step_size` 0.001 with `real_time_factor` 1, which is 1000
   physics steps per simulated second. Nav2's `tb3_sandbox` uses 0.003. The
   `<physics type="ode">` attribute and `<ode>` solver block are effectively
   ignored. gz-sim 8 loads `gz-physics-dartsim-plugin` unless `--physics-engine`
   or `<engine>` overrides it (`src/systems/physics/Physics.cc` around line 926).
3. **The `real_time_factor` 1 setting.** It caps the simulation at RTF 1 even
   on a fast host. Raise it only to speed up runs. It does not help a slow host.
4. **CPU contention with Nav2.** MPPI, Nav2's default controller, is the
   heavy option. The service repository's DWB at 10 Hz is lighter. The
   controller loop uses `rclcpp::WallRate` (`nav2_controller/src/controller_server.cpp`
   around line 503), so at RTF below 1 it runs more often per simulated second.
   That adds CPU load and can trigger "control loop missed its desired rate"
   warnings. Timeouts that use the ROS clock stretch with simulated time.
5. **Bridge and DDS copying.** This cost is large only for full-resolution
   images.

## 4. Robot duties the simulation already covers

Sources: the `turtlebot3_gazebo` 2.3.7 launch files, bridge YAML files, and
model SDFs.

| Duty on the real robot | Real source (service repository) | Source in simulation (ROBOTIS `turtlebot3_gazebo`) |
|---|---|---|
| `/cmd_vel` to the wheels | `turtlebot3-bringup`: `turtlebot3_node` to OpenCR, `TwistStamped` | `gz-sim-diff-drive-system`, bridged from `geometry_msgs/TwistStamped` to `gz.msgs.Twist` |
| `/odom` and the `odom → base_footprint` transform | `turtlebot3_node`, wheels fused with the IMU | DiffDrive: `odom_topic odom`, `tf_topic /tf`, 30 Hz, bridged |
| `/scan` in `base_scan` | LDS driver (`hls_lfcd_lds_driver` or `ld08_driver`) | `gpu_lidar` `hls_lfcd_lds`, `gz_frame_id base_scan`, range 0.12–3.5 m, bridged |
| `/tf_static` from the URDF | `robot_state_publisher` | `robot_state_publisher.launch.py`, included by `turtlebot3_world.launch.py` |
| `/imu`, `/joint_states` | `turtlebot3_node` | Imu system at 200 Hz and JointStatePublisher, both bridged |
| `/clock` | None. Real robots use wall time | Bridged `gz.msgs.Clock`. All nodes need `use_sim_time:=true` |
| Camera images | `v4l2-camera`: `/image` and `/camera_info`, 640×480 `rgb8` | Waffle and Waffle Pi publish `/camera/image_raw` through `ros_gz_image image_bridge` and `/camera/camera_info` through the bridge. The Burger has no camera |

The simulation therefore replaces the whole `turtlebot3-bringup` service
(OpenCR and LDS) and the `v4l2-camera` service. The topic names, QoS, and
clock differ, as follows:

- **Camera topics.** The simulated camera publishes on `/camera/image_raw`,
  not `/image`. It needs a remap or a relay.
- **QoS.** `ros_gz_bridge` publishes with the default reliable QoS. The
  service declares `/scan` as best-effort. A best-effort subscriber still
  matches a reliable publisher.
- **Clock.** The `nav2-localization` and `nav2-navigation` images hard-code
  `use_sim_time:=false` in their `CMD`. A simulated run would need to override
  that setting.

## 5. Practical blockers in the stock launch files

- **The GUI client always starts.** `turtlebot3_world.launch.py` 2.3.7 always
  includes a `gz_sim.launch.py` with `-g` (the GUI). Both server and GUI set
  `on_exit_shutdown: true`, so a GUI that cannot start in a display-less
  container may bring down the whole launch. A headless run would start
  `gz sim -r -s <world>` directly, or use a custom launch file.
- **The world loads models from the network.** `turtlebot3_world.world`
  includes `https://fuel.gazebosim.org/1.0/OpenRobotics/models/Ground Plane`
  and `…/Sun`, so the first start needs network access to Fuel. To run
  offline, pre-populate the Fuel cache (`~/.gz/fuel`) in the image or inline
  those models. Nav2's `tb3_sandbox.sdf.xacro` defines its own sun and ground
  plane and has no Fuel dependency.
- **Nav2's world drops the SceneBroadcaster when headless.** With
  `headless:=true`, `tb3_sandbox.sdf.xacro` omits the SceneBroadcaster.
  Nav2 does this because only a GUI needs it, which saves work.
- **`TURTLEBOT3_MODEL` must be set.** `spawn_turtlebot3.launch.py` reads it
  from the environment and raises `KeyError` if it is unset.
- **Multi-container runs need gz-transport discovery.** If Gazebo and the
  bridge run in separate containers, gz-transport discovery must work between
  them, for example through a shared network namespace or `GZ_IP`. **Not
  investigated.**

## Options (not ranked)

1. **ROBOTIS `turtlebot3_gazebo` 2.3.7 with a headless launch file of our own.**
   - Waffle Pi (640×480 camera) or Burger (no camera).
   - Matches the `turtlebot3-bringup` and `v4l2-camera` interfaces most closely.
   - Needs Fuel caching and a launch file without `-g`.
2. **Nav2 `nav2_minimal_tb3_sim` 1.0.1 with `tb3_simulation_launch.py headless:=True`.**
   - Waffle with lidar plus a 5 Hz depth camera. Self-contained world.
   - Exercised in Nav2's CPU CI.
   - Bridges no RGB camera, so it does not replace `v4l2-camera`.
3. **Either option with a model SDF override.** Lower the camera's resolution
   or rate, or the lidar rate, to trade fidelity for real-time factor.

## Unknowns to probe cheaply before a design choice

- **Measured RTF on our pipeline hosts.** Start `gz sim -s -r` for each model
  (Burger, Waffle Pi, Waffle) with and without Nav2, and read
  `gz topic -e -t /stats` (the `real_time_factor` field) for about 60 s.
- **Whether EGL starts in our unprivileged container without `/dev/dri`.**
  Compare EGL on llvmpipe with Xvfb and GLX, and identify which Mesa packages
  the image needs.
- **Peak memory** with Nav2 and Gazebo together, compared with the
  agent-container limits.
- **Whether Nav2's Jazzy system tests are currently green.** This determines
  how much weight the CI evidence carries.
