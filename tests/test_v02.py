import json
import tempfile
import unittest
from pathlib import Path

from printo.backend import (
    EVERYWHERE,
    Backend,
    Job,
    JobState,
    Printer,
    PrinterState,
    PrintoError,
    suggest_printer_name,
    supports_driverless,
)
from printo.presets import BUILTIN, Preset, PresetStore
from printo.watch import JobWatcher, PrinterWatcher


class AdminConn:
    def __init__(self):
        self.calls = []

    def getDevices(self, timeout=0):
        return {
            "usb://HP/Laser": {
                "device-class": "direct",
                "device-info": ["HP Laser"],
                "device-make-and-model": ["HP LaserJet 400"],
            },
            "ipp://10.0.0.5/ipp/print": {
                "device-class": "network",
                "device-info": "Office",
                "device-make-and-model": "Unknown",
            },
            "file:///dev/null": {"device-class": "file"},
            "socket": {"device-class": "network", "device-info": "AppSocket/HP JetDirect"},
        }

    def getPPDs2(self):
        return {
            # real pycups gives lists for these; some versions give plain strings
            "a.ppd": {"ppd-make-and-model": ["HP LaserJet 400 pcl"], "ppd-natural-language": ["en", "th"]},
            "b.ppd": {"ppd-make-and-model": "Epson L3150", "ppd-natural-language": "en"},
        }

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return 5

        return call


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.conn = AdminConn()
        self.be = Backend(self.conn)

    def test_devices_skip_file_and_network_first(self):
        devs = self.be.devices()
        self.assertEqual([d.uri for d in devs], ["ipp://10.0.0.5/ipp/print", "usb://HP/Laser"])
        self.assertEqual(devs[0].label, "Office")  # Unknown model falls back to info
        self.assertEqual(devs[1].label, "HP LaserJet 400")

    def test_drivers_search_all_words(self):
        found = self.be.drivers("laserjet hp")
        self.assertEqual([d.name for d in found], ["a.ppd"])
        self.assertEqual(found[0].language, "en,th")
        self.assertEqual(self.be.drivers("   "), [])
        self.assertEqual(self.be.drivers("epson")[0].language, "en")  # str form works too

    def test_add_printer(self):
        self.be.add_printer("Office", "ipp://x/ipp/print")
        names = [c[0] for c in self.conn.calls]
        self.assertEqual(names, ["addPrinter", "enablePrinter", "acceptJobs"])
        self.assertEqual(self.conn.calls[0][2]["ppdname"], EVERYWHERE)

    def test_add_printer_rejects_bad_input(self):
        with self.assertRaises(PrintoError):
            self.be.add_printer("bad name!", "ipp://x")
        with self.assertRaises(PrintoError):
            self.be.add_printer("ok", "")
        self.assertEqual(self.conn.calls, [])

    def test_helpers(self):
        self.assertEqual(suggest_printer_name("HP LaserJet #400"), "HP_LaserJet_400")
        self.assertEqual(suggest_printer_name("!!!"), "printer")
        self.assertTrue(supports_driverless("ipps://host/x"))
        self.assertFalse(supports_driverless("usb://HP/x"))

    def test_auth_error_is_friendly(self):
        class Denied(AdminConn):
            def deletePrinter(self, name):
                raise RuntimeError(1285, "client-error-not-authorized")

        with self.assertRaises(PrintoError) as ctx:
            Backend(Denied()).delete_printer("x")
        self.assertIn("lpadmin", str(ctx.exception))

    def test_job_final_state(self):
        class C(AdminConn):
            def getJobAttributes(self, jid, requested_attributes=None):
                return {"job-state": 9}

        self.assertEqual(Backend(C()).job_final_state(1), JobState.COMPLETED)

        class Gone(AdminConn):
            def getJobAttributes(self, jid, requested_attributes=None):
                raise RuntimeError("gone")

        self.assertIsNone(Backend(Gone()).job_final_state(1))


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "presets.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        store = PresetStore(self.path)
        store.save(Preset("Photos", copies=2, paper="A4", orientation="landscape"))
        again = PresetStore(self.path)
        self.assertEqual([p.name for p in again.all()], [*(b.name for b in BUILTIN), "Photos"])
        self.assertEqual(again.all()[-1].copies, 2)

    def test_replace_same_name(self):
        store = PresetStore(self.path)
        store.save(Preset("X", copies=1))
        store.save(Preset("x", copies=4))
        user = store.all()[len(BUILTIN):]
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0].copies, 4)

    def test_builtin_protected(self):
        store = PresetStore(self.path)
        with self.assertRaises(ValueError):
            store.delete("Default")
        with self.assertRaises(ValueError):
            store.save(Preset("default"))

    def test_delete(self):
        store = PresetStore(self.path)
        store.save(Preset("Tmp"))
        store.delete("Tmp")
        self.assertEqual(len(PresetStore(self.path).all()), len(BUILTIN))

    def test_corrupt_files_do_not_crash(self):
        self.path.parent.mkdir(parents=True)
        for text in ("not json", "{}", "[1, null, {\"nope\": 1}]"):
            self.path.write_text(text)
            self.assertEqual(len(PresetStore(self.path).all()), len(BUILTIN))

    def test_bad_values_are_cleaned(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps([
            {"name": "Odd", "copies": "abc", "sides": "weird", "paper": "B7", "extra": 1},
            {"name": "Odd"},  # duplicate
            {"name": "default"},  # clashes with built-in
        ]))
        user = PresetStore(self.path).all()[len(BUILTIN):]
        self.assertEqual(len(user), 1)
        self.assertEqual((user[0].copies, user[0].sides, user[0].paper), (1, "one-sided", ""))

    def test_no_temp_files_left(self):
        store = PresetStore(self.path)
        store.save(Preset("A"))
        self.assertEqual([f.name for f in self.path.parent.iterdir()], ["presets.json"])


def job(i, title="doc", state=JobState.PROCESSING):
    return Job(i, title, "p", "u", state, 1, 0)


class WatchTest(unittest.TestCase):
    def test_done_and_failed(self):
        w = JobWatcher()
        states = {1: JobState.COMPLETED, 2: JobState.ABORTED, 3: JobState.CANCELED}
        self.assertEqual(w.update([job(1), job(2), job(3), job(4)], states.get), [])
        events = w.update([job(4)], states.get)
        self.assertEqual([(e.kind, e.job.id) for e in events], [("done", 1), ("failed", 2)])
        self.assertEqual(w.update([job(4)], states.get), [])  # no repeats

    def test_unknown_final_state_is_silent(self):
        w = JobWatcher()
        w.update([job(1)], lambda i: None)
        self.assertEqual(w.update([], lambda i: None), [])

    def test_keeps_job_name(self):
        w = JobWatcher()
        w.update([job(1, "report.pdf")], lambda i: None)
        w.update([job(1, "(untitled)")], lambda i: None)
        events = w.update([], lambda i: JobState.COMPLETED)
        self.assertEqual(events[0].job.title, "report.pdf")

    def test_printer_stopped_once(self):
        def pr(state):
            return Printer("p", "P", state, "", False, "")

        w = PrinterWatcher()
        self.assertEqual(w.update([pr(PrinterState.STOPPED)]), [])  # first poll primes
        self.assertTrue(w.any_stopped)
        self.assertEqual(w.update([pr(PrinterState.IDLE)]), [])
        self.assertFalse(w.any_stopped)
        self.assertEqual(len(w.update([pr(PrinterState.STOPPED)])), 1)
        self.assertEqual(w.update([pr(PrinterState.STOPPED)]), [])


if __name__ == "__main__":
    unittest.main()
