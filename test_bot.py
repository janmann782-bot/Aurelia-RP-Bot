"""Offline Telegram update tests using real aiogram dispatcher, no Telegram calls."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update
from engine import Game

_import_tmp = tempfile.TemporaryDirectory()
os.environ['DB_PATH']=str(Path(_import_tmp.name)/'import.db')
import bot as app

class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.sent=[]
    async def close(self):pass
    async def stream_content(self,*args,**kwargs):
        if False:yield b''
    async def make_request(self,bot,method,timeout=None):
        self.sent.append(method)
        if method.__api_method__ in ('sendMessage','sendDocument'):
            return Message(message_id=len(self.sent),date=datetime.now(timezone.utc),chat={'id':int(method.chat_id),'type':'private'},text=getattr(method,'text',None))
        return True

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=1000.0
        app.game=Game(Path(self.tmp.name)/'test.db',{1},clock=lambda:self.now)
        app.game.start_clock()
        app.ADMINS={1};app.confirmations.clear();app.last_action.clear()
        app.game.seed()
        for uid in (1,2,3):app.game.register(uid,f'User {uid}')
        self.session=FakeSession();self.bot=Bot('123456:TEST_TOKEN_FOR_OFFLINE_TESTING',session=self.session)
        self.seq=0
    async def asyncTearDown(self):
        await self.bot.session.close();self.tmp.cleanup()
    async def message(self,uid,text,chat_type='private'):
        self.seq+=1
        u=Update.model_validate({'update_id':self.seq,'message':{'message_id':self.seq,'date':1,'chat':{'id':uid,'type':chat_type},'from':{'id':uid,'is_bot':False,'first_name':'Tester'},'text':text}})
        await app.dp.feed_update(self.bot,u)
    async def callback(self,uid,data):
        self.seq+=1
        u=Update.model_validate({'update_id':self.seq,'callback_query':{'id':str(self.seq),'chat_instance':'test','from':{'id':uid,'is_bot':False,'first_name':'Tester'},'data':data,'message':{'message_id':1,'date':1,'chat':{'id':uid,'type':'private'},'from':{'id':123456,'is_bot':True,'first_name':'Bot'},'text':'Menu'}}})
        await app.dp.feed_update(self.bot,u)
    def text(self):
        return '\n'.join(getattr(m,'text','') or '' for m in self.session.sent)
    def nonce(self):return next(reversed(app.confirmations))

    async def test_start_menu_claim_admin_approval(self):
        await self.message(2,'/start')
        await self.callback(2,'act:claim:1')
        await self.callback(1,'view:admin:0')
        self.assertIn('Заявка №1',self.text())
        await self.callback(1,'confirm:approve:1')
        await self.callback(1,'yes:'+self.nonce())
        self.assertEqual(app.game.rows('SELECT owner FROM countries WHERE id=1')[0]['owner'],2)
        await self.message(2,'/me')
        self.assertIn('Кефирстан',self.text())

    async def test_forged_admin_callbacks_denied(self):
        await self.callback(2,'confirm:turn:0')
        await self.callback(2,'view:admin:0')
        self.assertEqual(len(app.confirmations),0)
        self.assertTrue(any(getattr(m,'show_alert',False) for m in self.session.sent))

    async def test_confirmations_bound_to_actor(self):
        await self.message(1,'/grant 1 treasury 100')
        nonce=self.nonce()
        await self.callback(2,'yes:'+nonce)
        self.assertEqual(app.game.rows('SELECT treasury FROM countries WHERE id=1')[0]['treasury'],10000)
        await self.callback(1,'yes:'+nonce)
        self.assertEqual(app.game.rows('SELECT treasury FROM countries WHERE id=1')[0]['treasury'],10100)

    async def test_time_and_legacy_turn_are_readonly(self):
        await self.message(2,'/time')
        await self.message(1,'/turn')
        self.assertFalse(app.confirmations)
        self.assertIn('1 день = 3 реальных часа',self.text())
        self.assertEqual(app.game.rows('SELECT turn FROM world')[0]['turn'],0)

    async def test_commands_process_due_day_before_read(self):
        self.now+=10800
        await self.message(2,'/time')
        self.assertIn('Игровой день 2',self.text())
        self.assertEqual(app.game.rows('SELECT turn FROM world')[0]['turn'],1)

    async def test_clock_background_task_processes_due_day(self):
        import asyncio
        self.now+=10800
        task=asyncio.create_task(app.economy_clock())
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(app.game.rows('SELECT turn FROM world')[0]['turn'],1)

    async def test_rp_preserves_quotes(self):
        app.game.execute(1,'assign',['1','2'])
        await self.message(2,'/rp Принят закон "О портах" и указ президента')
        self.assertEqual(app.game.rows('SELECT body FROM rp')[0]['body'],'Принят закон "О портах" и указ президента')

    async def test_commands_do_not_mutate_from_group(self):
        await self.message(1,'/grant 1 treasury 100','group')
        self.assertFalse(app.confirmations)
        self.assertEqual(app.game.rows('SELECT treasury FROM countries WHERE id=1')[0]['treasury'],10000)

    async def test_player_screens_and_build_confirm(self):
        app.game.execute(1,'assign',['1','2'])
        for screen in ('me','build','projects','market','news','countries','help'):
            await self.callback(2,f'view:{screen}:0')
        await self.callback(2,'confirm:build:farm')
        self.assertIn('800 кредитов',self.text())
        await self.callback(2,'yes:'+self.nonce())
        self.assertEqual(app.game.rows('SELECT count(*) n FROM projects')[0]['n'],1)

if __name__=='__main__':unittest.main()
