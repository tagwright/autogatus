# Live harness: the autogatus.offline label

Operator-run. autogatus's CI runs on GitHub-hosted runners, which have no Docker
socket, so this leg is run by hand and attested in `LAST-RUN`.

    python3 test/integration/harness.py

It needs python3 (standard library only), git, and a docker CLI allowed to start
a privileged container. A run takes about ten minutes. It refuses to run
with uncommitted changes to tracked files, because the image it tests is built
from `git archive HEAD`, and at the end it writes `LAST-RUN` with that sha, the
date and a result per scenario. Commit `LAST-RUN` on its own afterwards.

## What it stands up

One container on the host: `autogatus-itest-dind`, a privileged `docker:dind`
whose daemon listens on its unix socket only and keeps its store on a tmpfs, so
nothing it pulls or builds lands on the host's disks. It carries the label
`autogatus.enable=false`, so an autogatus already running on the host does not
monitor it. Everything else runs inside that daemon and never touches the
host's containers:

- Gatus `v5.36.0` with sqlite storage and a `custom` alert provider
  (failure-threshold 2, send-on-resolved) pointed at a request-recording sink.
- The autogatus image built from the commit under test with the repo's own
  Dockerfile, started through the shipped `examples/docker-compose.yml` with
  `compose.itest.yml` on top: monitoring on, resync 2s, heartbeat 30s, exec on.
- Target containers `autogatus-itest-t0` to `t3` in their own compose project,
  each with `network_mode: none`.

The dind is removed with its volumes at the end of a run and on any failure.

## Scenarios

Each scenario waits for Gatus's reload after any autogatus write before it
asserts. Evidence comes from Gatus's API, the generated file, autogatus's log,
Gatus's log (which records every accepted push by key), the dind daemon's debug
log (which records every exec call) and the sink.

| | Scenario | What it shows |
|---|---|---|
| H1 | park and freeze | `up --no-start` leaves the container created and never started. The key is declared `enabled: false` with no alerts, still listed, and gets no result for ten cycles plus a heartbeat interval: no push, no heartbeat check, no alert |
| H2 | autogatus restart while parked | Gatus deletes nothing for the parked key, and its count and newest result are unchanged. A stopped unlabeled control container is deleted, proving the check can fail |
| H3 | parked to live | `up -d` with the label re-enables the key with alerts, drift results arrive, and exactly one triggered alert reaches the sink |
| H4 | live to parked on a running container | `up --no-start --force-recreate` parks a running container, the row freezes, and no resolved alert is sent |
| H5 | drift resolved by unlabeling | One triggered alert on drift, then removing the label and `up -d` sends exactly one resolved alert |
| H6 | live to ran | A stopped labeled container keeps receiving failing results with the re-park hint and is never frozen |
| H7 | ran to parked | `--force-recreate` on the exited container freezes the row, and every result recorded since H1 is still there |
| H8 | parked to ran by a failed start | A start that fails leaves `State.Error` set, the key is re-enabled, a failing result with the hint arrives, and the sink gets a triggered alert |
| H9 | checks and tier-1 while parked | A parked container's exec check is frozen and never executed, while its `gatus.*` endpoint keeps gaining failing probe results and pages nobody. A running control shows the exec calls first |
| H10 | stray push | A push straight to the frozen key is accepted and stored and pages nobody |
| H11 | failure path | With Gatus stopped while a target goes from parked to live, autogatus logs failed drift pushes in its cycle summary, recovers when Gatus returns, and the drift alert follows |

Out of its reach: how the Gatus UI draws a frozen row. It checks results and
keys through the API, not the rendering.
