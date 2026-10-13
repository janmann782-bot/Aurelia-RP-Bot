"""Transactional RP economy. All money is integer game credits, never real currency."""
import json
import sqlite3
import time
from contextlib import contextmanager, closing
from pathlib import Path

RESOURCES = ('food', 'steel', 'oil')
LABELS = {'food': 'Продовольствие', 'steel': 'Сталь', 'oil': 'Нефть'}
BUILDINGS = {
    'farm': ('Ферма', 800, 10, 0, 2),
    'mill': ('Металлургия', 1500, 30, 5, 3),
    'well': ('Нефтепромысел', 2000, 40, 10, 3),
    'factory': ('Завод', 2500, 50, 15, 4),
}
DAY_SECONDS = 3 * 60 * 60
MAX = 10**12
SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY, name TEXT NOT NULL, banned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS countries(
 id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, owner INTEGER UNIQUE REFERENCES players(id),
 treasury INTEGER NOT NULL DEFAULT 10000 CHECK(treasury>=0), population INTEGER NOT NULL DEFAULT 1000000,
 tax INTEGER NOT NULL DEFAULT 15 CHECK(tax BETWEEN 0 AND 40), stability INTEGER NOT NULL DEFAULT 70 CHECK(stability BETWEEN 0 AND 100),
 army INTEGER NOT NULL DEFAULT 5 CHECK(army>=0), farm INTEGER NOT NULL DEFAULT 2,
 mill INTEGER NOT NULL DEFAULT 1, well INTEGER NOT NULL DEFAULT 1, factory INTEGER NOT NULL DEFAULT 1,
 food INTEGER NOT NULL DEFAULT 200 CHECK(food>=0), steel INTEGER NOT NULL DEFAULT 100 CHECK(steel>=0), oil INTEGER NOT NULL DEFAULT 100 CHECK(oil>=0));
CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY, player INTEGER REFERENCES players(id), country INTEGER REFERENCES countries(id), status TEXT DEFAULT 'pending');
CREATE UNIQUE INDEX IF NOT EXISTS pending_player ON claims(player) WHERE status='pending';
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, country INTEGER REFERENCES countries(id), kind TEXT, remaining INTEGER, status TEXT DEFAULT 'building');
CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY, seller INTEGER REFERENCES countries(id), resource TEXT, quantity INTEGER, price INTEGER, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS rp(id INTEGER PRIMARY KEY, country INTEGER REFERENCES countries(id), body TEXT, turn INTEGER);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor INTEGER, action TEXT, detail TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS world(id INTEGER PRIMARY KEY CHECK(id=1), turn INTEGER DEFAULT 0, frozen INTEGER DEFAULT 0);
INSERT OR IGNORE INTO world(id) VALUES(1);
'''

class RuleError(Exception):
    pass

class Game:
    def __init__(self, path='aurelia.db', admins=(), clock=None):
        self.clock = clock or time.time
        self.path = str(path)
        self.admins = set(admins)
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(world)')}
            if 'next_tick' not in columns:
                db.execute('ALTER TABLE world ADD COLUMN next_tick REAL')
            if 'paused_remaining' not in columns:
                db.execute('ALTER TABLE world ADD COLUMN paused_remaining REAL')
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def register(self, uid, name):
        with self.transaction() as db:
            db.execute('INSERT INTO players(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name[:100]))

    def seed(self):
        # Names only. Economic numbers are game presets, not Aurelia canon.
        with self.transaction() as db:
            for name in ('Кефирстан', 'Йогуртстан', 'Турбания', 'Курбания', 'Дервессния', 'Веранд', 'Медия', 'Вордсфалия', 'Деши', 'Элебстан', 'Элебия', 'Шавермастан'):
                db.execute('INSERT OR IGNORE INTO countries(name) VALUES(?)', (name,))

    def start_clock(self):
        """Initialize once; restarts preserve the existing deadline."""
        with self.transaction() as db:
            w = db.execute('SELECT * FROM world WHERE id=1').fetchone()
            if w['next_tick'] is None and w['paused_remaining'] is None:
                if w['frozen']:
                    db.execute('UPDATE world SET paused_remaining=? WHERE id=1', (DAY_SECONDS,))
                else:
                    db.execute('UPDATE world SET next_tick=? WHERE id=1', (self.clock()+DAY_SECONDS,))

    def _advance_due(self, db, now, limit=None):
        w = db.execute('SELECT * FROM world WHERE id=1').fetchone()
        if w['frozen'] or w['next_tick'] is None or w['next_tick'] > now:
            return 0
        due = w['next_tick']
        count = int((now-due)//DAY_SECONDS)+1
        if limit is not None:
            count = min(count, limit)
        for index in range(count):
            result = self.tick(db, w['turn']+index+1)
            db.execute('INSERT INTO audit(actor,action,detail) VALUES(0,?,?)',
                       ('auto_day', json.dumps({'due': due+index*DAY_SECONDS, 'result': result}, ensure_ascii=False)))
        db.execute('UPDATE world SET next_tick=? WHERE id=1', (due+count*DAY_SECONDS,))
        return count

    def advance_due(self, limit=64):
        """Catch up due days atomically; multiple workers cannot count twice."""
        with self.transaction() as db:
            return self._advance_due(db, self.clock(), limit)

    def time_status(self):
        w = self.rows('SELECT * FROM world WHERE id=1')[0]
        remaining = w['paused_remaining'] if w['frozen'] else (max(0,w['next_tick']-self.clock()) if w['next_tick'] else DAY_SECONDS)
        seconds = max(0,int(remaining if remaining is not None else DAY_SECONDS))
        h, rest = divmod(seconds,3600)
        m, sec = divmod(rest,60)
        return f"Игровой день {w['turn']+1} · 1 день = 3 реальных часа\n" + (f"Пауза · осталось {h:02d}:{m:02d}:{sec:02d}" if w['frozen'] else f"До следующего дня: {h:02d}:{m:02d}:{sec:02d}")

    @staticmethod
    def number(value, low=1, high=MAX):
        try:
            n = int(value)
        except (ValueError, TypeError):
            raise RuleError('Нужно целое число') from None
        if not low <= n <= high:
            raise RuleError(f'Допустимо от {low} до {high}')
        return n

    def admin(self, uid):
        if uid not in self.admins:
            raise RuleError('Только для администратора')

    @staticmethod
    def country(db, cid):
        r = db.execute('SELECT * FROM countries WHERE id=?', (cid,)).fetchone()
        if not r:
            raise RuleError('Страна не найдена')
        return r

    @staticmethod
    def owned(db, uid):
        r = db.execute('SELECT * FROM countries WHERE owner=?', (uid,)).fetchone()
        if not r:
            raise RuleError('Сначала подай заявку на страну: /countries')
        return r

    @staticmethod
    def spend(db, c, money=0, steel=0, oil=0):
        if c['treasury'] < money or c['steel'] < steel or c['oil'] < oil:
            raise RuleError('Не хватает денег или ресурсов')
        db.execute('UPDATE countries SET treasury=treasury-?,steel=steel-?,oil=oil-? WHERE id=?', (money, steel, oil, c['id']))

    def execute(self, uid, action, args=(), key=None):
        """Deduplicate Telegram deliveries; auth + all changes in one transaction."""
        self.advance_due(limit=None)
        with self.transaction() as db:
            p = db.execute('SELECT * FROM players WHERE id=?', (uid,)).fetchone()
            if not p:
                raise RuleError('Нажми /start')
            if p['banned']:
                raise RuleError('Доступ приостановлен администратором')
            receipt_key = f'{uid}:{key}' if key else None
            if receipt_key:
                old = db.execute('SELECT result FROM receipts WHERE key=?', (receipt_key,)).fetchone()
                if old:
                    return old['result']
            admin_actions = {'approve', 'reject', 'turn', 'freeze', 'grant', 'assign', 'add', 'ban', 'unban', 'population'}
            if action in admin_actions:
                self.admin(uid)
            elif db.execute('SELECT frozen FROM world WHERE id=1').fetchone()[0]:
                raise RuleError('Игра на паузе')
            try:
                result = self._apply(db, uid, action, args)
            except (IndexError, TypeError):
                raise RuleError('Неверные аргументы. Открой /help') from None
            db.execute('INSERT INTO audit(actor,action,detail) VALUES(?,?,?)', (uid, action, json.dumps({'args': args, 'result': result}, ensure_ascii=False)))
            if receipt_key:
                db.execute('INSERT INTO receipts(key,result) VALUES(?,?)', (receipt_key, result))
            return result

    def _apply(self, db, uid, action, a):
        if action == 'claim':
            cid = self.number(a[0])
            c = self.country(db, cid)
            if c['owner'] or db.execute('SELECT 1 FROM countries WHERE owner=?', (uid,)).fetchone():
                raise RuleError('Страна занята или у тебя уже есть страна')
            if db.execute("SELECT 1 FROM claims WHERE player=? AND status='pending'", (uid,)).fetchone():
                raise RuleError('У тебя уже есть заявка. Отменить: /withdraw')
            cur = db.execute('INSERT INTO claims(player,country) VALUES(?,?)', (uid, cid))
            return f'Заявка №{cur.lastrowid} на {c["name"]} ожидает администратора'
        if action == 'withdraw':
            db.execute("UPDATE claims SET status='withdrawn' WHERE player=? AND status='pending'", (uid,))
            return 'Заявка отозвана'
        if action in ('approve', 'reject'):
            rid = self.number(a[0])
            r = db.execute("SELECT * FROM claims WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not r:
                raise RuleError('Заявка уже обработана или не найдена')
            if action == 'approve':
                c = self.country(db, r['country'])
                player = db.execute('SELECT * FROM players WHERE id=?', (r['player'],)).fetchone()
                if c['owner'] or player['banned'] or db.execute('SELECT 1 FROM countries WHERE owner=?', (r['player'],)).fetchone():
                    raise RuleError('Назначение невозможно: страна занята, игрок заблокирован или уже назначен')
                db.execute('UPDATE countries SET owner=? WHERE id=?', (r['player'], r['country']))
                db.execute("UPDATE claims SET status='rejected' WHERE country=? AND status='pending' AND id<>?", (r['country'], rid))
            db.execute('UPDATE claims SET status=? WHERE id=?', ('approved' if action == 'approve' else 'rejected', rid))
            return f'Заявка №{rid}: ' + ('одобрена' if action == 'approve' else 'отклонена')
        if action == 'add':
            name = ' '.join(a).strip()
            if not 2 <= len(name) <= 80:
                raise RuleError('Название: 2–80 символов')
            if db.execute('SELECT 1 FROM countries WHERE name=?', (name,)).fetchone():
                raise RuleError('Такая страна уже есть')
            db.execute('INSERT INTO countries(name) VALUES(?)', (name,))
            return f'Добавлена страна {name}'
        if action == 'assign':
            cid, player = self.number(a[0]), self.number(a[1], 0)
            self.country(db, cid)
            if player:
                p = db.execute('SELECT * FROM players WHERE id=?', (player,)).fetchone()
                if not p or p['banned']:
                    raise RuleError('Игрок должен нажать /start и не быть заблокирован')
                if db.execute('SELECT 1 FROM countries WHERE owner=? AND id<>?', (player, cid)).fetchone():
                    raise RuleError('У игрока уже есть страна')
            db.execute('UPDATE countries SET owner=? WHERE id=?', (player or None, cid))
            db.execute("UPDATE claims SET status='rejected' WHERE status='pending' AND (country=? OR player=?)", (cid, player))
            return f'Правитель страны №{cid}: {player or "не назначен"}'
        if action in ('ban', 'unban'):
            target = self.number(a[0])
            if target in self.admins:
                raise RuleError('Администратора нельзя блокировать в игре')
            if not db.execute('SELECT 1 FROM players WHERE id=?', (target,)).fetchone():
                raise RuleError('Игрок не найден')
            db.execute('UPDATE players SET banned=? WHERE id=?', (int(action == 'ban'), target))
            return 'Доступ игрока изменен'
        if action == 'freeze':
            val = self.number(a[0], 0, 1)
            w = db.execute('SELECT * FROM world WHERE id=1').fetchone()
            if val and not w['frozen']:
                remaining = max(0,w['next_tick']-self.clock()) if w['next_tick'] is not None else DAY_SECONDS
                db.execute('UPDATE world SET frozen=1,paused_remaining=?,next_tick=NULL WHERE id=1',(remaining,))
            elif not val and w['frozen']:
                remaining = w['paused_remaining'] if w['paused_remaining'] is not None else DAY_SECONDS
                db.execute('UPDATE world SET frozen=0,next_tick=?,paused_remaining=NULL WHERE id=1',(self.clock()+remaining,))
            return 'Игра на паузе' if val else 'Игра продолжается'
        if action == 'grant':
            cid, field, amount = self.number(a[0]), a[1], self.number(a[2], -MAX, MAX)
            if field not in ('treasury', *RESOURCES):
                raise RuleError('Поле: treasury / food / steel / oil')
            c = self.country(db, cid)
            if not 0 <= c[field] + amount <= MAX:
                raise RuleError('Результат выходит за допустимый баланс')
            db.execute(f'UPDATE countries SET {field}={field}+? WHERE id=?', (amount, cid))
            return f'Страна №{cid}: {field} {amount:+d}'
        if action == 'population':
            cid, value = self.number(a[0]), self.number(a[1], 1000, 10**9)
            self.country(db, cid)
            db.execute('UPDATE countries SET population=? WHERE id=?', (value, cid))
            return 'Игровое население изменено'
        if action == 'turn':
            raise RuleError('Дни идут автоматически: 1 игровой день = 3 реальных часа. Время: /time')
        c = self.owned(db, uid)
        if action == 'tax':
            rate = self.number(a[0], 0, 40)
            db.execute('UPDATE countries SET tax=? WHERE id=?', (rate, c['id']))
            return f'Налог установлен: {rate}%'
        if action == 'build':
            kind = a[0]
            if kind not in BUILDINGS:
                raise RuleError('Стройки: farm / mill / well / factory')
            count = db.execute("SELECT count(*) FROM projects WHERE country=? AND status='building'", (c['id'],)).fetchone()[0]
            if count >= 3:
                raise RuleError('Одновременно можно строить 3 объекта')
            name, price, steel, oil, turns = BUILDINGS[kind]
            pending = db.execute("SELECT count(*) FROM projects WHERE country=? AND kind=? AND status='building'", (c['id'], kind)).fetchone()[0]
            if c[kind] + pending >= 100:
                raise RuleError('Лимит: 100 объектов каждого типа')
            self.spend(db, c, price, steel, oil)
            db.execute('INSERT INTO projects(country,kind,remaining) VALUES(?,?,?)', (c['id'], kind, turns))
            return f'{name}: строительство начато, {turns} дн.'
        if action == 'recruit':
            n = self.number(a[0], 1, 100)
            if c['army'] + n > 1000:
                raise RuleError('Лимит армии: 1000 подразделений')
            self.spend(db, c, 400*n, 8*n, 4*n)
            db.execute('UPDATE countries SET army=army+? WHERE id=?', (n, c['id']))
            return f'Набрано подразделений: {n}. Содержание: +{80*n}/день'
        if action == 'demobilize':
            n = self.number(a[0], 1, c['army'])
            db.execute('UPDATE countries SET army=army-? WHERE id=?', (n, c['id']))
            return f'Демобилизовано: {n}. Возврата ресурсов нет'
        if action == 'sell':
            resource, quantity, price = a[0], self.number(a[1], 1, 10**6), self.number(a[2], 1, 10**6)
            if resource not in RESOURCES:
                raise RuleError('Ресурс: food / steel / oil')
            if c[resource] < quantity:
                raise RuleError('Не хватает ресурса')
            if db.execute("SELECT count(*) FROM offers WHERE seller=? AND status='open'", (c['id'],)).fetchone()[0] >= 10:
                raise RuleError('Лимит: 10 открытых предложений')
            db.execute(f'UPDATE countries SET {resource}={resource}-? WHERE id=?', (quantity, c['id']))
            r = db.execute('INSERT INTO offers(seller,resource,quantity,price) VALUES(?,?,?,?)', (c['id'], resource, quantity, price))
            return f'Предложение №{r.lastrowid}: {quantity} {LABELS[resource]}, цена {price}/ед. Ресурс зарезервирован'
        if action in ('buy', 'cancel'):
            oid = self.number(a[0])
            o = db.execute("SELECT * FROM offers WHERE id=? AND status='open'", (oid,)).fetchone()
            if not o:
                raise RuleError('Предложение закрыто или не найдено')
            resource = o['resource']
            if action == 'cancel':
                if o['seller'] != c['id']:
                    raise RuleError('Это чужое предложение')
                if c[resource] + o['quantity'] > MAX:
                    raise RuleError('Лимит склада')
                db.execute(f'UPDATE countries SET {resource}={resource}+? WHERE id=?', (o['quantity'], c['id']))
            else:
                if o['seller'] == c['id']:
                    raise RuleError('У себя покупать нельзя')
                seller = self.country(db, o['seller'])
                if seller['owner']:
                    sp = db.execute('SELECT banned FROM players WHERE id=?', (seller['owner'],)).fetchone()
                    if sp['banned']:
                        raise RuleError('Торговля с заблокированным игроком приостановлена')
                total = o['quantity'] * o['price']
                if seller['treasury'] + total > MAX or c[resource] + o['quantity'] > MAX:
                    raise RuleError('Лимит баланса или склада')
                self.spend(db, c, total)
                db.execute(f'UPDATE countries SET {resource}={resource}+? WHERE id=?', (o['quantity'], c['id']))
                db.execute('UPDATE countries SET treasury=treasury+? WHERE id=?', (total, o['seller']))
            db.execute('UPDATE offers SET status=? WHERE id=?', ('sold' if action == 'buy' else 'cancelled', oid))
            return f'Предложение №{oid}: ' + ('куплено' if action == 'buy' else 'отменено, ресурс возвращен')
        if action == 'rp':
            body = ' '.join(a).strip()
            if not 10 <= len(body) <= 1500:
                raise RuleError('RP-событие: 10–1500 символов')
            turn = db.execute('SELECT turn FROM world WHERE id=1').fetchone()[0]
            if db.execute('SELECT count(*) FROM rp WHERE country=? AND turn=?', (c['id'], turn)).fetchone()[0] >= 3:
                raise RuleError('Лимит: 3 RP-события на игровой день')
            db.execute('INSERT INTO rp(country,body,turn) VALUES(?,?,?)', (c['id'], body, turn))
            return 'RP-событие записано. Оно не меняет ресурсы и границы автоматически'
        raise RuleError('Неизвестное действие')

    def tick(self, db, turn):
        lines = [f'Игровой день {turn} завершен']
        for c in db.execute('SELECT * FROM countries ORDER BY id').fetchall():
            cid = c['id']
            # Projects finish after N turns. Newly completed buildings produce next turn.
            db.execute("UPDATE projects SET remaining=remaining-1 WHERE country=? AND status='building'", (cid,))
            for p in db.execute("SELECT * FROM projects WHERE country=? AND status='building' AND remaining<=0", (cid,)).fetchall():
                db.execute(f'UPDATE countries SET {p["kind"]}=MIN(100,{p["kind"]}+1) WHERE id=?', (cid,))
                db.execute("UPDATE projects SET status='done' WHERE id=?", (p['id'],))
            food = min(MAX, c['food'] + c['farm']*80)
            need = max(1, (c['population'] + 9999)//10000)
            shortage = food < need
            food = max(0, food-need)
            oil = min(MAX, c['oil'] + c['well']*25)
            steel = c['steel']
            mills = min(c['mill'], oil//5)
            oil -= mills*5
            steel = min(MAX, steel + mills*25)
            factories = min(c['factory'], steel//5, oil//5)
            oil -= factories*5
            steel -= factories*5
            income = c['population']*c['tax']*c['stability']//1_000_000 + factories*300
            upkeep = c['army']*80 + c['farm']*20 + c['mill']*40 + c['well']*50 + c['factory']*60
            funds = c['treasury'] + income
            deficit = funds < upkeep
            treasury = min(MAX, max(0, funds-upkeep))
            delta = (-8 if shortage else 1) + (-5 if deficit else 0) + (-3 if c['tax'] > 25 else (1 if c['tax'] <= 10 else 0))
            stability = max(0, min(100, c['stability']+delta))
            db.execute('UPDATE countries SET food=?,steel=?,oil=?,treasury=?,stability=? WHERE id=?', (food, steel, oil, treasury, stability, cid))
            flags = ('; дефицит продовольствия' if shortage else '') + ('; казна не покрыла расходы' if deficit else '')
            lines.append(f'{c["name"]}: доход {income}, расходы {upkeep}, казна {treasury}, стабильность {stability}%{flags}')
        db.execute('UPDATE world SET turn=? WHERE id=1', (turn,))
        return '\n'.join(lines)

    def backup(self, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
