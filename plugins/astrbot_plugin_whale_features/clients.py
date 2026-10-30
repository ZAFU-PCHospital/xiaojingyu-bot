import asyncio
import ipaddress
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .core import public_url

class Web:
    def __init__(self):self.session=None
    async def http(self):
        if self.session is None or self.session.closed:
            self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=22),connector=aiohttp.TCPConnector(limit=4),headers={'User-Agent':'PCHospital-Whale/0.1 (read-only)'})
        return self.session
    async def request(self,method,url,*,params=None,body=None,headers=None):
        session=await self.http()
        async with session.request(method,url,params=params,json=body,headers=headers,allow_redirects=False) as r:
            if not 200<=r.status<300:raise RuntimeError('上游HTTP '+str(r.status))
            chunks=[]; size=0
            async for chunk in r.content.iter_chunked(16384):
                size+=len(chunk)
                if size>1024*1024:raise ValueError('上游内容超过1MiB。')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))
    async def verify_public(self,url):
        url=public_url(url)
        infos=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(urlsplit(url).hostname,443,type=socket.SOCK_STREAM),8)
        if not infos or any(not ipaddress.ip_address(x[4][0]).is_global for x in infos):raise ValueError('不读取内网地址。')
        return url
    async def search_api(self,tool,arguments):
        path=Path('data/config/astrbot_plugin_anysearch_config.json')
        cfg=json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
        key=str(cfg.get('api_key','')).strip()
        result=await self.request('POST','https://api.anysearch.com/mcp',body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':arguments}},headers={'Authorization':'Bearer '+key} if key else {})
        if result.get('error') or result.get('result',{}).get('isError'):raise RuntimeError('搜索/正文提取未成功。')
        content=result.get('result',{}).get('content',[])
        text='\n'.join(str(x.get('text','')) for x in content if isinstance(x,dict) and x.get('type')=='text')
        if not text.strip():raise RuntimeError('没有取得可用正文。')
        return text[:16000]
    async def extract(self,url):
        url=await self.verify_public(url)
        return await self.search_api('extract',{'url':url})
    async def hardware(self,kind,query):
        if kind not in ('cpu','gpu') or not query.strip() or len(query)>100:raise ValueError('请给出CPU/GPU型号。')
        # No speculative parameters or static ranking masquerading as live data.
        terms='site:intel.com OR site:amd.com OR site:nvidia.com OR site:techpowerup.com'
        return await self.search_api('search',{'query':f'{query} {kind} specifications 参数 {terms}','max_results':5})
    async def steam_search(self,term):
        data=await self.request('GET','https://store.steampowered.com/api/storesearch/',params={'term':term[:100],'l':'schinese','cc':'cn'})
        return [{'appid':x['id'],'name':x['name'],'price':x.get('price')} for x in data.get('items',[])[:5]]
    async def steam(self,appid):
        appid=int(appid)
        if not 1<=appid<=99999999:raise ValueError('AppID无效。')
        data=await self.request('GET','https://store.steampowered.com/api/appdetails',params={'appids':appid,'cc':'cn','l':'schinese'})
        row=data.get(str(appid)) or {}
        if not row.get('success'):raise ValueError('中国区无法取得该游戏，未改用其他币种。')
        d=row['data']; price=d.get('price_overview') or {}; release=d.get('release_date') or {}
        return {'appid':appid,'name':d['name'],'currency':price.get('currency'),'price':0 if d.get('is_free') else price.get('final'),'formatted':'免费' if d.get('is_free') else price.get('final_formatted','未公布售价'),'discount':price.get('discount_percent',0),'released':not release.get('coming_soon',False),'release_date':release.get('date'),'url':f'https://store.steampowered.com/app/{appid}/'}
    async def close(self):
        if self.session:await self.session.close()
