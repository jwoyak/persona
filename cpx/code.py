# Persona CPX - code.py  (CircuitPython 8/9, Circuit Playground Express)
#
# LED ring = wound meters fed by Persona over USB.
#   Slide switch: MEAT mode shows Physical + Mental, CYBER mode shows Matrix + drone.
#   Button A: modem handshake.  Button B: Bork barks.
# Put your own sounds in /sounds/modem.wav and /sounds/woof.wav
# (mono, 16-bit or 8-bit PCM, <= 22050 Hz) to replace the synthesized ones.

import array
import math
import random
import time

import board
import digitalio
import neopixel
import usb_cdc
from audiocore import RawSample, WaveFile

try:
    from audioio import AudioOut
except ImportError:
    AudioOut = None

# ---- settings ---------------------------------------------------------------
BRIGHTNESS = 0.08        # whole ring; NeoPixels are blinding above a screen
UNMARKED = 0.15          # glow of an empty box, relative to a marked one
VOLUME = 0.25            # 0..1 for synthesized sounds
CYBER_WHEN_SWITCH_IS = False   # flip if MEAT/CYBER come out backwards

# Pixels for each side, first box first. Both bars fill bottom-to-top on a
# standard CPX; run the boot sweep to check against your mounting.
LEFT = (5, 6, 7, 8, 9)
RIGHT = (4, 3, 2, 1, 0)

# slot order matches leds="..." in the template: Physical, Mental, Matrix, drone
COLORS = ((255, 0, 0), (0, 60, 255), (0, 255, 40), (170, 0, 255))
MODES = {False: (0, 1), True: (2, 3)}   # cyber? -> (left slot, right slot)

# ---- hardware -----------------------------------------------------------------
pixels = neopixel.NeoPixel(board.NEOPIXEL, 10, brightness=BRIGHTNESS, auto_write=False)


def din(pin, pull):
    d = digitalio.DigitalInOut(pin)
    d.switch_to_input(pull=pull)
    return d


switch = din(board.SLIDE_SWITCH, digitalio.Pull.UP)
btn_a = din(board.BUTTON_A, digitalio.Pull.DOWN)
btn_b = din(board.BUTTON_B, digitalio.Pull.DOWN)
speaker_en = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
speaker_en.switch_to_output(value=False)
audio = AudioOut(board.SPEAKER) if AudioOut else None

serial = usb_cdc.data
if serial:
    serial.timeout = 0
    serial.write_timeout = 0


def send(text):
    if serial and serial.connected:
        try:
            serial.write((text + "\n").encode())
        except Exception:  # noqa
            pass


# ---- sound ---------------------------------------------------------------------
LEN = 16
amp = int(32767 * VOLUME)
square = array.array("H", [32768 + amp] * (LEN // 2) + [32768 - amp] * (LEN // 2))
noise = array.array("H", [32768 + random.randint(-amp, amp) for _ in range(256)])

queue = []        # pending steps: ("tone", hz, s) ("noise", rate, s) ("rest", s) ("wav", path)
step_end = 0.0
wav_file = None


def modem():
    seq = [("tone", random.choice((697, 770, 852, 941)), 0.07) for _ in range(5)]
    seq += [("rest", 0.25), ("tone", 2100, 0.7), ("rest", 0.05)]
    for _ in range(4):
        seq += [("tone", 1650, 0.05), ("tone", 1850, 0.05)]
    seq += [("noise", 9000, 0.25), ("tone", 1200, 0.12), ("tone", 2400, 0.12),
            ("noise", 14000, 0.35), ("tone", 980, 0.1), ("noise", 22000, 0.6)]
    return seq


def woof():
    seq = []
    for start in (560, 470):
        for k in range(10):          # quick downward glide = a bark
            seq.append(("tone", int(start * (1 - k / 14)), 0.016))
        seq += [("noise", 6000, 0.03), ("rest", 0.13)]
    return seq


def play(name, synth):
    global queue, step_end
    try:
        open("/sounds/%s.wav" % name, "rb").close()
        queue = [("wav", "/sounds/%s.wav" % name)]
    except OSError:
        queue = synth()
    step_end = 0.0


def run_audio(now):
    global queue, step_end, wav_file
    if not audio:
        return
    if wav_file:
        if audio.playing:
            return
        wav_file.close()
        wav_file = None
    if now < step_end:
        return
    audio.stop()
    if not queue:
        speaker_en.value = False
        return
    speaker_en.value = True
    step = queue.pop(0)
    kind = step[0]
    if kind == "tone":
        audio.play(RawSample(square, sample_rate=step[1] * LEN), loop=True)
        step_end = now + step[2]
    elif kind == "noise":
        audio.play(RawSample(noise, sample_rate=step[1]), loop=True)
        step_end = now + step[2]
    elif kind == "rest":
        step_end = now + step[1]
    elif kind == "wav":
        wav_file = open(step[1], "rb")
        audio.play(WaveFile(wav_file))


# ---- wound state ------------------------------------------------------------------
tracks = [None, None, None, None]   # per slot: (marks list, incap_start index)
flash = [0.0, 0.0, 0.0, 0.0]        # per slot: flash-until time
flash_box = [-1, -1, -1, -1]
linked = False
last_seen = 0.0


def parse(payload):
    out = []
    for part in payload.split(";")[:4]:
        part = part.strip()
        if not part or part == "-":
            out.append(None)
            continue
        groups = part.split("|")
        marks = [c == "1" for c in "".join(groups)]
        incap = len(marks) - len(groups[-1]) if len(groups) > 1 else len(marks)
        out.append((marks, incap))
    while len(out) < 4:
        out.append(None)
    return out


def update(payload, now):
    global tracks
    new = parse(payload)
    for s in range(4):
        old = tracks[s]
        if new[s] and old:
            before, after = sum(old[0]), sum(new[s][0])
            if after > before:      # took a hit: flash the newest box
                flash[s] = now + 0.7
                flash_box[s] = after - 1
    tracks = new


def scale(c, f):
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f))


def draw_side(pins, slot, now):
    t = tracks[slot]
    color = COLORS[slot]
    for i, p in enumerate(pins):
        if not t or i >= len(t[0]):
            pixels[p] = (0, 0, 0)
            continue
        marks, incap = t
        if marks[i]:
            f = 1.0
            if i >= incap:          # incapacitated: slow pulse
                f = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(now * 5))
            pixels[p] = scale(color, f)
        else:
            pixels[p] = scale(color, UNMARKED)
        if now < flash[slot] and i == flash_box[slot] and int(now * 10) % 2 == 0:
            pixels[p] = (255, 255, 255)


def draw(now, cyber):
    if not linked:                  # waiting for Persona: slow breathe
        f = 0.05 + 0.1 * (0.5 + 0.5 * math.sin(now * 1.5))
        pixels.fill(scale(COLORS[2] if cyber else COLORS[1], f))
    else:
        left, right = MODES[cyber]
        draw_side(LEFT, left, now)
        draw_side(RIGHT, right, now)
    pixels.show()


def sweep():
    """Boot test: left bar fills red, right bar fills blue, in box order."""
    pixels.fill((0, 0, 0))
    for i in range(5):
        pixels[LEFT[i]] = COLORS[0]
        pixels[RIGHT[i]] = COLORS[1]
        pixels.show()
        time.sleep(0.08)
    time.sleep(0.25)
    pixels.fill((0, 0, 0))
    pixels.show()


def mode_sweep(cyber):
    left, right = MODES[cyber]
    for i in range(5):
        pixels[LEFT[i]] = COLORS[left]
        pixels[RIGHT[i]] = COLORS[right]
        pixels.show()
        time.sleep(0.03)


# ---- main loop --------------------------------------------------------------------
sweep()
cyber = switch.value == CYBER_WHEN_SWITCH_IS
prev_a = prev_b = False
buf = b""

while True:
    now = time.monotonic()

    if serial and serial.in_waiting:
        buf += serial.read(serial.in_waiting)
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode().strip()
            if line == "HELLO":
                linked = True
                send("SWITCH " + ("cyber" if cyber else "meat"))
            elif line == "BYE":
                linked = False
            elif line.startswith("W "):
                linked = True
                update(line[2:], now)
            last_seen = now
        if len(buf) > 512:
            buf = b""

    c = switch.value == CYBER_WHEN_SWITCH_IS
    if c != cyber:
        cyber = c
        send("SWITCH " + ("cyber" if cyber else "meat"))
        mode_sweep(cyber)

    a, b = btn_a.value, btn_b.value
    if a and not prev_a:
        play("modem", modem)
    if b and not prev_b:
        play("woof", woof)
    prev_a, prev_b = a, b

    run_audio(now)
    draw(now, cyber)
    time.sleep(0.01)
