#!/usr/bin/env bash
# PROTOTYPE (GUR-182), throwaway. Brings the stand-in cluster up, pins one
# multi-arch ROS image by per-arch digest, deploys one pod per host, applies
# the CD pass rule, tears down, and prints timings. Usage: run.sh OUT_DIR
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=$1; mkdir -p "$out"
cd "$here"
dc() { docker compose -f "$here/compose.yaml" "$@"; }
t0=$(date +%s)
stamp() { echo "[$(( $(date +%s) - t0 ))s] $*" | tee -a "$out/timeline.txt"; }

stamp "compose up"
dc up -d 2>&1 | tail -n 3
stamp "containers started"

kport=$(dc port xycar 6443 | cut -d: -f2)
rport=$(dc port registry 5000 | cut -d: -f2)
until [ -s out/kubeconfig.yaml ]; do sleep 1; done
sed "s#https://127.0.0.1:6443#https://127.0.0.1:$kport#" out/kubeconfig.yaml > "$out/kubeconfig"
export KUBECONFIG=$out/kubeconfig
stamp "server API kubeconfig written"

declare -A ready=()
while [ ${#ready[@]} -lt 4 ] && [ $(( $(date +%s) - t0 )) -lt 180 ]; do
    for n in xycar waffle burger orin; do
        [ -n "${ready[$n]:-}" ] && continue
        s=$(kubectl get node "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
        if [ "$s" = True ]; then ready[$n]=1; stamp "node $n Ready"; fi
    done
    sleep 2
done
kubectl get nodes -L sdi/host,kubernetes.io/arch -o wide > "$out/nodes.txt"

stamp "seed registry"
docker buildx imagetools create --tag "127.0.0.1:$rport/sdi/ros-proto:jazzy" docker.io/library/ros:jazzy-ros-base > "$out/seed.txt" 2>&1
docker buildx imagetools inspect --raw "127.0.0.1:$rport/sdi/ros-proto:jazzy" > "$out/index.json"
dig() { python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["manifests"]; print(next(x["digest"] for x in m if x.get("platform",{}).get("architecture")==sys.argv[2] and x["platform"].get("os")=="linux"))' "$out/index.json" "$1"; }
amd=$(dig amd64); arm=$(dig arm64)
stamp "seeded: amd64=$amd arm64=$arm"

# host -> pinned digest. Every node is amd64; arm64 hosts get the arm64 digest.
declare -A pin=([xycar]=$amd [waffle]=$arm [burger]=$arm [orin]=$arm)
for h in xycar waffle burger orin; do cat <<EOF
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: proto-$h}
spec:
  replicas: 1
  selector: {matchLabels: {app: proto-$h}}
  template:
    metadata: {labels: {app: proto-$h}}
    spec:
      hostNetwork: true
      nodeSelector: {sdi/host: $h}
      containers:
        - name: talker
          image: registry:5000/sdi/ros-proto@${pin[$h]}
          command: [bash, -c, 'source /opt/ros/jazzy/setup.bash && exec ros2 topic pub -r 10 /chatter_$h std_msgs/msg/String "{data: hi}"']
EOF
done > "$out/manifest.yaml"

ns=sdi-proto
kubectl create namespace $ns >/dev/null
stamp "apply"
kubectl apply -n $ns -f "$out/manifest.yaml" >/dev/null

declare -A running=()
deadline=$(( $(date +%s) + 900 ))
while [ ${#running[@]} -lt 4 ] && [ "$(date +%s)" -lt $deadline ]; do
    for h in xycar waffle burger orin; do
        [ -n "${running[$h]:-}" ] && continue
        line=$(kubectl get pod -n $ns -l app=proto-$h -o jsonpath='{.items[0].status.phase} {.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
        case $line in
            Running*) running[$h]=1; stamp "pod $h Running" ;;
            *ErrImagePull*|*ImagePullBackOff*|*CrashLoopBackOff*) running[$h]=fail; stamp "pod $h $line" ;;
        esac
    done
    sleep 2
done
stamp "settle 60s"
sleep 60
kubectl get pods -n $ns -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,IMAGEID:.status.containerStatuses[0].imageID,WAIT:.status.containerStatuses[0].state.waiting.reason' | tee "$out/pods.txt"
for h in xycar waffle burger orin; do echo "$h pinned ${pin[$h]}"; done | tee "$out/pins.txt"
kubectl describe pods -n $ns > "$out/describe.txt"
kubectl get events -n $ns --sort-by=.lastTimestamp > "$out/events.txt"
docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $(dc ps -q) | tee "$out/stats.txt"
stamp "teardown namespace"
kubectl delete namespace $ns --wait >/dev/null
stamp "namespace deleted"
