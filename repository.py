"""Persistence boundary and SQLite implementation for domain objects."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Optional, Protocol

from models import Ad, AdAccount, AdDraft, Video


class AdsRepository(Protocol):
    def save_account(self, account: AdAccount) -> None: ...
    def get_account(self, account_id: int) -> Optional[AdAccount]: ...
    def save_ad(self, draft: AdDraft, creation_time: int) -> Ad: ...
    def get_ad(self, ad_id: int) -> Optional[Ad]: ...
    def save_video(self, owner_id: int, name: str, video_hash: str, upload_time: int) -> Video: ...
    def get_video(self, video_id: int) -> Optional[Video]: ...
    def rename_video(self, video_id: int, name: str) -> Optional[Video]: ...
    def list_videos(self, owner_id: int, limit: int, after: Optional[tuple] = None,
                    since: Optional[int] = None, until: Optional[int] = None) -> list: ...


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
                "image_hash TEXT, text_content TEXT, creator_id INTEGER, creation_time INTEGER, "
                "video_id INTEGER)"
            )
            self._add_ads_video_column(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY, name TEXT, "
                "owner_id INTEGER, upload_time INTEGER, video_hash TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS videos_by_owner_time "
                         "ON videos (owner_id, upload_time)")
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _add_ads_video_column(conn):
        # Databases created before video ads lack the column; add it in place.
        if "video_id" in {row[1] for row in conn.execute("PRAGMA table_info(ads)")}:
            return
        try:
            conn.execute("ALTER TABLE ads ADD COLUMN video_id INTEGER")
        except sqlite3.OperationalError as exc:
            # Another connection added it first.
            if "duplicate column" not in str(exc):
                raise

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
                    draft.creator_id, creation_time, draft.video_id)
            conn.execute(
                "INSERT INTO ads (id, system_name, image_hash, text_content, creator_id, "
                "creation_time, video_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ad.id, ad.system_name, ad.image_hash, ad.text, ad.creator_id,
                 ad.creation_time, ad.video_id),
            )
        return ad

    def get_ad(self, ad_id):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, system_name, image_hash, text_content, creator_id, creation_time, "
                "video_id FROM ads WHERE id = ?", (ad_id,),
            ).fetchone()
        return Ad(*row) if row is not None else None

    def save_video(self, owner_id, name, video_hash, upload_time):
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO videos (name, owner_id, upload_time, video_hash) VALUES (?, ?, ?, ?)",
                (name, owner_id, upload_time, video_hash),
            )
        return Video(cursor.lastrowid, name, owner_id, upload_time, video_hash)

    def get_video(self, video_id):
        with closing(self._connect()) as conn:
            return self._select_video(conn, video_id)

    def rename_video(self, video_id, name):
        # Name is the only column ever updated; the rest is fixed at upload.
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE videos SET name = ? WHERE id = ?", (name, video_id))
            return self._select_video(conn, video_id)

    def list_videos(self, owner_id, limit, after=None, since=None, until=None):
        """Newest first; `after` is the (upload_time, id) of the previous page's last video."""
        clauses, params = ["owner_id = ?"], [owner_id]
        if after is not None:
            clauses.append("(upload_time, id) < (?, ?)")
            params.extend(after)
        if since is not None:
            clauses.append("upload_time >= ?")
            params.append(since)
        if until is not None:
            clauses.append("upload_time < ?")
            params.append(until)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, owner_id, upload_time, video_hash FROM videos "
                f"WHERE {' AND '.join(clauses)} ORDER BY upload_time DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [Video(*row) for row in rows]

    def _select_video(self, conn, video_id):
        row = conn.execute(
            "SELECT id, name, owner_id, upload_time, video_hash FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()
        return Video(*row) if row is not None else None
