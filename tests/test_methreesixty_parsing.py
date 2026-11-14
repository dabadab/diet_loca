"""
MeThreeSixty's sign-in plumbing. No database, no network.

The request signing is plain AWS SigV4, so it is pinned against AWS's own
published test vectors (the `get-vanilla` family, AKIDEXAMPLE keys) rather than
against itself. The redirect parsing is what a person pastes out of devtools.
"""

import datetime as dt
import urllib.parse

import pytest

from app import methreesixty

AWS_KEY = "AKIDEXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
AWS_NOW = dt.datetime(2015, 8, 30, 12, 36, tzinfo=dt.timezone.utc)


def _signature(url: str) -> str:
    headers = methreesixty.sign("GET", url, {}, b"", AWS_KEY, AWS_SECRET,
                                service="service", region="us-east-1", now=AWS_NOW)
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/"
                           "us-east-1/service/aws4_request, "
                           "SignedHeaders=host;x-amz-date, Signature=")
    return auth.rsplit("=", 1)[1]


class TestSigV4:
    def test_get_vanilla(self):
        assert _signature("https://example.amazonaws.com/") == \
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"

    def test_query_parameters_are_sorted(self):
        assert _signature("https://example.amazonaws.com/?Param2=value2&Param1=value1") == \
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"

    def test_session_token_is_signed(self):
        headers = methreesixty.sign("GET", "https://example.amazonaws.com/", {}, b"",
                                    AWS_KEY, AWS_SECRET, "TOKEN", now=AWS_NOW)
        assert headers["x-amz-security-token"] == "TOKEN"
        assert "SignedHeaders=host;x-amz-date;x-amz-security-token," \
            in headers["Authorization"]


class TestLogin:
    def test_authorize_url_goes_straight_to_google_with_pkce(self):
        url, verifier = methreesixty.start_login()
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert parts.netloc == "prod-otp-three-d-id.auth.us-east-1.amazoncognito.com"
        assert q["identity_provider"] == "Google"
        assert q["redirect_uri"] == "methreesixty://auth"
        assert q["code_challenge_method"] == "S256"
        assert len(verifier) > 43

    @pytest.mark.parametrize("pasted", [
        "methreesixty://auth?code=abc-123-def&state=xyz",
        "location: methreesixty://auth?code=abc-123-def&state=xyz",
        '"methreesixty://auth?code=abc-123-def"',
        "abc-123-def",
    ])
    def test_code_is_found_in_what_people_paste(self, pasted):
        assert methreesixty.code_from_redirect(pasted) == "abc-123-def"

    def test_refusal_is_reported(self):
        with pytest.raises(methreesixty.MeThreeSixtyError, match="access_denied"):
            methreesixty.code_from_redirect(
                "methreesixty://auth?error=access_denied&error_description=no")

    def test_wrong_paste_is_refused(self):
        with pytest.raises(methreesixty.MeThreeSixtyError, match="no code"):
            methreesixty.code_from_redirect("https://accounts.google.com/x?y=1")


# The 6 Nov 2026 scan as the probe returned it; the average is trimmed.
MEASURES = {"BMI": 27.89, "BicepLeft": 13.39, "BicepRight": 13.2, "BodyFat": 23.76,
            "BodySurfaceArea": 3050.55, "CalfLeft": 15.32, "CalfRight": 15.46,
            "Chest": 42.27, "FatMassIndex": 6.63, "Fitness": 16.82,
            "ForearmLeft": 11.52, "ForearmRight": 11.45, "LeanBodyMass": 145.22,
            "Seat": 40.54, "Stomach": 39.7, "ThighLeft": 22.74, "ThighRight": 22.98,
            "VisceralAdiposeTissue": 2.22, "Waist": 37.83, "WaistToHipRatio": 96.88,
            "Weight": 190.48}
AVERAGE = {"BicepLeft": 13.628, "BodyFat": 25.7, "Chest": 43.034000000000006,
           "LeanBodyMass": 143.574, "Stomach": 40.942, "Waist": 39.038,
           "Weight": 190.48, "BodySurfaceArea": 3099.696}
SCAN = {"id": "99525c3c-b381-4352-8ed7-250f6944ff8d", "date": "2026-11-06T06:14:37Z",
        "status": "complete", "type": "2d3d-silhouette-2.1.0",
        "links": {"upload": "https://signed.example/x"}, "data": {"mesh": "u"}}
PAYLOAD = {"status": "complete", "version": 1, "input": {"scanId": SCAN["id"]},
           "output": {"measures": MEASURES, "moving_average": AVERAGE}}


class TestParsing:
    def test_the_scan_is_converted_to_metric(self):
        row = methreesixty.extract_scan(SCAN, PAYLOAD)
        assert row["external_id"] == SCAN["id"]
        assert row["measured_at"] == dt.datetime(2026, 11, 6, 6, 14, 37,
                                                 tzinfo=dt.timezone.utc)
        assert row["chest_cm"] == 107.4          # 42.27 in
        assert row["stomach_cm"] == 100.8
        assert row["calf_right_cm"] == 39.3
        assert row["body_fat_pct"] == 23.8
        assert row["body_surface_area_m2"] == 1.97
        assert row["lean_body_mass_kg"] == 65.9
        assert row["entered_weight_kg"] == 86.4

    def test_the_moving_average_matches_the_apps_screen(self):
        """What the Measurements screen showed on 14 Nov 2026."""
        avg = methreesixty.extract_scan(SCAN, PAYLOAD)["app_average"]
        assert avg["chest_cm"] == 109.3
        assert avg["stomach_cm"] == 104.0
        assert avg["waist_cm"] == 99.2
        assert avg["bicep_left_cm"] == 34.6
        assert avg["body_fat_pct"] == 25.7
        assert avg["lean_body_mass_kg"] == 65.1
        assert avg["body_surface_area_m2"] == 2.0

    def test_figures_without_a_column_stay_in_raw_and_links_do_not(self):
        row = methreesixty.extract_scan(SCAN, PAYLOAD)
        assert "bmi" not in row and "fitness" not in row
        assert row["raw"]["measures"]["output"]["measures"]["Fitness"] == 16.82
        assert "links" not in row["raw"]["scan"] and "data" not in row["raw"]["scan"]

    def test_an_unknown_or_non_numeric_value_is_left_out(self):
        assert methreesixty.to_metric({"Chest": "n/a", "Neck": 15.0,
                                       "BodyFat": True}) == {}

    @pytest.mark.parametrize("payload", [{}, {"output": None},
                                         {"output": {"measures": {}}}])
    def test_a_scan_without_measures_is_not_stored(self, payload):
        assert methreesixty.extract_scan(SCAN, payload) is None

    def test_only_finished_scans_count(self):
        assert methreesixty.is_complete({"status": "complete"})
        assert not methreesixty.is_complete({"status": "waiting for data"})
