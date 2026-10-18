"""Offline tests for key_fallback (no network, no real API keys).

    python -m unittest tests.test_key_fallback -v
"""
import os
import unittest

import httpx
import requests
from requests.adapters import BaseAdapter

import key_fallback

PERSONAL = {"GROQ_API_KEY": "gsk_personal", "GEMINI_API_KEY": "gem_personal", "PEXELS_API_KEY": "pex_personal"}
SHARED = {"GROQ_API_KEY_SHARED": "gsk_shared", "GEMINI_API_KEY_SHARED": "gem_shared", "PEXELS_API_KEY_SHARED": "pex_shared"}


def _auth(request):
    h = request.headers
    return h.get("authorization") or h.get("x-goog-api-key") or str(getattr(request, "url", ""))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key_fallback.install()

    def setUp(self):
        key_fallback.reset()
        self._saved = dict(os.environ)
        os.environ.update(PERSONAL)
        os.environ.update(SHARED)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)


class HttpxTests(_Base):
    def _client(self, status_for_personal, calls, body=b"{}"):
        def handler(request):
            calls.append(request)
            tok = request.headers.get("authorization", "") + request.headers.get("x-goog-api-key", "") + str(request.url)
            if "personal" in tok:
                return httpx.Response(status_for_personal, content=body)
            return httpx.Response(200, json={"ok": True})
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_groq_429_retries_with_shared_key(self):
        calls = []
        c = self._client(429, calls)
        r = c.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": "Bearer gsk_personal"}, json={"a": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[0].headers["authorization"], "Bearer gsk_personal")
        self.assertEqual(calls[1].headers["authorization"], "Bearer gsk_shared")
        self.assertEqual(calls[0].read(), calls[1].read())  # body preserved

    def test_cooldown_skips_personal_key_on_next_call(self):
        calls = []
        c = self._client(429, calls)
        url = "https://api.groq.com/openai/v1/chat/completions"
        c.post(url, headers={"Authorization": "Bearer gsk_personal"}, json={})
        calls.clear()
        c.post(url, headers={"Authorization": "Bearer gsk_personal"}, json={})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer gsk_shared")

    def test_gemini_key_in_query_and_header(self):
        for kw in ({"params": {"key": "gem_personal"}}, {"headers": {"x-goog-api-key": "gem_personal"}}):
            key_fallback.reset()
            calls = []
            c = self._client(429, calls)
            r = c.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent", json={}, **kw)
            self.assertEqual(r.status_code, 200)
            self.assertIn("gem_shared", str(calls[1].url) + calls[1].headers.get("x-goog-api-key", ""))

    def test_gemini_400_invalid_key_falls_back(self):
        calls = []
        c = self._client(400, calls, body=b'{"error":{"message":"API key not valid. Please pass a valid API key."}}')
        r = c.post("https://generativelanguage.googleapis.com/v1beta/m:generateContent",
                   headers={"x-goog-api-key": "gem_personal"}, json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_gemini_ordinary_400_does_not_fall_back(self):
        calls = []
        c = self._client(400, calls, body=b'{"error":{"message":"Invalid JSON payload"}}')
        r = c.post("https://generativelanguage.googleapis.com/v1beta/m:generateContent",
                   headers={"x-goog-api-key": "gem_personal"}, json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(calls), 1)

    def test_401_sticks_for_rest_of_run(self):
        calls = []
        c = self._client(401, calls)
        url = "https://api.groq.com/openai/v1/chat/completions"
        c.post(url, headers={"Authorization": "Bearer gsk_personal"}, json={})
        self.assertTrue(key_fallback._sticky("groq"))

    def test_other_hosts_untouched(self):
        calls = []
        c = self._client(429, calls)
        r = c.get("https://example.com/x", headers={"Authorization": "Bearer gsk_personal"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(calls), 1)

    def test_no_shared_key_means_no_retry(self):
        del os.environ["GROQ_API_KEY_SHARED"]
        calls = []
        c = self._client(429, calls)
        r = c.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": "Bearer gsk_personal"}, json={})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(calls), 1)

    def test_shared_equals_personal_means_no_retry(self):
        os.environ["GROQ_API_KEY_SHARED"] = os.environ["GROQ_API_KEY"]
        calls = []
        c = self._client(429, calls)
        r = c.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": "Bearer gsk_personal"}, json={})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(calls), 1)

    def test_shared_also_failing_returns_that_failure_once(self):
        calls = []
        c = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(429))[1]))
        r = c.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": "Bearer gsk_personal"}, json={})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(calls), 2)  # personal, then shared; no loop


class _FakeAdapter(BaseAdapter):
    def __init__(self, personal_status, calls):
        super().__init__()
        self.personal_status, self.calls = personal_status, calls

    def send(self, request, **kwargs):
        self.calls.append(request)
        resp = requests.Response()
        resp.request = request
        resp.url = request.url
        resp.status_code = self.personal_status if "personal" in (request.headers.get("Authorization", "") + request.url) else 200
        resp._content = b"{}"
        return resp

    def close(self):
        pass


class RequestsTests(_Base):
    def _session(self, status, calls):
        s = requests.Session()
        s.mount("https://", _FakeAdapter(status, calls))
        return s

    def test_pexels_429_retries_with_shared_key(self):
        calls = []
        r = self._session(429, calls).get("https://api.pexels.com/videos/search",
                                          headers={"Authorization": "pex_personal"}, params={"query": "diya"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[0].headers["Authorization"], "pex_personal")
        self.assertEqual(calls[1].headers["Authorization"], "pex_shared")
        self.assertIn("query=diya", calls[1].url)

    def test_gemini_rest_key_in_url(self):
        calls = []
        r = self._session(429, calls).post(
            "https://generativelanguage.googleapis.com/v1beta/models/veo:generateVideos?key=gem_personal", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("key=gem_shared", calls[1].url)

    def test_pexels_ok_response_passes_through(self):
        calls = []
        r = self._session(200, calls).get("https://api.pexels.com/v1/search",
                                          headers={"Authorization": "pex_personal"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
