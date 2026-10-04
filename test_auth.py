"""auth 模块测试：密码哈希与校验往返。"""
import unittest
from unittest.mock import patch, MagicMock

import auth


class TestAuth(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        """相同密码应通过校验，不同密码应失败。"""
        hashed = auth._hash_password("test-password-123")
        self.assertTrue(auth._verify_password("test-password-123", hashed))
        self.assertFalse(auth._verify_password("wrong-password", hashed))

    def test_hash_uses_random_salt(self):
        """相同密码两次哈希结果应不同（随机盐）。"""
        h1 = auth._hash_password("same-password")
        h2 = auth._hash_password("same-password")
        self.assertNotEqual(h1, h2)
        # 但都能通过校验
        self.assertTrue(auth._verify_password("same-password", h1))
        self.assertTrue(auth._verify_password("same-password", h2))

    def test_verify_invalid_stored_format(self):
        """存储格式错误时应返回 False 而非抛异常。"""
        self.assertFalse(auth._verify_password("any", "invalid-format"))
        self.assertFalse(auth._verify_password("any", ""))
        self.assertFalse(auth._verify_password("any", None))

    @patch("db.set_setting")
    def test_set_password_too_short(self, _mock_set):
        """密码少于 8 位应抛 ValueError。"""
        with self.assertRaises(ValueError):
            auth.set_access_password("1234567")  # 7 位

    @patch("db.set_setting")
    def test_set_password_min_length_ok(self, mock_set):
        """密码恰好 8 位应通过。"""
        auth.set_access_password("12345678")
        mock_set.assert_called_once()

    @patch("db.get_setting")
    def test_verify_no_password_set(self, mock_get):
        """未设置密码时 verify_access_password 返回 False。"""
        mock_get.return_value = None
        self.assertFalse(auth.verify_access_password("any-password"))

    @patch("db.get_setting")
    def test_has_access_password(self, mock_get):
        """has_access_password 应反映 get_setting 返回值。"""
        mock_get.return_value = "some_hash"
        self.assertTrue(auth.has_access_password())
        mock_get.return_value = None
        self.assertFalse(auth.has_access_password())


if __name__ == "__main__":
    unittest.main()
