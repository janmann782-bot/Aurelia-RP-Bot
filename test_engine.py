import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from engine import Game, RuleError, DAY_SECONDS

class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.game = Game(Path(self.tmp.name)/'test.db', {1, 4}, clock=lambda:self.now)
        self.game.start_clock()
        self.game.seed()
        for uid in (1,2,3,4,5):
            self.game.register(uid,f'Player {uid}')
        self.game.execute(1,'assign',('1','2'))
        self.game.execute(1,'assign',('2','3'))
        self.game.execute(1,'assign',('3','5'))

    def tearDown(self):
        self.tmp.cleanup()

    def day(self):
        self.now += DAY_SECONDS
        return self.game.advance_due()

    def c(self,cid=1):
        return self.game.rows('SELECT * FROM countries WHERE id=?',(cid,))[0]

    def test_admin_auth_cannot_be_forged(self):
        before=self.c()
        for action,args in [('grant',['1','treasury','999']),('turn',['0']),('assign',['1','3']),('ban',['3'])]:
            with self.assertRaises(RuleError):
                self.game.execute(2,action,args)
        self.assertEqual(before,self.c())

    def test_claim_approval_and_competing_claims(self):
        self.game.register(10,'A');self.game.register(11,'B')
        self.game.execute(10,'claim',['4'])
        self.game.execute(11,'claim',['4'])
        self.game.execute(1,'approve',['1'])
        self.assertEqual(self.c(4)['owner'],10)
        self.assertEqual(self.game.rows('SELECT status FROM claims WHERE id=2')[0]['status'],'rejected')
        with self.assertRaises(RuleError): self.game.execute(1,'approve',['2'])

    def test_one_pending_claim_and_one_country(self):
        self.game.register(10,'A')
        self.game.execute(10,'claim',['4'])
        with self.assertRaises(RuleError):self.game.execute(10,'claim',['5'])
        with self.assertRaises(RuleError):self.game.execute(2,'claim',['5'])
        self.game.execute(10,'withdraw')
        self.game.execute(10,'claim',['5'])

    def test_receipt_replay_survives_restart(self):
        self.game.execute(2,'recruit',['1'],key='delivery42')
        again=Game(self.game.path,{1},clock=lambda:self.now)
        again.execute(2,'recruit',['1'],key='delivery42')
        self.assertEqual(self.c()['army'],6)
        self.assertEqual(self.c()['treasury'],9600)

    def test_trade_escrow_conserves_resources_and_money(self):
        money=sum(self.c(i)['treasury'] for i in (1,2))
        food=sum(self.c(i)['food'] for i in (1,2))
        self.game.execute(2,'sell',['food','50','10'])
        self.assertEqual(self.c()['food'],150)
        self.game.execute(3,'buy',['1'])
        self.assertEqual(sum(self.c(i)['treasury'] for i in (1,2)),money)
        self.assertEqual(sum(self.c(i)['food'] for i in (1,2)),food)
        with self.assertRaises(RuleError):self.game.execute(3,'buy',['1'])

    def test_cancel_refunds_once(self):
        self.game.execute(2,'sell',['steel','20','5'])
        with self.assertRaises(RuleError):self.game.execute(3,'cancel',['1'])
        self.game.execute(2,'cancel',['1'])
        self.assertEqual(self.c()['steel'],100)
        with self.assertRaises(RuleError):self.game.execute(2,'cancel',['1'])

    def test_insufficient_funds_rolls_back_trade(self):
        self.game.execute(2,'sell',['food','50','1000000'])
        before=self.c(2)
        with self.assertRaises(RuleError):self.game.execute(3,'buy',['1'])
        self.assertEqual(before,self.c(2))
        self.assertEqual(self.game.rows('SELECT status FROM offers')[0]['status'],'open')

    def test_concurrent_buy_only_one_winner(self):
        self.game.execute(2,'sell',['food','50','10'])
        def buy(uid):
            try:self.game.execute(uid,'buy',['1']);return True
            except RuleError:return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(buy,[3,5])),1)
        self.assertEqual(self.c()['treasury'],10500)

    def test_concurrent_days_only_one_commit(self):
        self.now += DAY_SECONDS
        def advance(_):
            return self.game.advance_due()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(advance,[1,4])),1)
        self.assertEqual(self.game.rows('SELECT turn FROM world')[0]['turn'],1)

    def test_project_exact_duration_and_output(self):
        self.game.execute(2,'build',['farm'])
        self.day()
        self.assertEqual(self.c()['farm'],2)
        self.day()
        self.assertEqual(self.c()['farm'],3)
        self.assertEqual(self.c()['food'],320)
        self.day()
        self.assertEqual(self.c()['food'],460)

    def test_default_economy_and_no_autonomous_population_growth(self):
        self.day()
        c=self.c()
        self.assertEqual(c['treasury'],10760)
        self.assertEqual(c['population'],1000000)
        self.assertEqual(c['steel'],120)
        self.assertEqual(c['oil'],115)

    def test_shortage_and_bankruptcy_cannot_create_negative_balances(self):
        self.game.execute(1,'grant',['1','food','-200'])
        self.game.execute(1,'grant',['1','treasury','-10000'])
        self.game.execute(2,'tax',['0'])
        self.game.execute(1,'population',['1','10000000'])
        self.day()
        self.assertEqual(self.c()['food'],0)
        self.assertEqual(self.c()['treasury'],0)
        self.assertEqual(self.c()['stability'],58)

    def test_pause_and_ban_block_mutations(self):
        self.game.execute(1,'freeze',['1'])
        with self.assertRaises(RuleError):self.game.execute(2,'tax',['40'])
        self.assertEqual(self.day(),0)
        self.game.execute(1,'freeze',['0'])
        self.game.execute(1,'ban',['2'])
        with self.assertRaises(RuleError):self.game.execute(2,'build',['farm'])
        self.game.execute(1,'unban',['2'])
        self.game.execute(2,'tax',['20'])

    def test_boundaries_and_resource_whitelist(self):
        before=self.c()
        for action,args in [('tax',['41']),('sell',['food','-5','10']),('build',['nuclear']),('grant',['1','owner','5']),('grant',['1','treasury','-10001']),('recruit',['999999999999999999999'])]:
            with self.assertRaises(RuleError):self.game.execute(1 if action=='grant' else 2,action,args)
        self.assertEqual(before,self.c())

    def test_three_projects_and_failed_spend_atomic(self):
        for _ in range(3):self.game.execute(2,'build',['farm'])
        before=self.c()
        with self.assertRaises(RuleError):self.game.execute(2,'build',['farm'])
        self.assertEqual(before,self.c())
        self.game.execute(1,'grant',['1','steel','-70'])
        before=self.c()
        with self.assertRaises(RuleError):self.game.execute(2,'recruit',['1'])
        self.assertEqual(before,self.c())

    def test_rp_limit_no_free_resources(self):
        before=self.c()
        for _ in range(3):self.game.execute(2,'rp',['Захватили миллион танков — RP только'])
        with self.assertRaises(RuleError):self.game.execute(2,'rp',['Четвертое событие за этот ход'])
        self.assertEqual(before,self.c())

    def test_backup_integrity(self):
        path=str(Path(self.tmp.name)/'backup.db')
        self.game.backup(path)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM countries').fetchone()[0],12)

    def test_clock_exact_boundary_and_no_double_tick(self):
        self.now += DAY_SECONDS-1
        self.assertEqual(self.game.advance_due(),0)
        self.now += 1
        self.assertEqual(self.game.advance_due(),1)
        self.assertEqual(self.game.advance_due(),0)
        self.assertEqual(self.c()['treasury'],10760)

    def test_eight_days_per_real_day(self):
        self.now += 24*3600
        self.assertEqual(self.game.advance_due(),8)
        self.assertEqual(self.game.rows('SELECT turn FROM world')[0]['turn'],8)
        self.assertEqual(self.game.rows('SELECT next_tick FROM world')[0]['next_tick'],1000+9*DAY_SECONDS)

    def test_restart_preserves_deadline_and_catches_up(self):
        original=self.game.rows('SELECT next_tick FROM world')[0]['next_tick']
        self.now += 2*DAY_SECONDS+100
        restarted=Game(self.game.path,{1},clock=lambda:self.now)
        restarted.start_clock()
        self.assertEqual(restarted.rows('SELECT next_tick FROM world')[0]['next_tick'],original)
        self.assertEqual(restarted.advance_due(),2)
        self.assertEqual(restarted.advance_due(),0)

    def test_pause_preserves_remainder_no_paused_backlog(self):
        self.now += 3600
        self.game.execute(1,'freeze',['1'])
        self.assertEqual(self.game.rows('SELECT paused_remaining FROM world')[0]['paused_remaining'],7200)
        self.now += 10*DAY_SECONDS
        self.assertEqual(self.game.advance_due(),0)
        restarted=Game(self.game.path,{1},clock=lambda:self.now)
        restarted.start_clock()
        self.assertEqual(restarted.rows('SELECT paused_remaining FROM world')[0]['paused_remaining'],7200)
        restarted.execute(1,'freeze',['0'])
        self.now += 7199
        self.assertEqual(restarted.advance_due(),0)
        self.now += 1
        self.assertEqual(restarted.advance_due(),1)

    def test_overdue_days_precede_player_mutation(self):
        self.now += DAY_SECONDS
        self.game.execute(2,'tax',['40'])
        # Day ended at the old 15% rate, not the just-submitted 40%.
        self.assertEqual(self.c()['treasury'],10760)
        self.assertEqual(self.c()['tax'],40)

    def test_rejected_action_does_not_rollback_elapsed_day(self):
        self.now += DAY_SECONDS
        with self.assertRaises(RuleError):self.game.execute(2,'tax',['99'])
        self.assertEqual(self.game.rows('SELECT turn FROM world')[0]['turn'],1)

    def test_clock_batches_catchup_without_drift(self):
        self.now += 130*DAY_SECONDS+50
        self.assertEqual(self.game.advance_due(),64)
        self.assertEqual(self.game.advance_due(),64)
        self.assertEqual(self.game.advance_due(),2)
        self.assertEqual(self.game.rows('SELECT next_tick FROM world')[0]['next_tick'],1000+131*DAY_SECONDS)

    def test_manual_days_disabled(self):
        with self.assertRaises(RuleError):self.game.execute(1,'turn',['0'])
        self.assertEqual(self.game.rows('SELECT turn FROM world')[0]['turn'],0)

    def test_old_database_migration_preserves_world(self):
        path=Path(self.tmp.name)/'old.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE world(id INTEGER PRIMARY KEY,turn INTEGER,frozen INTEGER)')
            db.execute('INSERT INTO world VALUES(1,17,1)')
        old=Game(path,{1},clock=lambda:self.now)
        old.start_clock()
        w=old.rows('SELECT * FROM world')[0]
        self.assertEqual(w['turn'],17)
        self.assertEqual(w['paused_remaining'],DAY_SECONDS)
        self.assertEqual(old.advance_due(),0)

if __name__=='__main__':unittest.main()
