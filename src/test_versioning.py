import unittest, random
from versioning import *


def obj(events, eco="npm", n="demo"):
    return {
        "affected": [
            {
                "package": {"ecosystem": eco, "name": n},
                "ranges": [
                    {
                        "type": "SEMVER" if eco == "npm" else "ECOSYSTEM",
                        "events": events,
                    }
                ],
            }
        ]
    }


class Tests(unittest.TestCase):
    def test_boundaries(self):
        o = obj([{"introduced": "1.0.0"}, {"fixed": "1.2.0"}])
        r = compile_record(o)
        for v, s in [
            ("0.9.0", "unaffected"),
            ("1.0.0", "affected"),
            ("1.1.99", "affected"),
            ("1.2.0", "unaffected"),
        ]:
            self.assertEqual(membership(r, ("npm", "demo"), v), s)

    def test_union_branches(self):
        o = obj(
            [
                {"introduced": "0"},
                {"fixed": "1.2.0"},
                {"introduced": "2.0.0"},
                {"fixed": "2.1.0"},
            ]
        )
        r = compile_record(o)
        self.assertEqual(membership(r, ("npm", "demo"), "1.2.0"), "unaffected")
        self.assertEqual(membership(r, ("npm", "demo"), "2.0.9"), "affected")

    def test_last_inclusive(self):
        r = compile_record(obj([{"introduced": "0"}, {"last_affected": "1.2.0"}]))
        self.assertEqual(membership(r, ("npm", "demo"), "1.2.0"), "affected")

    def test_withdrawal(self):
        o = obj([{"introduced": "0"}])
        o["withdrawn"] = "2024-01-01"
        self.assertEqual(
            membership(compile_record(o), ("npm", "demo"), "1.0.0"), "withdrawn"
        )

    def test_invalid_unknown(self):
        r = compile_record(obj([{"introduced": "0"}, {"fixed": "banana"}]))
        self.assertEqual(membership(r, ("npm", "demo"), "1.0.0"), "unknown")

    def test_pypi_epoch_post(self):
        r = compile_record(
            obj(
                [{"introduced": "1!1.0"}, {"fixed": "1!1.0.post1"}], "PyPI", "Demo_Name"
            )
        )
        self.assertEqual(membership(r, ("PyPI", "demo-name"), "1!1.0"), "affected")
        self.assertEqual(
            membership(r, ("PyPI", "demo-name"), "1!1.0.post1"), "unaffected"
        )

    def test_npm_prerelease_build(self):
        self.assertLess(version("npm", "1.2.0-rc.2"), version("npm", "1.2.0"))
        self.assertEqual(version("npm", "1.0.0+abc"), version("npm", "1.0.0+def"))
        self.assertFalse(stable("npm", "1.0.0-rc.1"))

    def test_semantic_merge(self):
        a = compile_record(obj([{"introduced": "0"}, {"fixed": "2.0.0"}]))
        o = obj(
            [
                {"introduced": "0"},
                {"fixed": "1.0.0"},
                {"introduced": "1.0.0"},
                {"fixed": "2.0.0"},
            ]
        )
        # A duplicated boundary event is flagged; split range objects have unambiguous union.
        o["affected"][0]["ranges"] = [
            {"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.0.0"}]},
            {"type": "SEMVER", "events": [{"introduced": "1.0.0"}, {"fixed": "2.0.0"}]},
        ]
        self.assertEqual(signature(a), signature(compile_record(o)))

    def test_randomized_oracle(self):
        rng = random.Random(260927)
        for eco in ("npm", "PyPI"):
            for _ in range(1000):
                nums = sorted(rng.sample(range(1, 100), 4))
                vs = [f"1.{n}.0" for n in nums]
                o = obj(
                    [
                        {"introduced": vs[0]},
                        {"fixed": vs[1]},
                        {"introduced": vs[2]},
                        {"fixed": vs[3]},
                    ],
                    eco,
                )
                r = compile_record(o)
                for n in range(0, 101):
                    v = f"1.{n}.0"
                    self.assertEqual(
                        membership(r, (eco, "demo"), v),
                        direct_membership(o, (eco, "demo"), v),
                    )


if __name__ == "__main__":
    unittest.main()
