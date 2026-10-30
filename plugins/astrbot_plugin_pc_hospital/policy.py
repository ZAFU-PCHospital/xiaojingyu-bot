DISCLAIMER='AI 建议仅供参考。'

def with_disclaimer(text):
    text=text.rstrip()
    return text if not text or text.endswith(DISCLAIMER) else text+'\n\n'+DISCLAIMER
