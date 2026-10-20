# PROTOTYPE (GUR-182): K3s stand-in cluster findings

Throwaway. Measured 2026-10-20 on WSL2 (kernel 6.18, Docker 29.8, 32 CPUs),
`rancher/k3s:v1.33.5-k3s1`, image `ros:jazzy-ros-base` copied as an index into
the prototype registry and pinned by per-arch digest.

- Whole arm64 K3s nodes (`platform: linux/arm64`) fail: the agent exits with
  `kube-proxy exited: iptables is not available on this host`.
- An amd64 node pulls an arm64 per-arch digest through the `registries.yaml`
  mirror and runs it under binfmt/QEMU (`uname -m` = aarch64). `imageID`
  reports the pinned digest. A `ros2 topic pub` pod held Running with 0
  restarts over the 60 s settle at about 3% CPU.
- Fresh start of 4 amd64 nodes (server doubles as `xycar`): all Ready in 22 s.
  Idle: server about 500 MiB, each agent about 90 MiB.
- First pull plus start of the arm64 ROS image: 11 s. Namespace teardown: 10 s.
- `compose down` leaves each node's 4 anonymous volumes dangling (about 3 GB
  after one run). `compose rm -fsv <k3s services>` removes exactly those.
