#!/usr/bin/env python3
"""ArtNet → BRmesh: Ring poller, continuous send loop"""
import socket, os, configparser, time, sys, threading
import dbus, dbus.exceptions, dbus.mainloop.glib, dbus.service

try:
    from gi.repository import GLib
except ImportError:
    import gobject as GObject  # python2 fallback

# =============================================================================
#  Configuration (from artnet_listen.ini)
# =============================================================================
INI = os.path.dirname(os.path.abspath(__file__)) + "/artnet_listen.ini"
cfg = configparser.ConfigParser(); cfg.read(INI)
UNIVERSE = cfg.getint("artnet", "universe", fallback=512)
my_key = eval(cfg.get("brmesh", "key", fallback=[0x37, 0x39, 0x39, 0x36]))
N = cfg.getint("artnet", "num_lights", fallback=24)

# =============================================================================
#  BRMesh / BlueZ constants (from brMeshMQTT gateway.py)
# =============================================================================
default_key             = [0x5e, 0x36, 0x7b, 0xc4]
DEFAULT_BLE_FASTCON_ADDRESS = [0xC1, 0xC2, 0xC3]
BLE_CMD_RETRY_CNT       = 1
BLE_CMD_ADVERTISE_LENGTH = 3000
SEND_COUNT              = 1
SEND_SEQ                = 0

BLUEZ_SERVICE_NAME          = 'org.bluez'
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
DBUS_OM_IFACE               = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE             = 'org.freedesktop.DBus.Properties'
LE_ADVERTISEMENT_IFACE      = 'org.bluez.LEAdvertisement1'

# =============================================================================
#  BlueZ D-Bus exception classes
# =============================================================================
class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'
class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.NotSupported'
class NotPermittedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.NotPermitted'
class InvalidValueLengthException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.InvalidValueLength'
class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.bluez.Error.Failed'

# =============================================================================
#  BlueZ LE Advertisement (D-Bus service)
# =============================================================================
_adv_path = 0
class Advertisement(dbus.service.Object):
    PATH_BASE = '/org/bluez/example/advertisement'
    def __init__(self, bus, index, advertising_type):
        global _adv_path; _adv_path += 1
        self.path = self.PATH_BASE + str(_adv_path)
        self.bus = bus
        self.ad_type = advertising_type
        self.service_uuids = None
        self.manufacturer_data = None
        self.solicit_uuids = None
        self.service_data = None
        self.local_name = None
        self.include_tx_power = None
        self.data = None
        self.discoverable = None
        dbus.service.Object.__init__(self, bus, self.path)
    def get_properties(self):
        properties = dict()
        properties['Type'] = self.ad_type
        if self.service_uuids is not None:
            properties['ServiceUUIDs'] = dbus.Array(self.service_uuids, signature='s')
        if self.solicit_uuids is not None:
            properties['SolicitUUIDs'] = dbus.Array(self.solicit_uuids, signature='s')
        if self.manufacturer_data is not None:
            properties['ManufacturerData'] = dbus.Dictionary(self.manufacturer_data, signature='qv')
        if self.service_data is not None:
            properties['ServiceData'] = dbus.Dictionary(self.service_data, signature='sv')
        if self.local_name is not None:
            properties['LocalName'] = dbus.String(self.local_name)
        if self.include_tx_power is not None:
            properties['IncludeTxPower'] = dbus.Boolean(self.include_tx_power)
        if self.discoverable is not None:
            properties['Discoverable'] = dbus.Boolean(self.discoverable)
        if self.data is not None:
            properties['Data'] = dbus.Dictionary(self.data, signature='yv')
        return {LE_ADVERTISEMENT_IFACE: properties}
    def get_path(self):
        return dbus.ObjectPath(self.path)
    def add_service_uuid(self, uuid):
        if not self.service_uuids: self.service_uuids = []
        self.service_uuids.append(uuid)
    def add_solicit_uuid(self, uuid):
        if not self.solicit_uuids: self.solicit_uuids = []
        self.solicit_uuids.append(uuid)
    def add_manufacturer_data(self, manuf_code, data):
        if not self.manufacturer_data:
            self.manufacturer_data = dbus.Dictionary({}, signature='qv')
        self.manufacturer_data[manuf_code] = dbus.Array(data, signature='y')
    def add_service_data(self, uuid, data):
        if not self.service_data:
            self.service_data = dbus.Dictionary({}, signature='sv')
        self.service_data[uuid] = dbus.Array(data, signature='y')
    def add_local_name(self, name):
        if not self.local_name: self.local_name = ""
        self.local_name = dbus.String(name)
    def add_data(self, ad_type, data):
        if not self.data: self.data = dbus.Dictionary({}, signature='yv')
        self.data[ad_type] = dbus.Array(data, signature='y')
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]
    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        print('%s: Released!' % self.path)

class brMeshAdvertisement(Advertisement):
    def __init__(self, bus, index, mdata):
        Advertisement.__init__(self, bus, index, 'peripheral')
        self.add_manufacturer_data(0xfff0, mdata)
        self.discoverable = True

def find_adapter(bus):
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OM_IFACE)
    objects = remote_om.GetManagedObjects()
    for o, props in objects.items():
        if LE_ADVERTISING_MANAGER_IFACE in props:
            return o
    return None

# =============================================================================
#  BRMesh Fastcon BLE protocol (reverse-engineered, from brMeshMQTT)
# =============================================================================
def reverse_8(d):
    result = 0
    for k in range(8):
        result |= ((d >> k) & 1) << (7 - k)
    return result

def reverse_16(d):
    result = 0
    for k in range(16):
        result |= ((d >> k) & 1) << (15 - k)
    return result

def crc16(addr, data):
    crc = 0xFFFF
    for i in range(len(addr) - 1, -1, -1):
        crc ^= addr[i] << 8
        for _ in range(4):
            tmp = crc << 1
            if crc & 0x8000 != 0: tmp ^= 0x1021
            crc = tmp << 1
            if tmp & 0x8000 != 0: crc ^= 0x1021
    for i in range(len(data)):
        crc ^= reverse_8(data[i]) << 8
        for _ in range(4):
            tmp = crc << 1
            if crc & 0x8000 != 0: tmp ^= 0x1021
            crc = tmp << 1
            if tmp & 0x8000 != 0: crc ^= 0x1021
    crc = (~reverse_16(crc)) & 0xFFFF
    return crc

def package_ble_fastcon_body(i, i2, sequence, safe_key, forward, data, key):
    body = []
    body.append((i2 & 0b1111) | ((i & 0b111) << 4) | ((forward & 0xff) << 7))
    body.append(sequence & 0xff)
    body.append(safe_key)
    body.append(0)  # checksum placeholder

    body += data

    checksum = 0
    for j in range(len(body)):
        if j == 3: continue
        checksum = (checksum + body[j]) & 0xff
    body[3] = checksum

    # pad payload with zeros
    for _ in range(12 - len(data)):
        body.append(0)

    for j in range(4):
        body[j] = default_key[j & 3] ^ body[j]
    for j in range(12):
        body[4 + j] = key[j & 3] ^ body[4 + j]

    return body

def get_payload_with_inner_retry(i, data, i2, key, forward, use_22_data):
    global SEND_COUNT, SEND_SEQ
    SEND_COUNT += 1
    SEND_SEQ = SEND_COUNT & 0xff
    safe_key = 0xff
    if key[0] == 0 or key[1] == 0 or key[2] == 0 or key[3] == 0:
        pass
    else:
        safe_key = key[3]
    if use_22_data:
        print("Ooops! use_22_data")
        return -1
    else:
        return package_ble_fastcon_body(i, i2, SEND_SEQ, safe_key, forward, data, key)

def get_rf_payload(addr, data):
    data_offset = 0x12
    inverse_offset = 0x0f
    result_data_size = data_offset + len(addr) + len(data)
    resultbuf = [0] * (result_data_size + 2)

    resultbuf[0x0f] = 0x71
    resultbuf[0x10] = 0x0f
    resultbuf[0x11] = 0x55

    # reverse copy the address
    for i in range(len(addr)):
        resultbuf[data_offset + len(addr) - i - 1] = addr[i]
    resultbuf[data_offset + len(addr):data_offset + len(addr) + len(data)] = data[:]

    for i in range(inverse_offset, inverse_offset + len(addr) + 3):
        resultbuf[i] = reverse_8(resultbuf[i])

    crc = crc16(addr, data)
    resultbuf[result_data_size] = crc & 0xFF
    resultbuf[result_data_size + 1] = (crc >> 8) & 0xFF
    return resultbuf

def whitening_init(val, ctx):
    v0 = [(val >> 5) & 1, (val >> 4) & 1, (val >> 3) & 1, (val >> 2) & 1]
    ctx[0] = 1
    ctx[1] = v0[0]
    ctx[2] = v0[1]
    ctx[3] = v0[2]
    ctx[4] = v0[3]
    ctx[5] = (val >> 1) & 1
    ctx[6] = val & 1

def whitening_encode(data, ctx):
    result = list(data)
    for i in range(len(result)):
        varC  = ctx[3]
        var14 = ctx[5]
        var18 = ctx[6]
        var10 = ctx[4]
        var8  = var14 ^ ctx[2]
        var4  = var10 ^ ctx[1]
        _var  = var18 ^ varC
        var0  = _var ^ ctx[0]

        c = result[i]
        result[i]  = ((c & 0x80) ^ ((var8 ^ var18) << 7)) & 0xFF
        result[i] += ((c & 0x40) ^ (var0 << 6)) & 0xFF
        result[i] += ((c & 0x20) ^ (var4 << 5)) & 0xFF
        result[i] += ((c & 0x10) ^ (var8 << 4)) & 0xFF
        result[i] += ((c & 0x08) ^ (_var << 3)) & 0xFF
        result[i] += ((c & 0x04) ^ (var10 << 2)) & 0xFF
        result[i] += ((c & 0x02) ^ (var14 << 1)) & 0xFF
        result[i] += ((c & 0x01) ^ (var18 << 0)) & 0xFF

        ctx[2] = var4
        ctx[3] = var8
        ctx[4] = var8 ^ varC
        ctx[5] = var0 ^ var10
        ctx[6] = var4 ^ var14
        ctx[0] = var8 ^ var18
        ctx[1] = var0
    return result

def do_generate_command(i, data, key, _retry_count, _send_interval, forward, use_default_adapter, use_22_data, i2):
    i2_ = max(i2, 0)
    payload = get_payload_with_inner_retry(i, data, i2_, key, forward, use_22_data)
    payload = get_rf_payload(DEFAULT_BLE_FASTCON_ADDRESS, payload)
    whiteningContext = [0] * 7
    whitening_init(0x25, whiteningContext)
    payload = whitening_encode(payload, whiteningContext)
    payload = payload[0x0f:]
    return payload

# =============================================================================
#  BLE advertisement sender (talks to patched BlueZ via D-Bus)
# =============================================================================
adv_lock = threading.Lock()

def single_control(addr, key, data, delay):
    global mainloop
    with adv_lock:
        result = [2 | (((0xFFFFFFF & (len(data) + 1)) << 4) & 0xFF), addr & 0xFF] + list(data)
        ble_adv_cmd = do_generate_command(5, result, key,
                        BLE_CMD_RETRY_CNT, BLE_CMD_ADVERTISE_LENGTH,
                        True, True, (addr > 256) & 0xFF, (addr // 256) & 0xFF)

        # Fresh D-Bus connection per call (no leak)
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        adapter = find_adapter(bus)
        if not adapter: return
        ap = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter), DBUS_PROP_IFACE)
        ap.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(1))
        ad_manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter), LE_ADVERTISING_MANAGER_IFACE)

        adv = brMeshAdvertisement(bus, 0, ble_adv_cmd)
        mainloop = GLib.MainLoop()
        ad_manager.RegisterAdvertisement(adv.get_path(), {},
            reply_handler=lambda: None, error_handler=lambda e: None)
        threading.Thread(target=lambda: (time.sleep(0.1), mainloop.quit())).start()
        mainloop.run()

        # Clean up
        try: ad_manager.UnregisterAdvertisement(adv)
        except: pass
        try: adv.remove_from_connection()
        except: pass
        try: bus.close()
        except: pass

# =============================================================================
#  Light class – high-level interface to a single BRMesh flood light
# =============================================================================
class Light:
    def __init__(self, mesh_key, device_id):
        self.id = int(device_id)
        self.key = mesh_key

    def setOnOff(self, on, brightness):
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

# =============================================================================
#  ArtNet listener setup
# =============================================================================
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", 6454))
sock.settimeout(0.05)

print(f"ArtNet->BRmesh | Univ:{UNIVERSE} | {N} lights | Ring send loop")
print("[R]=OFF [A]=PINK Ctrl+C=Exit\n")

import termios, tty, select as _sel
_has_kbd = sys.stdin.isatty()
if _has_kbd: orig = termios.tcgetattr(sys.stdin); tty.setcbreak(sys.stdin)

lock = threading.Lock()
latest = [(0,0,0)] * N
dmx_count = [0, 0]  # [packets, last_print_time]
change_log = []     # (light_id, r, g, b, timestamp)

# =============================================================================
#  Ring send loop (continuous operation)
# =============================================================================
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

# =============================================================================
#  Status poller (every 0.2s)
# =============================================================================
def monitor():
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

# =============================================================================
#  Keyboard commands
# =============================================================================
def all_off():
    print("⏻ OFF...", end=" ", flush=True)
    for i in range(N): Light(my_key,i+1).setOnOff(0,0)
    print("OK")
def all_pink():
    print("🌸 PINK...", end=" ", flush=True)
    for i in range(N): Light(my_key,i+1).Colored(1,38,255,50,180,True)
    print("OK")

# =============================================================================
#  Main: ArtNet → latest[]
# =============================================================================
while True:
    if _has_kbd and _sel.select([sys.stdin],[],[],0)[0]:
        k = sys.stdin.read(1).lower()
        if k=='r': all_off()
        elif k=='a': all_pink()
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
