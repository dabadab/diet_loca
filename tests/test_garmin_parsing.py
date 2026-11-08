"""
Parsing Garmin's payloads. No database, no network.

Garmin's response shape is not a contract -- field names have moved before --
so the rule these tests exist to defend is: a field that is missing or
unreadable yields *nothing*, never a guess. Every assertion below is either
"the value arrived intact" or "the absence was handled without inventing one".
"""

from datetime import date, datetime, timezone

import pytest

from app import garmin

# Shaped like the real thing: weight in grams, sleep in seconds.
STATS = {
    "totalKilocalories": 2634.0, "activeKilocalories": 812.0, "totalSteps": 11423,
    "restingHeartRate": 51, "weight": 83700.0, "bodyFat": 19.4,
    "userProfileId": 1234, "calendarDate": "2026-09-08",
}
SLEEP = {"dailySleepDTO": {"sleepTimeSeconds": 26100, "deepSleepSeconds": 5400}}

RIDE = {
    "activityId": 111, "activityName": "Evening Ride",
    "activityType": {"typeKey": "cycling"},
    "startTimeGMT": "2026-09-12 16:00:00", "startTimeLocal": "2026-09-12 18:00:00",
    "duration": 4400.0, "movingDuration": 4200.0, "distance": 42000.0,
    "calories": 400.0, "averageHR": 141.0, "maxHR": 178.0, "elevationGain": 310.0,
    "averageSpeed": 9.5, "aerobicTrainingEffect": 3.4, "anaerobicTrainingEffect": 0.7,
    "hrTimeInZone_1": 525.209, "hrTimeInZone_2": 1103.993, "hrTimeInZone_3": 1017.997,
    "hrTimeInZone_4": 1332.546, "hrTimeInZone_5": 389.004,
    "startLatitude": 47.4979, "startLongitude": 19.0402,
    "endLatitude": 47.5, "endLongitude": 19.04,
    "geoPolylineDTO": {"polyline": [1, 2, 3]},
}


class TestDailyMetrics:
    def test_every_metric_and_its_unit_conversion(self):
        got = {r.metric: (r.value, r.unit) for r in garmin.extract(STATS, SLEEP)}
        assert got == {
            "total_kcal": (2634.0, "kcal"),
            "active_kcal": (812.0, "kcal"),
            "steps": (11423.0, "count"),
            "resting_hr": (51.0, "bpm"),
            "weight_kg": (83.7, "kg"),        # grams in, kilos out
            "body_fat_pct": (19.4, "%"),
            "sleep_minutes": (435.0, "min"),  # seconds in, minutes out
        }

    def test_a_moved_field_is_skipped_not_guessed(self):
        moved = {"totalKilocalories": 2634.0, "activeKilocaloriesRenamed": 812.0}
        got = [r.metric for r in garmin.extract(moved, None)]
        assert got == ["total_kcal"], "a renamed field must yield nothing at all"

    def test_a_non_numeric_value_is_refused(self):
        got = [r.metric for r in garmin.extract({"totalKilocalories": "lots"}, None)]
        assert got == []

    def test_describe_payload_surfaces_what_did_arrive(self):
        """The diagnostic that makes a moved field findable without a live account."""
        keys = garmin.describe_payload({"activeKilocaloriesRenamed": 812.0})
        assert "activeKilocaloriesRenamed=812.0" in keys

    def test_an_unpopulated_day_is_recognised_by_shape_not_field_names(self):
        """Today comes back as a skeleton until Garmin fills it in. Detecting that
        by counting keys keeps working if the names change; listing the names
        would not."""
        skeleton = {"userProfileId": 131731171, "netRemainingKilocalories": 0.0,
                    "from": 1789344000000, "until": 1789430399999}
        assert garmin.looks_empty(skeleton, None) is True
        assert garmin.looks_empty(STATS, SLEEP) is False


class TestActivities:
    def test_the_fields_that_become_columns(self):
        row = garmin.extract_activity(RIDE)
        assert row["external_id"] == "111"
        assert row["activity_type"] == "cycling"
        assert row["name"] == "Evening Ride"
        # startTimeGMT carries no zone but is UTC; reading it as local would
        # move the activity to the wrong day for anyone outside Greenwich.
        assert row["started_at"] == datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc)
        assert (row["duration_s"], row["moving_s"]) == (4400.0, 4200.0)
        assert (row["distance_m"], row["kcal"]) == (42000.0, 400.0)
        assert (row["avg_hr"], row["max_hr"]) == (141.0, 178.0)
        assert row["training_effect_aerobic"] == 3.4

    def test_hr_zones_come_from_the_list_item(self):
        """Garmin sends these inline. There is a per-activity endpoint too, and
        using it cost one request per workout for figures already in hand."""
        assert garmin.extract_activity(RIDE)["hr_zones"] == [
            {"zone": 1, "secs": 525.209}, {"zone": 2, "secs": 1103.993},
            {"zone": 3, "secs": 1017.997}, {"zone": 4, "secs": 1332.546},
            {"zone": 5, "secs": 389.004},
        ]

    def test_zone_seconds_roughly_account_for_the_duration(self):
        row = garmin.extract_activity(RIDE)
        total = sum(z["secs"] for z in row["hr_zones"])
        assert abs(total - row["duration_s"]) < 60, "zones should be seconds"

    def test_an_activity_without_a_heart_rate_reading_has_no_zones(self):
        walk = {"activityId": 2, "activityType": {"typeKey": "walking"},
                "startTimeGMT": "2026-09-12 16:00:00", "duration": 600.0}
        assert garmin.extract_activity(walk)["hr_zones"] is None

    def test_a_junk_zone_value_drops_only_that_zone(self):
        item = dict(RIDE, hrTimeInZone_3="nonsense")
        zones = garmin.extract_activity(item)["hr_zones"]
        assert [z["zone"] for z in zones] == [1, 2, 4, 5]

    def test_fields_an_activity_type_does_not_have_are_null(self):
        lift = {"activityId": 3, "activityName": "Gym",
                "activityType": {"typeKey": "strength_training"},
                "startTimeGMT": "2026-09-11 17:00:00",
                "duration": 2700.0, "calories": 210.0}
        row = garmin.extract_activity(lift)
        assert row["kcal"] == 210.0
        for absent in ("distance_m", "elevation_gain_m", "avg_speed_mps",
                       "moving_s", "avg_hr"):
            assert row[absent] is None, f"{absent} should be NULL, not invented"

    @pytest.mark.parametrize("missing, item", [
        ("activityId", {"activityType": {"typeKey": "x"},
                        "startTimeGMT": "2026-09-11 17:00:00"}),
        ("a start time", {"activityId": 4, "activityType": {"typeKey": "x"}}),
        ("a type", {"activityId": 5, "startTimeGMT": "2026-09-11 17:00:00"}),
        ("anything at all", "not even a dict"),
    ])
    def test_an_unidentifiable_activity_is_skipped(self, missing, item):
        """Those three fields are the row's identity; without them there is
        nothing useful to store, so the item is dropped rather than stored
        half-formed."""
        assert garmin.extract_activity(item) is None, f"should skip: no {missing}"

    def test_coordinates_never_reach_the_stored_payload(self):
        """`raw` is readable through query_sql by the model. A location trace has
        no bearing on diet and does not belong in that surface."""
        raw = garmin.extract_activity(RIDE)["raw"]
        assert raw, "the rest of the summary should still be kept"
        for key in raw:
            assert "atitude" not in key and "ongitude" not in key
            assert "olyline" not in key
        assert raw["calories"] == 400.0


class TestRateLimitDetection:
    @pytest.mark.parametrize("exc", [
        RuntimeError("429 Too Many Requests"),
        RuntimeError("rate limit exceeded"),
        type("TooManyRequestsError", (Exception,), {})("slow down"),
    ])
    def test_recognised(self, exc):
        assert garmin.is_rate_limited(exc) is True

    def test_an_ordinary_failure_is_not_a_rate_limit(self):
        """Mistaking one for the other would back off for half an hour over a
        transient error, or hammer an endpoint that asked us to stop."""
        assert garmin.is_rate_limited(RuntimeError("connection reset")) is False


def test_days_back_is_newest_first_and_inclusive():
    assert list(garmin.days_back(date(2026, 9, 14), 3)) == [
        date(2026, 9, 14), date(2026, 9, 13), date(2026, 9, 12)]


# ------------------------------------------------------- pushing weigh-ins ---

from decimal import Decimal
from struct import pack

# Shaped like a diet.weigh_ins row: Postgres numerics arrive as Decimal.
FULL_WEIGH_IN = {
    "measured_at": datetime(2026, 11, 8, 8, 36, 41, tzinfo=timezone.utc),
    "weight_kg": Decimal("86.40"), "body_fat_pct": Decimal("22.6"),
    "body_water_pct": Decimal("56.8"), "bone_mass_kg": Decimal("4.29"),
    "skeletal_muscle_pct": Decimal("44.8"), "muscle_mass_kg": Decimal("62.47"),
    "bmr_kcal": Decimal("1815"), "metabolic_age": Decimal("51"),
    "visceral_fat": Decimal("6.0"), "bmi": Decimal("27.9"),
}
FIT_EPOCH = 631065600


class TestBodyComposition:
    def test_garmins_muscle_mass_is_the_skeletal_share_not_all_lean_tissue(self):
        """Garmin labels the field skeletal muscle mass. The scale's 62 kg of
        muscle would read there as 72 % of body weight."""
        fields = garmin.body_composition(FULL_WEIGH_IN)
        assert fields["muscle_mass"] == Decimal("38.71")      # 86.40 * 44.8 %
        assert fields["weight"] == Decimal("86.40")
        assert fields["percent_fat"] == Decimal("22.6")
        assert fields["percent_hydration"] == Decimal("56.8")
        assert fields["basal_met"] == Decimal("1815")
        assert fields["visceral_fat_rating"] == Decimal("6.0")

    def test_a_figure_the_scale_did_not_take_is_not_sent(self):
        fields = garmin.body_composition({**FULL_WEIGH_IN, "skeletal_muscle_pct": None,
                                          "bone_mass_kg": None})
        assert fields["muscle_mass"] is None and fields["bone_mass"] is None

    def test_the_file_carries_the_weigh_ins_own_instant_exactly(self):
        """The library's own path reads the time in the process's timezone and
        truncates scaled floats (4.29 kg of bone became 4.28). The record is
        checked byte for byte: timestamp, then weight, fat, water, visceral
        fat mass (not sent), bone, muscle."""
        data = garmin.body_composition_fit(FULL_WEIGH_IN)
        ts = int(FULL_WEIGH_IN["measured_at"].timestamp()) - FIT_EPOCH
        assert pack("<IHHHHHH", ts, 8640, 2260, 5680, 0xFFFF, 429, 3871) in data

    def test_the_same_weigh_in_always_makes_the_same_file(self):
        assert (garmin.body_composition_fit(FULL_WEIGH_IN)
                == garmin.body_composition_fit(dict(FULL_WEIGH_IN)))


class TestUploadWeighIn:
    class Client:
        def __init__(self, fail=None):
            self.fail, self.posts = fail, []

        def post(self, domain, path, **kw):
            self.posts.append((path, kw))
            if self.fail:
                raise self.fail
            return {}

    class Api:
        garmin_connect_upload = "/upload-service/upload"

        def __init__(self, client):
            self.client = client

    def test_it_is_uploaded_as_a_fit_file(self):
        client = self.Client()
        assert garmin.upload_weigh_in(self.Api(client), FULL_WEIGH_IN) is True
        path, kw = client.posts[0]
        assert path == "/upload-service/upload"
        name, data = kw["files"]["file"]
        assert name.endswith(".fit") and data[8:12] == b".FIT"

    def test_a_duplicate_counts_as_delivered(self):
        client = self.Client(RuntimeError("API Error 409 - Duplicate"))
        assert garmin.upload_weigh_in(self.Api(client), FULL_WEIGH_IN) is False

    def test_any_other_error_is_raised(self):
        client = self.Client(RuntimeError("API Error 500"))
        with pytest.raises(RuntimeError):
            garmin.upload_weigh_in(self.Api(client), FULL_WEIGH_IN)
