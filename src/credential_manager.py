"""Subscription-aware credential management for LLM Cortex.

Uses one-way PBKDF2 hashing for secret material integrity checks and
records an audit trail of credential lifecycle operations.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from subscription import Provider, SubscriptionTier, parse_tier


DEFAULT_DATA_DIR = Path(os.environ.get("CORTEX_DATA_DIR", str(Path.home() / ".cortex" / "data"))).expanduser()
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "cortex-credentials.db"


@dataclass
class CredentialRecord:
    id: int
    provider: str
    tier: str
    key_id: str
    hash_version: int
    validation_status: str
    last_validated_at: Optional[str]
    last_used_at: Optional[str]
    created_at: str
    rotated_at: Optional[str]


class CredentialManager:
    HASH_VERSION = 1
    ITERATIONS = 200_000

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                subscription_tier TEXT NOT NULL,
                key_id TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                hash_version INTEGER NOT NULL DEFAULT 1,
                validation_status TEXT NOT NULL DEFAULT 'unknown',
                last_validated_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                rotated_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider, subscription_tier, key_id)
            );

            CREATE TABLE IF NOT EXISTS credential_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                credential_id INTEGER,
                provider TEXT NOT NULL,
                subscription_tier TEXT NOT NULL,
                key_id TEXT NOT NULL,
                event TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(credential_id) REFERENCES credentials(id)
            );

            CREATE INDEX IF NOT EXISTS idx_credentials_active
                ON credentials(provider, subscription_tier, is_active);
            CREATE INDEX IF NOT EXISTS idx_credentials_validation
                ON credentials(validation_status);
            CREATE INDEX IF NOT EXISTS idx_audit_credential
                ON credential_audit_log(credential_id, created_at DESC);
            """
        )
        self.conn.commit()

    @staticmethod
    def _hash_secret(secret: str, salt: bytes) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            secret.encode("utf-8"),
            salt,
            CredentialManager.ITERATIONS,
        )
        return digest.hex()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _audit(
        self,
        *,
        event: str,
        provider: str,
        tier: str,
        key_id: str,
        credential_id: Optional[int] = None,
        details: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO credential_audit_log "
            "(credential_id, provider, subscription_tier, key_id, event, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (credential_id, provider, tier, key_id, event, details, self._now()),
        )

    def store_credential(
        self,
        *,
        provider: Provider | str,
        subscription_tier: SubscriptionTier | str,
        key_id: str,
        secret: str,
        replace_existing: bool = True,
        retain_old: bool = False,
    ) -> int:
        provider_value = Provider(provider).value
        tier_value = parse_tier(subscription_tier.value if isinstance(subscription_tier, SubscriptionTier) else subscription_tier).value

        now = self._now()
        if replace_existing:
            if retain_old:
                self.conn.execute(
                    "UPDATE credentials SET is_active = 0, rotated_at = ? "
                    "WHERE provider = ? AND subscription_tier = ? AND key_id = ? AND is_active = 1",
                    (now, provider_value, tier_value, key_id),
                )
            else:
                self.conn.execute(
                    "DELETE FROM credentials WHERE provider = ? AND subscription_tier = ? AND key_id = ?",
                    (provider_value, tier_value, key_id),
                )

        salt = os.urandom(16)
        secret_hash = self._hash_secret(secret, salt)
        cursor = self.conn.execute(
            "INSERT INTO credentials "
            "(provider, subscription_tier, key_id, secret_hash, salt, hash_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (provider_value, tier_value, key_id, secret_hash, salt.hex(), self.HASH_VERSION, now),
        )
        credential_id = int(cursor.lastrowid)
        self._audit(
            event="created",
            provider=provider_value,
            tier=tier_value,
            key_id=key_id,
            credential_id=credential_id,
        )
        self.conn.commit()
        return credential_id

    def get_active_credentials(
        self,
        *,
        provider: Optional[Provider | str] = None,
        subscription_tier: Optional[SubscriptionTier | str] = None,
    ) -> list[CredentialRecord]:
        where = ["is_active = 1"]
        params: list[str] = []

        if provider is not None:
            where.append("provider = ?")
            params.append(Provider(provider).value)
        if subscription_tier is not None:
            parsed_tier = parse_tier(
                subscription_tier.value if isinstance(subscription_tier, SubscriptionTier) else subscription_tier
            )
            where.append("subscription_tier = ?")
            params.append(parsed_tier.value)

        query = (
            "SELECT id, provider, subscription_tier, key_id, hash_version, validation_status, "
            "last_validated_at, last_used_at, created_at, rotated_at "
            "FROM credentials WHERE " + " AND ".join(where)
        )
        rows = self.conn.execute(query, params).fetchall()
        return [
            CredentialRecord(
                id=row["id"],
                provider=row["provider"],
                tier=row["subscription_tier"],
                key_id=row["key_id"],
                hash_version=row["hash_version"],
                validation_status=row["validation_status"],
                last_validated_at=row["last_validated_at"],
                last_used_at=row["last_used_at"],
                created_at=row["created_at"],
                rotated_at=row["rotated_at"],
            )
            for row in rows
        ]

    def verify_secret(self, credential_id: int, candidate_secret: str) -> bool:
        row = self.conn.execute(
            "SELECT secret_hash, salt FROM credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        if not row:
            return False
        expected_hash = row["secret_hash"]
        candidate_hash = self._hash_secret(candidate_secret, bytes.fromhex(row["salt"]))
        return hmac.compare_digest(expected_hash, candidate_hash)

    def set_validation_status(
        self,
        *,
        credential_id: int,
        status: str,
        details: Optional[str] = None,
    ) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE credentials SET validation_status = ?, last_validated_at = ? WHERE id = ?",
            (status, now, credential_id),
        )
        row = self.conn.execute(
            "SELECT provider, subscription_tier, key_id FROM credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        if row:
            self._audit(
                event="validated" if status == "valid" else "invalidated",
                provider=row["provider"],
                tier=row["subscription_tier"],
                key_id=row["key_id"],
                credential_id=credential_id,
                details=details,
            )
        self.conn.commit()

    def mark_used(self, credential_id: int):
        now = self._now()
        self.conn.execute(
            "UPDATE credentials SET last_used_at = ? WHERE id = ?",
            (now, credential_id),
        )
        self.conn.commit()

    def invalidate(self, credential_id: int, reason: str = "compromised"):
        row = self.conn.execute(
            "SELECT provider, subscription_tier, key_id FROM credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        if row is None:
            return
        self.conn.execute(
            "UPDATE credentials SET is_active = 0, validation_status = 'invalid', rotated_at = ? WHERE id = ?",
            (self._now(), credential_id),
        )
        self._audit(
            event="invalidated",
            provider=row["provider"],
            tier=row["subscription_tier"],
            key_id=row["key_id"],
            credential_id=credential_id,
            details=reason,
        )
        self.conn.commit()

    def audit_log(
        self,
        *,
        provider: Optional[Provider | str] = None,
        subscription_tier: Optional[SubscriptionTier | str] = None,
        limit: int = 100,
    ) -> list[dict]:
        where = ["1=1"]
        params: list[str] = []
        if provider is not None:
            where.append("provider = ?")
            params.append(Provider(provider).value)
        if subscription_tier is not None:
            parsed_tier = parse_tier(
                subscription_tier.value if isinstance(subscription_tier, SubscriptionTier) else subscription_tier
            )
            where.append("subscription_tier = ?")
            params.append(parsed_tier.value)
        params.append(str(limit))

        rows = self.conn.execute(
            "SELECT id, credential_id, provider, subscription_tier, key_id, event, details, created_at "
            "FROM credential_audit_log WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        self.conn.close()
