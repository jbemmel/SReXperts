#!/bin/bash
# Copyright 2023 Nokia
# Licensed under the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause


sudo clab dep -t lab/certs.clab.yaml -c

targets="$(docker ps -f label=containerlab=certs --format {{.Names}} | paste -s -d, -)"

while ! gnmic -a $targets --insecure -u admin -p admin cap --timeout 5s 2> /dev/null 
do 
echo "[$(date)] - target(s) not ready yet"
sleep 5
done