import tempfile
import unittest
from pathlib import Path

from printo.backend import (
    Backend,
    JobState,
    PrinterState,
    PrintoError,
    build_options,
)


class FakeConn:
    def __init__(self):
        self.calls = []

    def getPrinters(self):
        return {
            "zeta": {"printer-info": "Zeta Laser", "printer-state": 3},
            "alpha": {
                "printer-info": "Alpha Ink",
                "printer-state": 5,
                "printer-state-message": "Out of paper",
                "device-uri": "ipp://alpha",
            },
        }

    def getDefault(self):
        return "zeta"

    def getJobs(self, **kwargs):
        self.calls.append(("getJobs", kwargs))
        # Like real CUPS: names only shown for our own jobs (job 12).
        if kwargs.get("my_jobs"):
            return {
                12: {
                    "job-name": "b.pdf",
                    "job-originating-user-name": "me",
                    "job-printer-uri": "ipp://localhost/printers/zeta",
                    "job-state": 5,
                    "job-k-octets": 40,
                }
            }
        return {
            12: {
                "job-printer-uri": "ipp://localhost/printers/zeta",
                "job-state": 5,
                "job-k-octets": 40,
            },
            7: {
                "job-printer-uri": "ipp://localhost/printers/alpha",
                "job-state": 4,
            },
        }

    def printFile(self, printer, path, title, options):
        self.calls.append(("printFile", printer, path, title, options))
        return 99

    def cancelJob(self, job_id):
        self.calls.append(("cancelJob", job_id))

    def setJobHoldUntil(self, job_id, when):
        self.calls.append(("setJobHoldUntil", job_id, when))


class BuildOptionsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            build_options(), {"copies": "1", "sides": "one-sided"}
        )

    def test_full(self):
        opts = build_options(
            copies=3,
            sides="two-sided-long-edge",
            page_ranges="1-3, 5",
            paper="A4",
            orientation="landscape",
        )
        self.assertEqual(opts["page-ranges"], "1-3,5")
        self.assertEqual(opts["media"], "A4")
        self.assertEqual(opts["orientation-requested"], "4")

    def test_bad_input(self):
        for kwargs in (
            {"copies": 0},
            {"sides": "weird"},
            {"page_ranges": "a-b"},
            {"page_ranges": "5-2"},
            {"page_ranges": "1-2-3"},
            {"page_ranges": "0"},
            {"orientation": "upside-down"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    build_options(**kwargs)


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        self.be = Backend(self.conn)

    def test_printers_default_first(self):
        printers = self.be.printers()
        self.assertEqual([p.name for p in printers], ["zeta", "alpha"])
        self.assertTrue(printers[0].is_default)
        self.assertEqual(printers[1].state, PrinterState.STOPPED)
        self.assertFalse(printers[1].is_ready)
        self.assertEqual(printers[1].label, "Alpha Ink")

    def test_jobs(self):
        jobs = self.be.jobs()
        self.assertEqual([j.id for j in jobs], [7, 12])
        self.assertEqual(jobs[0].printer, "alpha")
        self.assertEqual(jobs[0].state, JobState.HELD)
        self.assertTrue(jobs[0].is_held)
        self.assertEqual(jobs[1].size_kb, 40)
        self.assertTrue(jobs[1].is_active)
        # our job shows its name; someone else's job stays anonymous
        self.assertEqual(jobs[1].title, "b.pdf")
        self.assertEqual(jobs[1].user, "me")
        self.assertEqual(jobs[0].title, "(untitled)")

    def test_print_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.pdf"
            f.write_bytes(b"%PDF")
            job = self.be.print_file("zeta", f, {"copies": "2"})
        self.assertEqual(job, 99)
        self.assertEqual(self.conn.calls[-1][1:], ("zeta", str(f), "x.pdf", {"copies": "2"}))

    def test_print_missing_file(self):
        with self.assertRaises(PrintoError):
            self.be.print_file("zeta", "/no/such/file.pdf")

    def test_cancel_hold_release(self):
        self.be.cancel(5)
        self.be.hold(5)
        self.be.release(5)
        self.assertEqual(
            self.conn.calls,
            [
                ("cancelJob", 5),
                ("setJobHoldUntil", 5, "indefinite"),
                ("setJobHoldUntil", 5, "no-hold"),
            ],
        )

    def test_errors_wrapped(self):
        class Broken(FakeConn):
            def cancelJob(self, job_id):
                raise RuntimeError("boom")

        be = Backend(Broken())
        with self.assertRaises(PrintoError):
            be.cancel(1)


if __name__ == "__main__":
    unittest.main()
