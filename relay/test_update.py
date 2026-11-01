"""Tests du plan de rafraîchissement (sans réseau) : python3 -m unittest discover relay"""

import unittest
from datetime import datetime, timedelta, timezone

import update

UTC = timezone.utc


def schedule(*races):
    return {"MRData": {"RaceTable": {"Races": list(races)}}}


def race(number, date, time="14:00:00Z", qualifying=None, sprint=None):
    payload = {"round": str(number), "date": date, "time": time}
    if qualifying:
        payload["Qualifying"] = {"date": qualifying, "time": "15:00:00Z"}
    if sprint:
        payload["Sprint"] = {"date": sprint, "time": "11:00:00Z"}
        payload["SprintQualifying"] = {"date": sprint, "time": "07:00:00Z"}
    return payload


ROUNDS = update.parse_schedule(schedule(
    race(1, "2026-03-08", qualifying="2026-03-07"),
    race(2, "2026-03-15", qualifying="2026-03-14", sprint="2026-03-14"),
    race(3, "2026-03-29", qualifying="2026-03-28"),
))


def paths(targets):
    return {t.path: t for t in targets}


class PlanTests(unittest.TestCase):

    def test_before_the_season_nothing_but_the_schedule(self):
        now = datetime(2026, 2, 1, tzinfo=UTC)
        self.assertEqual(update.plan_season(2026, ROUNDS, now, previous_season=False), [])

    def test_after_qualifying_only_qualifying_files(self):
        now = datetime(2026, 3, 7, 17, 0, tzinfo=UTC)
        planned = paths(update.plan_season(2026, ROUNDS, now, previous_season=False))
        self.assertIn("2026/1/qualifying.json", planned)
        self.assertIn("2026/qualifying", planned)
        self.assertNotIn("2026/1/results.json", planned)
        self.assertNotIn("2026/driverstandings.json", planned)

    def test_just_after_a_race_everything_is_hot(self):
        now = datetime(2026, 3, 8, 17, 0, tzinfo=UTC)  # arrivée estimée 16:30
        planned = paths(update.plan_season(2026, ROUNDS, now, previous_season=False))
        self.assertEqual(planned["2026/1/results.json"].ttl, update.TTL_HOT)
        self.assertEqual(planned["2026/driverstandings.json"].ttl, update.TTL_HOT)
        self.assertEqual(planned["2026/results"].ttl, update.TTL_HOT_PAGES)
        self.assertTrue(planned["2026/results"].paged)

    def test_sprint_file_only_for_sprint_weekends(self):
        now = datetime(2026, 3, 20, tzinfo=UTC)
        planned = paths(update.plan_season(2026, ROUNDS, now, previous_season=False))
        self.assertIn("2026/2/sprint.json", planned)
        self.assertNotIn("2026/1/sprint.json", planned)
        self.assertIn("2026/sprint", planned)

    def test_past_rounds_are_refreshed_weekly(self):
        now = datetime(2026, 3, 20, tzinfo=UTC)
        planned = paths(update.plan_season(2026, ROUNDS, now, previous_season=False))
        self.assertEqual(planned["2026/1/results.json"].ttl, update.TTL_PAST)
        # Plus de trois jours après la dernière course : la saison est au repos.
        self.assertEqual(planned["2026/driverstandings.json"].ttl, update.TTL_COLD)

    def test_recent_phase_after_hot_window(self):
        now = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
        planned = paths(update.plan_season(2026, ROUNDS, now, previous_season=False))
        self.assertEqual(planned["2026/2/results.json"].ttl, update.TTL_RECENT)

    def test_previous_season_is_frozen(self):
        now = datetime(2027, 1, 10, tzinfo=UTC)
        planned = update.plan_season(2026, ROUNDS, now, previous_season=True)
        self.assertTrue(planned)
        self.assertTrue(all(t.ttl == update.TTL_PREVIOUS_SEASON for t in planned))

    def test_missing_time_means_end_of_day(self):
        moment = update.parse_datetime("2010-03-14", None)
        self.assertEqual(moment, datetime(2010, 3, 14, 23, 59, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
