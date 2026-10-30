"""Small, bounded in-memory index. It never stores file contents or signed URLs on disk."""
from collections import defaultdict, deque
import secrets
import time


class HospitalIndex:
    def __init__(self, context_limit=80, clock=time.time):
        self.clock = clock
        self.context_limit = max(4, min(int(context_limit), 120))
        self.messages = defaultdict(lambda: deque(maxlen=self.context_limit))
        self.files = defaultdict(lambda: deque(maxlen=64))
        self.images = defaultdict(lambda: deque(maxlen=8))

    def record_message(self, group, message_id, sender, text, kind='human'):
        if not text:
            return
        queue = self.messages[str(group)]
        if message_id:
            old=next((x for x in queue if x['message_id']==str(message_id)),None)
            if old:
                if kind=='bot' and old.get('kind')=='bot':old.update(text=str(text)[:600],at=self.clock())
                return
        queue.append(dict(message_id=str(message_id), sender=str(sender)[:80], text=str(text)[:1200], kind=kind, at=self.clock()))

    def recent(self, group, limit=12):
        return list(self.messages[str(group)])[-max(1,min(int(limit),self.context_limit)):]

    def resize(self, limit):
        self.context_limit = max(4,min(int(limit),120))
        for group,rows in list(self.messages.items()):
            self.messages[group] = deque(rows,maxlen=self.context_limit)

    def add_file(self, group, name, *, file_id='', busid=None, url='', size=None, message_id=''):
        queue = self.files[str(group)]
        size = int(size) if size is not None else None
        for x in queue:
            if (file_id and x['file_id'] == str(file_id)) or (url and x['url'] == url):
                x.update(name=str(name)[:200], file_id=str(file_id) or x['file_id'], busid=busid if busid is not None else x['busid'], url=url or x['url'], at=self.clock())
                if size is not None:x['size']=size
                return x
        record = dict(file_ref=secrets.token_hex(6), name=str(name)[:200], file_id=str(file_id), busid=busid, url=str(url), size=size, message_id=str(message_id), at=self.clock())
        queue.append(record)
        return record

    def list_files(self, group, query=''):
        now=self.clock()
        return [self.public(x) for x in self.files[str(group)] if now-x['at'] <= 86400 and query.casefold() in x['name'].casefold()][-16:]

    def get_file(self, group, ref):
        return next((x for x in self.files[str(group)] if x['file_ref'] == ref and self.clock()-x['at'] <= 86400), None)

    def add_image(self, group, message_id, url):
        if not url or not str(url).startswith(('https://','http://')):return
        queue=self.images[str(group)]
        if not any(x['url']==url and x['message_id']==str(message_id) for x in queue):
            queue.append({'message_id':str(message_id),'url':url,'at':self.clock()})

    def context_images(self,group,message_ids):
        return [x['url'] for x in self.images[str(group)] if x['message_id'] in message_ids and self.clock()-x['at']<=1800][-2:]

    @staticmethod
    def public(x):
        return {k:x[k] for k in ('file_ref','name','size','message_id')}
