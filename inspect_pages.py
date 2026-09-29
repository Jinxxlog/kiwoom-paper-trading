from pathlib import Path
from PyPDF2 import PdfReader
path = Path(__file__).resolve().parent / '키움 REST API 문서.pdf'
reader = PdfReader(str(path))
for idx in range(8, 16):
    text = reader.pages[idx].extract_text() or ''
    print('--- PDF PAGE INDEX', idx, '---')
    print(text[:3000])
    print()
