"""Persistence boundary and SQLite implementation for domain objects."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Optional, Protocol

from models import Ad, AdAccount, AdDraft, Image, Video


class AdsRepository(Protocol):
    def insert_account(self, account: AdAccount, token_hash: str) -> bool: ...
    def rename_account(self, account_id: int, name: str) -> None: ...
    def get_account(self, account_id: int) -> Optional[AdAccount]: ...
    def get_token_hash(self, account_id: int) -> Optional[str]: ...
    def set_token_hash(self, account_id: int, token_hash: str) -> bool: ...
    def grant_image(self, owner_id: int, image_hash: str, creation_time: int,
                    image_name: Optional[str] = None) -> bool: ...
    def owns_image(self, owner_id: int, image_hash: str) -> bool: ...
    def get_image_hash_by_name(self, owner_id: int, image_name: str) -> Optional[str]: ...
    def list_images(self, owner_id: int, limit: int, after: Optional[tuple] = None,
                    since: Optional[int] = None, until: Optional[int] = None) -> list: ...
    def save_ad(self, draft: AdDraft, creation_time: int) -> Ad: ...
    def get_ad(self, ad_id: int) -> Optional[Ad]: ...
    def list_ads(self, creator_id: int, limit: int, after: Optional[tuple] = None,
                 since: Optional[int] = None, until: Optional[int] = None) -> list: ...
    def save_video(self, owner_id: int, name: str, video_hash: str, upload_time: int) -> Video: ...
    def get_video(self, video_id: int) -> Optional[Video]: ...
    def rename_video(self, video_id: int, name: str) -> Optional[Video]: ...
    def list_videos(self, owner_id: int, limit: int, after: Optional[tuple] = None,
                    since: Optional[int] = None, until: Optional[int] = None) -> list: ...


SCHEMA_VERSION = 2


class SQLiteAdsRepository:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _migrate(self):
        # publish builds a repository per call, so an up-to-date database is checked
        # without a lock; only a database behind SCHEMA_VERSION takes the write lock.
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have migrated while this one waited for the lock.
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version < 1:
                    self._create_v1(conn)
                if version < 2:
                    self._create_v2(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                # SQLite may already have rolled back (e.g. disk full); keep the original error.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _create_v1(self, conn):
        # Unversioned databases may predate tokens, videos, or image ownership.
        conn.execute("CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, name TEXT, "
                     "token_hash TEXT)")
        self._add_column(conn, "accounts", "token_hash TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ads (id INTEGER PRIMARY KEY, system_name TEXT, "
            "image_hash TEXT, text_content TEXT, creator_id INTEGER, creation_time INTEGER, "
            "video_id INTEGER)"
        )
        self._add_column(conn, "ads", "video_id INTEGER")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY, name TEXT, "
            "owner_id INTEGER, upload_time INTEGER, video_hash TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS videos_by_owner_time "
                     "ON videos (owner_id, upload_time)")
        # Identical image bytes share one hash; each account that ingested them owns it.
        conn.execute("CREATE TABLE IF NOT EXISTS image_owners (owner_id INTEGER, "
                     "image_hash TEXT, creation_time INTEGER, image_name TEXT, "
                     "PRIMARY KEY (owner_id, image_hash))")
        # Earlier builds lacked these columns; unknown times become 0 so cursors stay valid.
        self._add_column(conn, "image_owners", "creation_time INTEGER")
        self._add_column(conn, "image_owners", "image_name TEXT")
        conn.execute("UPDATE image_owners SET creation_time = 0 WHERE creation_time IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS image_owners_by_owner_time "
                     "ON image_owners (owner_id, creation_time)")
        # Names identify images within an account; unnamed images (NULL) are exempt.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS image_owners_by_name "
                     "ON image_owners (owner_id, image_name)")

    def _create_v2(self, conn):
        # list_ads filters by creator and pages by (creation_time, id).
        conn.execute("CREATE INDEX IF NOT EXISTS ads_by_creator_time "
                     "ON ads (creator_id, creation_time)")

    @staticmethod
    def _add_column(conn, table, column):
        # Databases created before a feature lack its column; add it in place.
        if column.split()[0] in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except sqlite3.OperationalError as exc:
            # An unversioned process added it first.
            if "duplicate column" not in str(exc):
                raise

    def insert_account(self, account, token_hash):
        """Return False, changing nothing, if the account ID is taken."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO accounts (id, name, token_hash) VALUES (?, ?, ?)",
                (account.id, account.name, token_hash),
            )
        return cursor.rowcount == 1

    def rename_account(self, account_id, name):
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))

    def get_account(self, account_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return AdAccount(*row) if row is not None else None

    def get_token_hash(self, account_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT token_hash FROM accounts WHERE id = ?",
                               (account_id,)).fetchone()
        return row[0] if row is not None else None

    def set_token_hash(self, account_id, token_hash):
        """Return False if the account does not exist."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("UPDATE accounts SET token_hash = ? WHERE id = ?",
                                  (token_hash, account_id))
        return cursor.rowcount == 1

    def grant_image(self, owner_id, image_hash, creation_time, image_name=None):
        """Return False, changing nothing, if another of the owner's images has the name."""
        # Re-ingesting an owned image keeps its creation time; a new name replaces the old.
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO image_owners (owner_id, image_hash, creation_time, image_name) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (owner_id, image_hash) DO UPDATE SET "
                    "image_name = COALESCE(excluded.image_name, image_name)",
                    (owner_id, image_hash, creation_time, image_name),
                )
        except sqlite3.IntegrityError as exc:
            if "image_name" not in str(exc):
                raise
            return False
        return True

    def get_image_hash_by_name(self, owner_id, image_name):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT image_hash FROM image_owners "
                               "WHERE owner_id = ? AND image_name = ?",
                               (owner_id, image_name)).fetchone()
        return row[0] if row is not None else None

    def owns_image(self, owner_id, image_hash):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM image_owners WHERE owner_id = ? AND image_hash = ?",
                               (owner_id, image_hash)).fetchone()
        return row is not None

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

    def list_ads(self, creator_id, limit, after=None, since=None, until=None):
        """Newest first; `after` is the (creation_time, id) of the previous page's last ad."""
        rows = self._list_page("SELECT id, system_name, image_hash, text_content, creator_id, "
                               "creation_time, video_id FROM ads",
                               creator_id, limit, after, since, until, owner_column="creator_id",
                               time_column="creation_time", key_column="id")
        return [Ad(*row) for row in rows]

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
        rows = self._list_page("SELECT id, name, owner_id, upload_time, video_hash FROM videos",
                               owner_id, limit, after, since, until, owner_column="owner_id",
                               time_column="upload_time", key_column="id")
        return [Video(*row) for row in rows]

    def list_images(self, owner_id, limit, after=None, since=None, until=None):
        """Newest first; `after` is the (creation_time, hash) of the previous page's last image."""
        rows = self._list_page("SELECT image_hash, owner_id, creation_time, image_name "
                               "FROM image_owners",
                               owner_id, limit, after, since, until, owner_column="owner_id",
                               time_column="creation_time", key_column="image_hash")
        return [Image(*row) for row in rows]

    def _list_page(self, select, owner_id, limit, after, since, until, *, owner_column,
                   time_column, key_column):
        # Keyset pagination: rows added after the first page never shift later pages.
        clauses, params = [f"{owner_column} = ?"], [owner_id]
        if after is not None:
            clauses.append(f"({time_column}, {key_column}) < (?, ?)")
            params.extend(after)
        if since is not None:
            clauses.append(f"{time_column} >= ?")
            params.append(since)
        if until is not None:
            clauses.append(f"{time_column} < ?")
            params.append(until)
        with closing(self._connect()) as conn:
            return conn.execute(
                f"{select} WHERE {' AND '.join(clauses)} "
                f"ORDER BY {time_column} DESC, {key_column} DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def _select_video(self, conn, video_id):
        row = conn.execute(
            "SELECT id, name, owner_id, upload_time, video_hash FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()
        return Video(*row) if row is not None else None
