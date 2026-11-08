from __future__ import annotations

from contextlib import contextmanager
import base64
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time

from .calendar_ui import MAX_SCHEDULE_SECONDS
from .domain import Product, validate_caption
from .editing import title_from_caption


class Stale(ValueError):
    def __init__(self, text="Esta vista previa o este control ya no está vigente. Envía el enlace de nuevo o usa /previa si el borrador sigue vigente."):
        super().__init__(text)


@contextmanager
def instance_lock(directory: Path):
    if not directory.is_dir():
        raise ValueError("DATA_DIR no existe: revisa el volumen o crea expresamente la carpeta bind.")
    try:
        probe = directory / (".write-" + secrets.token_hex(6))
        with probe.open("xb"):
            pass
        probe.unlink()
        lock = (directory / ".instance.lock").open("a")
    except OSError:
        raise ValueError("No se puede escribir en DATA_DIR. Comprueba el montaje y el UID:GID del servicio; no se cambiarán permisos automáticamente.") from None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Ya hay una instancia usando estos datos. Detén la otra instancia.") from None
        yield
    finally:
        lock.close()


def encode_state(value):
    if isinstance(value, bytes):
        return {'_bytes': base64.b64encode(value).decode('ascii')}
    raise TypeError('Unsupported editor state')


def decode_state(value):
    if set(value) == {'_bytes'}:
        return base64.b64decode(value['_bytes'])
    return value


def token() -> str:
    return secrets.token_hex(8)


class Store:
    def __init__(self, path: Path | str, ttl: int = 86400, now=time.time):
        self.path, self.ttl, self.now = Path(path), ttl, now
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                raise ValueError("La base de datos pertenece a una versión más reciente; utiliza su imagen correspondiente.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (
                    owner INTEGER PRIMARY KEY, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created REAL NOT NULL, data TEXT NOT NULL, photo BLOB, preview_id TEXT, aux TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    expires REAL NOT NULL, destination TEXT NOT NULL, caption TEXT NOT NULL, entities TEXT NOT NULL,
                    photo BLOB NOT NULL, photo_id TEXT, message_id INTEGER, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS editors (
                    owner INTEGER PRIMARY KEY, preview_id TEXT NOT NULL, prompt INTEGER NOT NULL, aux TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pickers (
                    owner INTEGER PRIMARY KEY, preview_id TEXT NOT NULL, token TEXT NOT NULL,
                    message_id INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, preview_id TEXT NOT NULL UNIQUE,
                    destination TEXT NOT NULL, caption TEXT NOT NULL, entities TEXT NOT NULL,
                    photo BLOB NOT NULL, photo_id TEXT, title TEXT NOT NULL, due REAL NOT NULL,
                    created REAL NOT NULL,
                    scheduled INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    not_before REAL NOT NULL DEFAULT 0, finished REAL, sent_message INTEGER, notice TEXT);
                CREATE INDEX IF NOT EXISTS posts_due ON posts(owner, status, due, not_before);
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, state TEXT NOT NULL,
                    recorded REAL NOT NULL, sent_message INTEGER);
                CREATE TABLE IF NOT EXISTS post_products (
                    post_id TEXT PRIMARY KEY, product_key TEXT NOT NULL, sent_at REAL);
                CREATE INDEX IF NOT EXISTS post_products_key ON post_products(product_key, sent_at);
            """)
            if "created" not in {row[1] for row in db.execute("PRAGMA table_info(posts)")}:
                db.execute("ALTER TABLE posts ADD COLUMN created REAL NOT NULL DEFAULT 0")
                # Completa la fecha de aprobación de las publicaciones pendientes.
                db.execute("UPDATE posts SET created=? WHERE status='pending'", (self.now(),))
                db.execute("UPDATE posts SET created=COALESCE(finished,due) WHERE status!='pending'")
            columns = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
            if "revision" not in columns:
                db.execute("ALTER TABLE posts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            if "original_url" not in columns:
                db.execute("ALTER TABLE posts ADD COLUMN original_url TEXT NOT NULL DEFAULT ''")
                for row in db.execute("SELECT id,caption FROM posts").fetchall():
                    match = re.search(r"https://[^\s<>]+", row["caption"], re.I)
                    if match:
                        db.execute("UPDATE posts SET original_url=? WHERE id=?", (match.group(), row["id"]))
            db.execute("""CREATE TABLE IF NOT EXISTS scheduled_views (
                token TEXT PRIMARY KEY, actor INTEGER NOT NULL, post_id TEXT NOT NULL,
                revision INTEGER NOT NULL, message_id INTEGER, kind TEXT NOT NULL,
                state TEXT NOT NULL, expires REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS scheduled_views_actor ON scheduled_views(actor,kind)")
            if "scheduled_due" not in columns:
                db.execute("ALTER TABLE posts ADD COLUMN scheduled_due REAL")
            if version < 5:
                db.execute("UPDATE posts SET scheduled_due=due WHERE scheduled=1 AND scheduled_due IS NULL")
            card_columns = {r[1] for r in db.execute("PRAGMA table_info(publication_cards)")}
            if card_columns and "terminal_state" not in card_columns:
                db.execute("ALTER TABLE publication_cards RENAME TO publication_cards_v4")
            db.execute("""CREATE TABLE IF NOT EXISTS publication_cards (
                post_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                media INTEGER NOT NULL DEFAULT 1, terminal_state TEXT, generation INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(post_id,chat_id,message_id))""")
            if card_columns and "terminal_state" not in card_columns:
                db.execute("INSERT OR IGNORE INTO publication_cards(post_id,chat_id,message_id) "
                           "SELECT post_id,chat_id,message_id FROM publication_cards_v4")
                db.execute("DROP TABLE publication_cards_v4")
            db.execute("""CREATE TABLE IF NOT EXISTS repeat_warnings (
                owner INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(owner,message_id))""")
            if version < 5:
                # Recover every known card, including legacy text cards, before views expire.
                for row in db.execute("SELECT * FROM scheduled_views WHERE kind!='editor' "
                                      "AND message_id IS NOT NULL ORDER BY rowid").fetchall():
                    state = json.loads(row["state"])
                    media = bool(state.get("media", row["kind"] in ("preview", "review")))
                    db.execute("INSERT OR IGNORE INTO publication_cards(post_id,chat_id,message_id,media) VALUES (?,?,?,?)",
                               (row["post_id"], row["actor"], row["message_id"], int(media)))
            for table in ('previews', 'posts'):
                existing = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
                for name, sql in [('template', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('source_photo', "BLOB NOT NULL DEFAULT x''"),
                                  ('product_data', "TEXT NOT NULL DEFAULT '{}'")]:
                    if name not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {sql}')
            if 'state' not in {r[1] for r in db.execute('PRAGMA table_info(editors)')}:
                db.execute("ALTER TABLE editors ADD COLUMN state TEXT NOT NULL DEFAULT '{}'")
            db.execute("CREATE TABLE IF NOT EXISTS preparations (owner INTEGER PRIMARY KEY, message_id INTEGER, aux TEXT NOT NULL DEFAULT '[]')")
            db.execute('CREATE TABLE IF NOT EXISTS ui_messages (owner INTEGER NOT NULL, message_id INTEGER NOT NULL, expires REAL NOT NULL, PRIMARY KEY(owner,message_id))')
            db.execute("CREATE TABLE IF NOT EXISTS private_screens (owner INTEGER NOT NULL, section TEXT NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(owner,message_id))")
            db.execute("CREATE INDEX IF NOT EXISTS private_screens_section ON private_screens(owner,section)")
            db.execute("CREATE TABLE IF NOT EXISTS operation_notices (token TEXT PRIMARY KEY,owner INTEGER NOT NULL,scope TEXT NOT NULL,issue TEXT NOT NULL,recover TEXT NOT NULL,kind TEXT NOT NULL,message_id INTEGER,input_id INTEGER)")
            db.execute("CREATE INDEX IF NOT EXISTS operation_notices_scope ON operation_notices(owner,scope,issue)")
            db.execute("CREATE TABLE IF NOT EXISTS notice_cleanup (owner INTEGER NOT NULL,message_id INTEGER NOT NULL,not_before REAL NOT NULL DEFAULT 0,PRIMARY KEY(owner,message_id))")
            if 'not_before' not in {r[1] for r in db.execute('PRAGMA table_info(notice_cleanup)')}:
                db.execute('ALTER TABLE notice_cleanup ADD COLUMN not_before REAL NOT NULL DEFAULT 0')
            db.execute("INSERT OR IGNORE INTO private_screens(owner,section,message_id) "
                       "SELECT p.owner,'draft-preview',p.message_id FROM previews p JOIN drafts d ON d.owner=p.owner AND d.id=p.draft_id "
                       "WHERE p.message_id IS NOT NULL AND p.status IN ('active','editing') "
                       "AND NOT EXISTS (SELECT 1 FROM publication_cards c WHERE c.chat_id=p.owner AND c.message_id=p.message_id)")
            if version < 6:
                for view in db.execute("SELECT * FROM scheduled_views WHERE kind='editor'").fetchall():
                    if view['message_id'] is not None:
                        db.execute('INSERT OR IGNORE INTO ui_messages VALUES (?,?,?)', (view['actor'], view['message_id'], self.now()))
                db.execute("DELETE FROM scheduled_views WHERE kind='editor'")
            # Recover already-visible draft messages so expiry also works after upgrading.
            for draft in db.execute('SELECT * FROM drafts').fetchall():
                expiry = draft['created'] + self.ttl
                ids = json.loads(draft['aux'])
                db.execute('UPDATE previews SET expires=? WHERE owner=? AND draft_id=?', (expiry, draft['owner'], draft['id']))
                ids += [r[0] for r in db.execute('SELECT message_id FROM previews WHERE owner=? AND draft_id=? AND message_id IS NOT NULL', (draft['owner'], draft['id']))]
                ids += [r[0] for r in db.execute('SELECT prompt FROM editors WHERE owner=?', (draft['owner'],))]
                ids += [r[0] for r in db.execute('SELECT message_id FROM repeat_warnings WHERE owner=?', (draft['owner'],))]
                for message_id in ids:
                    # Reapply the configured TTL from creation, never from restart or editing.
                    db.execute('INSERT INTO ui_messages VALUES (?,?,?) ON CONFLICT(owner,message_id) DO UPDATE SET expires=excluded.expires', (draft['owner'], message_id, expiry))
            db.execute("PRAGMA user_version=6")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def row(record):
        if record is None:
            return None
        value = dict(record)
        for key in ("data", "entities", "state", "aux", "product_data"):
            if key in value:
                value[key] = json.loads(value[key], object_hook=decode_state)
        return value

    def offset(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            return int(row[0]) if row else 0

    def acknowledge(self, update_id: int):
        with self.connect() as db:
            db.execute("INSERT INTO meta VALUES ('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(update_id + 1),))

    def draft(self, owner: int, live=True):
        with self.connect() as db:
            row = self.row(db.execute("SELECT * FROM drafts WHERE owner=?", (owner,)).fetchone())
        if not row:
            raise Stale("No hay borrador activo. Envía un enlace de Amazon.es.")
        if live and row["created"] + self.ttl <= self.now():
            raise Stale("La oferta ha caducado. Envía de nuevo el enlace y revisa sus precios.")
        return row

    def replace_draft(self, owner, product: Product, input_message: int):
        with self.connect(True) as db:
            self._clear_controls(db, owner)
            db.execute("DELETE FROM drafts WHERE owner=?", (owner,))
            db.execute("INSERT INTO drafts VALUES (?,?,1,?,?,NULL,NULL,?)",
                       (owner, token(), self.now(), json.dumps(product.to_dict()), json.dumps([input_message])))

    @staticmethod
    def _clear_controls(db, owner):
        for table in ("previews", "editors", "pickers", "repeat_warnings"):
            db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))

    def update_draft(self, owner, product: Product, photo: bytes | None = None):
        self.draft(owner)
        with self.connect(True) as db:
            self._clear_controls(db, owner)
            db.execute("UPDATE drafts SET revision=revision+1,data=?,photo=COALESCE(?,photo),preview_id=NULL WHERE owner=?",
                       (json.dumps(product.to_dict()), photo, owner))

    def add_aux(self, owner, message_id):
        draft = self.draft(owner, live=False)
        aux = list(dict.fromkeys([*draft["aux"], message_id]))
        with self.connect() as db:
            db.execute("UPDATE drafts SET aux=? WHERE owner=?", (json.dumps(aux), owner))

    def clear_aux(self, owner):
        with self.connect() as db:
            db.execute("UPDATE drafts SET aux='[]' WHERE owner=?", (owner,))

    def add_repeat_warning(self, owner, message_id):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO repeat_warnings VALUES (?,?)", (owner, message_id))

    def repeat_warning_messages(self, owner):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT message_id FROM repeat_warnings WHERE owner=?", (owner,))]

    def forget_repeat_warning(self, owner, message_id):
        with self.connect() as db:
            db.execute("DELETE FROM repeat_warnings WHERE owner=? AND message_id=?", (owner, message_id))


    def terminal_cards(self, post_id, state):
        with self.connect() as db:
            return [self.row(row) for row in db.execute(
                "SELECT * FROM publication_cards WHERE post_id=? AND COALESCE(terminal_state,'')<>?",
                (post_id, state))]

    def card_updated(self, post_id, chat_id, message_id, state, generation):
        with self.connect() as db:
            return bool(db.execute("UPDATE publication_cards SET terminal_state=? WHERE post_id=? AND chat_id=? "
                                   "AND message_id=? AND generation=?",
                                   (state, post_id, chat_id, message_id, generation)).rowcount)

    def forget_publication_card(self, chat_id, message_id):
        with self.connect() as db:
            db.execute("DELETE FROM publication_cards WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    def stage_preview(self, owner, destination, photo: bytes, caption: str, entities: list, *, template=False, source_photo=b"", product_data=None):
        draft = self.draft(owner)
        preview_id = token()
        with self.connect() as db:
            db.execute("INSERT INTO previews(id,owner,draft_id,revision,expires,destination,caption,entities,photo,photo_id,message_id,status) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,'staged')",
                       (preview_id, owner, draft["id"], draft["revision"], draft["created"] + self.ttl,
                        destination, caption, json.dumps(entities), photo or b''))
            db.execute('UPDATE previews SET template=?,source_photo=?,product_data=? WHERE id=?',
                       (int(template), source_photo or draft['photo'] or b'', json.dumps(product_data or draft['data']), preview_id))
        return preview_id

    def activate(self, owner, preview_id, message_id, photo_id):
        with self.connect(True) as db:
            row = db.execute("SELECT p.id FROM previews p JOIN drafts d ON d.id=p.draft_id AND d.revision=p.revision "
                             "WHERE p.id=? AND p.owner=? AND p.status='staged' AND p.expires>?",
                             (preview_id, owner, self.now())).fetchone()
            if not row:
                raise Stale()
            db.execute("DELETE FROM previews WHERE owner=? AND id<>?", (owner, preview_id))
            db.execute("UPDATE previews SET message_id=?,photo_id=?,status='active' WHERE id=?", (message_id, photo_id, preview_id))
            db.execute("UPDATE drafts SET preview_id=? WHERE owner=?", (preview_id, owner))
            db.execute("INSERT INTO private_screens VALUES (?,'draft-preview',?) "
                       "ON CONFLICT(owner,message_id) DO UPDATE SET section=excluded.section", (owner, message_id))
            db.execute('INSERT OR REPLACE INTO ui_messages VALUES (?,?,(SELECT created+? FROM drafts WHERE owner=?))', (owner, message_id, self.ttl, owner))
            db.execute("DELETE FROM editors WHERE owner=?", (owner,))
            db.execute("DELETE FROM pickers WHERE owner=?", (owner,))

    def _preview(self, db, owner, preview_id=None, message_id=None, editing=False):
        row = self.row(db.execute("SELECT p.* FROM previews p JOIN drafts d ON d.preview_id=p.id "
                                  "AND d.id=p.draft_id AND d.revision=p.revision WHERE p.owner=?", (owner,)).fetchone())
        if (not row or row["status"] != ("editing" if editing else "active") or row["expires"] <= self.now()
                or (preview_id is not None and row["id"] != preview_id)
                or (message_id is not None and row["message_id"] != message_id)):
            raise Stale()
        return row

    def preview(self, owner, preview_id=None, message_id=None, editing=False):
        with self.connect() as db:
            return self._preview(db, owner, preview_id, message_id, editing)

    def editor(self, owner):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM editors WHERE owner=?", (owner,)).fetchone())

    def begin_edit(self, owner, old_id, source_message, edit_id, prompt, aux):
        with self.connect(True) as db:
            self._preview(db, owner, old_id, source_message)
            db.execute("INSERT INTO previews SELECT ?,owner,draft_id,revision,expires,destination,caption,entities,photo,photo_id,message_id,'editing',template,source_photo,product_data FROM previews WHERE id=?",
                       (edit_id, old_id))
            db.execute("DELETE FROM previews WHERE owner=? AND id<>?", (owner, edit_id))
            db.execute("UPDATE drafts SET preview_id=? WHERE owner=?", (edit_id, owner))
            db.execute("DELETE FROM pickers WHERE owner=?", (owner,))
            db.execute("INSERT OR REPLACE INTO editors(owner,preview_id,prompt,aux) VALUES (?,?,?,?)", (owner, edit_id, prompt, json.dumps(aux)))

    def cancel_edit(self, owner, edit_id, prompt):
        with self.connect(True) as db:
            preview = self._preview(db, owner, edit_id, editing=True)
            editor = db.execute("SELECT prompt FROM editors WHERE owner=? AND preview_id=?", (owner, edit_id)).fetchone()
            if not editor or editor[0] != prompt:
                raise Stale()
            db.execute("UPDATE previews SET status='active' WHERE id=?", (edit_id,))
            db.execute("DELETE FROM editors WHERE owner=?", (owner,))
            return preview

    def discard(self, owner, preview_id=None, message_id=None):
        with self.connect(True) as db:
            if preview_id is not None:
                self._preview(db, owner, preview_id, message_id)
            self._clear_controls(db, owner)
            db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def set_picker(self, owner, preview_id, message_id, state, previous_token=None):
        fresh = token()
        with self.connect(True) as db:
            self._preview(db, owner, preview_id)
            if previous_token is not None:
                old = db.execute("SELECT token,message_id FROM pickers WHERE owner=?", (owner,)).fetchone()
                if not old or old[0] != previous_token or old[1] != message_id:
                    raise Stale()
            db.execute("INSERT OR REPLACE INTO pickers VALUES (?,?,?,?,?)", (owner, preview_id, fresh, message_id, json.dumps(state, default=encode_state)))
        return fresh

    def picker(self, owner, picker_token, message_id):
        with self.connect() as db:
            row = self.row(db.execute("SELECT * FROM pickers WHERE owner=? AND token=? AND message_id=?",
                                      (owner, picker_token, message_id)).fetchone())
            if not row:
                raise Stale("Este selector ha sido sustituido. Abre Programar en la vista previa vigente.")
            self._preview(db, owner, row["preview_id"])
            return row

    def current_picker(self, owner):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM pickers WHERE owner=?", (owner,)).fetchone())

    def close_picker(self, owner, picker_token, message_id, *, close_preview=False):
        with self.connect(True) as db:
            picker = db.execute("SELECT preview_id FROM pickers WHERE owner=? AND token=? AND message_id=?",
                                (owner, picker_token, message_id)).fetchone()
            if not picker:
                raise Stale()
            db.execute("DELETE FROM pickers WHERE owner=? AND token=?", (owner, picker_token))
            if close_preview:
                db.execute("UPDATE previews SET status='closed' WHERE id=? AND owner=?", (picker["preview_id"], owner))

    def consume(self, owner, preview_id, source_message, destination, *, due=None, picker_token=None):
        post_id = token()[:10]
        with self.connect(True) as db:
            now = self.now()
            if due is not None and due <= now:
                raise ValueError("La hora elegida ya ha pasado; elige otra fecha futura.")
            if due is not None and due > now + MAX_SCHEDULE_SECONDS:
                raise ValueError("Solo se puede programar durante los próximos 30 días.")
            preview = self._preview(db, owner, preview_id, source_message)
            if preview["destination"] != destination:
                raise Stale("El destino configurado ha cambiado. Genera una nueva vista previa y revísalo antes de aprobar.")
            if picker_token:
                picker = self.row(db.execute("SELECT * FROM pickers WHERE owner=? AND token=?", (owner, picker_token)).fetchone())
                if (not picker or picker["preview_id"] != preview_id or picker["state"].get("screen") != "review"
                        or picker["state"].get("due") != due or picker["state"].get("destination") != destination):
                    raise Stale()
            draft = db.execute("SELECT data FROM drafts WHERE owner=?", (owner,)).fetchone()
            product = json.loads(draft[0])
            title = preview["product_data"].get("title") or title_from_caption(preview["caption"])
            db.execute("INSERT INTO posts (id,owner,preview_id,destination,caption,entities,photo,photo_id,title,due,created,scheduled,status) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                       (post_id, owner, preview_id, destination, preview["caption"], json.dumps(preview["entities"]),
                        preview["photo"], preview["photo_id"], title, now if due is None else due, now, int(due is not None)))
            validate_caption(preview['caption'], preview['entities'], has_photo=bool(preview['photo'] or preview['photo_id']))
            db.execute("UPDATE posts SET original_url=?,scheduled_due=?,template=?,source_photo=?,product_data=? WHERE id=?",
                       (product['original_url'], due, preview['template'], preview['source_photo'], json.dumps(preview['product_data']), post_id))
            db.execute("INSERT INTO publication_cards(post_id,chat_id,message_id,media) VALUES (?,?,?,?)",
                       (post_id, owner, preview["message_id"], int(bool(preview["photo"] or preview["photo_id"]))))
            db.execute("DELETE FROM ui_messages WHERE owner=? AND message_id=?", (owner, preview["message_id"]))
            # A draft card becomes a final/publication card: future draft cleanup must leave it alone.
            db.execute("DELETE FROM private_screens WHERE owner=? AND message_id=?", (owner, preview["message_id"]))
            product_key = self.product_key(product)
            db.execute("INSERT INTO post_products(post_id,product_key) VALUES (?,?)", (post_id, product_key))
            self._clear_controls(db, owner)
            db.execute("DELETE FROM drafts WHERE owner=?", (owner,))
        return post_id


    def pending_all(self, owners, limit=None):
        owners = tuple(owners)
        if not owners:
            return []
        placeholders = ",".join("?" for _ in owners)
        with self.connect() as db:
            return [self.row(row) for row in db.execute(
                f"SELECT * FROM posts WHERE owner IN ({placeholders}) AND status='pending' AND scheduled=1 ORDER BY due LIMIT ?",
                (*owners, -1 if limit is None else limit))]

    def get_post(self, post_id):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())

    @staticmethod
    def product_key(product):
        return "asin:" + product["asin"].upper() if product.get("asin") else "url:" + product["original_url"]

    def recent_product(self, product, days):
        if days <= 0:
            return None
        key = self.product_key(product)
        with self.connect() as db:
            pending = db.execute("SELECT 1 FROM post_products pp JOIN posts p ON p.id=pp.post_id "
                                 "WHERE pp.product_key=? AND p.status='pending' LIMIT 1", (key,)).fetchone()
            if pending:
                return "pendiente"
            sent = db.execute("SELECT 1 FROM post_products WHERE product_key=? AND sent_at>=? LIMIT 1",
                              (key, self.now() - days * 86400)).fetchone()
            return "publicado" if sent else None

    def next_due(self, owner):
        with self.connect() as db:
            now = self.now()
            row = db.execute("SELECT id FROM posts WHERE owner=? AND status='pending' AND due<=? AND not_before<=? "
                             "AND created>? ORDER BY due LIMIT 1",
                             (owner, now, now, now - MAX_SCHEDULE_SECONDS)).fetchone()
            return row[0] if row else None

    def claim(self, owner, post_id):
        with self.connect(True) as db:
            now = self.now()
            count = db.execute("UPDATE posts SET status='sending',attempts=attempts+1 WHERE owner=? AND id=? "
                               "AND status='pending' AND due<=? AND not_before<=? AND created>?",
                               (owner, post_id, now, now, now - MAX_SCHEDULE_SECONDS)).rowcount
            return self.row(db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()) if count else None

    def retry(self, post_id, delay):
        with self.connect() as db:
            db.execute("UPDATE posts SET status='pending',not_before=? WHERE id=? AND status='sending'", (self.now() + max(1, delay), post_id))

    def _finish(self, db, post_id, status, message_id, notice):
        row = db.execute("SELECT owner,scheduled_due FROM posts WHERE id=?", (post_id,)).fetchone()
        if row:
            if status == "cancelled" and row["scheduled_due"] is not None:
                notice = "scheduled:cancelled"
            db.execute("UPDATE posts SET status=?,finished=?,sent_message=?,notice=? WHERE id=?",
                       (status, self.now(), message_id, notice, post_id))
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)", (post_id, row[0], status, self.now(), message_id))
            if status == "sent":
                db.execute("UPDATE post_products SET sent_at=? WHERE post_id=?", (self.now(), post_id))

    def finish(self, post_id, status, message_id=None, notice=None):
        with self.connect(True) as db:
            row = db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
            if row and row[0] == "sending":
                self._finish(db, post_id, status, message_id, notice)

    def cancel_post(self, owner, post_id, *, scheduled_only=False, allowed_owners=None):
        allowed = (owner,) if allowed_owners is None else tuple(allowed_owners)
        if owner not in allowed:
            return False
        with self.connect(True) as db:
            row = db.execute("SELECT id,owner FROM posts WHERE id=? AND status='pending' AND (scheduled=1 OR ?=0)",
                             (post_id, int(scheduled_only))).fetchone()
            if row and row["owner"] in allowed:
                self._finish(db, post_id, "cancelled", None, None)
                return True
            return False

    def status_counts(self, owners):
        owners = tuple(owners)
        if not owners:
            return 0, 0
        placeholders = ",".join("?" for _ in owners)
        total_keys = tuple(f"sent_count:{owner}" for owner in owners)
        with self.connect() as db:
            pending = db.execute(f"SELECT COUNT(*) FROM posts WHERE owner IN ({placeholders}) AND status='pending' AND scheduled=1", owners).fetchone()[0]
            recent_sent = db.execute(f"SELECT COUNT(*) FROM receipts WHERE owner IN ({placeholders}) AND state='sent'", owners).fetchone()[0]
            archived_sent = db.execute(f"SELECT COALESCE(SUM(CAST(value AS INTEGER)),0) FROM meta WHERE key IN ({placeholders})",
                                       total_keys).fetchone()[0]
            sent = recent_sent + archived_sent
            return pending, sent

    def recover(self, owner):
        with self.connect(True) as db:
            ids = [row[0] for row in db.execute("SELECT id FROM posts WHERE owner=? AND status='sending'", (owner,))]
            for post_id in ids:
                self._finish(db, post_id, "uncertain", None,
                             f"Envío {post_id} interrumpido, con resultado incierto. Revisa el destino antes de repetirlo; no se reintentará automáticamente.")
            return ids

    def notices(self, owner):
        with self.connect() as db:
            return [(row[0], row[1]) for row in db.execute("SELECT id,notice FROM posts WHERE owner=? AND notice IS NOT NULL LIMIT 20", (owner,))]

    def notice_delivered(self, post_id):
        with self.connect() as db:
            db.execute("UPDATE posts SET notice=NULL WHERE id=?", (post_id,))

    def prune(self, owner, history_days, repeat_days=7):
        with self.connect() as db:
            now = self.now()
            db.execute("DELETE FROM scheduled_views WHERE actor=? AND expires<=? AND (message_id IS NOT NULL OR expires<=?)", (owner, now, now - MAX_SCHEDULE_SECONDS))
            cutoff = now - 30 * 86400
            expired = [row[0] for row in db.execute(
                "SELECT id FROM posts WHERE owner=? AND status='pending' AND created<=?", (owner, cutoff))]
            for post_id in expired:
                self._finish(db, post_id, "expired", None,
                             f"La programación {post_id} ha caducado tras 30 días pendiente; no se publicará. Prepara la oferta de nuevo.")
            db.execute("DELETE FROM posts WHERE owner=? AND status NOT IN ('pending','sending') "
                       "AND finished<=? AND (notice IS NULL OR finished<=?) "
                       "AND NOT EXISTS (SELECT 1 FROM scheduled_views v WHERE v.post_id=posts.id "
                       "AND v.message_id IS NULL AND v.expires+2592000>?)",
                       (owner, now - min(history_days, 30) * 86400, cutoff, now))
            db.execute("DELETE FROM publication_cards WHERE NOT EXISTS (SELECT 1 FROM posts WHERE posts.id=publication_cards.post_id)")
            db.execute("DELETE FROM meta WHERE key LIKE 'reprogrammed:%' AND NOT EXISTS "
                       "(SELECT 1 FROM posts WHERE meta.key='reprogrammed:' || posts.id)")
            db.execute("DELETE FROM post_products WHERE sent_at IS NOT NULL AND sent_at<?",
                       (now - min(repeat_days, 30) * 86400,))
            db.execute("DELETE FROM post_products WHERE sent_at IS NULL AND NOT EXISTS "
                       "(SELECT 1 FROM posts WHERE posts.id=post_products.post_id)")
            archived = db.execute("SELECT COUNT(*) FROM receipts WHERE owner=? AND state='sent' AND recorded<=?",
                                  (owner, cutoff)).fetchone()[0]
            if archived:
                db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=CAST(meta.value AS INTEGER)+excluded.value",
                           (f"sent_count:{owner}", str(archived)))
            db.execute("DELETE FROM receipts WHERE owner=? AND recorded<=?", (owner, cutoff))
            # An expired selector must stay unusable, including after a restart.
            expired = db.execute("SELECT id FROM previews WHERE owner=? AND expires<=?", (owner, now)).fetchall()
            expired_draft = db.execute("SELECT 1 FROM drafts WHERE owner=? AND created+?<=?",
                                       (owner, self.ttl, now)).fetchone()
            if expired or expired_draft:
                self._clear_controls(db, owner)
            if expired_draft:
                db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def sent_between(self, owners, start, end):
        owners = tuple(owners)
        if not owners:
            return 0
        placeholders = ",".join("?" for _ in owners)
        with self.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM receipts WHERE owner IN ({placeholders}) "
                              "AND state='sent' AND recorded>=? AND recorded<?",
                              (*owners, start, end)).fetchone()[0]

    @staticmethod
    def _scheduled(db, post_id, revision, actor, allowed):
        allowed = tuple(allowed)
        row = Store.row(db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())
        changed = "⚠️ Esta programación ha cambiado. Abre de nuevo Programadas para consultar su estado."
        if actor not in allowed:
            raise Stale(changed)
        if not row:
            receipt = db.execute("SELECT owner,state FROM receipts WHERE id=?", (post_id,)).fetchone()
            if receipt and receipt["owner"] in allowed and receipt["state"] == "sent":
                raise Stale("✅ Esta publicación ya se ha enviado.")
            raise Stale(changed)
        if row["owner"] not in allowed:
            raise Stale(changed)
        if row["status"] == "sent":
            raise Stale("✅ Esta publicación ya se ha enviado.")
        if row["status"] == "sending":
            raise Stale("⏳ Esta publicación se está enviando. Consulta de nuevo Programadas.")
        if row["status"] != "pending" or not row["scheduled"] or row["revision"] != revision:
            raise Stale(changed)
        return row

    def scheduled_post(self, post_id, revision, actor, allowed):
        with self.connect() as db:
            return self._scheduled(db, post_id, revision, actor, allowed)

    def add_scheduled_view(self, actor, post_id, revision, kind, state, allowed):
        fresh = token()
        with self.connect(True) as db:
            self._scheduled(db, post_id, revision, actor, allowed)
            db.execute("INSERT INTO scheduled_views VALUES (?,?,?,?,NULL,?,?,?)",
                       (fresh, actor, post_id, revision, kind, json.dumps(state, default=encode_state),
                        state.get("_expires", self.now() + (self.ttl if kind in ("editor", "review", "picker", "preview") else MAX_SCHEDULE_SECONDS))))
        return fresh

    def bind_scheduled_view(self, actor, view_token, message_id):
        with self.connect(True) as db:
            db.execute("UPDATE scheduled_views SET message_id=? WHERE actor=? AND token=? AND message_id IS NULL",
                       (message_id, actor, view_token))
            view = self.row(db.execute("SELECT * FROM scheduled_views WHERE actor=? AND token=? AND message_id=?",
                                       (actor, view_token, message_id)).fetchone())
            if view:
                if view['kind'] in ('editor', 'review', 'picker', 'preview'):
                    db.execute('INSERT OR REPLACE INTO ui_messages VALUES (?,?,?)', (actor, message_id, view['expires']))
                else:
                    db.execute('DELETE FROM ui_messages WHERE owner=? AND message_id=?', (actor, message_id))
            if view and view["kind"] != "editor":
                media = bool(view["state"].get("media", view["kind"] in ("preview", "review")))
                db.execute("INSERT INTO publication_cards(post_id,chat_id,message_id,media) VALUES (?,?,?,?) "
                           "ON CONFLICT(post_id,chat_id,message_id) DO UPDATE SET media=excluded.media,terminal_state=NULL,generation=publication_cards.generation+1",
                           (view["post_id"], actor, message_id, int(media)))
                db.execute("UPDATE posts SET notice=COALESCE(notice,'scheduled:refresh') WHERE id=? "
                           "AND scheduled_due IS NOT NULL AND status IN ('sent','cancelled')", (view["post_id"],))

    def scheduled_view(self, actor, view_token, message_id=None):
        with self.connect() as db:
            row = self.row(db.execute("SELECT * FROM scheduled_views WHERE actor=? AND token=? AND expires>?",
                                      (actor, view_token, self.now())).fetchone())
            if not row or (message_id is not None and message_id != row["message_id"]):
                raise Stale("⚠️ Esta programación ha cambiado. Abre de nuevo Programadas para consultar su estado.")
            return row

    def pending_query_cards(self, actor):
        with self.connect() as db:
            return [self.row(row) for row in db.execute(
                "SELECT v.* FROM scheduled_views v JOIN posts p ON p.id=v.post_id "
                "JOIN publication_cards c ON c.post_id=v.post_id AND c.chat_id=v.actor AND c.message_id=v.message_id "
                "WHERE v.actor=? AND v.kind IN ('card','confirmation') AND p.status='pending' AND p.scheduled=1 "
                "AND c.terminal_state IS NULL", (actor,))]

    def scheduled_editor(self, actor):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM scheduled_views WHERE actor=? AND kind='editor' "
                                       "AND expires>? AND message_id IS NOT NULL ORDER BY message_id DESC LIMIT 1",
                                       (actor, self.now())).fetchone())

    def close_scheduled_view(self, actor, view_token):
        with self.connect(True) as db:
            db.execute("DELETE FROM scheduled_views WHERE actor=? AND token=?", (actor, view_token))

    def scheduled_messages(self, post_id, before_revision):
        with self.connect() as db:
            return [(r[0], r[1]) for r in db.execute(
                "SELECT actor,message_id FROM scheduled_views WHERE post_id=? AND revision<=? AND message_id IS NOT NULL",
                (post_id, before_revision))]

    def mutate_scheduled(self, actor, view_token, message_id, allowed, action, *, due=None):
        """Check revision and mutate under the same lock used by the delivery claim."""
        with self.connect(True) as db:
            view = self.row(db.execute("SELECT * FROM scheduled_views WHERE actor=? AND token=? AND message_id=? AND expires>?",
                                      (actor, view_token, message_id, self.now())).fetchone())
            if not view:
                raise Stale("⚠️ Esta programación ha cambiado. Abre de nuevo Programadas para consultar su estado.")
            post = self._scheduled(db, view["post_id"], view["revision"], actor, allowed)
            now = self.now()
            if action == "cancel":
                if view["kind"] not in ("card", "confirmation", "preview"):
                    raise Stale()
                self._finish(db, post["id"], "cancelled", None, None)
                db.execute("UPDATE posts SET revision=revision+1 WHERE id=?", (post["id"],))
            elif action == "save":
                if view["kind"] != "review":
                    raise Stale()
                state = view['state']
                caption, entities = state['caption'], state['entities']
                photo, photo_id = state.get('photo', post['photo']), state.get('photo_id', post['photo_id'])
                validate_caption(caption, entities, has_photo=bool(photo or photo_id))
                db.execute('UPDATE posts SET caption=?,entities=?,title=?,photo=?,photo_id=?,template=?,source_photo=?,product_data=?,revision=revision+1 WHERE id=?',
                           (caption, json.dumps(entities), title_from_caption(caption), photo or b'', photo_id,
                            int(state.get('template', post['template'])), state.get('source_photo', post['source_photo']),
                            json.dumps(state.get('product_data', post['product_data'])), post['id']))
            elif action == "schedule":
                state = view["state"]
                if (view["kind"] != "picker" or state.get("screen") != "review"
                        or state.get("due") != due or state.get("destination") != post["destination"]):
                    raise Stale()
                if due is None or not now < due <= now + MAX_SCHEDULE_SECONDS:
                    raise ValueError("Elige una fecha y hora futuras dentro de los próximos 30 días.")
                db.execute("UPDATE posts SET due=?,scheduled_due=?,created=?,not_before=0,revision=revision+1 WHERE id=?",
                           (due, due, now, post["id"]))
            elif action == "publish":
                if view["kind"] != "preview":
                    raise Stale()
                db.execute("UPDATE posts SET due=?,created=?,not_before=0,scheduled=0,revision=revision+1 WHERE id=?",
                           (now, now, post["id"]))
            else:
                raise ValueError("Acción de programación desconocida.")
            if action == "publish" or (action == "schedule" and view["state"].get("media")):
                db.execute("INSERT OR IGNORE INTO publication_cards(post_id,chat_id,message_id) VALUES (?,?,?)",
                           (post["id"], actor, message_id))
            return post

    def add_notice(self, owner, scope, issue, recover, kind, input_id=None):
        fresh = token()
        with self.connect() as db:
            db.execute("INSERT INTO operation_notices VALUES (?,?,?,?,?,?,NULL,?)",
                       (fresh, owner, scope, issue, json.dumps(list(recover)), kind, input_id))
        return fresh

    @staticmethod
    def _retire_notice(db, row):
        for message_id in (row['message_id'], row['input_id']):
            if message_id:
                db.execute("INSERT OR IGNORE INTO notice_cleanup(owner,message_id) VALUES (?,?)", (row['owner'], message_id))
        db.execute("DELETE FROM operation_notices WHERE token=?", (row['token'],))

    def bind_notice(self, owner, fresh, message_id):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM operation_notices WHERE owner=? AND token=?", (owner, fresh)).fetchone()
            if not row:
                return
            db.execute("UPDATE operation_notices SET message_id=? WHERE token=?", (message_id, fresh))
            for old in db.execute("SELECT * FROM operation_notices WHERE owner=? AND scope=? AND issue=? AND token<>?",
                                  (owner, row['scope'], row['issue'], fresh)).fetchall():
                self._retire_notice(db, old)

    def notice_scope(self, owner, fresh):
        with self.connect() as db:
            row = db.execute('SELECT scope FROM operation_notices WHERE owner=? AND token=?', (owner, fresh)).fetchone()
            return row[0] if row else None

    def notice_scopes(self, owner, issue):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT scope FROM operation_notices WHERE owner=? AND issue=?', (owner, issue))]

    def receipt(self, post_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM receipts WHERE id=?', (post_id,)).fetchone()
            return dict(row) if row else None

    def existing_notice(self, owner, scope, issue):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM operation_notices WHERE owner=? AND scope=? AND issue=? AND message_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                                       (owner, scope, issue)).fetchone())

    def resolve_notices(self, owner, scope, events=(), *, abandon=False):
        with self.connect(True) as db:
            for row in db.execute("SELECT * FROM operation_notices WHERE owner=? AND scope=?", (owner, scope)).fetchall():
                if row['kind'] == 'critical':
                    continue
                if (abandon and row['kind'] == 'temporary') or set(json.loads(row['recover'])).intersection(events):
                    self._retire_notice(db, row)

    def resolve_notice_event(self, owner, event):
        with self.connect(True) as db:
            for row in db.execute("SELECT * FROM operation_notices WHERE owner=? AND kind!='critical'", (owner,)).fetchall():
                if event in json.loads(row['recover']):
                    self._retire_notice(db, row)

    def review_notice(self, owner, fresh, message_id):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM operation_notices WHERE token=? AND owner=? AND message_id=? AND kind='critical'",
                             (fresh, owner, message_id)).fetchone()
            if not row:
                return False
            self._retire_notice(db, row)
            return True

    def notice_deletions(self, owners):
        owners = tuple(owners)
        if not owners:
            return []
        placeholders = ','.join('?' for _ in owners)
        with self.connect() as db:
            return [dict(r) for r in db.execute(f'SELECT * FROM notice_cleanup WHERE owner IN ({placeholders}) AND not_before<=? LIMIT 20', (*owners, self.now()))]

    def defer_notice_deletion(self, owner, message_id, delay):
        with self.connect() as db:
            db.execute('UPDATE notice_cleanup SET not_before=? WHERE owner=? AND message_id=?', (self.now() + max(1, delay), owner, message_id))

    def notice_deleted(self, owner, message_id):
        with self.connect() as db:
            db.execute('DELETE FROM notice_cleanup WHERE owner=? AND message_id=?', (owner, message_id))
            db.execute('DELETE FROM ui_messages WHERE owner=? AND message_id=?', (owner, message_id))
            db.execute('DELETE FROM private_screens WHERE owner=? AND message_id=?', (owner, message_id))

    def register_screens(self, owner, section, message_ids):
        with self.connect(True) as db:
            for message_id in message_ids:
                db.execute("INSERT INTO private_screens VALUES (?,?,?) ON CONFLICT(owner,message_id) DO UPDATE SET section=excluded.section",
                           (owner, section, message_id))
            return [r[0] for r in db.execute("SELECT message_id FROM private_screens WHERE owner=? AND section=?", (owner, section))
                    if r[0] not in message_ids]

    def screen_messages(self, owner, section):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT message_id FROM private_screens WHERE owner=? AND section=?", (owner, section))]

    def forget_screen(self, owner, message_id):
        with self.connect() as db:
            db.execute("DELETE FROM private_screens WHERE owner=? AND message_id=?", (owner, message_id))

    def cancellation_cards(self, owner, post_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT message_id,terminal_state FROM publication_cards WHERE chat_id=? AND post_id=?", (owner, post_id))]

    def track_message(self, owner, message_id, expires=None):
        if expires is None:
            draft = self.draft(owner, live=False)
            expires = draft['created'] + self.ttl
        with self.connect() as db:
            db.execute('INSERT INTO ui_messages VALUES (?,?,?) ON CONFLICT(owner,message_id) DO UPDATE SET expires=excluded.expires', (owner, message_id, expires))

    def keep_message(self, owner, message_id):
        with self.connect() as db:
            db.execute('DELETE FROM ui_messages WHERE owner=? AND message_id=?', (owner, message_id))

    def expired_messages(self, owners):
        with self.connect() as db:
            result = [dict(r) for r in db.execute('SELECT * FROM ui_messages WHERE expires<=?', (self.now(),)) if r['owner'] in owners]
        return result

    def saved_preview(self, owner):
        draft = self.draft(owner)
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM previews WHERE id=? AND owner=? AND draft_id=? AND revision=? "
                                       "AND status IN ('active','editing') AND expires>?",
                                      (draft['preview_id'], owner, draft['id'], draft['revision'], self.now())).fetchone())

    def editor_state(self, owner, state):
        with self.connect() as db:
            db.execute('UPDATE editors SET state=? WHERE owner=?', (json.dumps(state, default=encode_state), owner))

    def update_view_state(self, actor, view_token, state):
        with self.connect() as db:
            db.execute('UPDATE scheduled_views SET state=? WHERE actor=? AND token=? AND expires>?',
                       (json.dumps(state, default=encode_state), actor, view_token, self.now()))

    def preparation(self, owner):
        with self.connect() as db:
            return self.row(db.execute('SELECT * FROM preparations WHERE owner=?', (owner,)).fetchone())

    def set_preparation(self, owner, message_id):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preparations VALUES (?,?,'[]')", (owner, message_id))

    def add_preparation_warning(self, owner, message_id):
        with self.connect(True) as db:
            db.execute("INSERT OR IGNORE INTO preparations VALUES (?,NULL,'[]')", (owner,))
            db.execute('UPDATE preparations SET aux=? WHERE owner=?', (json.dumps([message_id]), owner))

    def clear_preparation(self, owner):
        with self.connect() as db:
            db.execute('DELETE FROM preparations WHERE owner=?', (owner,))
