"""
Fit Profile's wire format and weigh-in parsing, with no network.

Nothing about this API is documented: the encryption and field names were read
out of the Android app. The tests that matter most here pin the encryption to
bytes the real server produced, so a "tidy-up" of the cipher code that changes
its output fails here rather than as a poller that silently stores nothing.
"""

import datetime as dt
import json
import time

import pytest

from app import fitprofile

# A full body-composition weigh-in and a weight-only one, as list_measurement
# returns them (trimmed to the fields that matter; the rest go to `raw`).
FULL = {
    "measurement_id": "4655000000000000001", "user_id": "42",
    "weight": 86.45, "bodyfat": 22.4, "time_stamp": 1793951338,
    "bmi": 27.9, "muscle": 44.9, "sinew": 62.76, "fat_free_weight": 67.17,
    "subfat": 20.1, "visfat": 6.0, "water": 56.9, "protein": 15.7,
    "bone": 4.41, "bmr": 1819, "bodyage": 51, "scale_name": "Fit Plus",
    "bodyfat_left_arm": 0.86,
}
WEIGHT_ONLY = {
    "measurement_id": "4655000000000000002", "user_id": "42",
    "weight": 86.7, "bodyfat": 0.0, "time_stamp": 1793920000,
    "bmi": 27.9, "muscle": 0.0, "sinew": 0.0, "fat_free_weight": 0.0,
    "subfat": 0.0, "visfat": 0.0, "water": 0.0, "protein": 0.0,
    "bone": 0.0, "bmr": 0, "bodyage": 0,
}


class TestWireFormat:
    def test_decrypts_what_the_real_server_sent(self):
        """The reply data the live service returned for a sign-in to an
        unknown account. If this stops decrypting, the key or mode is wrong."""
        assert fitprofile.decrypt("vCyWgE0oASMhQDs73vrQOg==") == "{}"

    def test_encryption_round_trips(self):
        text = json.dumps({"user_id": "42", "last_updated_at": 0})
        assert fitprofile.decrypt(fitprofile.encrypt(text)) == text

    def test_encryption_is_url_safe_and_unpadded(self):
        """The app uses Base64 flags 11 (URL_SAFE | NO_WRAP | NO_PADDING); the
        GET form puts this straight into a query string."""
        out = fitprofile.encrypt("x" * 100)
        assert not set(out) & set("+/=\n")

    def test_the_password_is_never_sent_as_itself(self):
        enc = fitprofile.encrypt_password("hunter2")
        assert "hunter2" not in enc and len(enc) > 100

    def test_reply_data_is_decrypted_in_place(self):
        reply = {"code": "200", "msg": "ok",
                 "data": fitprofile.encrypt(json.dumps({"a": [1]}))}
        assert fitprofile.unwrap(reply)["data"] == {"a": [1]}

    def test_the_logged_out_code_asks_for_a_login(self):
        with pytest.raises(fitprofile.NeedsLogin):
            fitprofile.unwrap({"code": "403", "msg": "token expired"})

    def test_any_other_error_code_is_a_refusal(self):
        """20101 is what an unknown account gets back. Refused, not
        NeedsLogin, because a session cannot tell on its own which it means;
        sign_in turns it into NeedsLogin since there it does mean that."""
        with pytest.raises(fitprofile.Refused):
            fitprofile.unwrap({"code": "20101", "msg": "Account does not exist",
                               "data": "vCyWgE0oASMhQDs73vrQOg=="})


class TestWeighIns:
    def test_a_full_weigh_in_maps_every_column(self):
        row = fitprofile.extract_weigh_in(FULL)
        assert row["external_id"] == "4655000000000000001"
        assert row["weight_kg"] == 86.45 and row["body_fat_pct"] == 22.4
        assert row["skeletal_muscle_pct"] == 44.9 and row["muscle_mass_kg"] == 62.76
        assert row["visceral_fat"] == 6.0 and row["bmr_kcal"] == 1819
        assert row["metabolic_age"] == 51
        assert row["measured_at"] == dt.datetime.fromtimestamp(1793951338, dt.timezone.utc)

    def test_zeros_mean_not_measured_not_zero(self):
        """A weigh-in in socks reports 0.0 for every impedance figure. Stored
        as 0 % body fat it would be the leanest day on record."""
        row = fitprofile.extract_weigh_in(WEIGHT_ONLY)
        assert row["weight_kg"] == 86.7
        for col in fitprofile.BODY_COLUMNS:
            if col == "bmi":
                continue            # computed from weight, so always present
            assert row[col] is None, col

    def test_the_whole_payload_is_kept(self):
        row = fitprofile.extract_weigh_in(FULL)
        assert row["raw"]["bodyfat_left_arm"] == 0.86

    @pytest.mark.parametrize("broken", [
        {**FULL, "weight": 0}, {**FULL, "weight": None},
        {**FULL, "measurement_id": None}, {**FULL, "time_stamp": None}, "nope",
    ])
    def test_an_unusable_item_yields_nothing(self, broken):
        assert fitprofile.extract_weigh_in(broken) is None

    def test_a_garbled_figure_costs_only_itself(self):
        row = fitprofile.extract_weigh_in({**FULL, "water": "n/a"})
        assert row["body_water_pct"] is None and row["body_fat_pct"] == 22.4

    def test_another_family_members_weigh_ins_are_not_ours(self):
        assert fitprofile.extract_weigh_in({**FULL, "user_id": "99"}, "42") is None
        assert fitprofile.extract_weigh_in(FULL, "42") is not None


class FakeSession(fitprofile.Session):
    """Pages handed out in order; records what was asked for."""

    def __init__(self, pages):
        super().__init__({"token": "t", "user_id": "42",
                          "expires_at": time.time() + 86400 * 100})
        self.pages = list(pages)

    def get(self, path, params):
        self.calls.append((path, params))
        return {"code": 200, "data": self.pages.pop(0)}


class TestFetch:
    SINCE = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    def test_one_finished_page_is_one_request(self):
        s = FakeSession([{"measurements": [FULL], "delete_measurement_ids": ["9"],
                          "finish_flag": 1, "last_updated_at": 5,
                          "last_measurement_id": "0"}])
        items, deleted = fitprofile.fetch_weigh_ins(s, self.SINCE)
        assert items == [FULL] and deleted == ["9"]
        path, params = s.calls[0]
        assert path == fitprofile.LIST_PATH
        assert params == {"last_updated_at": int(self.SINCE.timestamp()),
                          "last_measurement_id": "0", "user_id": "42"}

    def test_an_unfinished_page_hands_the_servers_cursor_back(self):
        s = FakeSession([
            {"measurements": [FULL], "finish_flag": 0,
             "last_updated_at": 1793950000, "last_measurement_id": "77"},
            {"measurements": [WEIGHT_ONLY], "finish_flag": 1},
        ])
        items, _ = fitprofile.fetch_weigh_ins(s, self.SINCE)
        assert len(items) == 2
        assert s.calls[1][1]["last_updated_at"] == 1793950000
        assert s.calls[1][1]["last_measurement_id"] == "77"

    def test_raw_replies_are_archived(self, tmp_path):
        s = FakeSession([{"measurements": [FULL], "finish_flag": 1}])
        fitprofile.fetch_weigh_ins(s, self.SINCE, tmp_path)
        files = list(tmp_path.glob("fitprofile-*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["measurements"][0] == FULL


class TestSession:
    def test_an_expiring_token_is_renewed_with_the_stored_password(self, monkeypatch):
        fresh = {"email": "e", "password": "p", "token": "new", "user_id": "42",
                 "expires_at": time.time() + 180 * 86400}
        monkeypatch.setattr(fitprofile, "sign_in", lambda e, p: fresh)
        seen = []
        monkeypatch.setattr(fitprofile, "_request",
                            lambda m, path, params, token: seen.append(token) or {})
        s = fitprofile.Session({"email": "e", "password": "p", "token": "old",
                                "user_id": "42", "expires_at": time.time() + 3600})
        s.get("x", {})
        assert seen == ["new"] and s.refreshed

    def test_a_refused_token_is_retried_once_after_signing_in(self, monkeypatch):
        fresh = {"email": "e", "password": "p", "token": "new", "user_id": "42",
                 "expires_at": time.time() + 180 * 86400}
        monkeypatch.setattr(fitprofile, "sign_in", lambda e, p: fresh)
        seen = []

        def request(method, path, params, token):
            seen.append(token)
            if token == "old":
                raise fitprofile.NeedsLogin("revoked")
            return {}
        monkeypatch.setattr(fitprofile, "_request", request)
        s = fitprofile.Session({"email": "e", "password": "p", "token": "old",
                                "user_id": "42",
                                "expires_at": time.time() + 100 * 86400})
        s.get("x", {})
        assert seen == ["old", "new"] and s.refreshed

    def test_without_a_password_an_expired_token_needs_a_human(self):
        s = fitprofile.Session({"token": "old", "user_id": "42", "expires_at": 0})
        with pytest.raises(fitprofile.NeedsLogin):
            s.get("x", {})


class TestRequest:
    """_request is the one place that builds HTTP; these run it through httpx's
    own request validation with the network replaced."""

    def _capture(self, monkeypatch):
        import httpx2
        sent = []

        def fake_send(self, request, **kw):
            sent.append(request)
            return httpx2.Response(200, json={"code": "200", "msg": "ok",
                                              "data": fitprofile.encrypt("{}")},
                                   request=request)
        monkeypatch.setattr(httpx2.Client, "send", fake_send)
        return sent

    def test_sign_in_goes_out_without_an_authorization_header(self, monkeypatch):
        """The first version sent "Bearer " with an empty token, which httpx
        refuses as an illegal header -- so the sign-in never left the machine."""
        sent = self._capture(monkeypatch)
        fitprofile._request("POST", fitprofile.SIGN_IN_PATH, {"email": "e"})
        assert "authorization" not in sent[0].headers
        body = json.loads(sent[0].content)
        assert json.loads(fitprofile.decrypt(body["ciphertext"])) == {"email": "e"}

    def test_a_listing_carries_the_token_and_encrypted_params(self, monkeypatch):
        sent = self._capture(monkeypatch)
        fitprofile._request("GET", fitprofile.LIST_PATH, {"user_id": "42"}, "tok")
        req = sent[0]
        assert req.headers["authorization"] == "Bearer tok"
        assert req.url.params["app_id"] == "fit_profile"
        assert json.loads(fitprofile.decrypt(req.url.params["ciphertext"])) == {
            "user_id": "42"}
