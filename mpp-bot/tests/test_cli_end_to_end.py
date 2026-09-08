"""Le pipeline complet, vu depuis la ligne de commande, dans un vrai navigateur.

C'est le test qui repond a la question "est-ce que ca marche vraiment ?" :
`python -m mpp run` part d'une URL, se connecte, trouve la page, lit les cartes,
calcule, saisit, verifie, journalise, et rend le bon code de sortie.

Le faux site est genere ici avec une date explicite (demain), pour que les
matchs tombent toujours dans la fenetre J-1 quelle que soit l'heure a laquelle
la suite est executee.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tests.test_e2e_browser import CHROMIUM, HAS_PW

REPO = Path(__file__).resolve().parents[1]

CARDS = [
    ("90210", "AEK Athens", "LASK", "18h45", 65, 124, 129, 70, 21, 8),
    ("90211", "Bruges", "Aston Villa", "18h45", 100, 117, 93, 15, 23, 62),
    ("90212", "Real Madrid", "Inter", "21h00", 62, 124, 136, 85, 11, 4),
    ("90213", "LOSC", "Betis", "21h00", 88, 121, 103, 63, 25, 13),
    ("90214", "Dortmund", "Villarreal", "21h00", 73, 119, 122, 81, 15, 4),
    ("90215", "Porto", "Man City", "21h00", 127, 124, 67, 7, 12, 81),
]

PERSIST = """
<script>
(function () {
  const KEY = 'mpp-cli-store';
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } };
  const save = (s) => localStorage.setItem(KEY, JSON.stringify(s));
  const inputs = Array.from(document.querySelectorAll('input'));
  const state = load();
  inputs.forEach((el, i) => {
    if (state[i] !== undefined) el.value = state[i];
    const w = () => { const s = load(); s[i] = el.value; save(s); };
    el.addEventListener('change', w); el.addEventListener('input', w);
  });
})();
</script>
"""


def write_site(root: Path, day: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "login.html").write_text(
        """<!doctype html><html lang="fr"><head><meta charset="utf-8"></head><body>
        <!-- Comme un vrai site : un mauvais mot de passe laisse le formulaire
             affiche. C'est exactement le signal que le bot utilise pour
             detecter un echec de connexion. -->
        <form onsubmit="event.preventDefault();
              if (m.value && p.value === 'secret') { location.href = './pronos.html'; }
              else { document.getElementById('err').textContent = 'Identifiants incorrects'; }">
          <input id="m" type="text"><input id="p" type="password">
          <button type="submit">Se connecter</button>
        </form><p id="err"></p></body></html>""",
        encoding="utf-8",
    )
    body = [
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">',
        "<title>Pronostics</title></head><body><div class='grid'>",
    ]
    for mid, home, away, hhmm, c1, cn, c2, p1, pn, p2 in CARDS:
        body.append(
            f"""<section class="k" data-match-id="{mid}">
              <span class="d">{day}</span><span class="t">{hhmm}</span>
              <span class="n">{home}</span>
              <input type="text"><input type="text">
              <span class="n">{away}</span>
              <em>{c1}</em><em>{cn}</em><em>{c2}</em>
              <i>{p1}%</i><i>{pn}%</i><i>{p2}%</i>
              <button>Valider</button>
            </section>"""
        )
    body.append("</div>" + PERSIST + "</body></html>")
    (root / "pronos.html").write_text("\n".join(body), encoding="utf-8")
    return root / "login.html"


@unittest.skipUnless(HAS_PW and CHROMIUM, "Playwright ou Chromium indisponible")
class TestCliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d/%m")
        login = write_site(root / "site", tomorrow)

        self.state = root / "state"
        self.config = root / "config.yaml"
        self.config.write_text(
            "state_dir: state\n"
            "max_matches_per_run: 40\n"
            "strategy:\n"
            "  target_deviations: 2\n"
            "windows:\n"
            "  early_min: 360\n"
            "  early_max: 2880\n"
            "site:\n"
            f"  base_url: \"{login.as_uri()}\"\n"
            f"  login_url: \"{login.as_uri()}\"\n"
            f"  matches_url: \"{(root / 'site' / 'pronos.html').as_uri()}\"\n"
            "  action_delay_s: [0.0, 0.0]\n"
            "  timeout_ms: 10000\n"
            f"  executable_path: \"{CHROMIUM}\"\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def mpp(self, *argv: str, **env_overrides: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "MPP_EMAIL": "moi@example.com",
            "MPP_PASSWORD": "secret",
            "PYTHONPATH": str(REPO),
            **env_overrides,
        }
        return subprocess.run(
            [sys.executable, "-m", "mpp", "--config", str(self.config),
             "--state-dir", str(self.state), *argv],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
        )

    def test_dry_run_computes_but_writes_nothing(self):
        result = self.mpp("run", "--phase", "early", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("deviations = 2/6", result.stdout)
        self.assertIn("Real Madrid - Inter", result.stdout)

        journal = (self.state / "journal.jsonl").read_text(encoding="utf-8")
        statuses = {json.loads(l)["status"] for l in journal.splitlines() if l}
        self.assertEqual(statuses, {"dry-run"})

    def test_real_run_submits_and_confirms_by_rereading(self):
        result = self.mpp("run", "--phase", "early")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("6 envoyes", result.stdout)

        submitted = [
            json.loads(l)
            for l in (self.state / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            if l and json.loads(l).get("status") == "submitted"
        ]
        self.assertEqual(len(submitted), 6)
        for event in submitted:
            outcome = (
                "1" if event["home_goals"] > event["away_goals"]
                else "N" if event["home_goals"] == event["away_goals"] else "2"
            )
            self.assertEqual(outcome, event["outcome"])

    def test_second_run_is_idempotent(self):
        """Le bot ecrase cote MPP, mais ne renvoie pas a l'identique : moins de
        requetes, moins d'exposition, journal lisible."""
        self.assertEqual(self.mpp("run", "--phase", "early").returncode, 0)
        second = self.mpp("run", "--phase", "early")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("0 envoyes, 6 inchanges", second.stdout)

    def test_gate_uses_the_cached_fixtures(self):
        self.mpp("run", "--phase", "early", "--dry-run")
        self.assertTrue((self.state / "fixtures.json").exists())

        early = self.mpp("gate", "--phase", "early")
        self.assertIn("due=true", early.stdout)
        late = self.mpp("gate", "--phase", "late")
        self.assertIn("due=false", late.stdout)   # rien avant T-90min

    def test_doctor_reports_without_writing(self):
        result = self.mpp("doctor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("6 cartes lues, 0 illisibles", result.stdout)
        self.assertIn("connexion OK", result.stdout)
        self.assertFalse((self.state / "journal.jsonl").exists())

    def test_report_summarises_the_journal(self):
        self.mpp("run", "--phase", "early")
        result = self.mpp("report")
        self.assertEqual(result.returncode, 0)
        self.assertIn("submitted", result.stdout)
        self.assertIn("Real Madrid", result.stdout)

    def test_missing_credentials_exit_fatal_and_alert(self):
        result = self.mpp("run", "--phase", "early", MPP_PASSWORD="")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("MPP_EMAIL et MPP_PASSWORD", result.stdout + result.stderr)

    def test_rejected_credentials_exit_fatal(self):
        """Mot de passe refuse : le formulaire reste affiche, et c'est ce signal
        - et non un libelle ou une URL - que le bot lit comme un echec."""
        result = self.mpp("run", "--phase", "early", MPP_PASSWORD="mauvais")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("ECHEC", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
