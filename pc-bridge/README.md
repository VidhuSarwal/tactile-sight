# pc-bridge — laptop between the depth server and the band

```
camera board (:8081/grid)  ──HTTP──>  laptop  ──USB serial──>  spare Uno Q  ──I2C──>  27 motors
```

## Why this exists

The Arduino Router Bridge never registered `set_haptic_grid` on our board. The
Linux half was proven correct — the app's own Python calls it through
`arduino.app_utils.Bridge`, the officially supported path — but the MCU never
came up: no method registration, no `Monitor` output, and OpenOCD reporting the
CPU `in unknown state` across a flash, a reset and a full reboot.

Plain USB serial is already proven on this hardware; the pattern-demo sketch
drove all 27 motors through it. So this route sidesteps the RPC layer entirely
and puts a second board on the job.

It also splits the work sensibly: the laptop fetches and the band's Uno Q only
has to receive bytes and buzz.

## Run it

```bash
cd pc-bridge
./run.sh                      # venv, install, autodetect the port, go
./run.sh --test               # moving column, no camera needed
./run.sh --port /dev/ttyACM0  # if autodetect picks wrong
./run.sh --host 10.89.1.5     # the board is DHCP and moves
```

`run.sh` creates `.venv`, installs `pyserial` (the only dependency — everything
else is stdlib on purpose) and runs the bridge. Ctrl-C sends an all-zero frame
first, so the band goes quiet rather than buzzing on forever.

**Start with `--test`.** It proves the serial link and the motors with the
camera out of the picture, so a fault has one place to be instead of three.

## Flash the sketch

`../linux/haptic_serial_receiver.ino` onto the **spare** Uno Q. It needs the
`Adafruit PWM Servo Driver Library`. Wiring is unchanged from the pattern demo:
PCA9685 at `0x40` (motors 0–13) and `0x60` (motors 14–26), column-major,
`motor = col * 3 + row`.

## Wire format

24 bytes per frame:

```
0xAA 0x55   <21 cell bytes, 0..255>   <XOR checksum of the 21>
```

Framed rather than newline-delimited because the payload is raw binary and
`0x0A` is a legal intensity — a line-based protocol would cut frames in half at
exactly the value that means *something is close*.

## Reading what it prints

```
sent=412 cells_on=6/21 board: OK 412 motors=9
```

`cells_on` is what the laptop sent; `motors` is what the board says it is
driving. The board answers **every** frame, so the two numbers together are
proof the band is receiving rather than an assumption that it is.

Other lines from the board:

| Line | Meaning |
|---|---|
| `READY TactileSight serial receiver 9x3` | sketch booted |
| `OK <n> motors=<m>` | frame accepted, `m` motors driven |
| `BAD checksum n=<n>` | corrupt frame, dropped — a few is normal, a flood means baud or cable |
| `FAILSAFE no data` | nothing for 1 s, motors cut |

## When nothing buzzes

Work down this list; each step removes one possibility.

1. **No LED-matrix sweep at boot** → the sketch is not running. Re-flash.
2. **`--test` buzzes but live data does not** → the camera path, not the band.
   Check `curl http://10.89.1.1:8081/grid`.
3. **`cells_on=0/21` constantly** → nothing is within 2 m. `DETECT_MM = 2000`
   on the board, so a far wall reads as empty and silence is *correct*. Put a
   hand in front of the camera.
4. **No serial port found** → plug the Uno Q in; on Linux you may need
   `sudo usermod -aG dialout $USER`, then log out and back in.
5. **Matrix mirrors the grid but motors are silent** → the link is fine and the
   problem is the harness: PCA9685 power, I2C pull-ups, or the motor supply.
   The matrix is driven by the MCU alone and needs neither.
