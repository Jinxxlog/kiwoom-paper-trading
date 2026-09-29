from pathlib import Path
from PyPDF2 import PdfReader

path = Path(__file__).resolve().parent / '키움 REST API 문서.pdf'
reader = PdfReader(str(path))
keywords = ['ka00001', 'ka10004', 'ka10006', 'ka10005', 'kt00001', 'kt00007', 'kt00009', 'au10001']

for i, page in enumerate(reader.pages):
    text = page.extract_text() or ''
    found = [k for k in keywords if k in text]
    if found:
        print('--- PAGE', i+1, '---', found)
        print(text[:4000])
        print()
