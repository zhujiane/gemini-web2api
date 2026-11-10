import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from gemini_web2api import webchannel


def frame(items):
    data = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return (str(len(data.encode("utf-16-le")) // 2) + "\n" + data).encode()


class Response(io.BytesIO):
    def __init__(self, content, headers=None):
        super().__init__(content)
        self.headers = headers or {}


class WebChannelTests(unittest.TestCase):
    def test_frames_count_utf16_units(self):
        data = frame([[1, ["你好😀"]]])
        self.assertEqual(list(webchannel._frames(io.BytesIO(data))), [(1, ["你好😀"])])

    def test_truncated_frame_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Truncated"):
            list(webchannel._frames(io.BytesIO(b"20\n[]")))

    def test_argon_session_and_thinking_level(self):
        inner = [None] * 43
        inner[4] = [["rc", ["你好😀"]]]
        inner[39:43] = ["95a4c0abcba24364", None, None, "4 Argon"]
        candidate = [1, None, None, [None, None, None, inner]]
        message = [None, [None, None, None, None, None, None, [candidate]]]
        down = frame([[1, [[[]]]], [2, [message]], [3, [[None, None, None, []]]]])
        calls = []

        def fake_open(request, timeout):
            calls.append(request)
            if request.full_url == "https://gemini.google.com/app":
                return Response(('"d2zJAe":"' + 'AIza' + 'a' * 35 + '"').encode())
            query = parse_qs(urlparse(request.full_url).query)
            if request.data == b"count=0":
                return Response(frame([[0, ["c", "sid"]]]), {"X-HTTP-Session-Id": "gsid"})
            if query.get("RID") == ["rpc"]:
                return Response(down)
            return Response(b"")

        opener = mock.Mock()
        opener.open.side_effect = fake_open
        with mock.patch.object(webchannel, "load_cookie", return_value=("SAPISID=secret", "secret")), \
             mock.patch.object(webchannel.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(webchannel.time, "sleep"):
            self.assertEqual("".join(webchannel.generate_argon("hello", 4)), "你好😀")
        handshake = parse_qs(urlparse(calls[1].full_url).query)["$httpHeaders"][0]
        self.assertIn(webchannel._model_header(4), handshake)
        posts = [parse_qs(c.data.decode()) for c in calls if c.data and c.data != b"count=0"]
        self.assertEqual([p["ofs"][0] for p in posts], ["0", "1", "2"])
        self.assertEqual(json.loads(posts[1]["req0___data__"][0])[1][8], ["hello"])

        inner[42] = "3.5 Flash-Lite"
        down = frame([[1, [[[]]]], [2, [message]], [3, [[None, None, None, []]]]])
        with mock.patch.object(webchannel, "load_cookie", return_value=("SAPISID=secret", "secret")), \
             mock.patch.object(webchannel.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(webchannel.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "different model"):
                list(webchannel.generate_argon("hello", 1))

    def test_reported_model_detection(self):
        self.assertEqual(webchannel._reported_model(["0123456789abcdef", None, None, "3.5 Flash-Lite"]), "3.5 Flash-Lite")
        self.assertIsNone(webchannel._reported_model(["hello"]))

    def test_requires_account_cookie(self):
        with mock.patch.object(webchannel, "load_cookie", return_value=("", None)):
            with self.assertRaisesRegex(RuntimeError, "requires a configured account cookie"):
                list(webchannel.generate_argon("hello"))


if __name__ == "__main__":
    unittest.main()
