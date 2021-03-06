#!/usr/bin/env python
from __future__ import unicode_literals
import unittest
from elliottlib.runtime import Runtime


class RuntimeTestCase(unittest.TestCase):
    def test_parallel_exec(self):
        ret = Runtime._parallel_exec(lambda x: x * 2, range(5), n_threads=20)
        self.assertEqual(ret.get(), [0, 2, 4, 6, 8])


if __name__ == "__main__":
    unittest.main()
