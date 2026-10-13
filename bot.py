"""Aurelia RP Telegram interface. Python 3.11+ / aiogram 3."""
import asyncio
from contextlib import suppress
import logging
import os
import secrets
import shlex
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BotCommand
from dotenv import load_dotenv
from engine import Game, RuleError, BUILDINGS, LABELS

load_dotenv(Path(__file__).with_name('.env'))
TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMINS = {int(v.strip()) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip()}
DB = os.getenv('DB_PATH', str(Path(__file__).with_name('aurelia.db')))
game = Game(DB, ADMINS)
dp = Dispatcher()
confirmations = {}
last_action = {}

HELP = '''АУРЕЛИЯ RP
/countries — страны и заявки
/me — государство и прогноз экономики
/tax 15 — налог, от 0 до 40%
/build farm — стройка: farm / mill / well / factory
/projects — строительство
/recruit 2 — набор подразделений
/demobilize 2 — сокращение армии
/market — торговля
/sell food 50 10 — продать 50 ед. по 10 кредитов
/buy 1 — купить все предложение №1
/cancel 1 — отменить свое предложение
/rp текст — запись RP-события
/news — события стран
/time — игровой день и время до следующего
/withdraw — отозвать заявку
/id — твой Telegram ID

Деньги — игровые расчетные кредиты, не национальные валюты. 1 игровой день = 3 реальных часа. Экономика считается автоматически. Войны и дипломатия отыгрываются участниками; RP-запись не дает бесплатных ресурсов.'''
ADMIN_HELP = '''АДМИНИСТРАТОР
/admin — панель и заявки
/add Название — новая страна
/assign СТРАНА_ID ИГРОК_ID — назначить (0 = освободить)
/grant СТРАНА_ID treasury 1000 — изменить баланс
Поля: treasury / food / steel / oil; сумма может быть отрицательной
/population СТРАНА_ID 1000000 — игровое население
/ban ИГРОК_ID / /unban ИГРОК_ID
/time — время до следующего игрового дня
/freeze 1 — пауза, /freeze 0 — продолжить
/audit — журнал админских и игровых действий
/backup — скачать базу (только в личке)

Управление доступом админов: ADMIN_IDS в .env и перезапуск. Экономические параметры настраиваются в engine.py.'''


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


def menu(uid):
    rows = [[('🏛 Моя страна', 'view:me:0'), ('🌍 Страны', 'view:countries:0')],
            [('🏗 Строить', 'view:build:0'), ('📦 Рынок', 'view:market:0')],
            [('📰 События', 'view:news:0'), ('❓ Помощь', 'view:help:0')]]
    if uid in ADMINS:
        rows.append([('⚙ Админ-панель', 'view:admin:0')])
    return keyboard(rows)


def check(uid):
    p = game.rows('SELECT banned FROM players WHERE id=?', (uid,))
    if not p:
        raise RuleError('Нажми /start')
    if p[0]['banned']:
        raise RuleError('Доступ приостановлен администратором')


def pages(kind, offset, has_next):
    row = []
    if offset:
        row.append(('←', f'view:{kind}:{max(0,offset-8)}'))
    if has_next:
        row.append(('→', f'view:{kind}:{offset+8}'))
    return [row] if row else []


def view(uid, kind, offset=0):
    check(uid)
    offset = max(0, min(100000, offset))
    if kind in {'time','turn'}:
        return game.time_status(), menu(uid)
    if kind == 'help':
        return HELP + ('\n\n'+ADMIN_HELP if uid in ADMINS else ''), menu(uid)
    if kind == 'me':
        countries = game.rows('SELECT * FROM countries WHERE owner=?', (uid,))
        if not countries:
            return 'Страна еще не назначена. Выбери ее и подай заявку', keyboard([[('Выбрать страну', 'view:countries:0')]])
        c = countries[0]
        w = game.rows('SELECT * FROM world')[0]
        food_need = max(1, (c['population']+9999)//10000)
        oil = c['oil']+c['well']*25
        mills = min(c['mill'], oil//5)
        factories = min(c['factory'], (c['steel']+mills*25)//5, (oil-mills*5)//5)
        income = c['population']*c['tax']*c['stability']//1_000_000+factories*300
        upkeep = c['army']*80+c['farm']*20+c['mill']*40+c['well']*50+c['factory']*60
        text = f'''{c['name']} · №{c['id']} · день {w['turn']+1}{' · ПАУЗА' if w['frozen'] else ''}
Казна: {c['treasury']:,} кредитов
Население (игровое): {c['population']:,}
Налог: {c['tax']}% · Стабильность: {c['stability']}%
Армия: {c['army']} подразделений
Продовольствие: {c['food']} · Сталь: {c['steel']} · Нефть: {c['oil']}
Фермы: {c['farm']} · Металлургия: {c['mill']}
Нефтепромыслы: {c['well']} · Заводы: {c['factory']}

Прогноз за текущий игровой день:
Доход: {income} · Расходы: {upkeep} · Баланс: {income-upkeep:+d}
Продовольствие: +{c['farm']*80}, потребление {food_need}
Новые объекты производят со следующего дня после завершения
{game.time_status()}'''
        return text.replace(',', ' '), keyboard([[('🏗 Стройка', 'view:build:0'), ('📋 Проекты', 'view:projects:0')], [('Налог 10%', 'act:tax:10'), ('Налог 15%', 'act:tax:15'), ('Налог 25%', 'act:tax:25')], [('📦 Торговля', 'view:market:0')]])
    if kind == 'build':
        rows = []
        lines = ['СТРОИТЕЛЬСТВО · одновременно 3 объекта']
        for key, (name, price, steel, oil, turns) in BUILDINGS.items():
            lines.append(f'{name}: {price} кредитов, {steel} стали, {oil} нефти, {turns} дн.')
            rows.append([(f'Построить: {name}', f'confirm:build:{key}')])
        lines.append('Ферма: +80 еды; металлургия: 5 нефти → 25 стали; промысел: +25 нефти; завод: 5 стали + 5 нефти → 300 кредитов/день')
        return '\n'.join(lines), keyboard(rows)
    if kind == 'projects':
        c = game.rows('SELECT id FROM countries WHERE owner=?', (uid,))
        if not c:
            raise RuleError('У тебя нет страны')
        data = game.rows("SELECT * FROM projects WHERE country=? AND status='building' ORDER BY id", (c[0]['id'],))
        return '\n'.join(['СТРОИТЕЛЬСТВО']+[f'№{p["id"]}: {BUILDINGS[p["kind"]][0]}, осталось {p["remaining"]} дн.' for p in data]) if data else 'Нет строящихся объектов', menu(uid)
    if kind == 'countries':
        data = game.rows('SELECT id,name,owner FROM countries ORDER BY id LIMIT 9 OFFSET ?', (offset,))
        rows = [[(f'{c["id"]}. {c["name"]}'+(' · занята' if c['owner'] else ' · заявка'), f'card:{c["id"]}')] for c in data[:8]]
        return 'СТРАНЫ · выбери для просмотра или заявки', keyboard(rows+pages(kind,offset,len(data)>8))
    if kind == 'market':
        data = game.rows("SELECT o.*,c.name FROM offers o JOIN countries c ON c.id=o.seller WHERE status='open' ORDER BY o.id DESC LIMIT 9 OFFSET ?", (offset,))
        own = game.rows('SELECT id FROM countries WHERE owner=?', (uid,))
        cid = own[0]['id'] if own else None
        rows = []
        lines = ['РЫНОК · цена за единицу, покупается вся партия']
        for o in data[:8]:
            lines.append(f'№{o["id"]} · {o["name"]}: {LABELS[o["resource"]]} {o["quantity"]} × {o["price"]} = {o["quantity"]*o["price"]}')
            action = 'cancel' if cid == o['seller'] else 'buy'
            rows.append([(f'{"Отменить" if action=="cancel" else "Купить"} №{o["id"]}', f'confirm:{action}:{o["id"]}')])
        if not data:
            lines.append('Открытых предложений нет')
        lines.append('Продажа: /sell food 50 10 (food / steel / oil)')
        return '\n'.join(lines), keyboard(rows+pages(kind,offset,len(data)>8)) if rows else menu(uid)
    if kind == 'news':
        data = game.rows('SELECT r.*,c.name FROM rp r JOIN countries c ON c.id=r.country ORDER BY r.id DESC LIMIT 2 OFFSET ?', (offset,))
        text = '\n\n'.join(f'День {r["turn"]+1} · {r["name"]}\n{r["body"]}' for r in data) or 'Событий пока нет. Записать: /rp текст'
        rows = [[('←',f'view:news:{max(0,offset-2)}'),('→',f'view:news:{offset+2}')]]
        return text, keyboard(rows)
    if kind in ('admin', 'audit'):
        game.admin(uid)
        w = game.rows('SELECT * FROM world')[0]
        if kind == 'audit':
            data = game.rows('SELECT * FROM audit ORDER BY id DESC LIMIT 8 OFFSET ?', (offset,))
            text = '\n'.join(f'#{r["id"]} {r["created"]} · {r["actor"]} · {r["action"]}\n{r["detail"][:180]}' for r in data) or 'Журнал пуст'
            return text, keyboard(pages(kind,offset,len(data)==8)+[[('Админка','view:admin:0')]])
        claims = game.rows("SELECT q.*,p.name AS player_name,c.name AS country_name FROM claims q JOIN players p ON p.id=q.player JOIN countries c ON c.id=q.country WHERE status='pending' ORDER BY q.id LIMIT 9 OFFSET ?", (offset,))
        lines = ['АДМИН-ПАНЕЛЬ', game.time_status()]
        rows = [[('🕒 Время','view:time:0'), ('▶ Продолжить' if w['frozen'] else '⏸ Пауза', f'confirm:freeze:{0 if w["frozen"] else 1}')]]
        for q in claims[:8]:
            lines.append(f'Заявка №{q["id"]}: {q["player_name"]} ({q["player"]}) → {q["country_name"]}')
            rows.append([(f'✓ №{q["id"]}',f'confirm:approve:{q["id"]}'), (f'✗ №{q["id"]}', f'confirm:reject:{q["id"]}')])
        if not claims:
            lines.append('Новых заявок нет')
        lines.append('Назначения, баланс, блокировки, резервная копия: /help')
        rows += pages(kind,offset,len(claims)>8)+[[('Журнал','view:audit:0'),('Команды','view:help:0')]]
        return '\n'.join(lines), keyboard(rows)
    raise RuleError('Раздел не найден')


def confirmation(uid, action, args):
    now = time.monotonic()
    for k in list(confirmations):
        if confirmations[k][3] < now:
            del confirmations[k]
    if len(confirmations) >= 1000:
        raise RuleError('Слишком много ожидающих действий. Попробуй через 2 минуты')
    key = secrets.token_hex(10)
    confirmations[key] = (uid, action, args, now+120)
    titles = {'approve':'Одобрить заявку', 'reject':'Отклонить заявку', 'freeze':'Изменить паузу (1 — пауза, 0 — продолжить)', 'grant':'Изменить баланс страны', 'assign':'Назначить правителя (0 — освободить страну)', 'add':'Добавить страну', 'ban':'Заблокировать игрока', 'unban':'Разблокировать игрока', 'population':'Изменить игровое население', 'demobilize':'Демобилизовать подразделения', 'cancel':'Отменить предложение'}
    text = f'{titles.get(action, action)}: {" ".join(map(str,args))}'
    if action == 'build' and args and args[0] in BUILDINGS:
        name, price, steel, oil, turns = BUILDINGS[args[0]]
        text = f'Построить: {name}?\nСписать {price} кредитов, {steel} стали, {oil} нефти. Срок: {turns} дн.'
    elif action == 'recruit' and args:
        n = game.number(args[0],1,100)
        text = f'Набрать {n} подразделений?\nСписать {400*n} кредитов, {8*n} стали, {4*n} нефти. Содержание: +{80*n}/день'
    elif action == 'buy' and args:
        offers = game.rows("SELECT * FROM offers WHERE id=? AND status='open'",(game.number(args[0]),))
        if not offers:
            raise RuleError('Предложение уже закрыто')
        o=offers[0]
        text = f'Купить предложение №{o["id"]}?\n{o["quantity"]} {LABELS[o["resource"]]} за {o["quantity"]*o["price"]} кредитов'
    return text+'\nСрок подтверждения: 2 минуты', keyboard([[('✓ Подтвердить',f'yes:{key}'),('Отмена',f'no:{key}')]])


async def reply(message, text, markup=None):
    # No parse mode: user-supplied country names and RP never become HTML.
    for pos in range(0, len(text), 3900):
        await message.answer(text[pos:pos+3900], reply_markup=markup if pos+3900 >= len(text) else None)


def mutate(uid, action, args, key):
    now = time.monotonic()
    if now-last_action.get(uid,0) < 0.5:
        raise RuleError('Секунду, дай предыдущему действию пройти')
    if len(last_action) > 10000:
        last_action.clear()
    last_action[uid] = now
    return game.execute(uid, action, args, key)


@dp.message(F.text)
async def commands(m: Message):
    if not m.from_user:
        return
    if m.chat.type != 'private':
        if m.text.startswith('/'):
            await m.answer('Управление RP-ботом — в личных сообщениях с ботом')
        return
    uid = m.from_user.id
    try:
        game.advance_due(limit=None)
        raw = m.text.split(maxsplit=1)
        head = raw[0].split('@')[0].lstrip('/') if raw else ''
        tokens = ([raw[0], raw[1] if len(raw)>1 else ''] if head in {'rp','add'} else shlex.split(m.text))
        if not tokens:
            return
        command = tokens[0].split('@')[0].lstrip('/')
        args = tokens[1:]
        if command == 'start':
            game.register(uid, m.from_user.full_name)
            check(uid)
            await reply(m, 'АУРЕЛИЯ RP\nУправляй страной, торгуй и развивай экономику. Назначение подтверждает администратор\n\n'+HELP, menu(uid))
            return
        if command == 'id':
            await reply(m, f'Telegram ID: {uid}')
            return
        check(uid)
        if command in ('help','me','countries','market','projects','news','admin','audit','time','turn'):
            text, markup = view(uid, command)
            await reply(m,text,markup)
            return
        if command == 'backup':
            game.admin(uid)
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp)/'aurelia_backup.db')
                game.backup(path)
                await m.answer_document(FSInputFile(path), caption='Резервная копия RP. Содержит Telegram ID игроков и журнал действий. Восстановление описано в README')
            return
        allowed = {'claim','withdraw','tax','build','recruit','demobilize','sell','buy','cancel','rp','approve','reject','freeze','grant','assign','add','ban','unban','population'}
        if command not in allowed:
            await reply(m,'Открой /help или выбери раздел',menu(uid))
            return
        if command in {'approve','reject','freeze','grant','assign','add','ban','unban','population'}:
            game.admin(uid)
            text, markup = confirmation(uid,command,args)
        elif command in {'build','recruit','demobilize','buy','cancel'}:
            text, markup = confirmation(uid,command,args)
        else:
            text = mutate(uid,command,args,f'm:{m.chat.id}:{m.message_id}')
            markup = menu(uid)
        await reply(m,text,markup)
    except (RuleError, ValueError) as e:
        await reply(m,str(e))
    except Exception:
        logging.exception('Message handler failed')
        await reply(m,'Внутренняя ошибка. Проверь журнал запуска; незавершенная операция в базе откатилась')


@dp.callback_query()
async def callbacks(q: CallbackQuery):
    if not q.message or q.message.chat.type != 'private':
        await q.answer('Открой личные сообщения с ботом',show_alert=True)
        return
    uid = q.from_user.id
    try:
        game.advance_due(limit=None)
        check(uid)
        parts = (q.data or '').split(':')
        kind = parts[0]
        if kind == 'view':
            text, markup = view(uid,parts[1],int(parts[2]))
        elif kind == 'card':
            c = game.rows('SELECT id,name,owner FROM countries WHERE id=?',(int(parts[1]),))
            if not c:
                raise RuleError('Страна не найдена')
            c = c[0]
            text = f'{c["name"]} · №{c["id"]}\n'+('Правитель назначен' if c['owner'] else 'Свободна. Заявку рассмотрит админ')
            markup = keyboard([[('Подать заявку',f'act:claim:{c["id"]}')]]) if not c['owner'] else menu(uid)
        elif kind in ('yes','no'):
            key = parts[1]
            item = confirmations.get(key)
            if not item or item[0] != uid or item[3] < time.monotonic():
                raise RuleError('Подтверждение истекло или принадлежит другому игроку')
            if kind == 'no':
                del confirmations[key]
                text = 'Действие отменено'
            else:
                # Keep confirmation until successful; transaction deduplicates the nonce.
                text = mutate(uid,item[1],item[2],f'confirm:{key}')
                del confirmations[key]
            markup = menu(uid)
        elif kind in ('act','confirm') and parts[1] in {'claim','tax','build','buy','cancel','freeze','approve','reject'}:
            action, args = parts[1], parts[2:]
            if action in {'freeze','approve','reject'}:
                game.admin(uid)
            if kind == 'confirm':
                text,markup = confirmation(uid,action,args)
            else:
                text = mutate(uid,action,args,f'c:{q.id}')
                markup = menu(uid)
        else:
            raise RuleError('Кнопка устарела')
        await q.answer()
        await reply(q.message,text,markup)
    except (RuleError,ValueError,IndexError) as e:
        await q.answer(str(e)[:190],show_alert=True)
    except Exception:
        logging.exception('Callback handler failed')
        await q.answer('Внутренняя ошибка. Проверь журнал запуска',show_alert=True)


async def economy_clock():
    while True:
        try:
            days = game.advance_due()
            if days:
                logging.info('Automatic game days processed: %s', days)
        except Exception:
            logging.exception('Economy clock failed; next cycle will retry')
        await asyncio.sleep(1)


async def main():
    if not TOKEN or not ADMINS:
        raise SystemExit('Заполни BOT_TOKEN и ADMIN_IDS в .env. Никто не становится админом через /start')
    game.seed()
    game.start_clock()
    while game.advance_due() == 64:
        await asyncio.sleep(0)
    bot = Bot(TOKEN)
    await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in [('start','Главное меню'),('me','Моя страна'),('countries','Выбрать страну'),('market','Рынок'),('news','События'),('admin','Админ-панель'),('help','Все команды'),('id','Telegram ID'),('time','Игровое время')]])
    timer = asyncio.create_task(economy_clock())
    try:
        await dp.start_polling(bot,handle_as_tasks=False)
    finally:
        timer.cancel()
        with suppress(asyncio.CancelledError):
            await timer

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
