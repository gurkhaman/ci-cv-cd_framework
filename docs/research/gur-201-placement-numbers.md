# GUR-201: sourced numbers for the robot-edge placement facts (C-05, C-06)

Question: which sourced numbers for host capacity, link bandwidth and service demand would the robot-edge placement facts declare for testcase combinations C-05 (TurtleBot3 Waffle Pi + Jetson Orin Nano) and C-06 (TurtleBot3 Burger + Jetson Orin Nano)?

Method: every row cites the page it was read from. A row without a primary source is marked ESTIMATE with its reasoning. "Derived" means arithmetic on cited inputs. Sources were read on 2026-10-27; one Wi-Fi source (R-14) was only seen as a search-result summary and is flagged.

## Table

| ID | Item | Value | Unit | Status | Source |
|---|---|---|---|---|---|
| R-01 | Robot host: CPU | 4 x Cortex-A72 at 1.5 GHz = 4000 | millicores | Sourced | [Pi 4 datasheet](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf) |
| R-02 | Robot host: memory (4 GB model) | 4096 | MiB | Sourced | same datasheet (1/2/4 GB LPDDR4; the 4 GB size is a repo assumption, see doubts) |
| R-03 | Reserve: K3s agent CPU | 100 (10% of one core; the same page's agent section says 5%, i.e. 50) | millicores | Sourced | [K3s resource profiling](https://docs.k3s.io/reference/resource-profiling) |
| R-04 | Reserve: K3s agent memory | 268 | MiB (page says "268 M") | Sourced | same page, Pi 4B row |
| R-05 | Reserve: OS system-reserved CPU and memory | 100 millicores, 100 MiB | - | Sourced as a convention only (not a Pi measurement) | [Kubernetes reserve-compute-resources](https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/) example values |
| R-06 | Reserve: hard-eviction headroom | 100 | MiB | Sourced as a convention | [GKE plan-node-sizes](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/plan-node-sizes) ("an additional 100 MiB ... for Pod eviction thresholds") |
| R-07 | Reserve total (derived R-03+R-05, R-04+R-05+R-06) | CPU 200, memory 468 | millicores, MiB | Derived | rows above |
| R-08 | Robot allocatable (derived R-01-R-07) | CPU 3800, memory 3628 | millicores, MiB | Derived | rows above |
| R-09 | Optional derate: Pi 4 thermal throttling | No number | - | No citable number | [Raspberry Pi docs](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html): Arm cores progressively throttled from 80 C, cores and GPU throttled at 85 C, "no soft limit defined", no Pi 4 clock figures. [Datasheet](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf) section 5.6: governor keeps the CPU under 85 C; ambient 0-50 C recommended; "further cooling may be needed" for continuous full load |
| R-10 | Waffle Pi and Burger battery | LiPo 11.1 V, 1800 mAh, 19.98 Wh, 5C | V, mAh, Wh | Sourced | [ROBOTIS e-Manual, features](https://emanual.robotis.com/docs/en/platform/turtlebot3/features/) |
| R-11 | Expected operating time | Waffle Pi 2 h, Burger 2 h 30 min | h | Sourced | same page. Implied mean draw 19.98 Wh / 2 h ~ 10 W (Waffle Pi), ~ 8 W (Burger): derived, and the e-Manual states no power figure |
| R-12 | Orin Nano dev kit power | Mains adapter, 19 V; carrier-board DC jack accepts 9-20 V; module power modes 7 / 15 / 25 W (8 GB) | V, W | Sourced; adapter wattage not stated | [Quick Start](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html) ("Power supply (19 V)"); [Jetson Orin page](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/); the 9-20 V and 3.5 A jack figures are from the [carrier board spec SP-11324-001](https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf), seen only through a search summary, not opened |
| E-01 | Edge host: CPU | 6 x Cortex-A78AE = 6000 | millicores | Sourced | [NVIDIA Jetson Orin](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/) (same 6 cores on 4 GB and 8 GB) |
| E-02 | Edge host: memory | 8 GB LPDDR5, 128-bit = 8192 | MiB | Sourced (8 GB module) | same page; the [dev kit page](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/) lists only 8 GB. The 4 GB module is 4096 MiB, 64-bit, if a 4 GB variant is ever used. Memory is shared with the GPU |
| L-01 | Link: PHY rate of the Pi 4 radio | 433 (CYW43455, 1x1, 802.11ac, 80 MHz, MCS9) | Mbit/s | Sourced | [Pi 4 datasheet](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf) (802.11 b/g/n/ac); 433.3 Mbit/s peak from the [Infineon CYW43455 page](https://www.infineon.com/cms/de/product/wireless-connectivity/airoc-wi-fi-plus-bluetooth-combos/cyw43455/), seen through a search summary |
| L-02 | Link: PHY to TCP/UDP derating | 65-70% of PHY on a clean link, large transfer | fraction | Sourced | [Cisco 802.11ac throughput testing](https://www.cisco.com/c/en/us/support/docs/wireless-mobility/wireless-lan-wlan/212892-802-11ac-wireless-throughput-testing-and.pdf): "assuming a 65% MAC efficiency"; "160 to 200 Mbps possibly (65 to 70% of protocol efficiency)" |
| L-03 | Link: ceiling for a 433 Mbit/s PHY (derived L-01 x L-02) | ~ 280-300 | Mbit/s | Derived, best case | rows above |
| L-04 | Link: measured Pi 4 5 GHz iperf3 | ~ 64 (Tom's Hardware, near the router); ~ 58 to 114 (MagPi, line of sight) | Mbit/s | Weak: secondary, and I saw both only in a search-result summary | [Tom's Hardware](https://www.tomshardware.com/news/raspberry-pi-5-wi-fi-faster); [MagPi via DEV](https://dev.to/admantium/ros-with-raspberry-pi-improving-wifi-upload-speed-4dol) |
| L-05 | Link: ROS 2 DDS over Wi-Fi | Default Fast DDS delivered a 512 KB message at 1.16 Hz on an ideal 802.11ac link (433 Mbit/s PHY); tuned DDS 28.89 Hz at 512 KB; 30 Hz of ~1 MB frames not tested | Hz | Sourced | [arXiv 2508.11366](https://arxiv.org/html/2508.11366v1), Table II (Humble, Fast DDS 2.6.9, laptops, not Pi) |
| S-01 | turtlebot3-bringup demand | ~ 150 m, ~ 150 MiB | millicores, MiB | ESTIMATE | See notes |
| S-02 | nav2-localization (AMCL + map server) demand | ~ 300 m, ~ 150 MiB | millicores, MiB | ESTIMATE | See notes |
| S-03 | nav2-navigation demand | ~ 1500 m (about 1.5 cores), ~ 400 MiB | millicores, MiB | ESTIMATE | See notes |
| S-04 | v4l2-camera demand | ~ 300 m, ~ 100 MiB | millicores, MiB | ESTIMATE | See notes |
| S-05 | audio-capture demand | ~ 50 m, ~ 80 MiB | millicores, MiB | ESTIMATE | See notes |
| S-06 | vosk-speech memory | ~ 300 (model) plus Python, ~ 400 total | MiB | Model part sourced, total ESTIMATE | [Vosk models](https://alphacephei.com/vosk/models): small models "require about 300Mb of memory in runtime", `vosk-model-small-en-us-0.15` is 40M, "for Android and RPi" |
| S-07 | vosk-speech CPU | ~ 1000 m while decoding, ~ 0 idle | millicores | ESTIMATE | See notes |
| S-08 | opencv-face-recognition: reference inference time | YuNet 5.43 ms at 160x120 and SFace 78.83 ms at 112x112 on a Pi 4B CPU | ms per call | Sourced (not Orin; not this input size) | [opencv_zoo benchmark at the pinned commit](https://github.com/opencv/opencv_zoo/blob/7e062e54cf5410c09b795ff71b4a255e58498c79/benchmark/README.md) (column "CPU-RPI"; the page does not name the board, so the Pi 4B mapping is from the Pi section of the same README, read via search summary); Jetson column is Nano B01 GPU: 12.18 / 24.88 ms |
| S-09 | opencv-face-recognition demand | ~ 1000 m, ~ 300 MiB | millicores, MiB | ESTIMATE | See notes |
| P-01 | v4l2-camera `/image` rate | 640 x 480 x 3 B = 921,600 B per frame; x 30 fps = 27.6 MB/s = 221 | Mbit/s | Derived; the 30 fps is an assumption | See notes: the SDI.md gives size and encoding, not frame rate or Mbit/s |
| P-02 | `/scan` (LDS-01) | ~ 360 points; ~ 2.9 KB per scan at 5 Hz ~ 14.5 KB/s = 0.12 | Mbit/s | Derived | [LDS-01 specs](https://emanual.robotis.com/docs/en/platform/turtlebot3/appendix_lds_01/) (300 rpm, 1.8 kHz) |
| P-03 | `/odom` | ~ 0.7 KB per message at ~ 20 Hz ~ 14 KB/s = 0.11 | Mbit/s | Derived, rate ESTIMATE | [turtlebot3_node](https://github.com/ROBOTIS-GIT/turtlebot3/blob/da785b7201d317e6e2a662e41bb3d3fd50ebd503/turtlebot3_node/src/odometry.cpp) (publishes from the joint-state callback, which fires on a 50 ms timer in `turtlebot3.cpp`; message size from the Odometry definition) |
| P-04 | `/tf` | ~ 0.1 KB at ~ 20 Hz ~ 2 KB/s = 0.016; `/tf_static` one-shot | Mbit/s | Derived, rate ESTIMATE | same |
| P-05 | `/audio_listener/audio` | 16,000 samples/s x 2 B = 32 KB/s = 0.256 (payload; 2000 B per message, 16 messages/s) | Mbit/s | Derived | `service-repository/audio-capture/SDI.md` lines 42-43 and 65-66 |
| P-06 | `/map` (one-shot, transient_local) | 384 x 384 cells x 1 B = 147,456 B ~ 1.2 once | Mbit once | Derived | `service-repository/nav2-localization/map.pgm` header (384 x 384, 0.05 m/pixel), file size 147,508 B |
| P-07 | Small topics (`/cmd_vel`, `/amcl_pose`, `/speech/text`, `/face_recognition/name`) | < 0.05 combined | Mbit/s | ESTIMATE | message sizes under 0.5 KB at under 20 Hz |
| Q-01 | SLAM and Navigation run on the Remote PC, bringup on the SBC | quoted below | - | Sourced | [ROBOTIS e-Manual, SLAM](https://emanual.robotis.com/docs/en/platform/turtlebot3/slam/); [Navigation](https://emanual.robotis.com/docs/en/platform/turtlebot3/navigation/) |

### Quote for Q-01

From the e-Manual Navigation page: "Navigation should be run on the Remote PC." and "If Bringup is not running on the TurtleBot3 SBC, launch Bringup." From the SLAM page: "SLAM should be run on the Remote PC." and "If the Bringup is not running on the TurtleBot3 SBC, launch the Bringup first." The Quick Start page adds: "The content in this chapter is for the initialization of the `Remote PC`" and "Do not complete these instructions on the TurtleBot3 platform itself." (the manual lists Gazebo, Cartographer and Navigation2 as Remote PC dependencies).

## Notes per row group

### Robot host (R-01 to R-09)

The reserve uses only documented numbers and does not double count. The K3s agent measurement (268 MiB, 5-10% of one core on a Pi 4B) already covers the kubelet, containerd and the K3s agent, so it stands in for kube-reserved. The Kubernetes example `{cpu: 100m, memory: 100Mi}` stands in for system-reserved (OS daemons and kernel). The Kubernetes page says its values are examples, not recommendations, and gives no formula for a Pi. The K3s profile ran Raspberry Pi OS 11, not Ubuntu, so it is a floor for Ubuntu 24.04.

The K3s requirements page (agent minimum 1 core and 512 MB; [source](https://docs.k3s.io/installation/requirements)) is a sizing minimum for the whole box, so it is not added on top.

I found no citable idle memory figure for Ubuntu 24.04 on a Pi 4 and none for ROS 2 DDS on a Pi. Ubuntu's own minimum (1.5 GB for ISO installs, 1 GB for cloud images; [source](https://ubuntu.com/server/docs/reference/installation/system-requirements/)) is a requirement, not a footprint. ROS 2 itself measured about 4 MB RSS without DDS ([ROS Discourse](https://discourse.openrobotics.org/t/ros2-memory-usage-no-dds/21206)); the same thread says a real DDS "will grow considerably" with no number. DDS memory is charged to each service (S-01 to S-09), because each container runs its own DDS participant; it is not in the node reserve.

Thermal: the Pi 4 has no soft limit; throttling begins at 80 C and is progressive to 85 C. No page gives a Pi 4 clock or percentage figure, so no derate number is declared. A placer that wants one must measure it.

### Power (R-10 to R-12)

Battery energy is 19.98 Wh on both robots. Whether the SBC, OpenCR, lidar and motors fit within it is not a placement-capacity number; the e-Manual gives operating time only. The Orin Nano is mains powered, so it carries no battery constraint, only the 7/15/25 W module mode (8 GB). The dev kit's adapter wattage is not stated in the pages I read.

### Edge host (E-01, E-02)

The dev kit is the 8 GB module: NVIDIA's Orin Nano dev kit page lists only "8GB 128-bit LPDDR5, 102 GB/s". The 4 GB module (4 GB, 64-bit, 51 GB/s; modes 7/10/25 W) exists as a separate module. The CPU is the same six cores on both.

### Link (L-01 to L-05)

The Pi 4 radio is 1x1, so the PHY ceiling is 433 Mbit/s, not the 867 Mbit/s two-stream figure. Cisco's 65% MAC efficiency gives about 280 Mbit/s as a best case. Pi 4 measurements are far lower (about 60-115 Mbit/s), but my only sources are secondary and seen as search summaries, so treat L-04 as a pointer, not a declaration. For DDS, the arXiv paper's testbed is two laptops, not a Pi, and its payloads top out at 512 KB, so it brackets but does not cover a 900 KiB image.

### Service demand (S-01 to S-09)

None of the seven `SDI.md` files carries a resource figure (every Evaluation section says "None recorded").

- S-06 is the best-sourced: the Vosk page states about 300 MB runtime memory for small models. CPU for Vosk is not documented, so S-07 is an estimate (assumed: one decoding thread busy while audio arrives).
- S-08 gives Pi 4B reference timings, but at 160x120 and 112x112 inputs, with quantised models. The service sets the YuNet input to the full frame (`setInputSize(message.width, message.height)`, `opencv_face_recognition.py` line 123), so at 640x480 it processes 16 times the pixels. S-09 is therefore an estimate. On a Jetson the OpenCV `dnn` module used by the Debian `python3-opencv` runs on CPU unless built with CUDA, so no GPU benefit is assumed. No Orin Nano timing exists in the pinned zoo benchmark.
- S-02 and S-03: no published per-process CPU for nav2_amcl or Nav2 on a Pi 4. The closest data point: a TurtleBot 4 navigation test with a Pi 4 "ran nearly at 100% CPU usage with 10% navigation success" ([ROS Discourse](https://discourse.openrobotics.org/t/announcement-turtlebot4-rpi5-raspberry-pi-5-intergration-with-turtlebot-5/51737)), without saying which nodes ran. The numbers here come from the config in this repo (AMCL 500-2000 particles; local costmap 3x3 m at 0.05 m, updated at 5 Hz; controller 10 Hz) and are reasoning, not measurement. They are the least certain row group.
- S-01, S-04, S-05: no sources. S-01 is three small nodes plus a state publisher; S-04 is YUYV-to-rgb8 conversion at 30 fps; S-05 is PyAudio plus a Python publisher. The memory figures assume 15-35 MB per DDS participant (Fast DDS 33 MB, Cyclone DDS 15.8 MB in ROS Discourse reports that I saw only as search summaries) plus the runtime. Treat all as order-of-magnitude.

### Publisher data rates (P-01 to P-07)

Check of the camera figure: `service-repository/v4l2-camera/SDI.md` line 43 says "640x480 `rgb8` by default" and line 57 says "Raw images are large; sending them over Wi-Fi to another host can drop frames or saturate the link." Neither line gives a frame rate or 220 Mbit/s. The figure is derived: 640 x 480 x 3 B x 8 x 30 fps = 221.2 Mbit/s. The 30 fps is an assumption: line 65 gives size, `YUYV` and `rgb8`, but no frame rate, and `v4l2_camera` takes the device default. At 15 fps it is 110.6 Mbit/s. At 30 fps it is below the L-03 best-case ceiling (~280-300 Mbit/s) but far above the measured Pi 4 figures in L-04 (~60-115 Mbit/s); at 15 fps it is still above the lower L-04 figure.

Scan, odom and tf use the TurtleBot3 sources above; the rates for odom and tf are inferred from the 50 ms publish timer and the joint-state-driven odometry callback, and are not read from a running robot.

## Open doubts

1. The C-05/C-06 SBC memory size (4 GB) comes from the ticket, not from the e-Manual: the manual says only "Raspberry Pi 4" with no RAM size, and the Pi 4 comes in 1/2/4/8 GB.
2. The e-Manual lists LDS-02 for both robots; `turtlebot3-bringup` defaults to `LDS-01` (`SDI.md` line 71). LDS-02 is 2.3 kHz at 5 Hz or more, so about 460 points per scan; P-02 would be about 0.15 Mbit/s. The services' declared lidar must match the robot.
3. Waffle Pi carries a Raspberry Pi Camera Module v2.1 (CSI); Burger has no camera. `v4l2-camera` is a USB/V4L2 service. For C-06 there is no camera, unless a USB webcam is added, and for C-05 the CSI camera is not a V4L2 UVC device by default. Decide whether the facts for these combinations include a camera.
4. K3s resource-profiling page contradicts itself on Pi 4B agent CPU (5% in the agent section, 10% in the summary). R-03 uses 10%.
5. Ubuntu 24.04 idle memory on a Pi is not documented by any source I could reach. The R-05 100 MiB is a Kubernetes example, not a measurement; the true figure is probably higher. One unverified article says 250-300 MiB. If the reserve is understated, R-08 is optimistic.
6. L-04 and L-01 (the 433.3 Mbit/s peak) and R-12's 9-20 V figure rest on search summaries of the pages, not pages I opened. Re-verify before they become declared numbers.
7. No Wi-Fi source covers a 900 KiB message at 30 Hz over ROS 2 DDS. The arXiv paper's default-DDS result (1.16 Hz at 512 KB on an ideal link) suggests that streaming `/image` raw over Wi-Fi fails in practice without DDS tuning, not merely that it exceeds the bandwidth.
8. S-01 to S-05, S-07 and S-09 need one measurement run on a real Pi 4 and the Orin Nano to replace the estimates. The cheapest way is to run each service image alone for 60 s under `docker stats`.
