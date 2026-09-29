from pathlib import Path
from PyPDF2 import PdfReader
path = Path(__file__).resolve().parent / '키움 REST API 문서.pdf'
reader = PdfReader(str(path))
pages = [9, 23, 27, 30, 379, 390, 393, 395]  # approximate 0-based page indices for pages 10,24,28,31 and some account pages
for idx in pages:
    print('--- PDF PAGE INDEX', idx, '---')
    text = reader.pages[idx].extract_text() or ''
    print(text[:5000])
    print()
