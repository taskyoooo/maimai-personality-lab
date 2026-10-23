import importlib.util, unittest, time, json
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
spec=importlib.util.spec_from_file_location('lab',Path(__file__).resolve().parents[1]/'scripts/lab.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)

class LabTests(unittest.TestCase):
    def setUp(self):
        self.old=lab.STATE.copy();lab.STATE.update(token='',tools=[],session=None,records={},catalog=[])
    def tearDown(self): lab.STATE.clear();lab.STATE.update(self.old)
    def test_budget_and_expiry_and_exclusion(self):
        items=[{'id':'a','name':'A','cents':3000,'tags':['classic'],'source':'live','at':100}, {'id':'b','name':'B','cents':3001,'tags':['classic'],'source':'live','at':100}, {'id':'c','name':'C','cents':100,'tags':['spicy'],'source':'live','at':100}]
        r=lab.recommend({'budget':'30','mood':'classic','avoid':['spicy']},items,now=200)
        self.assertEqual([x['id'] for x in r['items']],['a'])
        self.assertEqual(lab.recommend({'budget':30},items,now=1001)['items'],[])
    def test_money(self):
        self.assertEqual(lab.money('29.99'),2999)
        for bad in ('nan','Infinity',-1,True,'abc'):
            with self.assertRaises(ValueError):lab.money(bad)
    def test_pointer_and_candidate(self):
        record={'id':'r','tool':'query-meals','at':time.time(),'decoded':[{'menu':[{'name':'餐品','price':1290}]}]}
        lab.STATE['records']['r']=record
        item=lab.candidate({'recordId':'r','namePath':'/decoded/0/menu/0/name','pricePath':'/decoded/0/menu/0/price','priceUnit':'fen','tags':['classic']})
        self.assertEqual(item['cents'],1290);self.assertEqual(item['name'],'餐品')
        with self.assertRaises(KeyError):lab.candidate({'recordId':'r','namePath':'/invented','pricePath':'/price'})
    def test_mutation_blocked_before_network(self):
        for name in ('create-order','auto-bind-coupons','draw-lottery','unknown'):
            with self.assertRaises(ValueError):lab.call_tool(name,{})
    def test_undiscovered_tool_blocked(self):
        with self.assertRaises(ValueError):lab.call_tool('query-meals',{})
    def test_discovery_pagination(self):
        results=[{'protocolVersion':'2025-03-26'}, {}, {'tools':[{'name':'query-meals'}],'nextCursor':'next'}, {'tools':[{'name':'calculate-price'}]}]
        with patch.object(lab,'rpc',side_effect=results) as rpc:
            r=lab.discover();self.assertEqual(len(r['tools']),2)
            self.assertEqual(rpc.call_args_list[-1].args,('tools/list',{'cursor':'next'}))
    def test_tool_error_not_evidence(self):
        lab.STATE['tools']=[{'name':'query-meals'}]
        with patch.object(lab,'rpc',return_value={'isError':True}):
            with self.assertRaises(ValueError):lab.call_tool('query-meals',{})
        self.assertEqual(lab.STATE['records'],{})
    def test_decode_tool_text_json(self):
        lab.STATE['tools']=[{'name':'query-meals'}]
        with patch.object(lab,'rpc',return_value={'content':[{'type':'text','text':'{"menu":[]}'}]}):
            r=lab.call_tool('query-meals',{});self.assertEqual(r['decoded'],[{'menu':[]}])
    def test_card_escape_and_demo_label(self):
        r=lab.recommend({'budget':30},lab.DEMO);r['persona']='<script>&'
        svg=lab.card(r)
        self.assertNotIn('<script>',svg);self.assertIn('&lt;script&gt;&amp;',svg);self.assertIn('演示虚构数据',svg)
    def test_empty_no_fake_recommendations(self):
        r=lab.recommend({'budget':0},lab.DEMO)
        self.assertFalse(r['items']);self.assertTrue(r['empty'])

if __name__=='__main__':unittest.main()
