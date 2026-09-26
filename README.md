# 🌭 WeinerBeamer

Laser-engraved hotdogs for fundraisers. A web queue in front of one or more
diode-laser machines — Grbl CNCs like the Genmitsu 3018 Pro, Marlin 3D printers
retrofitted with a laser, or anything else via G-code export. A human always
places and aligns the dog; the software handles designs, the queue, routing
jobs to machines, and recovering when something goes wrong.

## Quick start

```sh
cd ~/code/weiner_beamer
uv run weiner-beamer                     # http://127.0.0.1:8420
uv run weiner-beamer --host 0.0.0.0 --password hotdog   # reachable from phones/tablets
```

First run creates two **simulated** machines (a Grbl 3018 and a Marlin fan-pin
retrofit) so you can try everything with no hardware. Point them at real ports
in **Settings** (`/dev/ttyUSB0`, `COM3`, `socket://192.168.1.50:23` for WiFi
boards like ESP3D/FluidNC). Data lives in `./data` (`--data` to move it); drop
extra `.ttf`/`.otf` fonts in `data/fonts/`.

## Event-day workflow

1. **Design** (Queue tab): Text, Pattern (crosshatch, diamond, zigzag, wave,
   stripes, grill marks, border, checker), Image (PNG/JPG logos; dither for
   photos) or a power/speed **Test grid** for dialing in on a sacrificial dog.
   The live preview shows every stroke on the dog, the in-focus zone (dashed
   blue), travel moves, burn time and size warnings.
2. **Queue**: set copies, passes, and optionally a target machine or dog type.
   Reorder (▲ ▼ ⤒), hold/release, edit, duplicate, cancel.
3. **Load & run**: each machine card offers "Load & run next N", one job per
   jig slot. A checklist (dogs placed · zero/focus set · eyewear/enclosure)
   must be ticked before it burns; "Trace outline first" moves the head around
   the design with the laser off.
4. **Watch**: progress bar, ETA, strokes done, and a live visualizer that turns
   burned strokes orange and tracks the head.
5. **Redo**: any finished, failed or aborted job has **Run again** (another dog,
   or re-burn the same one darker), **Resume** (continue from the last finished
   stroke, same machine, dog not moved), **Requeue**, and **Edit** (fix a typo;
   saving requeues it).

## Speed dial (darkness)

Every machine has a **Speed** dial from **−10** to **+10**, on its card, its
Machines page and the pre-burn checklist. **0** is the design's base speed (the
*Base speed* field in the composer); each step changes speed by 6% (set per
machine under Settings → Speed dial), so −10 ≈ ×0.54 (slower = darker) and
+10 ≈ ×1.79 (faster = lighter).

- The dial belongs to the machine and sticks until changed: tune it on the
  first dog of the day, and every later job on that machine uses it.
- It is baked into each job when it starts and recorded in History, so you can
  see which setting made which dog. Exported G-code (manual machines) includes it.
- Turn it during a burn and the change applies immediately: Grbl uses its
  realtime feed override (10–200%), Marlin gets `M220` after the few moves
  already buffered. The override resets when the job ends.
- Speeds are still capped at the machine's *Max feed*; very high steps on fast
  designs (e.g. image rasters) can hit that cap.

## Alignment tools (Machines & alignment tab)

- **Placement template** — tape card or scrap wood to the bed, zero on it, and
  burn one dog outline per jig slot (with centre ticks and slot tally marks).
  The template is by definition aligned to the machine; afterwards you just
  lay dogs on the outlines. This is the fastest setup for a busy line.
- **Loose dog: centre & square** — jog the pointer to the left tip, *Mark*,
  to the right tip, *Mark*, then *Centre & square*: the machine moves to the
  midpoint, zeroes X/Y there and rotates every design to the dog's angle
  (it also reports the measured length).
- **Jog pad** with 0.1–50 mm steps (arrow keys; PgUp/PgDn for Z), Home,
  Unlock, Zero X/Y, Zero Z at focus height, Go to zero / slot.
- **Pointer** — laser on at ≤5% power to see where the beam is (hard-capped).
- **Trace in-focus zone / trace job** — outline with the laser off.
- **Camera overlay** — a phone or webcam on the operator's device with a
  crosshair and resizable dog outline for lining up by eye.
- **Curvature**: each machine's dog profile computes the band of the dog that
  stays in focus. Optional **Wrap** maps the design around the cylinder so it
  isn't stretched at the flanks, and **Z-follow** moves Z with the curve so the
  beam stays focused further round the dog.

## Machines

| Firmware | Streaming | Laser control | Zero | Notes |
|---|---|---|---|---|
| **grbl** | character counting (128-byte RX), realtime `? ! ~ ^X` | `M4` dynamic power (or `M3`), `S0..$30` | `G10 L20 P1` | Needs laser mode `$32=1` (warned on connect). Pause = feed hold; abort = hold then soft reset, which keeps position and kills the beam. |
| **marlin** | `ok`-counted, `N`+checksum, handles `Resend:`/`busy:` | `inline` (`M3 I` + `G1 … S`), `m3` (`M3/M5` + `M400`), or `fan` (`M106/M107` + `M400`, the usual printer retrofit) | `G92` | Pause waits for a laser-off point (Marlin has no realtime hold). E-STOP sends `M410`+laser off; build Marlin with `EMERGENCY_PARSER` so they bypass the queue. Homing is `G28 X Y` only. |
| **manual** | none | — | — | Export-only: run the G-code in LightBurn/UGS/OctoPrint, then mark done/failed in the queue. |

Per-machine settings: port/baud, S range, a **power scale** to calibrate
different lasers to the same design, max feed, dog profile (preset or custom),
jig slots and pitch, template power.

**Routing**: a queued job runs on machine M if M is enabled, the job targets M
or "any machine", and the job's dog type (if set) matches M's loaded dog.
Designs are generated per machine at run time, so an "any" job is fitted to
whichever dog and laser it lands on.

## Fault tolerance

- Everything is in SQLite (WAL). If the server dies mid-burn, that job comes
  back **interrupted** with its last finished stroke recorded (written every
  second), ready to resume or re-run.
- A link that drops, stalls (no ack for `stall_timeout` seconds), resets or
  alarms ends only that job (marked *interrupted* with the reason); the laser
  is commanded off if the link still works; jobs in later slots go back to the
  queue. The controller reconnects in the background with backoff.
- Marlin line noise is recovered with checksums and resends; a resend loop
  that makes no progress fails the job instead of spinning.
- A design that fails to generate fails that job only; the rest of the batch runs.
- Every run is kept in **History** (machine, slot, strokes, duration, error).

## Safety

Laser diodes blind instantly and can start fires. Eyewear rated for your
laser's wavelength, an enclosure if you have one, never leave a burn
unattended, and have the hardware power switch within reach — **E-STOP ALL**
(or Esc twice) is software and needs a working link. `--host 0.0.0.0` without
`--password` warns on startup: anyone on the network could fire the laser.
Cooked hotdogs smoke and spatter; keep the lens clean and vent the smoke.

## Development

```sh
uv run pytest            # designs, dialects, and end-to-end runs against the simulators
```

Simulated ports: `sim`, `sim:<speedup>`, `sim:<speedup>:<corrupt_rate>`
(e.g. `sim:20:0.05` garbles 5% of Marlin lines to exercise resends).

```
src/weiner_beamer/
  designs.py      text (TTF outlines + hatch fill), patterns, images, test grid, template
  geometry.py     toolpath model, hatching, clipping, focus zone
  dialects.py     toolpath -> G-code per firmware; slot offset, rotation, wrap, Z-follow
  controllers.py  serial/socket link, Grbl + Marlin streaming, fault handling
  sim.py          simulated Grbl and Marlin
  fleet.py        machines, routing, batches, alignment, run bookkeeping
  store.py        SQLite: machines, jobs, runs, settings
  app.py          FastAPI routes;  static/  operator UI (no build step)
```
