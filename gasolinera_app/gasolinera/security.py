"""
Minimal security utilities: user storage, password hashing, and role checks.

The classes here exist to illustrate how a future admin dashboard or API could
enforce role-based access without locking the project to a specific framework.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Dict, Iterable, Set


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


@dataclass
class User:
    username: str
    salt: str
    password_hash: str
    roles: Set[str] = field(default_factory=set)

    def verify_password(self, password: str) -> bool:
        expected = _hash_password(password, self.salt)
        return hmac.compare_digest(expected, self.password_hash)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class AuthService:
    """In-memory authentication service."""

    def __init__(self):
        self._users: Dict[str, User] = {}

    def register_user(self, username: str, password: str, *, roles: Iterable[str] = ()) -> User:
        salt = hashlib.sha1(username.encode("utf-8")).hexdigest()[:10]
        user = User(
            username=username,
            salt=salt,
            password_hash=_hash_password(password, salt),
            roles=set(r.lower() for r in roles),
        )
        self._users[username] = user
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        user = self._users.get(username)
        if user and user.verify_password(password):
            return user
        return None

    def require_role(self, user: User, role: str) -> bool:
        return role.lower() in user.roles
