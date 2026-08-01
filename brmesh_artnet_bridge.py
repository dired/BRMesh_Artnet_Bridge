#!/usr/bin/env python3
"""ArtNet → BRmesh: Ring-Poller, Dauer-Sendeschleife"""
import socket, os, configparser, time, sys, threading

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

INI = os.path.dirname(os.path.abspath(__file__)) + "/artnet_listen.ini"
cfg = configparser.ConfigParser(); cfg.read(INI)
UNIVERSE = cfg.getint("artnet", "universe", fallback=512)
my_key = eval(cfg.get("brmesh", "key", fallback=[0x37, 0x39, 0x39, 0x36]))
print(my_key)
N = cfg.getint("artnet", "num_lights", fallback=24)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", 6454))
sock.settimeout(0.05)

print(f"ArtNet->BRmesh | Univ:{UNIVERSE} | {N} Fluter | Ring-Sendeschleife")
print("[R]=AUS [A]=ROSA Strg+C=Ende\n")

import termios, tty, select as _sel
_has_kbd = sys.stdin.isatty()
if _has_kbd: orig = termios.tcgetattr(sys.stdin); tty.setcbreak(sys.stdin)

lock = threading.Lock()
latest = [(0,0,0)] * N
dmx_count = [0, 0]  # [packets, last_print_time]
change_log = []     # (light_id, r, g, b, timestamp)

class Light:
    def __init__(self, mesh_key, device_id):
        self.id = int(device_id)
        self.key = mesh_key

    def setOnOff(self, on, brightness):
        pass
        command = [0] * 1
        command[0] = 0
        if on:
            command[0] = 128 + (int(brightness) & 127)
        single_control(self.id, self.key, command, 0)

    def Brightness(self, on, brightness):
        command = [0] * 1
        command[0] = 0
        if on:
            command[0] = int(brightness) & 127
        threading.Thread(target=single_control, args=(self.id, self.key, command, 0), kwargs={}).start()
       # single_control(self.id, self.key, command, 0)

    def WarmWhite(self, on, brightness, i5, i6):
        command = [0] * 6
        command[0] = 0
        command[4] = i5 & 0xFF
        command[5] = i6 & 0xFF
        if on:
            command[0] = 128 + (int(brightness) & 127)
        single_control(self.id, self.key, command, 0)

    def Colored(self, on, brightness, r, g, b, abs):
        command = [0] * 6
        color_normalization = 1
        command[0] = 0
        if on:
            command[0] += 128
        command[0] += int(brightness) & 127
        if not abs:
            color_normalization = 255.0 / (r + g + b)
        command[1] = int((b * color_normalization) & 0xFF)
        command[2] = int((r * color_normalization) & 0xFF)
        command[3] = int((g * color_normalization) & 0xFF)
        single_control(self.id, self.key, command, 0)


# === Ring-Sendeschleife (Dauerbetrieb) ===
def sender():
    last_sent = [(0,0,0)] * N
    i = 0
    while True:
        i = (i + 1) % N
        with lock:
            cur = latest[i]
        if cur != last_sent[i]:
            last_sent[i] = cur
            r, g, b = cur
            light = Light(my_key, i+1)
            if r==0 and g==0 and b==0:
                light.setOnOff(0, 0)
            else:
                light.Colored(1, 255, r, g, b, True)
            with lock:
                change_log.append((i+1, r, g, b, time.time()))

threading.Thread(target=sender, daemon=True).start()

# === Status-Poller (alle 0.2s) ===
def monitor():
    last_report = time.time()
    while True:
        time.sleep(0.2)
        now = time.time()
        with lock:
            recent = [c for c in change_log if now - c[4] < 0.25]
            change_log.clear()
            change_log.extend(recent)
            pkts = dmx_count[0]
        
        if recent:
            lights = {}
            for lid, r, g, b, _ in recent:
                lights[lid] = f"R{r:3d} G{g:3d} B{b:3d}"
            info = " | ".join(f"#{lid}:{val}" for lid, val in sorted(lights.items())[:8])
            if len(lights) > 8: info += f" +{len(lights)-8} more"
            print(f"  📡 {info}")
        
        if pkts > 0:
            print(f"  📥 {pkts} ArtNet pkts in 0.2s")
            with lock: dmx_count[0] = 0

threading.Thread(target=monitor, daemon=True).start()

# === Keys ===
def all_off():
    print("⏻ AUS...", end=" ", flush=True)
    for i in range(N): Light(my_key,i+1).setOnOff(0,0)
    print("OK")
def all_rosa():
    print("🌸 ROSA...", end=" ", flush=True)
    for i in range(N): Light(my_key,i+1).Colored(1,38,255,50,180,True)
    print("OK")

# === Main: ArtNet → latest[] ===
while True:
    if _has_kbd and _sel.select([sys.stdin],[],[],0)[0]:
        k = sys.stdin.read(1).lower()
        if k=='r': all_off()
        elif k=='a': all_rosa()
        continue
    try:
        data, addr = sock.recvfrom(2048)
    except socket.timeout:
        continue
    if len(data)<18 or data[:8]!=b"Art-Net\x00" or data[8]|(data[9]<<8)!=0x5000: continue
    if ((data[14]<<8)|data[15]) != UNIVERSE: continue
    dmx = data[18:18+min((data[16]<<8)|data[17], N*3)]
    with lock:
        for i in range(N):
            idx = i*3
            if idx+2 < len(dmx):
                latest[i] = (dmx[idx], dmx[idx+1], dmx[idx+2])
        dmx_count[0] += 1
