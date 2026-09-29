from pathlib import Path
from PyPDF2 import PdfReader
path = Path(__file__).resolve().parent / '키움 REST API 문서.pdf'
reader = PdfReader(str(path))
for idx in range(466, 487):
    text = reader.pages[idx].extract_text() or ''
    if 'API 정보' in text or 'ka00001' in text or 'url' in text.lower() or 'URL' in text:
        print('--- PDF PAGE INDEX', idx, '---')
        print(text[:5000])
        print()
