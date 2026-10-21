"""Persistence boundary and SQLite implementation for domain objects."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Optional, Protocol

from models import Ad, AdAccount, AdDraft


class AdsRepository(Protocol):
    def save_account(self, account: AdAccount) -> None: ...
    def get_account(self, account_id: int) -> Optional[AdAccount]: ...
    def save_ad(self, draft: AdDraft, creation_time: int) -> Ad: ...
    def get_ad(self, ad_id: int) -> Optional[Ad]: ...


class SQLiteAdsRepository:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ads (id INTEGER PRIMARY KEY, system_name TEXT, "
                "image_hash TEXT, text_content TEXT, creator_id INTEGER, creation_time INTEGER)"
            )
        except Exception:
            conn.close()
            raise
        return conn

    def save_account(self, account):
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO accounts (id, name) VALUES (?, ?)",
                         (account.id, account.name))

    def get_account(self, account_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return AdAccount(*row) if row is not None else None

    def save_ad(self, draft, creation_time):
        with closing(self._connect()) as conn, conn:
            # Reserve the next ID and insert in the same write transaction.
            conn.execute("BEGIN IMMEDIATE")
            ad_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM ads").fetchone()[0]
            ad = Ad(ad_id, f"ad_{ad_id}", draft.image_hash, draft.text,
                    draft.creator_id, creation_time)
            conn.execute(
                "INSERT INTO ads (id, system_name, image_hash, text_content, creator_id, "
                "creation_time) VALUES (?, ?, ?, ?, ?, ?)",
                (ad.id, ad.system_name, ad.image_hash, ad.text, ad.creator_id, ad.creation_time),
            )
        return ad

    def get_ad(self, ad_id):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, system_name, image_hash, text_content, creator_id, creation_time "
                "FROM ads WHERE id = ?", (ad_id,),
            ).fetchone()
        return Ad(*row) if row is not None else None
