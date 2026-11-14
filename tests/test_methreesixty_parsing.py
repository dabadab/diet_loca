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
