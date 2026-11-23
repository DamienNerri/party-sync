#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jbl_sync.py : mettre une JBL PartyBox 310 et des JBL PartyLight Stick sur les mêmes couleurs.

La 310 et le stick parlent le même protocole Bluetooth Low Energy (celui de l'appli JBL PartyBox) :
  - service 65786365-6c70-6f69-6e74-2e636f6d0000 ("excelpoint.com" en ASCII)
  - on écrit sur la caractéristique ...0002, les réponses arrivent en notifications sur ...0001
  - trames : AA <commande> <longueur> <données>
Ils ne se synchronisent pas tout seuls parce que la synchro d'origine passe par Auracast, que la 310
n'a pas. Ce programme fait le chef d'orchestre : il se connecte aux deux et leur envoie les mêmes
commandes au même moment.

    pip install bleak
    python jbl_sync.py scan            # trouve les appareils
    python jbl_sync.py color rouge     # même couleur partout
    python jbl_sync.py party           # change de couleur en rythme, partout à la fois
    python jbl_sync.py web             # télécommande dans le navigateur du téléphone

Tout est expliqué dans README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import colorsys
import contextlib
import importlib.util
import json
import os
import random
import re
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass

__version__ = "1.0.0"

# =============================================================================
# Protocole JBL (fonctions pures, testées dans test_jbl_sync.py)
# =============================================================================

SERVICE_UUID = "65786365-6c70-6f69-6e74-2e636f6d0000"
RX_UUID = "65786365-6c70-6f69-6e74-2e636f6d0001"  # appareil -> PC (notifications)
TX_UUID = "65786365-6c70-6f69-6e74-2e636f6d0002"  # PC -> appareil (écriture)
ADV_UUID_PARTYBOX = "0000fddf-0000-1000-8000-00805f9b34fb"  # données de service dans la pub BLE

SYNC = 0xAA
LONG_LENGTH_CMDS = frozenset({0xE1, 0xE2, 0xE3})  # commandes d'égaliseur : longueur sur 2 octets

CMD_ACK = 0x00
CMD_POWER = 0x03
CMD_REQ_DEVICE = 0x11
CMD_RET_DEVICE = 0x12
CMD_REQ_LIGHT = 0x31
CMD_RET_LIGHT = 0x32
CMD_SET_LIGHT = 0x33
CMD_HEARTBEAT = 0xEA

# Étiquettes (TLV) de la page "lumière" (commandes 31/32/33)
L_PATTERN = 0x31
L_COLOR = 0x32
L_STROBE = 0x33
L_CUSTOM_COLOR = 0x36
L_RING = 0x38
L_DOTS = 0x39
L_STRIPS = 0x44
L_INTENSITY = 0x45
L_SPEED = 0x46
L_PATTERNS_AVAILABLE = 0x4A
L_MAIN_SWITCH = 0x99

LIGHT_TAGS = {
    L_PATTERN: "motif", L_COLOR: "couleur", L_STROBE: "stroboscope", L_CUSTOM_COLOR: "couleur perso",
    L_RING: "anneau", L_DOTS: "points", L_STRIPS: "bandes blanches", L_INTENSITY: "intensité",
    L_SPEED: "vitesse", L_PATTERNS_AVAILABLE: "motifs dispo", L_MAIN_SWITCH: "interrupteur",
}
DEVICE_TAGS = {
    0x31: "produit", 0x32: "modèle", 0x33: "batterie", 0x36: "source audio", 0x37: "MAC",
    0x40: "n° de série", 0x41: "firmware", 0x44: "CRC réglages", 0xC1: "nom",
}

PATTERNS = {
    "off": 0, "rock": 1, "neon": 2, "club": 3, "flow": 4, "ripple": 5, "cross": 6, "flash": 7,
    "custom": 8, "loop": 9, "bounce": 10, "trim": 11, "switch": 12, "freeze": 13,
}
PATTERN_ALIASES = {"perso": "custom", "personnalise": "custom", "fixe": "custom", "eteint": "off"}
PATTERN_NAMES = {v: k for k, v in PATTERNS.items()}
CUSTOM = PATTERNS["custom"]
# Motifs du PartyLight Stick (et, a priori, de la 310)
SHARED_PATTERNS = ("neon", "loop", "bounce", "trim", "switch", "freeze", "custom")
PATTERN_LABELS = {"neon": "Neon", "loop": "Loop", "bounce": "Bounce", "trim": "Trim",
                  "switch": "Switch", "freeze": "Freeze", "custom": "Perso"}

# Identifiants produit (table de l'appli JBL PartyBox)
PRODUCTS = {
    0x1F5D: "PartyBox 310", 0x1F5F: "PartyBox 110", 0x1F61: "PartyBox 710",
    0x2062: "PartyBox Encore", 0x2063: "PartyBox Encore Essential", 0x20DD: "PartyBox Stage 320",
    0x20E2: "PartyBox Club 120", 0x2118: "PartyBox Encore 2", 0x2120: "PartyBox 520",
    0x212E: "PartyBox Encore Essential 2", 0x2156: "PartyBox 720", 0x215B: "PartyBox On-The-Go 2",
    0x2095: "PartyBox Ultimate",
}
PID_310 = 0x1F5D


def frame(cmd: int, payload: bytes = b"") -> bytes:
    """Construit une trame AA <cmd> <len> <payload>."""
    payload = bytes(payload)
    if cmd in LONG_LENGTH_CMDS:
        return bytes([SYNC, cmd]) + len(payload).to_bytes(2, "big") + payload
    if len(payload) > 255:
        raise ValueError("trame trop longue")
    return bytes([SYNC, cmd & 0xFF, len(payload)]) + payload


def light_set(*tlvs: tuple[int, bytes]) -> bytes:
    """Trame de réglage lumière : AA 33 <len> 00 (tag len valeur)...  (00 = l'appareil connecté)."""
    body = bytearray([0])
    for tag, value in tlvs:
        value = bytes(value)
        body += bytes([tag, len(value)]) + value
    return frame(CMD_SET_LIGHT, bytes(body))


def _byte(v: int, lo: int = 0, hi: int = 255) -> bytes:
    return bytes([max(lo, min(hi, int(v)))])


def f_pattern(p: int) -> bytes:
    return light_set((L_PATTERN, _byte(p)))


def f_color(rgb) -> bytes:
    r, g, b = rgb
    return light_set((L_CUSTOM_COLOR, b"\x01"), (L_COLOR, _byte(r) + _byte(g) + _byte(b)))


def f_main_switch(on: bool) -> bytes:
    return light_set((L_MAIN_SWITCH, b"\x01" if on else b"\x00"))


def f_strobe(on: bool) -> bytes:
    return light_set((L_STROBE, b"\x01" if on else b"\x00"))


def f_intensity(v: int) -> bytes:
    return light_set((L_INTENSITY, _byte(v, 0, 100)))


def f_speed(v: int) -> bytes:
    return light_set((L_SPEED, _byte(v, 0, 100)))


def f_req_light() -> bytes:
    return frame(CMD_REQ_LIGHT)


def f_req_device() -> bytes:
    return frame(CMD_REQ_DEVICE)


def f_heartbeat() -> bytes:
    return frame(CMD_HEARTBEAT)


class FrameReader:
    """Découpe le flux des notifications en trames (une notification peut en contenir plusieurs,
    et une trame longue peut arriver en plusieurs morceaux)."""

    STALE_AFTER = 0.6  # un début de trame resté seul plus longtemps que ça = trame tronquée

    def __init__(self):
        self.buf = bytearray()
        self._last = 0.0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        now = time.monotonic()
        out = []
        if self.buf and now - self._last > self.STALE_AFTER:
            # certains modèles tronquent les longues réponses : on garde ce qui est arrivé
            if self.buf[0] == SYNC and len(self.buf) >= 3 and self.buf[1] not in LONG_LENGTH_CMDS:
                out.append((self.buf[1], bytes(self.buf[3:])))
            self.buf.clear()
        self._last = now
        self.buf += data
        while self.buf:
            if self.buf[0] != SYNC:
                i = self.buf.find(bytes([SYNC]))
                if i < 0:
                    self.buf.clear()
                    break
                del self.buf[:i]
            if len(self.buf) < 3:
                break
            cmd = self.buf[1]
            if cmd in LONG_LENGTH_CMDS:
                if len(self.buf) < 4:
                    break
                n, start = int.from_bytes(self.buf[2:4], "big"), 4
            else:
                n, start = self.buf[2], 3
            end = start + n
            if len(self.buf) < end:
                break
            out.append((cmd, bytes(self.buf[start:end])))
            del self.buf[:end]
        return out


def parse_tlv(payload: bytes, has_index: bool = True) -> dict[int, bytes]:
    """Décode <index> (tag len valeur)... ; s'arrête proprement si la fin est tronquée."""
    out = {}
    i = 1 if has_index else 0
    while i + 2 <= len(payload):
        tag, n = payload[i], payload[i + 1]
        out[tag] = bytes(payload[i + 2:i + 2 + n])
        i += 2 + n
    return out


def decode_light(payload: bytes) -> dict:
    """État lumière (réponse AA 32) -> dict lisible."""
    t = parse_tlv(payload)
    st = {}
    v = t.get(L_PATTERN)
    if v is not None and len(v) == 1:
        st["pattern"] = v[0]
    v = t.get(L_COLOR)
    if v is not None and len(v) == 3:
        st["rgb"] = (v[0], v[1], v[2])
    for tag, key in ((L_CUSTOM_COLOR, "custom"), (L_MAIN_SWITCH, "on")):
        v = t.get(tag)
        if v is not None and len(v) == 1:
            st[key] = bool(v[0])
    for tag, key in ((L_INTENSITY, "intensity"), (L_SPEED, "speed")):
        v = t.get(tag)
        if v is not None and len(v) == 1:
            st[key] = v[0]
    if L_PATTERNS_AVAILABLE in t:
        st["available"] = list(t[L_PATTERNS_AVAILABLE])
    return st


def rgb_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _describe_light_tlvs(payload: bytes) -> str:
    parts = []
    for tag, val in parse_tlv(payload).items():
        name = LIGHT_TAGS.get(tag, f"tag {tag:02x}")
        if tag == L_PATTERN and len(val) == 1:
            v = PATTERN_NAMES.get(val[0], str(val[0]))
        elif tag == L_COLOR and len(val) == 3:
            v = rgb_hex(val)
        elif tag == L_PATTERNS_AVAILABLE:
            v = ",".join(PATTERN_NAMES.get(x, str(x)) for x in val) or "-"
        elif len(val) == 1:
            v = str(val[0])
        else:
            v = val.hex(" ")
        parts.append(f"{name}={v}")
    return ", ".join(parts) or "(vide)"


def _describe_device_tlvs(payload: bytes) -> str:
    parts = []
    for tag, val in parse_tlv(payload).items():
        name = DEVICE_TAGS.get(tag, f"tag {tag:02x}")
        if tag in (0x31, 0x32) and len(val) == 2:
            model = PRODUCTS.get(int.from_bytes(val, "big")) or PRODUCTS.get(int.from_bytes(val, "little"))
            v = val.hex() + (f" ({model})" if model else "")
        elif tag == 0x33 and len(val) == 1:
            pct = val[0] & 0x7F
            v = ("secteur" if pct > 100 else f"{pct} %") + (" (branché)" if val[0] & 0x80 and pct <= 100 else "")
        elif tag in (0x40, 0x41, 0xC1):
            v = val.decode("utf-8", "replace").strip("\x00 ")
        elif tag == 0x37:
            v = ":".join(f"{b:02X}" for b in val)
        elif len(val) == 1:
            v = str(val[0])
        else:
            v = val.hex(" ")
        parts.append(f"{name}={v}")
    return ", ".join(parts) or "(vide)"


def describe_frame(cmd: int, payload: bytes) -> str:
    """Traduction en clair d'une trame reçue."""
    if cmd == CMD_ACK and len(payload) >= 2:
        status = {0x00: "ok", 0xFF: "refusé"}.get(payload[1], f"statut {payload[1]:02x}")
        return f"ACK de la commande {payload[0]:02x} : {status}"
    if cmd == CMD_RET_LIGHT:
        return "état lumière : " + _describe_light_tlvs(payload)
    if cmd == CMD_RET_DEVICE:
        return "infos appareil : " + _describe_device_tlvs(payload)
    if cmd == CMD_HEARTBEAT:
        return "battement (keep-alive)"
    return f"commande {cmd:02x} ({len(payload)} octets)"


def describe_tx(data: bytes) -> str:
    """Traduction en clair d'une trame envoyée."""
    if len(data) < 3 or data[0] != SYNC:
        return ""
    cmd, payload = data[1], data[3:]
    if cmd == CMD_SET_LIGHT:
        return "réglage lumière : " + _describe_light_tlvs(payload)
    return {CMD_REQ_DEVICE: "demande d'infos appareil", CMD_REQ_LIGHT: "demande d'état lumière",
            CMD_HEARTBEAT: "battement (keep-alive)", CMD_POWER: "alimentation"}.get(cmd, "")


# =============================================================================
# Couleurs et motifs
# =============================================================================

COLOR_NAMES = {
    "rouge": (255, 0, 0), "orange": (255, 80, 0), "jaune": (255, 200, 0), "vert": (0, 255, 0),
    "turquoise": (0, 255, 150), "cyan": (0, 255, 255), "bleu": (0, 0, 255), "indigo": (70, 0, 255),
    "violet": (150, 0, 255), "magenta": (255, 0, 255), "rose": (255, 0, 110), "blanc": (255, 255, 255),
}
COLOR_NAMES.update({
    "red": COLOR_NAMES["rouge"], "yellow": COLOR_NAMES["jaune"], "green": COLOR_NAMES["vert"],
    "blue": COLOR_NAMES["bleu"], "purple": COLOR_NAMES["violet"], "pink": COLOR_NAMES["rose"],
    "white": COLOR_NAMES["blanc"],
})
SWATCHES = ["rouge", "orange", "jaune", "vert", "turquoise", "cyan",
            "bleu", "indigo", "violet", "magenta", "rose", "blanc"]
DEFAULT_PALETTE = ["rouge", "orange", "jaune", "vert", "turquoise", "cyan", "bleu", "violet", "magenta", "rose"]
RANDOM_WORDS = {"aleatoire", "random", "hasard"}


def _plain(text: str) -> str:
    """minuscules sans accents : 'Aléatoire' -> 'aleatoire'."""
    t = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def random_color() -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(random.random(), 1.0, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def parse_color(text: str) -> tuple[int, int, int]:
    """'rouge', '#FF0080', 'ff0080', 'f08', '255,0,128', '255 0 128', 'aleatoire'."""
    t = _plain(text)
    if t in RANDOM_WORDS:
        return random_color()
    if t in COLOR_NAMES:
        return COLOR_NAMES[t]
    h = t.lstrip("#")
    if len(h) in (3, 6) and all(c in "0123456789abcdef" for c in h):
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    parts = [p for p in re.split(r"[\s,;/]+", t) if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        vals = tuple(int(p) for p in parts)
        if all(0 <= v <= 255 for v in vals):
            return vals  # type: ignore[return-value]
    raise ValueError(f"couleur inconnue : {text!r} (exemples : rouge, #FF0080, 255,0,128, aleatoire)")


def parse_palette(text: str) -> list[tuple[int, int, int]]:
    """Liste de noms ou de codes #RRGGBB séparés par des virgules ou des espaces."""
    items = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if not items:
        raise ValueError("palette vide")
    return [parse_color(p) for p in items]


def parse_pattern(text: str) -> int:
    t = _plain(str(text))
    t = PATTERN_ALIASES.get(t, t)
    if t in PATTERNS:
        return PATTERNS[t]
    if t.isdigit() and 0 <= int(t) <= 255:
        return int(t)
    raise ValueError(f"motif inconnu : {text!r} (choix : {', '.join(SHARED_PATTERNS)}, off)")


# =============================================================================
# Découverte des appareils
# =============================================================================

@dataclass
class Found:
    kind: str  # "box" (enceinte), "light" (PartyLight) ou "jbl" (autre appareil JBL)
    name: str
    address: str
    rssi: int | None = None
    product_id: int | None = None
    device: object = None  # BLEDevice de bleak


def classify(device, adv) -> Found | None:
    """Reconnaît une PartyBox ou un PartyLight d'après sa publicité BLE."""
    name = (getattr(adv, "local_name", None) or getattr(device, "name", None) or "").strip()
    low = name.lower().replace(" ", "")
    pid = None
    for uuid, data in (getattr(adv, "service_data", None) or {}).items():
        if str(uuid).lower() == ADV_UUID_PARTYBOX and len(data) >= 2:
            pid = data[0] | (data[1] << 8)
    uuids = {str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])}
    if not (pid is not None or SERVICE_UUID in uuids or "partybox" in low or "partylight" in low or "jbl" in low):
        return None
    if "partylight" in low:
        kind = "light"
    elif pid in PRODUCTS or "partybox" in low:
        kind = "box"
    else:
        kind = "jbl"
    label = name or (f"JBL {PRODUCTS[pid]}" if pid in PRODUCTS else "JBL (sans nom)")
    return Found(kind, label, str(device.address), getattr(adv, "rssi", None), pid, device)


def _default_scanner_factory():
    from bleak import BleakScanner
    return BleakScanner


async def discover(timeout: float = 8.0, *, box_addr: str | None = None, light_addrs=(),
                   want_box: bool = True, want_lights: bool = True, grace: float = 1.5,
                   collect_all: bool = False, scanner_factory=None):
    """Scanne et renvoie (enceinte, [lumières], [tous les JBL vus], {autres appareils}).

    S'arrête dès que tout ce qu'on cherche est trouvé (+ `grace` secondes pour attraper un
    deuxième stick), sauf avec collect_all où l'on scanne pendant tout `timeout`."""
    found: dict[str, Found] = {}
    others: dict[str, tuple[str, int | None]] = {}
    box_l = box_addr.lower() if box_addr else None
    lights_l = [a.lower() for a in light_addrs]

    def callback(device, adv):
        addr = str(device.address)
        f = classify(device, adv)
        if f is None and addr.lower() in ({box_l} | set(lights_l)):
            kind = "box" if addr.lower() == box_l else "light"
            f = Found(kind, getattr(adv, "local_name", None) or device.name or addr, addr,
                      getattr(adv, "rssi", None), None, device)
        if f is None:
            if collect_all:
                others[addr] = (getattr(adv, "local_name", None) or device.name or "?", getattr(adv, "rssi", None))
            return
        found[addr] = f

    def satisfied() -> bool:
        seen = {a.lower() for a in found}
        has_box = (box_l in seen) if box_l else any(f.kind == "box" for f in found.values())
        has_lights = set(lights_l) <= seen if lights_l else any(f.kind == "light" for f in found.values())
        return (has_box or not want_box) and (has_lights or not want_lights)

    factory = scanner_factory or _default_scanner_factory()
    start = time.monotonic()
    ok_since = None
    async with factory(detection_callback=callback):
        while time.monotonic() - start < timeout:
            await asyncio.sleep(0.2)
            if collect_all:
                continue
            if satisfied():
                ok_since = ok_since or time.monotonic()
                if time.monotonic() - ok_since >= (0 if (box_l or lights_l) else grace):
                    break

    every = list(found.values())
    box = None
    if want_box:
        if box_l:
            box = next((f for f in every if f.address.lower() == box_l), None) or Found("box", box_addr, box_addr)
        else:
            boxes = sorted((f for f in every if f.kind == "box"),
                           key=lambda f: (f.product_id != PID_310, -(f.rssi if f.rssi is not None else -999)))
            box = boxes[0] if boxes else None
    lights: list[Found] = []
    if want_lights:
        if lights_l:
            for a in light_addrs:
                lights.append(next((f for f in every if f.address.lower() == a.lower()), None) or Found("light", a, a))
        else:
            lights = sorted((f for f in every if f.kind == "light"),
                            key=lambda f: -(f.rssi if f.rssi is not None else -999))
    return box, lights, every, others


async def _find_again(dev: "JBLDevice"):
    """Retrouve un appareil perdu (même adresse, sinon même type et même nom)."""
    from bleak import BleakScanner
    d = await BleakScanner.find_device_by_address(dev.address, timeout=6.0)
    if d is not None:
        return d

    def match(device, adv):
        f = classify(device, adv)
        return f is not None and f.kind == dev.kind and f.name == dev.name

    return await BleakScanner.find_device_by_filter(match, timeout=6.0)


# =============================================================================
# Liaison avec un appareil
# =============================================================================

ACK_TIMEOUT = 1.5         # attente max d'un accusé de réception
PACE = 0.06               # pause entre deux trames vers le même appareil (l'appli fait pareil)
HEARTBEAT_INTERVAL = 5.0  # l'appli envoie AA EA 00 toutes les 5 s


class JBLError(Exception):
    pass


class Abort(Exception):
    """Arrêt propre avec un message pour l'utilisateur."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def log(msg: str = "") -> None:
    print(msg, flush=True)


def find_jbl_characteristics(services):
    """Renvoie (caractéristique d'écriture, caractéristique de notification)."""
    svcs = list(services) if services is not None else []
    svcs.sort(key=lambda s: str(s.uuid).lower() != SERVICE_UUID)
    for s in svcs:
        tx = rx = None
        for c in s.characteristics:
            u = str(c.uuid).lower()
            if u == TX_UUID:
                tx = c
            elif u == RX_UUID:
                rx = c
        if tx is not None:
            props = {p.lower() for p in (rx.properties if rx is not None else [])}
            # Certains modèles notifient sur TX au lieu de RX
            notify = rx if rx is not None and props & {"notify", "indicate"} else tx
            return tx, notify
    return None, None


def _default_client_factory():
    from bleak import BleakClient
    return BleakClient


class JBLDevice:
    """Une connexion BLE à une PartyBox ou à un PartyLight."""

    def __init__(self, target, kind: str, name: str | None = None, *, client_factory=None, finder=None,
                 connect_lock=None, verbose: bool = False, ack_timeout: float = ACK_TIMEOUT,
                 wake_tries: int = 8, wake_timeout: float = 1.0):
        self.target = target
        self.address = str(getattr(target, "address", target))
        self.kind = kind
        self.name = name or getattr(target, "name", None) or ("PartyBox" if kind == "box" else "PartyLight")
        self.label = self.name
        self.client_factory = client_factory
        self.finder = finder
        self.connect_lock = connect_lock
        self.verbose = verbose
        self.ack_timeout = ack_timeout
        self.wake_tries = wake_tries
        self.wake_timeout = wake_timeout
        self.client = None
        self.tx = None
        self.tx_response = True
        self.connected = False
        self.awake = False
        self.state: dict = {}   # ce qu'on lui a envoyé en dernier (motif, couleur)
        self.listeners = []     # fonctions(dev, cmd, payload) appelées pour chaque trame reçue
        self._reader = FrameReader()
        self._waiters: list = []
        self._lock = None
        self._closing = False

    def __repr__(self):
        return f"<JBLDevice {self.label} {self.address}>"

    # --- connexion ---------------------------------------------------------

    async def connect(self, timeout: float = 20.0) -> bool:
        """Se connecte, s'abonne aux notifications puis attend que l'appareil réponde.
        Renvoie True s'il a répondu (la 310 ignore les premières commandes après connexion)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        self._closing = False
        factory = self.client_factory or _default_client_factory()
        async with (self.connect_lock or contextlib.nullcontext()):
            client = factory(self.target, disconnected_callback=self._on_disconnect, timeout=timeout)
            await client.connect()
            try:
                tx, notify = find_jbl_characteristics(client.services)
                if tx is None:
                    raise JBLError("le service de contrôle JBL n'apparaît pas (voir README, Dépannage)")
                self.client = client
                self.tx = tx
                self.tx_response = "write" in {p.lower() for p in tx.properties}
                self._reader = FrameReader()
                try:
                    await client.start_notify(notify, self._on_notify)
                except Exception as e:
                    # sans notifications, les commandes passent quand même, mais sans accusé
                    log(f"  ! {self.label} : notifications indisponibles ({explain_error(e)}), je continue sans")
            except BaseException:
                self.client = None
                with contextlib.suppress(Exception):
                    await client.disconnect()
                raise
        self.connected = True
        self.state = {}
        return await self._wake()

    async def _wake(self) -> bool:
        for _ in range(self.wake_tries):
            got = await self.exchange(f_req_device(), match=lambda c, p: True, timeout=self.wake_timeout)
            if got is not None:
                self.awake = True
                await asyncio.sleep(0.3)  # laisse arriver la fin de la réponse
                return True
            if not self.connected:
                break
        self.awake = False
        return False

    def _on_disconnect(self, _client=None):
        was = self.connected
        self.connected = False
        self.awake = False
        for _, fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        if was and not self._closing:
            log(f"  ! {self.label} : connexion perdue (je réessaie si le programme tourne en continu)")

    async def _drop_client(self):
        client, self.client = self.client, None
        self.connected = False
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def reconnect(self):
        await self._drop_client()
        if self.finder is not None:
            found = await self.finder(self)
            if found is not None:
                self.target = found
        await self.connect()

    async def close(self):
        self._closing = True
        await self._drop_client()

    async def keep_alive(self, interval: float = HEARTBEAT_INTERVAL, reconnect: bool = True):
        """Battement toutes les `interval` secondes, et reconnexion si la liaison tombe."""
        while not self._closing:
            if self.connected:
                await asyncio.sleep(interval)
                if self.connected and not self._closing:
                    await self.heartbeat()
            elif reconnect:
                try:
                    await self.reconnect()
                    log(f"  {self.label} : reconnecté")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if self.verbose:
                        log(f"  {self.label} : reconnexion ratée ({explain_error(e)})")
                    await asyncio.sleep(5)
            else:
                return

    # --- échanges ------------------------------------------------------------

    def _on_notify(self, _char, data):
        for cmd, payload in self._reader.feed(bytes(data)):
            self._dispatch(cmd, payload)

    def _dispatch(self, cmd: int, payload: bytes):
        if self.verbose:
            log(f"    {self.label} <- {frame(cmd, payload).hex(' ')}   {describe_frame(cmd, payload)}")
        for item in list(self._waiters):
            match, fut = item
            if not fut.done() and match(cmd, payload):
                fut.set_result((cmd, payload))
                with contextlib.suppress(ValueError):
                    self._waiters.remove(item)
                break
        for fn in list(self.listeners):
            try:
                fn(self, cmd, payload)
            except Exception as e:  # un écouteur ne doit jamais casser la réception
                log(f"  ! erreur dans un écouteur : {e}")

    async def _write(self, data: bytes):
        if not self.connected or self.client is None:
            raise JBLError("non connecté")
        if self.verbose:
            log(f"    {self.label} -> {data.hex(' ')}   {describe_tx(data)}")
        await self.client.write_gatt_char(self.tx, data, response=self.tx_response)

    async def exchange(self, data: bytes, match=None, timeout: float | None = None):
        """Envoie une trame ; si `match` est donné, attend la première trame reçue qui y répond.
        Renvoie (cmd, payload) ou None si rien n'est venu à temps. Une trame à la fois par appareil."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        timeout = self.ack_timeout if timeout is None else timeout
        fut = item = None
        if match is not None:
            fut = asyncio.get_running_loop().create_future()
            item = (match, fut)
            self._waiters.append(item)
        try:
            async with self._lock:
                await self._write(data)
                try:
                    if fut is None:
                        return None
                    try:
                        return await asyncio.wait_for(fut, timeout)
                    except asyncio.TimeoutError:
                        return None
                finally:
                    await asyncio.sleep(PACE)
        finally:
            if item is not None and item in self._waiters:
                self._waiters.remove(item)

    async def command(self, data: bytes) -> str:
        """Envoie un réglage et lit l'accusé AA 00 02 <cmd> <statut>.
        Renvoie 'ok', 'rejected' (refusé), 'no_ack' (pas de réponse), 'offline' ou 'erreur : ...'."""
        cmd = data[1]
        try:
            res = await self.exchange(data, match=lambda c, p: c == CMD_ACK and len(p) >= 2 and p[0] == cmd)
        except JBLError:
            return "offline"
        except Exception as e:
            return f"erreur : {explain_error(e)}"
        if res is None:
            return "no_ack" if self.connected else "offline"
        return "ok" if res[1][1] == 0 else "rejected"

    async def request(self, data: bytes, reply_cmd: int, timeout: float = 2.0):
        """Envoie une demande et renvoie les données de la réponse <reply_cmd>, ou None."""
        try:
            res = await self.exchange(data, match=lambda c, p: c == reply_cmd, timeout=timeout)
        except Exception:
            return None
        return None if res is None else res[1]

    async def heartbeat(self):
        with contextlib.suppress(Exception):
            await self.exchange(f_heartbeat())


# =============================================================================
# Le "chef d'orchestre" : mêmes commandes, au même moment, sur tous les appareils
# =============================================================================

STATUS_TEXT = {
    "ok": "ok", "rejected": "refusé par l'appareil", "no_ack": "pas de confirmation",
    "offline": "non connecté",
}


def status_text(s: str) -> str:
    return STATUS_TEXT.get(s, s)


def combine(steps: list[tuple[str, str]]) -> str:
    bad = [(label, s) for label, s in steps if s != "ok"]
    if not bad:
        return "ok"
    if len(steps) == 1:
        return steps[0][1]
    return ", ".join(f"{label} : {status_text(s)}" for label, s in bad)


class Show:
    def __init__(self, box: JBLDevice | None = None, lights=()):
        self.box = box
        self.lights = list(lights)
        self.pattern: int | None = None  # motif commun choisi (None : la 310 garde le sien, le stick passe en Perso)
        self.color: tuple[int, int, int] | None = None
        self.on = True

    @property
    def devices(self) -> list[JBLDevice]:
        return ([self.box] if self.box else []) + self.lights

    async def _each(self, fn, devices=None):
        devs = self.devices if devices is None else list(devices)

        async def one(d):
            if not d.connected:
                return "offline"
            try:
                return await fn(d)
            except Exception as e:
                return f"erreur : {explain_error(e)}"

        statuses = await asyncio.gather(*(one(d) for d in devs))
        return list(zip(devs, statuses))

    async def _ensure_pattern(self, dev: JBLDevice, p: int) -> str:
        if dev.state.get("pattern") == p:
            return "ok"
        st = await dev.command(f_pattern(p))
        if st in ("ok", "no_ack"):
            dev.state["pattern"] = p
        return st

    async def set_color(self, rgb, pattern: int | None = None):
        rgb = tuple(max(0, min(255, int(x))) for x in rgb)
        p = pattern if pattern is not None else self.pattern

        async def apply(dev):
            steps = []
            if p is not None:
                steps.append(("motif", await self._ensure_pattern(dev, p)))
            elif dev.kind == "light":
                # sur le stick, une couleur choisie s'affiche dans le motif "Perso"
                steps.append(("motif", await self._ensure_pattern(dev, CUSTOM)))
            steps.append(("couleur", await dev.command(f_color(rgb))))
            dev.state["color"] = rgb
            return combine(steps)

        if pattern is not None:
            self.pattern = pattern
        self.color = rgb
        self.on = True
        return await self._each(apply)

    async def set_pattern(self, p: int):
        if p == 0:
            return await self.power(False)

        async def apply(dev):
            st = await dev.command(f_pattern(p))
            if st in ("ok", "no_ack"):
                dev.state["pattern"] = p
            return st

        self.pattern = p
        self.on = True
        return await self._each(apply)

    async def power(self, on: bool, pattern: int | None = None):
        async def apply(dev):
            steps = []
            if dev.kind == "box":
                # modèles récents : interrupteur ; anciens (310) : motif 0. On envoie les deux.
                steps.append(("interrupteur", await dev.command(f_main_switch(on))))
            if on:
                p = pattern if pattern is not None else (
                    self.pattern or dev.state.get("before_off")
                    or (CUSTOM if dev.kind == "light" and self.color else PATTERNS["neon"]))
                st = await dev.command(f_pattern(p))
                steps.append(("motif", st))
                if st in ("ok", "no_ack"):
                    dev.state["pattern"] = p
                if self.color and (dev.kind == "box" or p == CUSTOM):
                    steps.append(("couleur", await dev.command(f_color(self.color))))
            else:
                if dev.state.get("pattern"):
                    dev.state["before_off"] = dev.state["pattern"]
                steps.append(("motif", await dev.command(f_pattern(0))))
                dev.state["pattern"] = 0
            if dev.kind == "box" and any(s == "ok" for _, s in steps[:2]):
                return "ok"  # un des deux moyens a marché, c'est suffisant
            return combine(steps)

        if on and pattern is not None:
            self.pattern = pattern
        self.on = on
        return await self._each(apply)

    async def stick_settings(self, speed: int | None = None, intensity: int | None = None):
        async def apply(dev):
            steps = []
            if intensity is not None:
                steps.append(("intensité", await dev.command(f_intensity(intensity))))
            if speed is not None:
                steps.append(("vitesse", await dev.command(f_speed(speed))))
            return combine(steps) if steps else "ok"

        return await self._each(apply, self.lights)

    async def strobe(self, on: bool):
        if self.box is None:
            return []
        return await self._each(lambda d: d.command(f_strobe(on)), [self.box])

    async def close(self):
        for d in self.devices:
            await d.close()


async def run_party(show: Show, palette, *, interval: float = 3.0, beats=None, every: int = 1,
                    count: int | None = None, shuffle: bool = False, pattern: int | None = None,
                    on_step=None) -> int:
    """Change la couleur de tout le monde en même temps : toutes les `interval` secondes,
    ou tous les `every` battements détectés par `beats` (BeatDetector)."""
    if not palette:
        raise ValueError("palette vide")
    i = n = 0
    prev = None
    while True:
        if shuffle and len(palette) > 1:
            rgb = random.choice([c for c in palette if c != prev] or palette)
        else:
            rgb = palette[i % len(palette)]
            i += 1
        prev = rgb
        results = await show.set_color(rgb, pattern=pattern)
        n += 1
        if on_step:
            on_step(n, rgb, results)
        if count and n >= count:
            return n
        if beats is not None:
            await beats.wait(every)
        else:
            await asyncio.sleep(interval)


async def apply_mirror(show: Show, st: dict):
    """Recopie un état lumière (celui de la 310) sur les PartyLight."""
    p, rgb = st.get("pattern"), st.get("rgb")

    async def apply(dev):
        if p == 0 or st.get("on") is False:
            return await dev.command(f_pattern(0))
        steps = []
        if rgb is not None and (st.get("custom") or p == CUSTOM or p is None):
            steps.append(("motif", await show._ensure_pattern(dev, CUSTOM)))
            steps.append(("couleur", await dev.command(f_color(rgb))))
        else:
            if p is not None:
                steps.append(("motif", await show._ensure_pattern(dev, p)))
            if rgb is not None:
                steps.append(("couleur", await dev.command(f_color(rgb))))
        return combine(steps) if steps else "ok"

    return await show._each(apply, show.lights)


async def run_mirror(show: Show, *, poll: float = 1.0, probe_timeout: float = 2.5, on_change=None,
                     max_changes: int | None = None):
    """(Expérimental) Lit l'état lumière de l'enceinte et le recopie sur les PartyLight.
    Renvoie None tout de suite si l'enceinte ne donne pas son état (cas signalé sur une 310)."""
    box = show.box
    if box is None or not box.connected:
        raise JBLError("pas d'enceinte connectée")
    first = await box.request(f_req_light(), CMD_RET_LIGHT, timeout=probe_timeout)
    if first is None:
        return None
    pushed: asyncio.Queue = asyncio.Queue()

    def listener(_dev, cmd, payload):
        if cmd == CMD_RET_LIGHT:
            pushed.put_nowait(payload)

    box.listeners.append(listener)
    last, changes, payload = None, 0, first
    try:
        while True:
            if payload is not None:
                st = decode_light(payload)
                if st and st != last:
                    await apply_mirror(show, st)
                    last = st
                    changes += 1
                    if on_change:
                        on_change(st)
                    if max_changes and changes >= max_changes:
                        return last
            try:
                payload = await asyncio.wait_for(pushed.get(), poll)
            except asyncio.TimeoutError:
                payload = None
                if box.connected:
                    # la réponse arrivera par l'écouteur
                    await box.exchange(f_req_light())
    finally:
        with contextlib.suppress(ValueError):
            box.listeners.remove(listener)


# =============================================================================
# Détection des battements au micro du PC (optionnel : pip install sounddevice numpy)
# =============================================================================

def beat_available() -> bool:
    return bool(importlib.util.find_spec("sounddevice") and importlib.util.find_spec("numpy"))


class BeatDetector:
    """Repère les coups de basse (40-180 Hz) dans le son capté par le micro."""

    def __init__(self, sensitivity: float = 1.5, min_gap: float = 0.28, device=None):
        import numpy as np
        import sounddevice as sd
        self.np, self.sd = np, sd
        self.sensitivity = sensitivity
        self.min_gap = min_gap
        self.device = device
        self.samplerate, self.block = 22050, 1024
        freqs = np.fft.rfftfreq(self.block, 1 / self.samplerate)
        self._band = (freqs >= 40) & (freqs <= 180)
        self._win = np.hanning(self.block).astype("float32")
        self._hist: collections.deque = collections.deque(maxlen=32)  # ~1,5 s d'historique
        self._last = 0.0
        self._loop = None
        self._queue: asyncio.Queue | None = None
        self._stream = None
        self.count = 0

    def start(self, loop=None) -> "BeatDetector":
        self._loop = loop or asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stream = self.sd.InputStream(samplerate=self.samplerate, blocksize=self.block, channels=1,
                                           dtype="float32", device=self.device, callback=self._callback)
        self._stream.start()
        return self

    def _callback(self, indata, frames, time_info, status):
        x = indata[:, 0]
        if len(x) != self.block:
            return
        spec = self.np.abs(self.np.fft.rfft(x * self._win)) ** 2
        e = float(spec[self._band].sum())
        now = time.monotonic()
        if len(self._hist) >= 8:
            avg = sum(self._hist) / len(self._hist)
            if e > self.sensitivity * avg and e > 1e-3 and now - self._last >= self.min_gap:
                self._last = now
                self.count += 1
                self._loop.call_soon_threadsafe(self._queue.put_nowait, now)
        self._hist.append(e)

    async def wait(self, n: int = 1):
        while not self._queue.empty():  # oublie les battements arrivés pendant l'envoi
            self._queue.get_nowait()
        for _ in range(max(1, n)):
            await self._queue.get()

    def stop(self):
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None


# =============================================================================
# Télécommande web (pour le téléphone, sur le même Wi-Fi)
# =============================================================================

WEB_PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b12">
<title>Party Sync</title>
<style>
:root{--bg:#0b0b12;--card:#161622;--chip:#25253a;--text:#f2f2f7;--muted:#9a9ab0;--accent:#ff3d7f;--ok:#2ecc71;--bad:#ff5252}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0 auto;max-width:560px;background:var(--bg);color:var(--text);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px 14px 96px}
h1{font-size:21px;margin:4px 0 12px;display:flex;align-items:center;gap:10px}
#live{width:14px;height:14px;border-radius:50%;background:var(--accent);box-shadow:0 0 14px var(--accent);transition:.3s}
.devs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.dev{font-size:13px;padding:5px 10px;border-radius:999px;background:var(--card);color:var(--muted)}
.dev.on::before{content:"\25CF  ";color:var(--ok)}.dev.off::before{content:"\25CF  ";color:var(--bad)}
section{background:var(--card);border-radius:16px;padding:14px;margin-bottom:12px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px;font-weight:600}
.sw{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.sw button{aspect-ratio:1;border-radius:50%;border:2px solid rgba(255,255,255,.18);cursor:pointer;padding:0}
.sw button:active{transform:scale(.92)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.chip{padding:10px 14px;border-radius:12px;border:0;background:var(--chip);color:var(--text);font-size:15px;cursor:pointer}
.chip.sel{background:var(--accent);color:#fff}
input[type=range]{width:100%;accent-color:var(--accent)}
input[type=color]{width:54px;height:44px;border:0;background:none;padding:0;cursor:pointer}
.big{flex:1;padding:16px;border-radius:14px;border:0;font-size:17px;font-weight:600;background:var(--chip);color:var(--text);cursor:pointer}
#party{background:var(--accent);color:#fff}#party.running{background:#3a3a55}
label{font-size:14px;color:var(--muted);display:block;margin:10px 0 4px}
label.inline{display:flex;gap:8px;align-items:center;margin:0}
#msg{position:fixed;left:12px;right:12px;bottom:12px;max-width:536px;margin:0 auto;background:#25253a;border-radius:12px;padding:12px 14px;font-size:14px;color:var(--muted);min-height:44px;box-shadow:0 6px 24px rgba(0,0,0,.4)}
</style></head><body>
<h1><span id="live"></span>Party Sync</h1>
<div class="devs" id="devs"><span class="dev">connexion au PC…</span></div>

<section><h2>Couleur, partout en même temps</h2>
<div class="sw" id="sw"></div>
<div class="row"><input type="color" id="pick" value="#ff3d7f" aria-label="Autre couleur"><button class="chip" id="rand">Aléatoire</button></div>
</section>

<section><h2>Mode fête</h2>
<div class="row"><button class="big" id="party">Lancer le mode fête</button></div>
<label>Nouvelle couleur toutes les <b id="ivv">3</b> s</label>
<input type="range" id="iv" min="0.5" max="15" step="0.5" value="3">
<div class="row"><label class="inline"><input type="checkbox" id="shuf"> Ordre aléatoire</label></div>
<div class="row" id="beatrow" hidden><label class="inline"><input type="checkbox" id="beat"> Sur le rythme (micro du PC)</label></div>
</section>

<section><h2>Motif d'animation</h2><div class="row" id="pat"></div></section>

<section><h2>PartyLight : vitesse et intensité</h2>
<label>Vitesse <b id="spv">50</b></label><input type="range" id="sp" min="0" max="100" value="50">
<label>Intensité <b id="inv">100</b></label><input type="range" id="in" min="0" max="100" value="100">
</section>

<section><h2>Enceinte</h2><div class="row">
<button class="chip" data-strobe="1">Stroboscope ON</button><button class="chip" data-strobe="0">Stroboscope OFF</button></div></section>

<div class="row"><button class="big" id="on">Allumer</button><button class="big" id="off">Éteindre</button></div>
<div id="msg">Prêt.</div>
<script>
const $=s=>document.querySelector(s);
let st=null,built=false;
const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function msg(t){$('#msg').textContent=t;}
async function api(path,body){
  msg('…');
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    const j=await r.json();
    msg(j.message||(j.ok?'OK':'Erreur'));
    if(j.state)render(j.state);
  }catch(e){msg('Le PC ne répond pas ('+e.message+')');}
}
function partyBody(on){return{on:on,interval:parseFloat($('#iv').value),beat:$('#beat').checked,shuffle:$('#shuf').checked};}
function build(s){
  $('#sw').innerHTML=s.swatches.map(c=>`<button style="background:${c.hex}" title="${esc(c.name)}" aria-label="${esc(c.name)}" data-c="${c.hex}"></button>`).join('');
  $('#sw').onclick=e=>{const c=e.target.dataset.c;if(c)api('/api/color',{color:c});};
  $('#pat').innerHTML=s.patterns.map(p=>`<button class="chip" data-p="${p.id}">${esc(p.name)}</button>`).join('');
  $('#pat').onclick=e=>{const p=e.target.dataset.p;if(p)api('/api/pattern',{pattern:p});};
  $('#iv').value=s.party.interval;$('#ivv').textContent=s.party.interval;
  built=true;
}
function render(s){
  st=s;if(!built)build(s);
  $('#devs').innerHTML=s.devices.length?s.devices.map(d=>`<span class="dev ${d.connected?'on':'off'}">${esc(d.name)}</span>`).join(''):'<span class="dev off">aucun appareil</span>';
  $('#party').textContent=s.party.on?'Arrêter le mode fête':'Lancer le mode fête';
  $('#party').classList.toggle('running',s.party.on);
  $('#beatrow').hidden=!s.beat_available;
  document.querySelectorAll('#pat .chip').forEach(b=>b.classList.toggle('sel',b.dataset.p===s.pattern));
  if(s.color){$('#live').style.background=s.color;$('#live').style.boxShadow='0 0 14px '+s.color;}
}
$('#pick').addEventListener('change',e=>api('/api/color',{color:e.target.value}));
$('#rand').onclick=()=>api('/api/color',{color:'aleatoire'});
$('#iv').oninput=e=>{$('#ivv').textContent=e.target.value;};
$('#iv').onchange=()=>{if(st&&st.party.on)api('/api/party',partyBody(true));};
$('#shuf').onchange=$('#beat').onchange=()=>{if(st&&st.party.on)api('/api/party',partyBody(true));};
$('#party').onclick=()=>api('/api/party',partyBody(!(st&&st.party.on)));
$('#sp').oninput=e=>{$('#spv').textContent=e.target.value;};
$('#sp').onchange=e=>api('/api/stick',{speed:+e.target.value});
$('#in').oninput=e=>{$('#inv').textContent=e.target.value;};
$('#in').onchange=e=>api('/api/stick',{intensity:+e.target.value});
document.querySelectorAll('[data-strobe]').forEach(b=>b.onclick=()=>api('/api/strobe',{on:b.dataset.strobe==='1'}));
$('#on').onclick=()=>api('/api/power',{on:true});
$('#off').onclick=()=>api('/api/power',{on:false});
async function poll(){try{const r=await fetch('/api/state');const j=await r.json();render(j.state);}catch(e){msg('PC injoignable…');}}
poll();setInterval(poll,3000);
</script>
</body></html>
"""


def summarize(title: str, results) -> str:
    if not results:
        return f"{title} : aucun appareil concerné"
    return f"{title} → " + " · ".join(f"{d.label} : {status_text(s)}" for d, s in results)


class WebRemote:
    def __init__(self, show: Show, *, beat_factory=None, interval: float = 3.0, palette=None):
        self.show = show
        self.beat_factory = beat_factory
        self.palette = palette or [COLOR_NAMES[n] for n in DEFAULT_PALETTE]
        self.party = {"on": False, "interval": interval, "beat": False, "shuffle": False}
        self.party_task: asyncio.Task | None = None
        self.beats = None

    def state(self) -> dict:
        s = self.show
        return {
            "devices": [{"name": d.label, "kind": d.kind, "connected": d.connected, "awake": d.awake}
                        for d in s.devices],
            "party": dict(self.party),
            "color": rgb_hex(s.color) if s.color else None,
            "pattern": PATTERN_NAMES.get(s.pattern) if s.pattern is not None else None,
            "on": s.on,
            "swatches": [{"name": n, "hex": rgb_hex(COLOR_NAMES[n])} for n in SWATCHES],
            "patterns": [{"id": p, "name": PATTERN_LABELS[p]} for p in SHARED_PATTERNS],
            "beat_available": self.beat_factory is not None,
        }

    async def stop_party(self):
        task, self.party_task = self.party_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.party["on"] = False

    async def start_party(self, interval=None, beat=None, shuffle=None) -> str:
        await self.stop_party()
        if interval is not None:
            self.party["interval"] = max(0.3, min(60.0, float(interval)))
        if shuffle is not None:
            self.party["shuffle"] = bool(shuffle)
        if beat is not None:
            self.party["beat"] = bool(beat) and self.beat_factory is not None
        beats = None
        if self.party["beat"]:
            if self.beats is None:
                try:
                    self.beats = self.beat_factory()
                except Exception as e:
                    self.party["beat"] = False
                    raise ValueError(f"micro inaccessible : {e}")
            beats = self.beats

        async def runner():
            try:
                await run_party(self.show, self.palette, interval=self.party["interval"], beats=beats,
                                shuffle=self.party["shuffle"])
            finally:
                self.party["on"] = False

        self.party_task = asyncio.create_task(runner())
        self.party["on"] = True
        how = "sur le rythme" if beats else f"toutes les {self.party['interval']:g} s"
        return f"Mode fête lancé ({how})"

    async def handle_api(self, method: str, path: str, data: dict):
        if path == "/api/state":
            return 200, {"ok": True, "state": self.state()}
        if method != "POST":
            return 405, {"ok": False, "message": "méthode non gérée"}
        if path == "/api/color":
            rgb = parse_color(str(data.get("color", "")))
            await self.stop_party()
            msg = summarize(f"Couleur {rgb_hex(rgb)}", await self.show.set_color(rgb))
        elif path == "/api/pattern":
            p = parse_pattern(str(data.get("pattern", "")))
            msg = summarize(f"Motif {PATTERN_NAMES.get(p, p)}", await self.show.set_pattern(p))
        elif path == "/api/power":
            on = bool(data.get("on"))
            if not on:
                await self.stop_party()
            msg = summarize("Allumage" if on else "Extinction", await self.show.power(on))
        elif path == "/api/party":
            if data.get("on"):
                msg = await self.start_party(data.get("interval"), data.get("beat"), data.get("shuffle"))
            else:
                await self.stop_party()
                msg = "Mode fête arrêté"
        elif path == "/api/stick":
            speed, intensity = data.get("speed"), data.get("intensity")
            speed = None if speed is None else int(speed)
            intensity = None if intensity is None else int(intensity)
            what = "Vitesse" if speed is not None else "Intensité"
            msg = summarize(what, await self.show.stick_settings(speed=speed, intensity=intensity))
        elif path == "/api/strobe":
            on = bool(data.get("on"))
            msg = summarize("Stroboscope " + ("ON" if on else "OFF"), await self.show.strobe(on))
        else:
            return 404, {"ok": False, "message": "inconnu"}
        return 200, {"ok": True, "message": msg, "state": self.state()}


async def serve_http(remote: WebRemote, host: str, port: int):
    """Mini serveur HTTP (pas de dépendance) : la page et une petite API JSON."""
    reasons = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
               405: "Method Not Allowed", 500: "Internal Server Error"}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0].upper(), parts[1].split("?", 1)[0]
            length = 0
            while True:
                h = await asyncio.wait_for(reader.readline(), 15)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                if k.strip().lower() == "content-length":
                    length = int(v.strip() or 0)
            body = await reader.readexactly(length) if 0 < length <= 65536 else b""
            if method == "GET" and path in ("/", "/index.html"):
                status, ctype, out = 200, "text/html; charset=utf-8", WEB_PAGE.encode("utf-8")
            elif path.startswith("/api/"):
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                try:
                    status, payload = await remote.handle_api(method, path, data)
                except (ValueError, TypeError) as e:
                    status, payload = 400, {"ok": False, "message": str(e), "state": remote.state()}
                except Exception as e:
                    status, payload = 500, {"ok": False, "message": f"Erreur : {e}", "state": remote.state()}
                ctype = "application/json; charset=utf-8"
                out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            elif path == "/favicon.ico":
                status, ctype, out = 204, "text/plain", b""
            else:
                status, ctype, out = 404, "text/plain; charset=utf-8", "Introuvable".encode("utf-8")
            head = (f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\nContent-Type: {ctype}\r\n"
                    f"Content-Length: {len(out)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
            writer.write(head.encode("latin-1") + out)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    return await asyncio.start_server(handler, host, port)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # aucun paquet n'est envoyé : ça choisit juste l'interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# =============================================================================
# Ligne de commande
# =============================================================================

USE_ANSI = False


def _setup_console():
    global USE_ANSI
    if os.name == "nt":
        os.system("")  # active les couleurs ANSI dans la console Windows
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")
    USE_ANSI = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def swatch(rgb) -> str:
    if not USE_ANSI:
        return ""
    r, g, b = rgb
    return f"\x1b[48;2;{r};{g};{b}m    \x1b[0m "


def explain_error(e: BaseException) -> str:
    name = type(e).__name__
    text = str(e) or name
    low = text.lower()
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "délai dépassé (trop loin ? appli JBL encore connectée ?)"
    if name == "BleakDeviceNotFoundError" or "not found" in low:
        return "introuvable (éteint, hors de portée, ou déjà connecté à l'appli JBL ?)"
    if isinstance(e, JBLError):
        return text
    return f"{name} : {text}"


def bluetooth_help(e: BaseException) -> str:
    text = f"{type(e).__name__} : {e}"
    tips = ["Le Bluetooth de ce PC ne répond pas.", f"  ({text})",
            "  - Vérifie que le Bluetooth est activé (ou branche un dongle Bluetooth 4.0+ / BLE)."]
    if sys.platform.startswith("linux"):
        tips.append("  - Linux : sudo systemctl start bluetooth  (et ton utilisateur doit pouvoir utiliser BlueZ).")
    elif sys.platform == "darwin":
        tips.append("  - macOS : autorise le Terminal dans Réglages > Confidentialité > Bluetooth.")
    return "\n".join(tips)


NOTHING_FOUND = """Aucun appareil JBL trouvé. Vérifie que :
  - l'enceinte et le stick sont allumés et à quelques mètres du PC ;
  - l'appli JBL PartyBox est complètement fermée sur ton téléphone (forcer l'arrêt sur Android) :
    ces appareils n'acceptent qu'une seule connexion de contrôle à la fois ;
  - tu peux aussi lancer :  python jbl_sync.py scan --all"""

MIRROR_UNSUPPORTED = """La 310 ne renvoie pas l'état de ses lumières (le projet qui l'a testée l'avait aussi constaté),
donc le stick ne peut pas "copier" ce que fait l'enceinte toute seule.
Fais l'inverse : laisse le programme choisir les couleurs pour les deux :
  python jbl_sync.py party        (ou)        python jbl_sync.py web"""


def print_results(title: str, results) -> None:
    log(title)
    if not results:
        log("  (aucun appareil concerné)")
    for dev, st in results:
        mark = "ok" if st == "ok" else " !"
        log(f"  [{mark}] {dev.label:<26} {status_text(st)}")


def exit_code(results) -> int:
    return 0 if results and all(s in ("ok", "no_ack") for _, s in results) else 1


def _label_duplicates(devices: list[JBLDevice]) -> None:
    counts = collections.Counter(d.name for d in devices)
    seen: collections.Counter = collections.Counter()
    for d in devices:
        if counts[d.name] > 1:
            seen[d.name] += 1
            d.label = f"{d.name} ({seen[d.name]})"


async def open_show(args, keep: bool = False) -> Show:
    """Trouve les appareils, s'y connecte (l'un après l'autre : BlueZ préfère) et renvoie un Show.
    keep=True : garde aussi ceux qui n'ont pas pu se connecter (ils seront retentés)."""
    want_box, want_lights = not args.no_box, not args.no_stick
    if not (want_box or want_lights):
        raise Abort("--no-box et --no-stick ensemble : il ne reste rien à piloter.")
    log(f"Recherche des appareils JBL ({args.scan_time:g} s max)...")
    try:
        box_f, lights_f, _, _ = await discover(args.scan_time, box_addr=args.box, light_addrs=args.stick or (),
                                               want_box=want_box, want_lights=want_lights)
    except Exception as e:
        raise Abort(bluetooth_help(e))
    if want_box and box_f is None:
        log("  ! Pas de PartyBox trouvée : je continue sans elle.")
    if want_lights and not lights_f:
        log("  ! Pas de PartyLight trouvé : je continue sans.")
    lock = asyncio.Lock()
    common = dict(verbose=args.verbose, finder=_find_again, connect_lock=lock)
    box = JBLDevice(box_f.device or box_f.address, "box", box_f.name, **common) if box_f else None
    lights = [JBLDevice(f.device or f.address, "light", f.name, **common) for f in lights_f]
    devices = ([box] if box else []) + lights
    if not devices:
        raise Abort(NOTHING_FOUND, 2)
    _label_duplicates(devices)
    log("Connexion...")
    for d in devices:
        try:
            awake = await d.connect()
        except Exception as e:
            log(f"  [ !] {d.label:<26} connexion impossible : {explain_error(e)}")
            continue
        if awake:
            log(f"  [ok] {d.label:<26} connecté")
        else:
            log(f"  [ ?] {d.label:<26} connecté mais muet (appli JBL encore ouverte ? enceinte en veille ?)")
    if not keep:
        box = box if box is not None and box.connected else None
        lights = [d for d in lights if d.connected]
        if box is None and not lights:
            raise Abort("Aucune connexion n'a abouti. Ferme l'appli JBL sur le téléphone, rapproche le PC "
                        "et réessaie (voir README, Dépannage).", 2)
    return Show(box, lights)


async def cmd_scan(args) -> int:
    log(f"Recherche pendant {args.scan_time:g} s...")
    try:
        box, lights, every, others = await discover(args.scan_time, collect_all=True)
    except Exception as e:
        raise Abort(bluetooth_help(e))
    order = {"box": 0, "light": 1, "jbl": 2}
    kinds = {"box": "ENCEINTE", "light": "LUMIÈRE", "jbl": "autre JBL"}
    for f in sorted(every, key=lambda f: (order[f.kind], -(f.rssi if f.rssi is not None else -999))):
        prod = ""
        if f.product_id is not None:
            prod = f"produit {f.product_id:04x}" + (f" ({PRODUCTS[f.product_id]})" if f.product_id in PRODUCTS else "")
        rssi = f"{f.rssi} dBm" if f.rssi is not None else ""
        log(f"  [{kinds[f.kind]:<9}] {f.name:<26} {f.address:<38} {rssi:>8}  {prod}")
    if args.all and others:
        log("\nAutres appareils BLE :")
        for addr, (name, rssi) in sorted(others.items(), key=lambda kv: -(kv[1][1] or -999)):
            log(f"  {name:<30} {addr:<38} {rssi if rssi is not None else '':>5} dBm")
    if not every:
        log(NOTHING_FOUND)
        return 2
    nb = sum(f.kind == "box" for f in every), sum(f.kind == "light" for f in every)
    log(f"\n→ {nb[0]} enceinte(s), {nb[1]} PartyLight. Essaie par exemple :  python jbl_sync.py color rouge")
    return 0


async def cmd_color(args) -> int:
    rgb = parse_color(" ".join(args.color))
    pattern = parse_pattern(args.pattern) if args.pattern else None
    show = await open_show(args)
    try:
        res = await show.set_color(rgb, pattern=pattern)
        print_results(f"Couleur {swatch(rgb)}{rgb_hex(rgb)}", res)
        return exit_code(res)
    finally:
        await show.close()


async def cmd_pattern(args) -> int:
    p = parse_pattern(args.pattern)
    rgb = parse_color(" ".join(args.color)) if args.color else None
    show = await open_show(args)
    try:
        res = await show.set_pattern(p)
        print_results(f"Motif {PATTERN_NAMES.get(p, p)}", res)
        if rgb is not None and p != 0:
            res = await show.set_color(rgb, pattern=p)
            print_results(f"Couleur {swatch(rgb)}{rgb_hex(rgb)}", res)
        return exit_code(res)
    finally:
        await show.close()


async def cmd_on(args) -> int:
    pattern = parse_pattern(args.pattern) if args.pattern else None
    rgb = parse_color(" ".join(args.color)) if args.color else None
    show = await open_show(args)
    try:
        show.color = rgb
        res = await show.power(True, pattern=pattern)
        print_results("Allumage", res)
        return exit_code(res)
    finally:
        await show.close()


async def cmd_off(args) -> int:
    show = await open_show(args)
    try:
        res = await show.power(False)
        print_results("Extinction", res)
        return exit_code(res)
    finally:
        await show.close()


async def cmd_stick(args) -> int:
    if args.speed is None and args.intensity is None:
        raise Abort("Indique --vitesse et/ou --intensite (0 à 100).")
    args.no_box = True
    show = await open_show(args)
    try:
        res = await show.stick_settings(speed=args.speed, intensity=args.intensity)
        print_results("Réglages PartyLight", res)
        return exit_code(res)
    finally:
        await show.close()


async def cmd_strobe(args) -> int:
    args.no_stick = True
    show = await open_show(args)
    try:
        res = await show.strobe(args.state == "on")
        print_results(f"Stroboscope {args.state.upper()}", res)
        return exit_code(res)
    finally:
        await show.close()


def _mic(args):
    """--mic 3 -> périphérique n°3 ; --mic "USB" -> premier micro dont le nom contient USB."""
    m = getattr(args, "mic", None)
    return int(m) if isinstance(m, str) and m.strip().isdigit() else m


def _start_beats(args):
    if not beat_available():
        raise Abort("Pour suivre le rythme il faut :  pip install sounddevice numpy")
    try:
        return BeatDetector(args.sensitivity, device=_mic(args)).start()
    except Exception as e:
        raise Abort(f"Micro inaccessible : {e}\n(liste des micros : python -m sounddevice ; puis --mic NUMERO)")


async def _keep_alive_all(show: Show) -> list:
    return [asyncio.create_task(d.keep_alive()) for d in show.devices]


async def _stop(tasks) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


async def cmd_party(args) -> int:
    palette = parse_palette(args.palette) if args.palette else [COLOR_NAMES[n] for n in DEFAULT_PALETTE]
    pattern = parse_pattern(args.pattern) if args.pattern else None
    beats = _start_beats(args) if args.beat else None
    show = await open_show(args, keep=True)
    tasks = await _keep_alive_all(show)
    end = "\n" if args.verbose or not USE_ANSI else ""

    def step(n, rgb, results):
        states = "  ".join(f"{d.label} : {'ok' if s == 'ok' else status_text(s)}" for d, s in results)
        beat_info = f"  (battements : {beats.count})" if beats else ""
        print(f"\r  #{n:<4} {swatch(rgb)}{rgb_hex(rgb)}   {states}{beat_info}\x1b[K" if USE_ANSI
              else f"  #{n:<4} {rgb_hex(rgb)}   {states}{beat_info}", end=end, flush=True)

    how = f"sur le rythme (1 couleur tous les {args.every} battement(s))" if beats else f"toutes les {args.interval:g} s"
    log(f"Mode fête : nouvelle couleur {how}. Ctrl+C pour arrêter.")
    try:
        await run_party(show, palette, interval=args.interval, beats=beats, every=args.every, count=args.count,
                        shuffle=args.shuffle, pattern=pattern, on_step=step)
    finally:
        print()
        if beats:
            beats.stop()
        await _stop(tasks)
        await show.close()
    return 0


async def cmd_mirror(args) -> int:
    show = await open_show(args, keep=True)
    if show.box is None or not show.box.connected:
        await show.close()
        raise Abort("Le mode miroir a besoin de l'enceinte connectée.")
    if not show.lights:
        await show.close()
        raise Abort("Le mode miroir a besoin d'au moins un PartyLight.")
    tasks = await _keep_alive_all(show)

    def changed(st):
        parts = []
        if "pattern" in st:
            parts.append(f"motif {PATTERN_NAMES.get(st['pattern'], st['pattern'])}")
        if "rgb" in st:
            parts.append(f"couleur {swatch(st['rgb'])}{rgb_hex(st['rgb'])}")
        log("  La 310 affiche : " + (", ".join(parts) or "?") + "  → recopié sur le(s) PartyLight")

    try:
        log("Mode miroir (expérimental) : je demande à la 310 l'état de ses lumières...")
        res = await run_mirror(show, poll=args.poll, on_change=changed)
        if res is None:
            log(MIRROR_UNSUPPORTED)
            return 2
        return 0
    finally:
        await _stop(tasks)
        await show.close()


async def cmd_web(args) -> int:
    show = await open_show(args, keep=True)
    tasks = await _keep_alive_all(show)
    factory = None
    if beat_available():
        def factory():
            return BeatDetector(args.sensitivity, device=_mic(args)).start()
    remote = WebRemote(show, beat_factory=factory, interval=args.interval)
    try:
        server = await serve_http(remote, args.host, args.port)
    except OSError as e:
        await _stop(tasks)
        await show.close()
        raise Abort(f"Impossible d'ouvrir le port {args.port} : {e} (essaie --port 8080)")
    url_host = lan_ip() if args.host in ("0.0.0.0", "") else args.host
    log("")
    log(f"Télécommande prête : sur ton téléphone (même Wi-Fi), ouvre  http://{url_host}:{args.port}")
    log("Ctrl+C pour arrêter.")
    try:
        async with server:
            await server.serve_forever()
    finally:
        await remote.stop_party()
        if remote.beats:
            remote.beats.stop()
        await _stop(tasks)
        await show.close()
    return 0


EXPLORE_HELP = """Commandes :
  aa 31 00            envoie une trame en hexadécimal sur la caractéristique de commande JBL
  info | lumiere | hb raccourcis pour  aa 11 00 | aa 31 00 | aa ea 00
  w <uuid> <hex>      écrit sur une autre caractéristique (fin d'UUID acceptée, ex. w 0af1 01)
  r <uuid>            lit une caractéristique
  aide                cette aide          q   quitter
Ne tape que des trames dont tu comprends le rôle : une commande inconnue peut dérégler l'appareil."""


async def cmd_explore(args) -> int:
    """Outil de rétro-ingénierie : liste le GATT, affiche les notifications, envoie des trames brutes."""
    from bleak import BleakClient
    target, name = args.address, args.address
    if not target:
        log(f"Recherche des appareils JBL ({args.scan_time:g} s)...")
        try:
            _, _, every, _ = await discover(args.scan_time, collect_all=True)
        except Exception as e:
            raise Abort(bluetooth_help(e))
        if not every:
            raise Abort(NOTHING_FOUND, 2)
        if len(every) == 1:
            pick = every[0]
        else:
            for i, f in enumerate(every, 1):
                log(f"  {i}. {f.name}  {f.address}  ({f.kind})")
            raw = await asyncio.get_running_loop().run_in_executor(None, input, "Numéro de l'appareil : ")
            try:
                pick = every[int(raw.strip()) - 1]
            except (ValueError, IndexError):
                raise Abort("Choix invalide.")
        target, name = pick.device or pick.address, pick.name
    logname = f"explore-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logf = open(logname, "w", encoding="utf-8")

    def out(s: str):
        print(s, flush=True)
        logf.write(s + "\n")
        logf.flush()

    def ts() -> str:
        return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"

    reader = FrameReader()
    client = BleakClient(target, timeout=20)
    try:
        await client.connect()
    except Exception as e:
        logf.close()
        raise Abort(f"Connexion impossible : {explain_error(e)}")
    out(f"Connecté à {name}. Journal : {logname}")
    chars = {}
    tx = None
    try:
        for s in client.services:
            out(f"[service] {s.uuid}  {getattr(s, 'description', '')}")
            for c in s.characteristics:
                chars[str(c.uuid).lower()] = c
                line = f"    [{c.handle:04x}] {c.uuid}  ({', '.join(c.properties)})"
                if "read" in c.properties:
                    try:
                        v = bytes(await client.read_gatt_char(c))
                        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in v)
                        line += f"  = {v.hex(' ')}  |{txt}|"
                    except Exception as e:
                        line += f"  (lecture impossible : {e})"
                out(line)
                if str(c.uuid).lower() == TX_UUID:
                    tx = c

        def on_notify(char, data):
            data = bytes(data)
            out(f"{ts()} <- [{str(char.uuid)[-4:]}] {data.hex(' ')}")
            for cmd, payload in reader.feed(data):
                out(f"             {describe_frame(cmd, payload)}")

        for c in chars.values():
            if {"notify", "indicate"} & set(c.properties):
                try:
                    await client.start_notify(c, on_notify)
                    out(f"abonné aux notifications de {c.uuid}")
                except Exception as e:
                    out(f"(abonnement impossible à {c.uuid} : {e})")
        if tx is None:
            out("! Pas de caractéristique de commande JBL ici : utilise  w <uuid> <hex>.")
        out(EXPLORE_HELP)

        async def heartbeat():
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                with contextlib.suppress(Exception):
                    await client.write_gatt_char(tx, f_heartbeat(), response="write" in tx.properties)

        hb = asyncio.create_task(heartbeat()) if tx is not None and not args.no_heartbeat else None
        loop = asyncio.get_running_loop()

        def find(uuid_part: str):
            u = uuid_part.lower()
            return next((c for k, c in chars.items() if k == u or k.endswith(u)), None)

        try:
            while client.is_connected:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                low = _plain(line)
                if not line:
                    continue
                if low in ("q", "quit", "exit", "quitter"):
                    break
                if low in ("aide", "help", "h", "?"):
                    out(EXPLORE_HELP)
                    continue
                try:
                    shortcuts = {"info": "aa1100", "lumiere": "aa3100", "light": "aa3100", "hb": "aaea00"}
                    if low.startswith("r "):
                        c = find(line.split()[1])
                        if c is None:
                            out("caractéristique inconnue")
                            continue
                        v = bytes(await client.read_gatt_char(c))
                        out(f"{ts()} lu [{str(c.uuid)[-4:]}] {v.hex(' ')}")
                        continue
                    if low.startswith("w "):
                        _, uuid_part, hexs = line.split(maxsplit=2)
                        c = find(uuid_part)
                        if c is None:
                            out("caractéristique inconnue")
                            continue
                    else:
                        c, hexs = tx, shortcuts.get(low, line)
                        if c is None:
                            out("pas de caractéristique de commande : utilise  w <uuid> <hex>")
                            continue
                    data = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", hexs))
                    await client.write_gatt_char(c, data, response="write" in c.properties)
                    out(f"{ts()} -> [{str(c.uuid)[-4:]}] {data.hex(' ')}   {describe_tx(data)}")
                except ValueError:
                    out("hexadécimal invalide (exemple : aa 31 00)")
                except Exception as e:
                    out(f"erreur : {explain_error(e)}")
        finally:
            if hb:
                hb.cancel()
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        logf.close()
    return 0


def _pct(text: str) -> int:
    v = int(text)
    if not 0 <= v <= 100:
        raise argparse.ArgumentTypeError("entre 0 et 100")
    return v


EXAMPLES = """exemples :
  python jbl_sync.py scan
  python jbl_sync.py color rouge
  python jbl_sync.py color "#FF0080" --motif neon
  python jbl_sync.py party --interval 2
  python jbl_sync.py party --beat               (suit le rythme grâce au micro du PC)
  python jbl_sync.py web                        (télécommande sur le téléphone)
  python jbl_sync.py off
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("appareils")
    g.add_argument("--box", metavar="ADRESSE", help="adresse de la PartyBox (sinon détection automatique)")
    g.add_argument("--stick", metavar="ADRESSE", action="append",
                   help="adresse d'un PartyLight (répétable ; sinon tous ceux trouvés)")
    g.add_argument("--no-box", action="store_true", help="ne pas utiliser l'enceinte")
    g.add_argument("--no-stick", action="store_true", help="ne pas utiliser les PartyLight")
    g.add_argument("--scan-time", type=float, default=8.0, metavar="S", help="recherche max en secondes (8)")
    g.add_argument("-v", "--verbose", action="store_true", help="affiche les trames envoyées et reçues")

    beat = argparse.ArgumentParser(add_help=False)
    b = beat.add_argument_group("rythme")
    b.add_argument("--interval", type=float, default=3.0, metavar="S", help="secondes entre deux couleurs (3)")
    b.add_argument("--sensitivity", "--sensibilite", dest="sensitivity", type=float, default=1.5, metavar="X",
                   help="détection des battements : plus petit = plus sensible (1.5)")
    b.add_argument("--mic", default=None, help="numéro ou nom du micro (voir : python -m sounddevice)")

    p = argparse.ArgumentParser(
        prog="jbl_sync.py", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES,
        description="Synchronise les lumières d'une JBL PartyBox 310 et de JBL PartyLight Stick.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMANDE")

    s = sub.add_parser("scan", parents=[common], help="liste les appareils JBL à portée")
    s.add_argument("--all", action="store_true", help="affiche aussi les autres appareils Bluetooth")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("color", aliases=["couleur"], parents=[common], help="même couleur partout")
    s.add_argument("color", nargs="+", metavar="COULEUR", help="rouge, #FF0080, 255,0,128, aleatoire...")
    s.add_argument("--pattern", "--motif", dest="pattern", metavar="MOTIF", help="motif commun (neon, loop...)")
    s.set_defaults(func=cmd_color)

    s = sub.add_parser("pattern", aliases=["motif"], parents=[common], help="même motif d'animation partout")
    s.add_argument("pattern", metavar="MOTIF", help=", ".join(SHARED_PATTERNS) + ", off")
    s.add_argument("--color", "--couleur", dest="color", nargs="+", metavar="COULEUR")
    s.set_defaults(func=cmd_pattern)

    s = sub.add_parser("on", aliases=["allumer"], parents=[common], help="allume les lumières")
    s.add_argument("--pattern", "--motif", dest="pattern", metavar="MOTIF")
    s.add_argument("--color", "--couleur", dest="color", nargs="+", metavar="COULEUR")
    s.set_defaults(func=cmd_on)

    s = sub.add_parser("off", aliases=["eteindre"], parents=[common], help="éteint les lumières")
    s.set_defaults(func=cmd_off)

    s = sub.add_parser("stick", parents=[common], help="vitesse / intensité des PartyLight")
    s.add_argument("--speed", "--vitesse", dest="speed", type=_pct, metavar="0-100")
    s.add_argument("--intensity", "--intensite", dest="intensity", type=_pct, metavar="0-100")
    s.set_defaults(func=cmd_stick)

    s = sub.add_parser("strobe", aliases=["strobo"], parents=[common], help="stroboscope de l'enceinte")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_strobe)

    s = sub.add_parser("party", aliases=["fete"], parents=[common, beat],
                       help="change de couleur en rythme, partout en même temps")
    s.add_argument("--palette", metavar="LISTE", help="ex. rouge,bleu,#00FF88 (défaut : arc-en-ciel)")
    s.add_argument("--beat", "--rythme", dest="beat", action="store_true", help="suit les battements (micro du PC)")
    s.add_argument("--every", "--tous-les", dest="every", type=int, default=1, metavar="N",
                   help="avec --beat : une couleur tous les N battements (1)")
    s.add_argument("--shuffle", "--melange", dest="shuffle", action="store_true", help="ordre aléatoire")
    s.add_argument("--pattern", "--motif", dest="pattern", metavar="MOTIF", help="motif commun pendant la fête")
    s.add_argument("--count", type=int, default=None, metavar="N", help="s'arrête après N couleurs (pour tester)")
    s.set_defaults(func=cmd_party)

    s = sub.add_parser("mirror", aliases=["miroir"], parents=[common],
                       help="(expérimental) recopie l'état lumière de la 310 sur le stick")
    s.add_argument("--poll", type=float, default=1.0, metavar="S", help="intervalle de lecture (1 s)")
    s.set_defaults(func=cmd_mirror)

    s = sub.add_parser("web", parents=[common, beat], help="télécommande dans le navigateur du téléphone")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accessible depuis le Wi-Fi ; 127.0.0.1 = ce PC seul")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("explore", parents=[common], help="rétro-ingénierie : GATT, notifications, trames brutes")
    s.add_argument("address", nargs="?", metavar="ADRESSE")
    s.add_argument("--no-heartbeat", action="store_true", help="ne pas envoyer AA EA 00 toutes les 5 s")
    s.set_defaults(func=cmd_explore)
    return p


def main(argv=None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if importlib.util.find_spec("bleak") is None:
        log("Il manque la bibliothèque Bluetooth « bleak » :  pip install bleak")
        return 1
    try:
        return asyncio.run(args.func(args)) or 0
    except Abort as e:
        log(f"\n{e}")
        return e.code
    except ValueError as e:
        log(f"Erreur : {e}")
        return 1
    except KeyboardInterrupt:
        log("\nArrêt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
