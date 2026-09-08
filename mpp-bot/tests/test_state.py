import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mpp.models import Card, Prediction
from mpp.state import Store

KO = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)


def prediction(match_id="m1", hg=2, ag=1) -> Prediction:
    return Prediction(
        match_id=match_id, home="Bruges", away="Aston Villa", kickoff=KO,
        outcome="1", home_goals=hg, away_goals=ag,
        market=(0.34, 0.29, 0.37), crowd=(0.15, 0.23, 0.62),
        cotes=(100.0, 117.0, 93.0), edge=0.19, deviation=True, contrarian=True,
        p_outcome=0.34, p_exact=0.09, expected_points=34.1, lambdas=(1.4, 1.3),
    )


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_last_submitted_score_tracks_the_latest_write(self):
        self.assertIsNone(self.store.last_submitted_score("m1"))
        self.store.record_submission(prediction(hg=2, ag=1), "submitted")
        self.assertEqual(self.store.last_submitted_score("m1"), (2, 1))
        self.store.record_submission(prediction(hg=1, ag=0), "submitted")
        self.assertEqual(self.store.last_submitted_score("m1"), (1, 0))

    def test_non_submitted_events_do_not_count(self):
        self.store.record_submission(prediction(hg=3, ag=0), "dry-run")
        self.store.record_submission(prediction(hg=4, ag=0), "failed")
        self.assertIsNone(self.store.last_submitted_score("m1"))

    def test_corrupted_line_does_not_break_reading(self):
        self.store.record_submission(prediction(), "submitted")
        with self.store.journal_path.open("a", encoding="utf-8") as fh:
            fh.write("{ceci n'est pas du json\n")
        self.store.record_submission(prediction(hg=0, ag=0), "submitted")
        self.assertEqual(self.store.last_submitted_score("m1"), (0, 0))

    def test_fixtures_round_trip(self):
        cards = [Card("m1", "Bruges", "Aston Villa", KO, competition="C1")]
        self.store.save_fixtures(cards)
        got = self.store.load_fixtures()
        self.assertEqual(got[0]["match_id"], "m1")
        self.assertEqual(datetime.fromisoformat(got[0]["kickoff"]), KO)

    def test_missing_or_broken_fixtures_return_empty(self):
        self.assertEqual(self.store.load_fixtures(), [])
        self.store.fixtures_path.write_text("{tronque", encoding="utf-8")
        self.assertEqual(self.store.load_fixtures(), [])

    def test_atomic_write_leaves_no_partial_file(self):
        self.store.save_last_run({"submitted": 3})
        payload = json.loads(self.store.last_run_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["submitted"], 3)
        self.assertIn("finished_at", payload)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()


class TestRedaction(unittest.TestCase):
    """Les captures de diagnostic partent en artefact GitHub Actions. Sur un
    depot public, ces artefacts sont telechargeables par n'importe qui : rien
    de sensible ne doit y figurer."""

    def test_secret_keys_are_masked_and_json_stays_valid(self):
        from mpp.site import redact

        payload = json.dumps(
            {
                "accessToken": "eyJhbGciOiJIUzI1NiJ9.abc.def",
                "refreshToken": "r-123",
                "Set-Cookie": "sid=xyz",
                "user": {"sessionId": "s-9", "pseudo": "moi", "apiKey": "k"},
                "matches": [{"home": "Inter", "cote": 124}],
                "libelle": 'il a dit "non"',
            }
        )
        out = json.loads(redact(payload))
        self.assertEqual(out["accessToken"], "[REDACTED]")
        self.assertEqual(out["refreshToken"], "[REDACTED]")
        self.assertEqual(out["Set-Cookie"], "[REDACTED]")
        self.assertEqual(out["user"]["sessionId"], "[REDACTED]")
        self.assertEqual(out["user"]["apiKey"], "[REDACTED]")
        # Les donnees utiles, elles, doivent survivre intactes.
        self.assertEqual(out["user"]["pseudo"], "moi")
        self.assertEqual(out["matches"][0]["cote"], 124)
        self.assertEqual(out["libelle"], 'il a dit "non"')

    def test_non_json_bodies_have_long_tokens_masked(self):
        from mpp.site import redact

        out = redact("Set-Cookie: sid=" + "a" * 48 + "; Path=/; texte normal")
        self.assertNotIn("a" * 48, out)
        self.assertIn("texte normal", out)
