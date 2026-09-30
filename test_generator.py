"""Offline checks for score selection, timezone parsing, and safe layouts."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import generate_bundesliga_bmp as generator


class GeneratorTests(unittest.TestCase):
    def fixture(self, **changes):
        result = {
            "matchID": 1, "leagueShortcut": "bl1", "leagueSeason": 2026,
            "group": {"groupOrderID": 4}, "matchIsFinished": True,
            "matchDateTimeUTC": "2026-09-19T13:30:00Z",
            "team1": {"teamName": "Borussia Mönchengladbach", "shortName": "Gladbach"},
            "team2": {"teamName": "Eintracht Frankfurt", "shortName": "Frankfurt"},
            "matchResults": [
                {"resultTypeID": 2, "pointsTeam1": 3, "pointsTeam2": 4},
                {"resultTypeID": 1, "pointsTeam1": 1, "pointsTeam2": 2},
            ],
        }
        result.update(changes)
        return result

    def test_full_time_not_last_or_half_time_result(self):
        match = generator.parse_match(self.fixture())
        self.assertEqual((match["home_score"], match["away_score"], match["status"]), (3, 4, "C"))

    def test_missing_final_score_not_invented(self):
        match = generator.parse_match(self.fixture(matchResults=[{"resultTypeID": 1, "pointsTeam1": 1}]))
        self.assertIsNone(match["home_score"])
        self.assertIsNone(match["away_score"])
        self.assertEqual(match["status"], "C")

    def test_false_status_and_missing_fields(self):
        match = generator.parse_match(self.fixture(matchIsFinished="false", team1=None,
                                                   matchDateTimeUTC=None, matchResults=None))
        self.assertEqual(match["status"], "U")
        self.assertEqual(match["home"]["name"], "Team TBC")
        self.assertIsNone(match["kickoff"])

    def test_pacific_dst_and_berlin_fallback(self):
        summer = generator.parse_kickoff({"matchDateTimeUTC": "2026-09-19T13:30:00Z"})
        winter = generator.parse_kickoff({"matchDateTimeUTC": "2026-12-05T14:30:00Z"})
        fallback = generator.parse_kickoff({"matchDateTime": "2026-09-19T15:30:00"})
        self.assertEqual((summer.hour, summer.minute, summer.tzname()), (6, 30, "PDT"))
        self.assertEqual((winter.hour, winter.minute, winter.tzname()), (6, 30, "PST"))
        self.assertEqual(summer, fallback)

    def test_source_current_matchday_used_even_when_finished(self):
        fixture = self.fixture()
        with patch.object(generator, "api_get", side_effect=[{"groupOrderID": 4}, [fixture], [fixture]]):
            data = generator.fetch_dashboard_data(datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(data["matchday"], 4)
        self.assertEqual(data["season"], "2026/27")

    def test_wrong_league_rejected(self):
        with patch.object(generator, "api_get", side_effect=[{"groupOrderID": 4}, [self.fixture()],
                                                            [self.fixture(leagueShortcut="bl2")]]):
            with self.assertRaisesRegex(RuntimeError, "outside"):
                generator.fetch_dashboard_data(datetime.now(timezone.utc))

    def test_all_upcoming_all_final_mixed_and_overflow(self):
        with tempfile.TemporaryDirectory() as temp:
            bmp, png = Path(temp) / "test.bmp", Path(temp) / "test.png"
            for count, completed in ((9, 0), (9, 9), (9, 4), (12, 6)):
                with self.subTest(count=count, completed=completed):
                    matches = [generator.parse_match(self.fixture(matchID=i, matchIsFinished=i < completed))
                               for i in range(count)]
                    data = {"season": "2026/27", "matchday": 34, "matches": matches,
                            "updated": datetime.now(generator.PACIFIC)}
                    generator.render_dashboard(data, bmp, png)
                    generator.validate_bmp(bmp, png)
            data["matches"] *= 3
            with self.assertRaisesRegex(RuntimeError, "Too many"):
                generator.render_dashboard(data, bmp, png)


if __name__ == "__main__":
    unittest.main()
