#!/usr/bin/env bash
set -euo pipefail

target="prepare_parquet_to_gcs.py build"

python3 - <<'PY'
import os
import signal

target = ("prepare_" + "parquet_to_gcs.py build").encode()
killed = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        comm = open(f"/proc/{name}/comm", encoding="utf-8").read().strip()
        cmd = open(f"/proc/{name}/cmdline", "rb").read().replace(b"\0", b" ")
    except Exception:
        continue
    if comm.startswith("python") and target in cmd:
        try:
            os.kill(int(name), signal.SIGKILL)
            killed.append(name)
        except ProcessLookupError:
            pass
print("killed", " ".join(killed))
PY

sleep 2
rm -f /mnt/localssd/work/build_parquet.log
rm -f /mnt/localssd/work/build_parquet.err.log

cd /home/admin/ashare_pipeline
nohup .venv/bin/python data_transfer/prepare_parquet_to_gcs.py build \
  --config data_transfer/cloud_parquet_config.yaml \
  > /mnt/localssd/work/build_parquet.log \
  2> /mnt/localssd/work/build_parquet.err.log &

sleep 2
pgrep -f "$target" | sort -n | head -n 1 > /mnt/localssd/work/build_parquet.pid
echo "started $(cat /mnt/localssd/work/build_parquet.pid)"
ps -eo pid,ppid,pcpu,pmem,etime,cmd | grep prepare_parquet_to_gcs | grep -v grep | head -n 40
