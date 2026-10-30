"""Select public webpages without treating rendered QQ attachments as articles."""
import re
from urllib.parse import urlsplit

from .core import public_url

URLS = re.compile(r'https://[^\s<>"“”]+')

def summary_candidates(text):
    links=[];seen=set();skipped=set()
    for count,match in enumerate(URLS.finditer(text)):
        if count>=32:break
        raw=match.group()
        prefix=text[text.rfind('\n',0,match.start())+1:match.start()]
        try:
            p=urlsplit(raw)
            attachment=(p.hostname=='multimedia.nt.qq.com.cn' and p.path.rstrip('/')=='/download')
            # Official merged messages render file metadata as Plain text.
            attachment=attachment or bool(re.match(r'\s*\[附件\d+\].*\bURL:\s*$',prefix))
            if attachment:
                skipped.add('attachment');continue
            canonical=public_url(raw)
        except ValueError:
            skipped.add('restricted');continue
        if canonical in seen:continue
        seen.add(canonical);links.append(canonical)
        if len(links)==2:break
    return links,skipped

def explicit_link_summary(text,addressed):
    if not addressed:return False
    # A forwarded transcript is material for ordinary chat, not a link command.
    if re.search(r'\[[^\n\]]*的聊天记录\]\s*\n=== 消息',text):return False
    words=URLS.sub('',text)
    return bool(re.search(r'摘要|总结|概括',words))
