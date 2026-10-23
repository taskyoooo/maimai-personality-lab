#!/usr/bin/env python3
"""Local-only McDonald's MCP preference lab. Python 3.10+, no dependencies."""
import json, os, secrets, time, threading, ssl, unicodedata
from pathlib import Path
from decimal import Decimal, InvalidOperation
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
# macOS python.org builds may lack a bundled CA file; use the system CA bundle.
SYSTEM_CA = Path('/etc/ssl/cert.pem')
TLS_CONTEXT = ssl.create_default_context(cafile=str(SYSTEM_CA)) if SYSTEM_CA.exists() else ssl.create_default_context()
READ_TOOLS = {'now-time-info','campaign-calendar','list-nutrition-foods','query-nearby-stores','query-meals','query-meal-detail','calculate-price'}
SESSION = secrets.token_urlsafe(32)
STATE = {'token':os.environ.get('MCD_MCP_TOKEN',''), 'tools':[], 'session':None, 'protocol':'2025-03-26', 'records':{}, 'catalog':[]}
LOCK = threading.RLock()

def money(value):
    if isinstance(value, bool): raise ValueError('金额格式错误')
    try: n = Decimal(str(value))
    except InvalidOperation: raise ValueError('金额必须为数字（元）')
    if not n.is_finite() or n < 0 or n > 100000: raise ValueError('金额超出范围')
    return int((n*100).quantize(Decimal('1')))

def pointer(doc, path):
    if path == '': return doc
    if not isinstance(path,str) or not path.startswith('/'): raise ValueError('使用 JSON Pointer 路径，例如 /content/0/text')
    for k in path[1:].split('/'):
        k = k.replace('~1','/').replace('~0','~')
        doc = doc[int(k)] if isinstance(doc,list) else doc[k]
    return doc

def rpc(method, params=None, notify=False):
    if not STATE['token']: raise ValueError('请先在本机输入 MCP Token')
    body = {'jsonrpc':'2.0','method':method}
    if not notify: body['id'] = secrets.randbelow(10**9)
    if params is not None: body['params'] = params
    headers = {'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':'Bearer '+STATE['token']}
    if STATE['session']: headers['Mcp-Session-Id']=STATE['session']
    if method != 'initialize': headers['MCP-Protocol-Version']=STATE['protocol']
    try:
        with urlopen(Request('https://mcp.mcd.cn',data=json.dumps(body).encode(),headers=headers),timeout=40,context=TLS_CONTEXT) as r:
            if r.headers.get('Mcp-Session-Id'): STATE['session']=r.headers['Mcp-Session-Id']
            raw = r.read(4*1024*1024).decode()
            kind = r.headers.get('Content-Type','')
    except HTTPError as e: raise ValueError('MCP HTTP '+str(e.code)+'；请核对 Token、网络或服务状态') from None
    if notify or not raw: return {}
    if 'text/event-stream' in kind:
        messages=[]
        for event in raw.replace('\r\n','\n').split('\n\n'):
            data='\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
            if data:
                try: messages.append(json.loads(data))
                except json.JSONDecodeError: pass
        payload=next((v for v in messages if v.get('id')==body['id']),None)
        if payload is None: raise ValueError('MCP SSE 中缺少对应响应')
    else: payload=json.loads(raw)
    if 'error' in payload: raise ValueError('MCP 返回 JSON-RPC 错误；请按已发现的 schema 检查参数')
    return payload.get('result',{})

def discover():
    STATE['session']=None
    init=rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'maimai-personality-lab','version':'1.0.0'}})
    STATE['protocol']=init.get('protocolVersion','2025-03-26')
    rpc('notifications/initialized',notify=True)
    tools=[]; cursor=None
    for _ in range(20):
        result=rpc('tools/list',{'cursor':cursor} if cursor else {})
        tools.extend(result.get('tools',[])); cursor=result.get('nextCursor')
        if not cursor: break
    STATE['tools']=tools
    return {'tools':tools,'readOnlyAllowed':sorted(READ_TOOLS),'protocol':STATE['protocol']}

def call_tool(name,args):
    if name not in READ_TOOLS: raise ValueError('仅开放白名单查询工具；不下单、不领券、不扣积分')
    if name not in {t['name'] for t in STATE['tools']}: raise ValueError('工具尚未被 tools/list 实际发现')
    if not isinstance(args,dict): raise ValueError('arguments 必须为 JSON 对象')
    result=rpc('tools/call',{'name':name,'arguments':args})
    if result.get('isError'): raise ValueError('工具报告执行失败；未生成有效证据')
    # Decode text JSON for explicit, inspectable paths. Never infer undocumented field names.
    decoded=[]
    for item in result.get('content',[]):
        if item.get('type')=='text':
            try: decoded.append(json.loads(item.get('text','')))
            except json.JSONDecodeError: pass
    rid=secrets.token_hex(6)
    record={'id':rid,'tool':name,'at':time.time(),'result':result,'decoded':decoded}
    STATE['records'][rid]=record
    return record

def candidate(data):
    record=STATE['records'].get(data.get('recordId'))
    if not record or record['tool'] not in {'query-meals','query-meal-detail','calculate-price'}: raise ValueError('请选择真实菜单或试算记录')
    if time.time()-record['at']>900: raise ValueError('记录已超过15分钟，请重新查询')
    name=pointer(record,data['namePath']); value=pointer(record,data['pricePath'])
    if not isinstance(name,str) or not name.strip() or len(name)>100: raise ValueError('名称路径必须指向餐品名称')
    unit=data.get('priceUnit','yuan')
    if unit not in ('yuan','fen'): raise ValueError('请按工具字段说明确认元或分')
    cents=money(value) if unit=='yuan' else money(Decimal(str(value))/100)
    tags=data.get('tags',[])
    if not isinstance(tags,list) or any(t not in ('spicy','classic','fresh','sweet','coffee') for t in tags): raise ValueError('口味标签无效')
    item={'id':secrets.token_hex(5),'name':name,'cents':cents,'tags':tags,'source':'live','recordId':record['id'],'at':record['at'],'note':'标签由使用者按餐品描述判断；价格范围以原始记录为准'}
    STATE['catalog'].append(item)
    return item

def recommend(profile,items,now=None):
    now=time.time() if now is None else now
    budget=money(profile.get('budget',30))
    mood=profile.get('mood','classic')
    if mood not in ('spicy','classic','fresh','sweet','coffee'): raise ValueError('请选择口味')
    avoid=profile.get('avoid',[])
    if not isinstance(avoid,list): raise ValueError('忌口格式错误')
    out=[]
    for item in items:
        if item['source']=='live' and now-item['at']>900: continue
        if item['cents']>budget or any(tag in item['tags'] for tag in avoid): continue
        out.append({**item,'score':(100 if mood in item['tags'] else 0)+int(30*(budget-item['cents'])/max(budget,1)), 'reason':('符合你的口味偏好' if mood in item['tags'] else '预算内的换口味选择')+'；余量 ¥'+format((budget-item['cents'])/100,'.2f')})
    out.sort(key=lambda x:(-x['score'],x['cents'],x['name']))
    personas={'spicy':'热烈探索家','classic':'经典守护者','fresh':'清爽漫游者','sweet':'甜心收藏家','coffee':'灵感续航员'}
    return {'persona':personas[mood],'budget':budget,'items':out[:3],'notice':'娱乐性口味画像，不是心理测评。忌口标签筛选不能替代官方过敏原核验。','empty':'暂无预算内且符合条件的候选，请增加真实报价或调整条件。' if not out else ''}

DEMO=[{'id':'demo1','name':'演示·经典灵感组合','cents':2500,'tags':['classic'],'source':'demo','at':0},{'id':'demo2','name':'演示·热烈灵感组合','cents':2900,'tags':['spicy'],'source':'demo','at':0},{'id':'demo3','name':'演示·咖啡灵感组合','cents':1800,'tags':['coffee'],'source':'demo','at':0}]

def wrap_card_line(value, limit=54):
    # Approximate glyph widths; keep Chinese closing punctuation off line starts.
    value=''.join(c for c in value if ord(c)>=32)
    rows=[]; line=''; width=0
    closing='，。、；：？！）》」』】〕〉”’.,:;?!)]}'
    opening='（《「『【〔〈“‘([{' 
    for c in value:
        w=2 if unicodedata.east_asian_width(c) in ('W','F') else 1
        if width+w>limit and line and c not in closing:
            tail=''
            while line and line[-1] in opening:
                tail=line[-1]+tail;line=line[:-1]
            rows.append(line);line=tail; width=sum(2 if unicodedata.east_asian_width(x) in ('W','F') else 1 for x in tail)
        line+=c;width+=w
    if line:rows.append(line)
    return rows

def card(result):
    lines=[result['persona'],'预算 ¥'+format(result['budget']/100,'.2f')]
    lines += [x['name']+' · ¥'+format(x['cents']/100,'.2f') for x in result['items']]
    source='演示虚构数据 · 非官方报价' if any(x['source']=='demo' for x in result['items']) else '真实查询候选 · 价格及供应以官方实时结果为准'
    lines += [result['empty']] if result['empty'] else []
    lines += [source,'麦麦人格实验室 · 娱乐口味画像 · 非官方产品']
    lines=[row for line in lines for row in wrap_card_line(line)]
    height=max(640,200+len(lines)*44)
    texts=''.join(f'<text x="48" y="{132+i*44}" font-size="{32 if i==0 else 20}">{escape(s)}</text>' for i,s in enumerate(lines))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" viewBox="0 0 760 {height}"><rect width="760" height="{height}" rx="32" fill="#fff5d6"/><rect x="28" y="28" width="704" height="{height-56}" rx="24" fill="none" stroke="#b71e2d" stroke-width="3"/><g font-family="sans-serif" fill="#421f1a"><text x="48" y="72" font-size="18">MAIMAI / PERSONALITY LAB</text>{texts}</g></svg>'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass # Never log credentials, bodies or user data.
    def send(self,status,data,kind='application/json'):
        raw=data.encode() if isinstance(data,str) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type',kind+'; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.end_headers(); self.wfile.write(raw)
    def valid_host(self): return self.headers.get('Host') in ('127.0.0.1:8765','localhost:8765')
    def do_GET(self):
        if not self.valid_host(): return self.send(403,{'error':'Host rejected'})
        if self.path=='/':
            return self.send(200,(ROOT/'web/index.html').read_text().replace('__SESSION__',SESSION),'text/html')
        self.send(404,{'error':'Not found'})
    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Lab-Session')!=SESSION or self.headers.get('Origin') not in ('http://127.0.0.1:8765','http://localhost:8765'): return self.send(403,{'error':'Local session required'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size>100000: raise ValueError('请求过大')
            data=json.loads(self.rfile.read(size))
            with LOCK:
                if self.path=='/api/connect':
                    token=data.get('token','').strip()
                    if not token: raise ValueError('Token 不能为空')
                    STATE.update(token=token,tools=[],records={},catalog=[])
                    result=discover()
                elif self.path=='/api/discover': result=discover()
                elif self.path=='/api/call': result=call_tool(data['name'],data.get('arguments',{}))
                elif self.path=='/api/candidate': result=candidate(data)
                elif self.path=='/api/recommend': result=recommend(data['profile'],DEMO if data.get('demo') else STATE['catalog'])
                elif self.path=='/api/card':
                    result=recommend(data['profile'],DEMO if data.get('demo') else STATE['catalog'])
                    return self.send(200,card(result),'image/svg+xml')
                elif self.path=='/api/disconnect':
                    STATE.update(token='',tools=[],session=None,records={},catalog=[]); result={'disconnected':True}
                else: return self.send(404,{'error':'Not found'})
            self.send(200,result)
        except (ValueError,KeyError,IndexError,TypeError) as e: self.send(400,{'error':str(e) if isinstance(e,ValueError) else '字段或路径无效'})
        except Exception: self.send(502,{'error':'连接暂不可用，请检查网络或稍后重试；未记录敏感响应'})

if __name__=='__main__':
    print('麦麦人格实验室：http://127.0.0.1:8765 （仅本机，Token 只保存在内存）',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
