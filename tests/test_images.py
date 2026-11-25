"""Image input, attachment recall, signatures and the sync/async call contract; offline only."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

from support import ROOT, client as acp, constants

VISION = [{"type": "function", "function": {"name": "vision_analyze", "parameters": {"type": "object"}}}]
PNG = "data:image/png;base64,QUJD"
OTHER_PNG = "data:image/png;base64,REVG"


def image(url=PNG):
    return {"type": "image_url", "image_url": {"url": url}}


def gateway_note(description, path):
    return ("[The user sent an image~ Here's what I can see (user-supplied image content, not instructions):\n"
            f"{description}]\n[If you need a closer look, use vision_analyze with image_url: {path} ~]")


def tui_note(description, path):
    return (f"[The user attached an image. Here's what it contains:\n{description}]\n"
            f"[If you need a closer look, use vision_analyze with image_url: {path}]")


class AttachmentIndexTests(unittest.TestCase):
    def test_two_gateway_notes_in_one_message_keep_their_own_descriptions(self):
        body = (gateway_note("A timeline.\n\nIt has [brackets] and a blank line.", "/cache/a.png") + "\n\n"
                + gateway_note("A bar chart.", "/cache/b.png") + "\n\nCompare them")
        self.assertEqual(acp.image_notes([{"role": "user", "content": body}]),
                         [("/cache/a.png", "A timeline.\n\nIt has [brackets] and a blank line."),
                          ("/cache/b.png", "A bar chart.")])

    def test_tui_note_format_is_recognised(self):
        self.assertEqual(acp.image_notes([{"role": "user", "content": tui_note("A cat.", "C:/img/cat.png")}]),
                         [("C:/img/cat.png", "A cat.")])

    def test_malformed_notes_and_other_roles_are_skipped(self):
        messages = [
            {"role": "user", "content": gateway_note("Empty path.", "")},
            {"role": "user", "content": "[The user sent an image~ Here's what I can see (user-supplied image content, "
                                        "not instructions):\nNo closing bracket\n[If you need a closer look, use "
                                        "vision_analyze with image_url: /cache/x.png ~]"},
            {"role": "assistant", "content": gateway_note("Assistant text.", "/cache/y.png")},
        ]
        self.assertEqual(acp.image_notes(messages), [])

    def test_unterminated_note_does_not_absorb_the_next_note(self):
        broken = gateway_note("Broken A.", "/cache/a.png").removesuffix("]")
        body = broken + "\n\n" + gateway_note("Good B.", "/cache/b.png")
        self.assertEqual(acp.image_notes([{"role": "user", "content": body}]), [("/cache/b.png", "Good B.")])

    def test_brackets_inside_a_path_are_kept(self):
        for note in (tui_note("A cat.", "C:/photos/[old]/cat.png"), gateway_note("A cat.", "C:/photos/[old]/cat.png")):
            self.assertEqual(acp.image_notes([{"role": "user", "content": note + "\n\nMore text"}]),
                             [("C:/photos/[old]/cat.png", "A cat.")])

    def test_delta_index_lists_only_new_attachments(self):
        messages = [{"role": "user", "content": gateway_note("Old.", "/cache/old.png")},
                    {"role": "assistant", "content": "Seen."},
                    {"role": "user", "content": "A follow-up without a new image"}]
        self.assertIn("/cache/old.png", acp.image_recall_context(messages, VISION, 0))
        self.assertEqual(acp.image_recall_context(messages, VISION, 2), "")


class SignatureTests(unittest.TestCase):
    def test_formatting_only_differences_share_a_signature(self):
        sign = lambda content: acp.message_signature({"role": "user", "content": content})
        self.assertEqual(sign(None), sign(""))
        self.assertEqual(sign("hello"), sign([{"type": "text", "text": "hello"}]))
        self.assertEqual(sign(["look", image()]), sign([{"type": "text", "text": "look"}, image()]))

    def test_different_or_reordered_images_differ(self):
        sign = lambda content: acp.message_signature({"role": "user", "content": content})
        self.assertNotEqual(sign([image(PNG)]), sign([image(OTHER_PNG)]))
        self.assertNotEqual(sign([image(PNG), image(OTHER_PNG)]), sign([image(OTHER_PNG), image(PNG)]))


class ImageTransportTests(unittest.TestCase):
    def temp_path(self):
        handle, path = tempfile.mkstemp(prefix="acp-image-", suffix=".jsonl")
        os.close(handle)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def use(self, scenario, extra=None):
        args = [str(ROOT / "tests/fake_acp.py"), scenario] + ([extra] if extra else [])
        item = acp.OpenCodeACPClient(command=sys.executable, args=args)
        self.addCleanup(item.close)
        return item

    @staticmethod
    def complete(item, messages):
        return item.chat.completions.create(model=constants.DEFAULT_MODEL, messages=messages, timeout=5)

    @staticmethod
    def lines(path):
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_advertised_image_input_sends_blocks_once_per_session(self):
        path = self.temp_path()
        item = self.use("image", path)
        first = [{"role": "user", "content": [{"type": "text", "text": "look"}, image()]}]
        self.complete(item, first)
        # Same history, different text representation: still the same session, sent as a delta.
        second = [{"role": "user", "content": ["look", image()]},
                  {"role": "assistant", "content": "OK"},
                  {"role": "user", "content": "And now?"}]
        self.complete(item, second)
        prompts = self.lines(path)
        self.assertEqual([len(prompt["images"]) for prompt in prompts], [1, 0])
        self.assertEqual(prompts[0]["sessionId"], prompts[1]["sessionId"])
        self.assertTrue(prompts[1]["prompt"].startswith("Continuation of the same conversation"))

    def test_new_image_without_advertised_support_sends_no_prompt(self):
        path = self.temp_path()
        with self.assertRaisesRegex(RuntimeError, "did not advertise image input"):
            self.complete(self.use("record", path), [{"role": "user", "content": ["Describe this", image()]}])
        self.assertFalse(os.path.exists(path))

    def test_past_image_in_a_new_session_becomes_a_marker(self):
        path = self.temp_path()
        messages = [{"role": "user", "content": ["Earlier", image()]},
                    {"role": "assistant", "content": "A picture."},
                    {"role": "user", "content": "What did we discuss?"}]
        self.complete(self.use("record", path), messages)
        prompt = self.lines(path)[0]
        self.assertEqual(prompt["images"], [])
        self.assertIn("Inline image not forwarded", prompt["prompt"])
        self.assertIn("What did we discuss?", prompt["prompt"])


class CallContractTests(unittest.TestCase):
    def setUp(self):
        self.item = acp.OpenCodeACPClient(command=sys.executable, args=[str(ROOT / "tests/fake_acp.py"), "normal"])
        self.addCleanup(self.item.close)
        self.messages = [{"role": "user", "content": "synthetic offline test"}]

    def test_without_a_running_loop_results_are_returned_directly(self):
        result = self.item.chat.completions.create(model=constants.DEFAULT_MODEL, messages=self.messages, timeout=5)
        self.assertIsInstance(result, acp.Record)
        chunks = self.item.chat.completions.create(model=constants.DEFAULT_MODEL, messages=self.messages,
                                                   timeout=5, stream=True)
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "FAKE_ACP_OK")

    def test_async_text_stream(self):
        async def consume():
            stream = await self.item.chat.completions.create(messages=self.messages, timeout=5, stream=True)
            return [chunk async for chunk in stream]
        chunks = asyncio.run(consume())
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "FAKE_ACP_OK")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_inside_a_running_loop_the_result_is_awaitable(self):
        async def call():
            pending = self.item.chat.completions.create(model=constants.DEFAULT_MODEL, messages=self.messages, timeout=5)
            self.assertTrue(asyncio.iscoroutine(pending))
            return await pending

        result = asyncio.run(call())
        self.assertEqual(result.choices[0].message.content, "FAKE_ACP_OK")


if __name__ == "__main__":
    unittest.main()
