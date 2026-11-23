"""Tests de jbl_sync.py avec de faux appareils (aucun Bluetooth nécessaire).

    python -m unittest test_jbl_sync -v
"""

import asyncio
import json
import time
import unittest

import jbl_sync as j


# ---------------------------------------------------------------------------
# Faux appareils : ils répondent comme les vrais (accusés AA 00 02 <cmd> <statut>, etc.)
# ---------------------------------------------------------------------------

class FakeChar:
    def __init__(self, uuid, props, handle):
        self.uuid, self.properties, self.handle = uuid, props, handle


class FakeService:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics, self.description = uuid, chars, "fake"


class FakeDevice:
    def __init__(self, name, address, *, ignore_first=0, silent=False, no_ack_for=(), reject_tags=(),
                 answers_light=True, split_replies=False, notify_on_tx=False, unreachable=False):
        self.name, self.address = name, address
        self.ignore_first, self.silent = ignore_first, silent
        self.no_ack_for, self.reject_tags = set(no_ack_for), set(reject_tags)
        self.answers_light, self.split_replies = answers_light, split_replies
        self.notify_on_tx, self.unreachable = notify_on_tx, unreachable
        self.writes = []
        self.received = 0
        self.client = None
        self.light = {"pattern": 2, "rgb": (255, 0, 0), "custom": 0}

    def light_reply(self):
        body = bytes([0, j.L_PATTERN, 1, self.light["pattern"], j.L_COLOR, 3, *self.light["rgb"],
                      j.L_CUSTOM_COLOR, 1, self.light["custom"], j.L_PATTERNS_AVAILABLE, 3, 2, 8, 9])
        return j.frame(j.CMD_RET_LIGHT, body)

    def respond(self, data):
        self.received += 1
        if self.silent or self.received <= self.ignore_first:
            return []
        cmd = data[1]
        if cmd == j.CMD_REQ_DEVICE:
            # vraie réponse vue sur une PartyBox 310 : AA 12 05 00 44 02 16 80
            return [bytes.fromhex("aa1205004402" "1680")]
        if cmd == j.CMD_REQ_LIGHT:
            return [self.light_reply()] if self.answers_light else []
        if cmd in self.no_ack_for:
            return []
        status = 0
        if cmd == j.CMD_SET_LIGHT:
            tags = j.parse_tlv(data[3:])
            if set(tags) & self.reject_tags:
                status = 0xFF
            else:
                if j.L_PATTERN in tags:
                    self.light["pattern"] = tags[j.L_PATTERN][0]
                if j.L_COLOR in tags:
                    self.light["rgb"] = tuple(tags[j.L_COLOR])
                if j.L_CUSTOM_COLOR in tags:
                    self.light["custom"] = tags[j.L_CUSTOM_COLOR][0]
        return [j.frame(j.CMD_ACK, bytes([cmd, status]))]

    def sent(self, cmd=None):
        """Trames reçues par l'appareil, sans les demandes de réveil ni les battements."""
        skip = {j.CMD_REQ_DEVICE, j.CMD_HEARTBEAT}
        return [w for w in self.writes if w[1] not in skip and (cmd is None or w[1] == cmd)]


class FakeClient:
    def __init__(self, target, disconnected_callback=None, timeout=20, **kw):
        self.dev = target
        self.disconnected_callback = disconnected_callback
        rx_props = ["read"] if self.dev.notify_on_tx else ["read", "notify"]
        tx_props = ["write", "write-without-response"] + (["notify"] if self.dev.notify_on_tx else [])
        self.services = [
            FakeService("00001800-0000-1000-8000-00805f9b34fb", [FakeChar("00002a00-0000-1000-8000-00805f9b34fb", ["read"], 3)]),
            FakeService(j.SERVICE_UUID, [FakeChar(j.RX_UUID, rx_props, 0x10), FakeChar(j.TX_UUID, tx_props, 0x0E)]),
        ]
        self.cb = None
        self.notify_char = None
        self.is_connected = False

    async def connect(self):
        await asyncio.sleep(0)
        if self.dev.unreachable:
            raise TimeoutError()
        self.is_connected = True
        self.dev.client = self

    async def start_notify(self, char, cb):
        self.notify_char, self.cb = char, cb

    async def write_gatt_char(self, char, data, response=None):
        assert str(char.uuid) == j.TX_UUID
        if not self.is_connected:
            raise RuntimeError("not connected")
        self.dev.writes.append(bytes(data))
        loop = asyncio.get_running_loop()
        for reply in self.dev.respond(bytes(data)):
            if self.dev.split_replies and len(reply) > 4:
                loop.call_later(0.003, self.push, reply[:4])
                loop.call_later(0.006, self.push, reply[4:])
            else:
                loop.call_later(0.003, self.push, reply)

    def push(self, data):
        if self.cb and self.is_connected:
            self.cb(self.notify_char, bytearray(data))

    async def disconnect(self):
        was = self.is_connected
        self.is_connected = False
        if was and self.disconnected_callback:
            self.disconnected_callback(self)

    def drop(self):
        """Simule une coupure radio."""
        self.is_connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)


def make(fake, kind, **kw):
    kw.setdefault("ack_timeout", 0.2)
    kw.setdefault("wake_timeout", 0.1)
    return j.JBLDevice(fake, kind, fake.name, client_factory=FakeClient, **kw)


class FakeAdv:
    def __init__(self, local_name=None, service_data=None, rssi=-60, service_uuids=None):
        self.local_name, self.service_data, self.rssi = local_name, service_data or {}, rssi
        self.service_uuids = service_uuids or []


class FakeBLEDevice:
    def __init__(self, address, name=None):
        self.address, self.name = address, name


# ---------------------------------------------------------------------------

class ProtocolTests(unittest.TestCase):
    def test_frames_match_documented_bytes(self):
        self.assertEqual(j.f_color((255, 0, 128)).hex(), "aa33090036010132" "03ff0080")
        self.assertEqual(j.f_pattern(2).hex(), "aa3304003101" "02")
        self.assertEqual(j.f_pattern(0).hex(), "aa3304003101" "00")
        self.assertEqual(j.f_main_switch(True).hex(), "aa3304009901" "01")
        self.assertEqual(j.f_strobe(False).hex(), "aa3304003301" "00")
        self.assertEqual(j.f_intensity(80).hex(), "aa3304004501" "50")
        self.assertEqual(j.f_speed(150).hex(), "aa3304004601" "64")  # borné à 100
        self.assertEqual(j.f_req_light().hex(), "aa3100")
        self.assertEqual(j.f_req_device().hex(), "aa1100")
        self.assertEqual(j.f_heartbeat().hex(), "aaea00")
        self.assertEqual(j.frame(0xE3, b"\x01\x02").hex(), "aae300020102")

    def test_reader_splits_and_joins(self):
        r = j.FrameReader()
        ack = j.frame(0, b"\x33\x00")
        light = bytes.fromhex("aa3209" "0031010232030000ff")
        self.assertEqual(r.feed(b"\x01\x02" + ack + light[:5]), [(0, b"\x33\x00")])  # déchets ignorés
        self.assertEqual(r.feed(light[5:]), [(0x32, light[3:])])
        self.assertEqual(r.feed(j.frame(0xE2, b"abc")), [(0xE2, b"abc")])
        self.assertEqual(r.buf, bytearray())

    def test_reader_keeps_truncated_frame(self):
        r = j.FrameReader()
        r.STALE_AFTER = 0.01
        self.assertEqual(r.feed(bytes.fromhex("aa3210003101")), [])
        time.sleep(0.03)
        self.assertEqual(r.feed(j.frame(0, b"\x33\x00")), [(0x32, bytes.fromhex("003101")), (0, b"\x33\x00")])

    def test_decode_light_and_describe(self):
        st = j.decode_light(bytes.fromhex("00" "310108" "3203ff0080" "360101" "4a03020809" "450132"))
        self.assertEqual(st, {"pattern": 8, "rgb": (255, 0, 128), "custom": True, "intensity": 50,
                              "available": [2, 8, 9]})
        self.assertIn("ok", j.describe_frame(0, b"\x33\x00"))
        self.assertIn("refusé", j.describe_frame(0, b"\x33\xff"))
        info = j.describe_frame(0x12, bytes.fromhex("00" "31021f5d" "3301e4" "4005414243"))
        self.assertIn("PartyBox 310", info)
        self.assertIn("100 %", info)
        self.assertIn("ABC", info)
        self.assertIn("couleur=#FF0080", j.describe_tx(j.f_color((255, 0, 128))))

    def test_parse_color(self):
        self.assertEqual(j.parse_color("rouge"), (255, 0, 0))
        self.assertEqual(j.parse_color("Bleu"), (0, 0, 255))
        self.assertEqual(j.parse_color("#FF0080"), (255, 0, 128))
        self.assertEqual(j.parse_color("f08"), (255, 0, 136))
        self.assertEqual(j.parse_color("255,0,128"), (255, 0, 128))
        self.assertEqual(j.parse_color("255 0 128"), (255, 0, 128))
        self.assertEqual(len(j.parse_color("Aléatoire")), 3)
        for bad in ("chartreuse-ish", "300,0,0", "#12345", ""):
            with self.assertRaises(ValueError):
                j.parse_color(bad)
        self.assertEqual(j.parse_palette("rouge, bleu #00FF00"), [(255, 0, 0), (0, 0, 255), (0, 255, 0)])

    def test_parse_pattern(self):
        self.assertEqual(j.parse_pattern("Neon"), 2)
        self.assertEqual(j.parse_pattern("perso"), 8)
        self.assertEqual(j.parse_pattern("éteint"), 0)
        self.assertEqual(j.parse_pattern("13"), 13)
        with self.assertRaises(ValueError):
            j.parse_pattern("disco")

    def test_classify(self):
        box = j.classify(FakeBLEDevice("AA:01"), FakeAdv(service_data={j.ADV_UUID_PARTYBOX: bytes.fromhex("5d1f01d064")}))
        self.assertEqual((box.kind, box.product_id, box.name), ("box", 0x1F5D, "JBL PartyBox 310"))
        stick = j.classify(FakeBLEDevice("AA:02", "JBL PartyLight Stick"), FakeAdv())
        self.assertEqual(stick.kind, "light")
        other = j.classify(FakeBLEDevice("AA:03"), FakeAdv(local_name="JBL Tune 510BT"))
        self.assertEqual(other.kind, "jbl")
        self.assertIsNone(j.classify(FakeBLEDevice("AA:04", "Mi Band 8"), FakeAdv()))

    def test_find_characteristics_notify_on_tx(self):
        tx, notify = j.find_jbl_characteristics(FakeClient(FakeDevice("x", "x", notify_on_tx=True)).services)
        self.assertEqual(str(tx.uuid), j.TX_UUID)
        self.assertIs(notify, tx)
        tx, notify = j.find_jbl_characteristics(FakeClient(FakeDevice("x", "x")).services)
        self.assertEqual(str(notify.uuid), j.RX_UUID)

    def test_cli_parsing(self):
        p = j.build_parser()
        a = p.parse_args(["color", "255", "0", "128", "--motif", "neon", "--stick", "A", "--stick", "B"])
        self.assertEqual((a.color, a.pattern, a.stick), (["255", "0", "128"], "neon", ["A", "B"]))
        a = p.parse_args(["fete", "--rythme", "--tous-les", "4", "--palette", "rouge,bleu", "--mic", "3"])
        self.assertTrue(a.beat)
        self.assertEqual((a.every, j._mic(a)), (4, 3))
        a = p.parse_args(["web", "--port", "9000"])
        self.assertIs(a.func, j.cmd_web)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        j.PACE = 0.0

    async def test_wake_after_ignored_commands(self):
        fake = FakeDevice("JBL PartyBox 310", "B0", ignore_first=2)
        dev = make(fake, "box")
        self.assertTrue(await dev.connect())
        self.assertEqual(fake.writes[:3], [j.f_req_device()] * 3)

    async def test_silent_device_connects_but_reports_it(self):
        dev = make(FakeDevice("JBL PartyBox 310", "B0", silent=True), "box", wake_tries=2)
        self.assertFalse(await dev.connect())
        self.assertTrue(dev.connected)
        self.assertEqual(await dev.command(j.f_pattern(2)), "no_ack")

    async def test_split_notifications(self):
        fake = FakeDevice("JBL PartyLight Stick", "S1", split_replies=True)
        dev = make(fake, "light")
        await dev.connect()
        self.assertEqual(await dev.command(j.f_color((1, 2, 3))), "ok")
        payload = await dev.request(j.f_req_light(), j.CMD_RET_LIGHT)
        self.assertEqual(j.decode_light(payload)["rgb"], (1, 2, 3))

    async def test_unreachable(self):
        dev = make(FakeDevice("JBL PartyLight Stick", "S1", unreachable=True), "light")
        with self.assertRaises(TimeoutError):
            await dev.connect()
        self.assertFalse(dev.connected)

    async def test_keep_alive_sends_heartbeats_and_reconnects(self):
        fake = FakeDevice("JBL PartyLight Stick", "S1")
        dev = make(fake, "light")
        await dev.connect()
        task = asyncio.create_task(dev.keep_alive(interval=0.05))
        await asyncio.sleep(0.2)
        self.assertGreaterEqual(fake.writes.count(j.f_heartbeat()), 2)
        fake.client.drop()
        self.assertFalse(dev.connected)
        await asyncio.sleep(0.3)
        self.assertTrue(dev.connected)  # reconnecté tout seul
        task.cancel()
        await dev.close()


class ShowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        j.PACE = 0.0
        self.fbox = FakeDevice("JBL PartyBox 310", "B0", reject_tags={j.L_MAIN_SWITCH})
        self.f1 = FakeDevice("JBL PartyLight Stick", "S1")
        self.f2 = FakeDevice("JBL PartyLight Stick", "S2")
        self.box = make(self.fbox, "box")
        self.l1, self.l2 = make(self.f1, "light"), make(self.f2, "light")
        for d in (self.box, self.l1, self.l2):
            await d.connect()
        j._label_duplicates([self.box, self.l1, self.l2])
        self.show = j.Show(self.box, [self.l1, self.l2])

    async def asyncTearDown(self):
        await self.show.close()

    async def test_same_color_everywhere(self):
        res = await self.show.set_color((0, 0, 255))
        self.assertEqual([s for _, s in res], ["ok", "ok", "ok"])
        self.assertEqual(self.fbox.sent(), [j.f_color((0, 0, 255))])  # la 310 garde son animation
        for f in (self.f1, self.f2):  # le stick passe en "Perso" pour afficher la couleur
            self.assertEqual(f.sent(), [j.f_pattern(8), j.f_color((0, 0, 255))])
        await self.show.set_color((255, 0, 0))
        self.assertEqual(self.f1.sent()[-1:], [j.f_color((255, 0, 0))])
        self.assertEqual(len(self.f1.sent()), 3)  # motif pas renvoyé
        self.assertEqual([d.label for d in self.show.devices],
                         ["JBL PartyBox 310", "JBL PartyLight Stick (1)", "JBL PartyLight Stick (2)"])

    async def test_color_with_shared_pattern(self):
        await self.show.set_color((0, 255, 0), pattern=j.PATTERNS["loop"])
        for f in (self.fbox, self.f1):
            self.assertEqual(f.sent(), [j.f_pattern(9), j.f_color((0, 255, 0))])

    async def test_power_off_and_on(self):
        res = await self.show.power(False)
        self.assertEqual([s for _, s in res], ["ok", "ok", "ok"])  # motif 0 suffit même si l'interrupteur est refusé
        self.assertEqual(self.fbox.sent(), [j.f_main_switch(False), j.f_pattern(0)])
        self.assertEqual(self.f1.sent(), [j.f_pattern(0)])
        await self.show.power(True)
        self.assertEqual(self.f1.sent()[-1], j.f_pattern(2))

    async def test_rejected_and_offline_are_reported(self):
        self.fbox.reject_tags = {j.L_PATTERN}
        await self.l2.close()
        res = dict((d.label, s) for d, s in await self.show.set_pattern(j.PATTERNS["freeze"]))
        self.assertEqual(res["JBL PartyBox 310"], "rejected")
        self.assertEqual(res["JBL PartyLight Stick (1)"], "ok")
        self.assertEqual(res["JBL PartyLight Stick (2)"], "offline")
        self.assertEqual(j.exit_code(list(res.items())), 1)

    async def test_stick_settings_only_touch_sticks(self):
        await self.show.stick_settings(speed=30, intensity=90)
        self.assertEqual(self.fbox.sent(), [])
        self.assertEqual(self.f1.sent(), [j.f_intensity(90), j.f_speed(30)])
        await self.show.strobe(False)
        self.assertEqual(self.fbox.sent(), [j.f_strobe(False)])

    async def test_party_changes_everyone_together(self):
        steps = []
        palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        n = await j.run_party(self.show, palette, interval=0.01, count=4,
                              on_step=lambda n, rgb, res: steps.append(rgb))
        self.assertEqual(n, 4)
        self.assertEqual(steps, palette + palette[:1])
        colors = [w for w in self.fbox.sent(j.CMD_SET_LIGHT)]
        self.assertEqual(colors, [j.f_color(c) for c in steps])

    async def test_party_on_beats(self):
        class Beats:
            def __init__(self):
                self.calls = 0

            async def wait(self, n):
                self.calls += 1
                await asyncio.sleep(0)

        beats = Beats()
        await j.run_party(self.show, [(1, 1, 1), (2, 2, 2)], beats=beats, every=4, count=3)
        self.assertEqual(beats.calls, 2)

    async def test_mirror_follows_the_box(self):
        self.fbox.light = {"pattern": 8, "rgb": (0, 255, 0), "custom": 1}
        seen = []
        task = asyncio.create_task(j.run_mirror(self.show, poll=0.05, on_change=seen.append, max_changes=2))
        await asyncio.sleep(0.15)
        self.assertEqual(self.f1.sent()[-2:], [j.f_pattern(8), j.f_color((0, 255, 0))])
        # quelqu'un change la couleur sur l'enceinte : elle l'annonce d'elle-même
        self.fbox.light = {"pattern": 8, "rgb": (255, 0, 255), "custom": 1}
        self.fbox.client.push(self.fbox.light_reply())
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.f2.sent()[-1], j.f_color((255, 0, 255)))
        self.assertEqual(len(seen), 2)

    async def test_mirror_unsupported_returns_none(self):
        self.fbox.answers_light = False
        t0 = time.monotonic()
        self.assertIsNone(await j.run_mirror(self.show, probe_timeout=0.2))
        self.assertLess(time.monotonic() - t0, 1)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_prefers_the_310_and_stops_early(self):
        adverts = [
            (FakeBLEDevice("C0", "JBL PartyBox Encore"), FakeAdv(rssi=-40,
                service_data={j.ADV_UUID_PARTYBOX: bytes.fromhex("6220")})),
            (FakeBLEDevice("B0"), FakeAdv(rssi=-70, service_data={j.ADV_UUID_PARTYBOX: bytes.fromhex("5d1f")})),
            (FakeBLEDevice("S1", "JBL PartyLight Stick"), FakeAdv(rssi=-65)),
            (FakeBLEDevice("S2", "JBL PartyLight Stick"), FakeAdv(rssi=-50)),
            (FakeBLEDevice("Z9", "Some Watch"), FakeAdv(rssi=-30)),
        ]

        class Scanner:
            def __init__(self, detection_callback):
                self.cb = detection_callback

            async def __aenter__(self):
                for dev, adv in adverts:
                    self.cb(dev, adv)
                return self

            async def __aexit__(self, *exc):
                return False

        t0 = time.monotonic()
        box, lights, every, _ = await j.discover(5, grace=0.1, scanner_factory=Scanner)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(box.address, "B0")
        self.assertEqual([f.address for f in lights], ["S2", "S1"])  # le plus proche d'abord
        self.assertEqual(len(every), 4)
        box, lights, _, _ = await j.discover(5, box_addr="c0", light_addrs=["S1"], scanner_factory=Scanner)
        self.assertEqual((box.address, [f.address for f in lights]), ("C0", ["S1"]))


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        j.PACE = 0.0
        self.fbox, self.f1 = FakeDevice("JBL PartyBox 310", "B0"), FakeDevice("JBL PartyLight Stick", "S1")
        self.box, self.l1 = make(self.fbox, "box"), make(self.f1, "light")
        await self.box.connect()
        await self.l1.connect()
        self.remote = j.WebRemote(j.Show(self.box, [self.l1]), interval=0.05)
        self.server = await j.serve_http(self.remote, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.remote.stop_party()
        self.server.close()
        await self.server.wait_closed()
        await self.remote.show.close()

    async def http(self, method, path, body=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        data = b"" if body is None else json.dumps(body).encode()
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), payload

    async def test_page_and_api(self):
        status, page = await self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Party Sync", page)
        status, body = await self.http("POST", "/api/color", {"color": "bleu"})
        reply = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("#0000FF", reply["message"])
        self.assertEqual(self.fbox.sent()[-1], j.f_color((0, 0, 255)))
        self.assertEqual(self.f1.sent()[-1], j.f_color((0, 0, 255)))
        state = json.loads((await self.http("GET", "/api/state"))[1])["state"]
        self.assertEqual(state["color"], "#0000FF")
        self.assertEqual([d["connected"] for d in state["devices"]], [True, True])
        self.assertEqual(len(state["swatches"]), 12)

    async def test_party_start_stop_and_errors(self):
        reply = json.loads((await self.http("POST", "/api/party", {"on": True, "interval": 0.3}))[1])
        self.assertTrue(reply["state"]["party"]["on"])
        await asyncio.sleep(0.5)
        self.assertGreaterEqual(len(self.f1.sent(j.CMD_SET_LIGHT)), 2)
        # choisir une couleur à la main arrête le mode fête
        reply = json.loads((await self.http("POST", "/api/color", {"color": "#FF0080"}))[1])
        self.assertFalse(reply["state"]["party"]["on"])
        status, body = await self.http("POST", "/api/color", {"color": "pas une couleur"})
        self.assertEqual(status, 400)
        self.assertIn("couleur inconnue", json.loads(body)["message"])
        status, _ = await self.http("POST", "/api/nope", {})
        self.assertEqual(status, 404)
        status, _ = await self.http("GET", "/rien")
        self.assertEqual(status, 404)

    async def test_pattern_power_stick_strobe(self):
        await self.http("POST", "/api/pattern", {"pattern": "freeze"})
        self.assertEqual(self.f1.sent()[-1], j.f_pattern(13))
        await self.http("POST", "/api/stick", {"speed": 70})
        self.assertEqual(self.f1.sent()[-1], j.f_speed(70))
        await self.http("POST", "/api/strobe", {"on": True})
        self.assertEqual(self.fbox.sent()[-1], j.f_strobe(True))
        reply = json.loads((await self.http("POST", "/api/power", {"on": False}))[1])
        self.assertFalse(reply["state"]["on"])
        self.assertEqual(self.f1.sent()[-1], j.f_pattern(0))


class CommandLineTests(unittest.TestCase):
    """Lance les vraies commandes, avec un faux Bluetooth branché à la place de bleak."""

    def setUp(self):
        j.PACE = 0.0
        self.fbox = FakeDevice("JBL PartyBox 310", "B0")
        self.fstick = FakeDevice("JBL PartyLight Stick", "S1")
        adverts = [(self.fbox, FakeAdv(service_data={j.ADV_UUID_PARTYBOX: bytes.fromhex("5d1f")})),
                   (self.fstick, FakeAdv(local_name="JBL PartyLight Stick"))]

        class Scanner:
            def __init__(self, detection_callback):
                self.cb = detection_callback

            async def __aenter__(self):
                for dev, adv in adverts:
                    self.cb(dev, adv)
                return self

            async def __aexit__(self, *exc):
                return False

        self.saved = j._default_scanner_factory, j._default_client_factory
        j._default_scanner_factory = lambda: Scanner
        j._default_client_factory = lambda: FakeClient

    def tearDown(self):
        j._default_scanner_factory, j._default_client_factory = self.saved

    def run_cli(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = j.main(list(argv))
        return code, buf.getvalue()

    def test_color(self):
        code, out = self.run_cli("color", "rose", "--scan-time", "3")
        self.assertEqual(code, 0, out)
        self.assertIn("#FF006E", out)
        self.assertEqual(self.fbox.sent(), [j.f_color((255, 0, 110))])
        self.assertEqual(self.fstick.sent(), [j.f_pattern(8), j.f_color((255, 0, 110))])
        self.assertIsNone(self.fbox.client.cb if self.fbox.client.is_connected else None)  # déconnecté à la fin

    def test_party_off_scan_mirror(self):
        code, out = self.run_cli("fete", "--count", "3", "--interval", "0", "--palette", "rouge,vert")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fstick.sent(j.CMD_SET_LIGHT)[-1], j.f_color((255, 0, 0)))
        code, out = self.run_cli("off")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fstick.sent()[-1], j.f_pattern(0))
        code, out = self.run_cli("scan", "--scan-time", "0.3")
        self.assertEqual(code, 0)
        self.assertIn("produit 1f5d (PartyBox 310)", out)
        self.fbox.answers_light = False
        code, out = self.run_cli("miroir")
        self.assertEqual(code, 2)
        self.assertIn("ne renvoie pas", out)

    def test_bad_input_is_reported_before_connecting(self):
        code, out = self.run_cli("color", "chartreuse")
        self.assertEqual(code, 1)
        self.assertIn("couleur inconnue", out)
        self.assertEqual(self.fbox.writes, [])


if __name__ == "__main__":
    unittest.main()
