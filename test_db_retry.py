# -*- coding: utf-8 -*-
"""db 层跨境连接抖动自动重试的单元测试（用假数据库接口，不连真实 Supabase）。"""
import time
import unittest
from unittest import mock

import psycopg2

import db


class _FakeConn:
    """假连接：记录执行次数，可设定前 N 次抛连接错误。"""

    def __init__(self, fail_times=0):
        self.closed = False
        self.autocommit = False
        self.fail_times = fail_times
        self.calls = 0

    def execute_should_fail(self):
        if self.calls < self.fail_times:
            self.calls += 1
            raise psycopg2.OperationalError("SSL connection has been closed unexpectedly")
        self.calls += 1
        return True


class TestDbRetry(unittest.TestCase):
    """@db_retry 装饰器的行为验证。"""

    def setUp(self):
        # 把退避等待压到 0，测试不用真的 sleep
        self.origin_delay = db._BASE_DELAY
        db._BASE_DELAY = 0

    def tearDown(self):
        db._BASE_DELAY = self.origin_delay

    def test_read_retries_then_succeeds(self):
        """读操作：前 2 次抖动、第 3 次成功，应返回结果且不报错。"""
        conn = _FakeConn(fail_times=2)

        @db.db_retry()
        def read_it():
            return conn.execute_should_fail()

        self.assertTrue(read_it())
        self.assertEqual(conn.calls, 3, "应重试到第三次才成功")

    def test_write_retries_less_than_read(self):
        """写操作重试次数比读少：第 3 次仍失败应直接抛出。"""
        conn = _FakeConn(fail_times=99)

        @db.db_retry(write=True)
        def write_it():
            return conn.execute_should_fail()

        with self.assertRaises(db.DbConnectionError):
            write_it()
        self.assertEqual(conn.calls, db._RETRY_WRITE)

    def test_read_exhausted_raises_friendly_error(self):
        """重试耗尽后抛出的是带中文说明的 DbConnectionError，而非裸 psycopg2 异常。"""

        @db.db_retry()
        def always_fail():
            raise psycopg2.OperationalError("could not connect to server")

        with self.assertRaises(db.DbConnectionError) as ctx:
            always_fail()
        msg = str(ctx.exception)
        self.assertIn("数据库连接不上", msg)
        self.assertIn("稍等", msg)

    def test_sql_logic_error_not_retried(self):
        """SQL 逻辑错误（如唯一约束冲突）不该重试，必须原样抛出。"""
        calls = {"n": 0}

        @db.db_retry()
        def bad_sql():
            calls["n"] += 1
            raise psycopg2.errors.SyntaxError("syntax error near FROM")

        with self.assertRaises(psycopg2.errors.SyntaxError):
            bad_sql()
        self.assertEqual(calls["n"], 1, "逻辑错误一次就够，不该重试")

    def test_pool_is_reset_between_retries(self):
        """每次重试前都应销毁并重建连接池，否则旧连接会继续失败。"""
        with mock.patch.object(db, "_reset_pool") as reset_mock:
            conn = _FakeConn(fail_times=1)

            @db.db_retry()
            def flaky():
                return conn.execute_should_fail()

            self.assertTrue(flaky())
            self.assertEqual(reset_mock.call_count, 1, "重试前应重置一次连接池")

    def test_function_metadata_preserved(self):
        """装饰器要保留原函数名和注释，方便排障和 introspection。"""

        @db.db_retry()
        def my_dao():
            """我是一个 DAO。"""
            return 1

        self.assertEqual(my_dao.__name__, "my_dao")
        self.assertEqual(my_dao.__doc__, "我是一个 DAO。")


class TestRetryConfig(unittest.TestCase):
    """重试参数本身的合理性。"""

    def test_read_retry_more_than_write(self):
        self.assertGreater(db._RETRY_READ, db._RETRY_WRITE,
                           "读可以多重试几次，写要保守以免重复写入")

    def test_transient_errors_only(self):
        self.assertIn(psycopg2.OperationalError, db._TRANSIENT_EXC)
        self.assertIn(psycopg2.InterfaceError, db._TRANSIENT_EXC)
        for exc in db._TRANSIENT_EXC:
            self.assertTrue(issubclass(exc, Exception))


if __name__ == "__main__":
    unittest.main()
