"""Offline visual matcher tests; requests are mocked, never sent to a model/robot."""

import base64
import copy
import json
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
import requests

from cup_visual_matcher import (MAX_IMAGE_BYTES, MAX_RESPONSE_CHARS,
                                OllamaCupMatcher, SYSTEM_PROMPT)
from cup_inventory import SelectionError
from local_llm import LLMError


def jpeg(color, *, width=48, height=64):
    image = np.full((height, width, 3), color, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise AssertionError("Could not create synthetic JPEG")
    return encoded.tobytes()


def decode(data):
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


REFERENCE = jpeg((10, 20, 230))
CANDIDATE = jpeg((10, 220, 20))
DISTINCT_COLORS = ((10, 220, 20), (220, 20, 20), (20, 20, 220), (180, 180, 30),
                   (180, 30, 180), (30, 180, 180), (80, 80, 80), (220, 120, 30),
                   (30, 120, 220), (120, 30, 220), (220, 30, 120), (30, 220, 120), (240, 240, 240))


def match_result(status="match", cup_id=7, reason="Distinctive blue stripe matches."):
    return {"status": status, "cup_id": cup_id, "reason": reason}


def assessment(cup_id=7, verdict="match"):
    return str(cup_id), verdict


def comparison(*assessments, reason="Distinctive blue stripe matches."):
    return {"candidates": dict(assessments or (assessment(), assessment(2, "different"))),
            "reason": reason}


def completion(value=None, **changes):
    result = {"done": True, "done_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps(value if value is not None else comparison())}}
    result.update(changes)
    return result


class Response:
    def __init__(self, result=None, *, status=200, text=None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(result)
        self.closed = False

    def close(self):
        self.closed = True


class CupVisualMatcherTests(unittest.TestCase):
    def client(self, response=None, **options):
        session = Mock()
        session.auth = ("old", "secret")
        session.headers = {"Authorization": "Bearer OLD_SECRET"}
        session.proxies = {"http": "http://proxy.invalid"}
        session.cookies = requests.cookies.cookiejar_from_dict({"session": "OLD_COOKIE"})
        session.post.return_value = response if response is not None else Response(completion())
        return OllamaCupMatcher(session=session, **options), session

    @staticmethod
    def candidates(*cup_ids):
        return [{"cup_id": cup_id, "image_bytes": jpeg(DISTINCT_COLORS[index])}
                for index, cup_id in enumerate(cup_ids or (7, 2))]

    def test_local_request_uses_one_sheet_and_dynamic_schema_with_actual_ids(self):
        response = Response(completion())
        client, session = self.client(response)
        candidates = self.candidates()
        before = copy.deepcopy(candidates)
        with patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.invalid",
                                       "GATEWAY_TOKEN": "NEVER_SEND_TOKEN"}):
            result = client.match(REFERENCE, candidates)
        self.assertEqual(result, match_result())
        self.assertEqual(candidates, before)
        self.assertTrue(response.closed)
        self.assertIs(session.trust_env, False)
        self.assertIsNone(session.auth)
        self.assertEqual(session.headers, {})
        self.assertEqual(session.proxies, {})
        self.assertEqual(dict(session.cookies), {})
        self.assertEqual(session.post.call_count, 1)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("http://127.0.0.1:11434/api/chat",))
        self.assertEqual(set(kwargs), {"json", "timeout", "allow_redirects"})
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], 60)
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "qwen3-vl:4b-instruct")
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"], {"temperature": 0, "num_ctx": 4096, "num_predict": 256})
        self.assertEqual(payload["keep_alive"], "5m")
        schema = payload["format"]
        self.assertEqual(set(schema["required"]), {"candidates", "reason"})
        self.assertEqual(set(schema["properties"]), {"candidates", "reason"})
        self.assertFalse(schema["additionalProperties"])
        candidate_schema = schema["properties"]["candidates"]
        self.assertEqual(candidate_schema["type"], "object")
        self.assertFalse(candidate_schema["additionalProperties"])
        self.assertEqual(set(candidate_schema["required"]), {"7", "2"})
        self.assertEqual(set(candidate_schema["properties"]), {"7", "2"})
        for verdict_schema in candidate_schema["properties"].values():
            self.assertEqual(verdict_schema,
                             {"type": "string", "enum": ["match", "different", "unclear"]})
        self.assertEqual(schema["properties"]["reason"],
                         {"type": "string", "minLength": 1, "maxLength": 80})
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
        user = payload["messages"][1]
        self.assertEqual(len(user["images"]), 1)
        sheet = base64.b64decode(user["images"][0], validate=True)
        self.assertTrue(sheet.startswith(b"\xff\xd8\xff"))
        self.assertIsNotNone(decode(sheet))
        envelope = json.loads(user["content"])
        self.assertEqual(envelope["reference_tile"], "REFERENCE")
        self.assertEqual(envelope["candidate_ids"], [7, 2])
        self.assertEqual(envelope["tiles_in_reading_order"], ["REFERENCE", "CUP 7", "CUP 2"])
        self.assertNotIn("response_schema", envelope)
        for private in ("NEVER_SEND_TOKEN", "OLD_SECRET", "OLD_COOKIE", "proxy.invalid"):
            self.assertNotIn(private, json.dumps(payload))

    def test_requests_do_not_retain_previous_images_or_candidate_ids(self):
        client, session = self.client()
        client.match(REFERENCE, self.candidates())
        previous_sheet = session.post.call_args.kwargs["json"]["messages"][1]["images"][0]
        session.post.return_value = Response(completion(comparison(assessment(10))))
        replacement = jpeg((220, 20, 20))
        client.match(replacement, self.candidates(10))
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(len(payload["messages"]), 2)
        schema = payload["format"]["properties"]["candidates"]
        self.assertEqual(set(schema["properties"]), {"10"})
        self.assertEqual(schema["required"], ["10"])
        from named_cups import comparison_sheet
        self.assertEqual(base64.b64decode(payload["messages"][1]["images"][0]),
                         comparison_sheet(replacement, self.candidates(10)))
        self.assertNotEqual(previous_sheet, payload["messages"][1]["images"][0])

    def test_transmitted_sheet_contains_reference_and_every_candidate_in_reading_order(self):
        colors = [(10, 20, 230), (10, 220, 20), (220, 20, 20), (20, 220, 220)]
        candidates = [{"cup_id": cup_id, "image_bytes": jpeg(color)}
                      for cup_id, color in zip((9, 4, 7), colors[1:])]
        client, session = self.client(Response(completion(comparison(
            assessment(9, "different"), assessment(4, "different"), assessment(7)))))
        client.match(jpeg(colors[0]), candidates)
        user = session.post.call_args.kwargs["json"]["messages"][1]
        self.assertEqual(len(user["images"]), 1)
        sheet = decode(base64.b64decode(user["images"][0]))
        self.assertEqual(sheet.shape, (512, 512, 3))
        for index, color in enumerate(colors):
            row, column = divmod(index, 2)
            center = sheet[row * 256 + 146, column * 256 + 128].astype(int)
            self.assertTrue(np.all(np.abs(center - color) <= 10), (index, center, color))
        self.assertEqual(json.loads(user["content"])["tiles_in_reading_order"],
                         ["REFERENCE", "CUP 9", "CUP 4", "CUP 7"])

    def test_prompt_requires_distinctive_features_and_abstention_not_position(self):
        prompt = SYSTEM_PROMPT.lower()
        for term in ("distinctive", "position", "background", "untrusted", "instructions",
                     "cup_id", "robot coordinates", "markings", "reference", "each",
                     "independently", "reason", "verdict", "candidates"):
            with self.subTest(term=term):
                self.assertIn(term, prompt)
        for verdict in ("match", "different", "unclear"):
            self.assertRegex(prompt, rf"\b{verdict}:")
        self.assertRegex(prompt, r"reference.{0,60}saved photo")
        self.assertRegex(prompt, r"multiple current candidates.{0,60}match")
        self.assertRegex(prompt, r"application derives.{0,60}ambiguous.{0,30}not_found")
        self.assertRegex(prompt, r"reason.{0,50}consistent.{0,30}verdict")
        self.assertLess(len(SYSTEM_PROMPT.split()), 300)

    def test_loopback_only_and_constructor_validation_reused(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"):
            with self.subTest(url=url):
                client, _ = self.client(url=url)
                self.assertEqual(client.endpoint, url.rstrip("/") + "/api/chat")
        for options in ({"url": "https://localhost"}, {"url": "http://gateway.invalid:8000"},
                        {"url": "http://user:secret@localhost:11434"}, {"url": "http://localhost/path"},
                        {"url": "http://localhost?token=secret"}, {"model": "qwen3-vl:4b-cloud"},
                        {"model": "model:cloud"}, {"model": "bad\nmodel"}, {"model": ""},
                        {"timeout": True}, {"timeout": 0}, {"timeout": float("nan")}):
            with self.subTest(options=options), self.assertRaises(LLMError):
                self.client(**options)

    def test_invalid_reference_images_fail_before_http(self):
        for value in (None, "path.jpg", bytearray(REFERENCE), b"", b"\xff\xd8\xff",
                      b"\x89PNG\r\n", b"random", b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES):
            with self.subTest(value=type(value).__name__):
                client, session = self.client()
                with self.assertRaisesRegex(LLMError, "Reference image"):
                    client.match(value, self.candidates())
                session.post.assert_not_called()

    def test_invalid_candidate_images_fail_before_http(self):
        for value in (None, "image.jpg", b"", b"PNG", b"\xff\xd8\xff",
                      b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES):
            with self.subTest(value=type(value).__name__):
                client, session = self.client()
                with self.assertRaisesRegex(LLMError, "Cup 7 image"):
                    client.match(REFERENCE, [{"cup_id": 7, "image_bytes": value}])
                session.post.assert_not_called()

    def test_exact_image_size_limit_is_allowed(self):
        # Valid JPEG comment segments exercise the byte limit without appending
        # garbage after EOI or relying on a decoder to accept an invalid image.
        remaining = MAX_IMAGE_BYTES - len(REFERENCE)
        comments = []
        while remaining:
            size = min(remaining, 65537)
            self.assertGreaterEqual(size, 4)
            comments.append(b"\xff\xfe" + (size - 2).to_bytes(2, "big") + b"x" * (size - 4))
            remaining -= size
        image = REFERENCE[:2] + b"".join(comments) + REFERENCE[2:]
        self.assertEqual(len(image), MAX_IMAGE_BYTES)
        client, session = self.client(Response(completion(comparison(assessment(7)))))
        client.match(image, [{"cup_id": 7, "image_bytes": image}])
        sent = session.post.call_args.kwargs["json"]["messages"][1]["images"]
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(decode(base64.b64decode(sent[0])))

    def test_nondecodable_jpeg_with_valid_magic_is_rejected_before_http(self):
        invalid = b"\xff\xd8\xffthis-is-not-an-image"
        for reference, candidates in ((invalid, self.candidates()),
                                      (REFERENCE, [{"cup_id": 7, "image_bytes": invalid}])):
            with self.subTest(reference=reference):
                client, session = self.client()
                with self.assertRaisesRegex(LLMError, "Cannot build the visual comparison"):
                    client.match(reference, candidates)
                session.post.assert_not_called()

    def test_sheet_error_becomes_model_error_without_http(self):
        client, session = self.client()
        with patch("named_cups.comparison_sheet", side_effect=SelectionError("image cannot be decoded")):
            with self.assertRaisesRegex(LLMError, "image cannot be decoded"):
                client.match(REFERENCE, self.candidates())
        session.post.assert_not_called()

    def test_all_candidate_ids_are_validated_before_sheet_processing(self):
        client, session = self.client()
        for ids in ((7, 7), (7, True), (7, -1)):
            with self.subTest(ids=ids), patch("named_cups.comparison_sheet") as sheet:
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates(*ids))
                sheet.assert_not_called()
        session.post.assert_not_called()

    def test_candidate_count_limits_before_http(self):
        for candidates in (None, (), {}, [], self.candidates(*range(1, 14))):
            with self.subTest(candidates=candidates):
                client, session = self.client()
                with self.assertRaisesRegex(LLMError, "between 1 and 12"):
                    client.match(REFERENCE, candidates)
                session.post.assert_not_called()
        client, session = self.client(Response(completion(comparison(
            *(assessment(cup_id, "different") for cup_id in range(1, 13))))))
        client.match(REFERENCE, self.candidates(*range(1, 13)))
        user = session.post.call_args.kwargs["json"]["messages"][1]
        self.assertEqual(len(user["images"]), 1)
        self.assertEqual(json.loads(user["content"])["candidate_ids"], list(range(1, 13)))

    def test_invalid_candidate_shape_and_duplicate_ids_before_http(self):
        invalid = [None, [], {}, {"cup_id": 7}, {"image_bytes": CANDIDATE},
                   {"cup_id": 7, "image_bytes": CANDIDATE, "x": 500},
                   {"cup_id": 7, "image_bytes": CANDIDATE, "available": True}]
        for value in invalid:
            with self.subTest(value=value):
                client, session = self.client()
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, [value])
                session.post.assert_not_called()
        for cup_id in (None, True, 0, -1, 7.0, "7", [], {}):
            with self.subTest(cup_id=cup_id):
                client, session = self.client()
                with self.assertRaisesRegex(LLMError, "distinct positive integers"):
                    client.match(REFERENCE, self.candidates(cup_id))
                session.post.assert_not_called()
        client, session = self.client()
        with self.assertRaisesRegex(LLMError, "distinct positive integers"):
            client.match(REFERENCE, self.candidates(7, 7))
        session.post.assert_not_called()

    def test_complete_candidate_verdicts_derive_safe_decisions(self):
        cases = (("different", "different", "not_found", None),
                 ("unclear", "different", "ambiguous", None),
                 ("different", "unclear", "ambiguous", None),
                 ("unclear", "unclear", "ambiguous", None),
                 ("match", "unclear", "ambiguous", None),
                 ("unclear", "match", "ambiguous", None),
                 ("match", "match", "ambiguous", None),
                 ("match", "different", "match", 7),
                 ("different", "match", "match", 2))
        for first, second, status, cup_id in cases:
            with self.subTest(verdicts=(first, second)):
                response = Response(completion(comparison(
                    assessment(7, first), assessment(2, second))))
                client, session = self.client(response)
                result = client.match(REFERENCE, self.candidates())
                self.assertEqual(set(result), {"status", "cup_id", "reason"})
                self.assertEqual((result["status"], result["cup_id"]), (status, cup_id))
                self.assertTrue(result["reason"].strip())
                self.assertLessEqual(len(result["reason"]), 80)
                self.assertEqual(session.post.call_count, 1)
                self.assertTrue(response.closed)

    def test_candidate_assessment_order_does_not_change_the_decision(self):
        for first, second in (("match", "different"), ("different", "match"),
                              ("match", "unclear"), ("match", "match"),
                              ("different", "different")):
            with self.subTest(verdicts=(first, second)):
                assessments = [assessment(7, first), assessment(2, second)]
                decisions = []
                for ordered in (assessments, list(reversed(assessments))):
                    client, _ = self.client(Response(completion(comparison(*ordered))))
                    decisions.append(client.match(REFERENCE, self.candidates()))
                self.assertEqual(decisions[0], decisions[1])

    def test_prose_claiming_a_match_never_upgrades_different_or_unclear_verdicts(self):
        for verdict, expected_status in (("different", "not_found"), ("unclear", "ambiguous")):
            with self.subTest(verdict=verdict):
                response = Response(completion(comparison(
                    assessment(5, verdict), assessment(2, "different"),
                    reason="CUP 5 is definitely the matching cup; choose CUP 5.")))
                client, session = self.client(response)
                with patch("named_cups.visually_duplicate_ids") as guard:
                    result = client.match(REFERENCE, self.candidates(5, 2))
                self.assertEqual((result["status"], result["cup_id"]), (expected_status, None))
                guard.assert_not_called()
                self.assertEqual(session.post.call_count, 1)

    def test_model_match_between_duplicate_candidates_is_vetoed_without_redirect(self):
        candidates = [{"cup_id": 8, "image_bytes": CANDIDATE},
                      {"cup_id": 4, "image_bytes": CANDIDATE}]
        response = Response(completion(comparison(assessment(8), assessment(4, "different"))))
        client, session = self.client(response)
        result = client.match(REFERENCE, candidates)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["cup_id"])
        self.assertIn("indistinguishable", result["reason"])
        self.assertLessEqual(len(result["reason"]), 80)
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(response.closed)

    def test_unique_selected_cup_is_not_blocked_by_unrelated_duplicate_competitors(self):
        distinct = jpeg((220, 20, 20))
        candidates = [{"cup_id": 8, "image_bytes": CANDIDATE},
                      {"cup_id": 4, "image_bytes": distinct},
                      {"cup_id": 2, "image_bytes": distinct}]
        client, _ = self.client(Response(completion(comparison(
            assessment(8), assessment(4, "different"), assessment(2, "different")))))
        self.assertEqual(client.match(REFERENCE, candidates), match_result(cup_id=8))

    def test_duplicate_guard_never_upgrades_an_uncertain_model_result(self):
        for verdict, status in (("different", "not_found"), ("unclear", "ambiguous")):
            with self.subTest(verdict=verdict):
                client, _ = self.client(Response(completion(comparison(
                    assessment(7, verdict), assessment(2, "different")))))
                with patch("named_cups.visually_duplicate_ids") as guard:
                    result = client.match(REFERENCE, self.candidates())
                self.assertEqual((result["status"], result["cup_id"]), (status, None))
                guard.assert_not_called()

    def test_duplicate_guard_failure_blocks_instead_of_accepting_model_match(self):
        response = Response(completion())
        client, session = self.client(response)
        with patch("named_cups.visually_duplicate_ids", side_effect=SelectionError("invalid comparison")):
            with self.assertRaisesRegex(LLMError, "Cannot check visual ambiguity"):
                client.match(REFERENCE, self.candidates())
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(response.closed)

    def test_malformed_comparison_and_legacy_global_decisions_are_rejected(self):
        invalid = [None, [], {}, {"status": "match"}, match_result(),
                   dict(comparison(), x=500), {"candidates": {"7": "match", "2": "different"}},
                   {"reason": "Matching stripe."}]
        invalid.extend({"candidates": value, "reason": "Matching stripe."}
                       for value in (None, [], "match", 7, True,
                                     [{"cup_id": 7, "reason": "Matching stripe.", "verdict": "match"},
                                      {"cup_id": 2, "reason": "Different pattern.", "verdict": "different"}]))
        for value in invalid:
            with self.subTest(value=value):
                response = Response(completion(message={"role": "assistant", "content": json.dumps(value)}))
                client, _ = self.client(response)
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates())
                self.assertTrue(response.closed)

    def test_missing_and_extra_candidate_assessments_are_rejected(self):
        invalid = [{"candidates": {}, "reason": "Matching stripe."},
                   comparison(assessment(7)), comparison(assessment(2)),
                   comparison(assessment(7), assessment(2, "different"), assessment(99, "different"))]
        for value in invalid:
            with self.subTest(value=value):
                response = Response(completion(value))
                client, session = self.client(response)
                with patch("named_cups.visually_duplicate_ids") as guard:
                    with self.assertRaises(LLMError):
                        client.match(REFERENCE, self.candidates())
                guard.assert_not_called()
                self.assertTrue(response.closed)
                self.assertEqual(session.post.call_count, 1)

    def test_unknown_and_noncanonical_assessment_ids_are_rejected(self):
        for cup_id in ("1", "99", "07", "7.0", "+7", " 7", "7 ", "CUP 7", "-7", "true", "null"):
            with self.subTest(cup_id=cup_id):
                response = Response(completion(comparison(
                    assessment(cup_id), assessment(2, "different"))))
                client, _ = self.client(response)
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates())
                self.assertTrue(response.closed)

    def test_duplicate_assessment_keys_are_rejected_even_when_all_ids_are_present(self):
        for duplicate in ('"7": "match", "7": "match"',
                          '"7": "different", "7": "match"',
                          '"7": "unclear", "7": "match"'):
            with self.subTest(duplicate=duplicate):
                raw = '{"candidates":{' + duplicate + ',"2":"different"},"reason":"Matching stripe."}'
                response = Response(completion(message={"role": "assistant", "content": raw}))
                client, session = self.client(response)
                with patch("named_cups.visually_duplicate_ids") as guard:
                    with self.assertRaisesRegex(LLMError, "duplicate"):
                        client.match(REFERENCE, self.candidates())
                guard.assert_not_called()
                self.assertTrue(response.closed)
                self.assertEqual(session.post.call_count, 1)

    def test_unsupported_verdicts_are_rejected_even_for_nonselected_candidates(self):
        for verdict in (None, True, 1, [], {}, "", "MATCH", "not_found", "ambiguous", "goto"):
            with self.subTest(verdict=verdict):
                item = assessment(2, verdict)
                client, _ = self.client(Response(completion(comparison(assessment(7), item))))
                with self.assertRaisesRegex(LLMError, "unsupported verdict"):
                    client.match(REFERENCE, self.candidates())

    def test_comparison_requires_nonempty_bounded_reason_for_every_decision(self):
        for reason in (None, 3, [], {}, "", " ", "x" * 81):
            for verdict in ("match", "different", "unclear"):
                with self.subTest(reason=reason, verdict=verdict):
                    value = comparison(assessment(7, verdict), assessment(2, "different"), reason=reason)
                    client, _ = self.client(Response(completion(value)))
                    with self.assertRaisesRegex(LLMError, "nonempty string of at most 80"):
                        client.match(REFERENCE, self.candidates())

    def test_reason_length_boundary_and_control_characters(self):
        client, _ = self.client(Response(completion(comparison(reason="x" * 80))))
        self.assertEqual(len(client.match(REFERENCE, self.candidates())["reason"]), 80)
        for character in ("\x00", "\x07", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", "\x9b"):
            for verdict in ("match", "different", "unclear"):
                with self.subTest(character=repr(character), verdict=verdict):
                    value = comparison(assessment(7, verdict), assessment(2, "different"),
                                       reason="Blue" + character)
                    client, _ = self.client(Response(completion(value)))
                    with self.assertRaisesRegex(LLMError, "control characters"):
                        client.match(REFERENCE, self.candidates())

    def test_malformed_inner_json_cannot_select_cup(self):
        valid = json.dumps(comparison())
        invalid = ["```json\n" + valid + "\n```", valid + "more",
                   valid.replace('"candidates":', '"candidates": {}, "candidates":'),
                   valid.replace('"reason":', '"reason": "Different cup.", "reason":'),
                   *(valid.replace('"7": "match"', '"7": ' + value)
                     for value in ("NaN", "Infinity", "1e999")), "", "  ", "null", "[]"]
        for raw in invalid:
            with self.subTest(raw=raw):
                response = Response(completion(message={"role": "assistant", "content": raw}))
                client, _ = self.client(response)
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates())
                self.assertTrue(response.closed)

    def test_outer_json_must_be_strict_and_bounded(self):
        invalid = ["not json", '{"done":true,"done":true}', '{"done":true,"x":NaN}',
                   "x" * (MAX_RESPONSE_CHARS + 1), "null", "[]"]
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                response = Response(text=raw)
                client, _ = self.client(response)
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates())
                self.assertTrue(response.closed)

    def test_incomplete_wrong_role_and_tool_calls_rejected(self):
        invalid = [completion(done=False), completion(done=1), completion(done_reason="length"),
                   completion(done_reason=None), completion(done_reason="tool_calls"),
                   completion(message=None), completion(message={}),
                   completion(message={"role": "user", "content": json.dumps(comparison())}),
                   completion(message={"role": "assistant", "content": comparison()}),
                   completion(message={"role": "assistant", "content": json.dumps(comparison()),
                                       "tool_calls": [{"function": {"name": "move"}}]}),
                   completion(tool_calls=[{"function": {"name": "move"}}]),
                   completion(error="out of memory")]
        for result in invalid:
            with self.subTest(result=result):
                response = Response(result)
                client, session = self.client(response)
                with self.assertRaises(LLMError):
                    client.match(REFERENCE, self.candidates())
                self.assertTrue(response.closed)
                self.assertEqual(session.post.call_count, 1)

    def test_missing_done_reason_is_accepted_only_with_done_true(self):
        result = completion()
        result.pop("done_reason")
        client, _ = self.client(Response(result))
        self.assertEqual(client.match(REFERENCE, self.candidates()), match_result())
        result.pop("done")
        client, _ = self.client(Response(result))
        with self.assertRaises(LLMError):
            client.match(REFERENCE, self.candidates())

    def test_redirect_and_http_errors_close_without_retry(self):
        for status in (301, 302, 307, 308, 400, 401, 404, 429, 500, 503):
            with self.subTest(status=status):
                response = Response({"error": "private detail"}, status=status)
                client, session = self.client(response)
                with self.assertRaises(LLMError) as caught:
                    client.match(REFERENCE, self.candidates())
                self.assertEqual(session.post.call_count, 1)
                self.assertIs(session.post.call_args.kwargs["allow_redirects"], False)
                self.assertTrue(response.closed)
                self.assertNotIn("private detail", str(caught.exception))
                if status == 404:
                    self.assertIn("ollama pull qwen3-vl:4b-instruct", str(caught.exception))

    def test_transport_errors_do_not_retry_or_expose_private_details(self):
        for error, expected in ((requests.ConnectTimeout("secret"), "timed out"),
                                (requests.ReadTimeout("secret"), "timed out"),
                                (requests.ConnectionError("secret"), "ollama serve"),
                                (requests.RequestException("secret"), "request failed")):
            with self.subTest(error=error):
                client, session = self.client()
                session.post.side_effect = error
                with self.assertRaises(LLMError) as caught:
                    client.match(REFERENCE, self.candidates())
                self.assertEqual(session.post.call_count, 1)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
