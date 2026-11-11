import hashlib
import json
import re
import resource
import subprocess
import sys
import tempfile
from html.parser import HTMLParser

from .errors import CorpusError, require
from .paths import bounded


PDF_WORKER = '''
import io,json,socket,sys
def denied(*args, **kwargs):
    raise RuntimeError('parser network forbidden')
socket.socket = denied
socket.create_connection = denied
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(10 * 1024 * 1024 + 1)), strict=True)
if reader.is_encrypted or len(reader.pages) > 2000:
    raise ValueError('encrypted or excessive PDF pages')
pages = []
total = 0
for page in reader.pages:
    text = page.extract_text() or ''
    total += len(text.encode('utf-8'))
    if total > 8 * 1024 * 1024:
        raise ValueError('PDF text exceeds bound')
    pages.append(text.replace('\\r\\n','\\n').replace('\\r','\\n'))
sys.stdout.write(json.dumps(pages, ensure_ascii=False))
'''


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parser_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def parse(root, path, media_type, expected_hash=None, expected_size=None):
    path = bounded(path, root)
    require(path.stat().st_size <= 10 * 1024 * 1024, 'PARSER_LIMIT', 'parser input over 10 MiB')
    content = path.read_bytes()
    original_hash = hashlib.sha256(content).hexdigest()
    require((expected_hash is None or original_hash == expected_hash) and (expected_size is None or len(content) == expected_size),
            'OBJECT_CORRUPT', 'consumed original hash/size mismatch')
    warnings = ['figures_formulas_ocr_not_extracted', 'page_coordinates_unavailable']
    page_ranges = []
    if media_type == 'application/pdf':
        import importlib.util
        require(importlib.util.find_spec('pypdf') is not None, 'PARSER_UNAVAILABLE', 'optional pypdf dependency is not installed')
        runtime = bounded(root / 'runtime', root)
        runtime.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryFile(dir=runtime) as output:
                subprocess.run([sys.executable, '-I', '-B', '-c', PDF_WORKER], input=content,
                               cwd=runtime, env={'HOME': str(root), 'TMPDIR': str(runtime)},
                               stdout=output, stderr=subprocess.DEVNULL, timeout=15, check=True,
                               preexec_fn=parser_limits)
                output.seek(0)
                pages = json.loads(output.read(16 * 1024 * 1024 + 1))
            text = ''
            for page_number, page_text in enumerate(pages, 1):
                start = len(text)
                text += page_text + '\n'
                page_ranges.append((start, len(text), page_number))
            warnings.remove('page_coordinates_unavailable')
            warnings.append('pdf_page_provenance_no_bbox')
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            raise CorpusError('PARSE_FAILED', 'isolated PDF extraction failed') from error
    else:
        require(media_type in ('text/plain', 'text/markdown', 'text/html'), 'PARSER_UNAVAILABLE', 'unsupported media type')
        try:
            text = content.decode('utf-8')
        except UnicodeError as error:
            raise CorpusError('PARSE_FAILED', 'input is not UTF-8') from error
        if media_type == 'text/html':
            parser = VisibleHTML()
            parser.feed(text)
            text = ''.join(parser.parts)
            warnings.append('html_tables_flattened')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    require(bool(text.strip()), 'PARSE_EMPTY', 'no extractable text; OCR not implemented')
    canonical_hash = hashlib.sha256(text.encode()).hexdigest()
    chunks = []
    heading = []
    position = 0
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('```'):
            fenced = not fenced
        match = re.match(r'^(#{1,6})\s+(.+)', line) if not fenced else None
        if match:
            depth = len(match.group(1))
            title = match.group(2).strip()
            if len(title) > 500 and 'heading_locator_title_truncated' not in warnings:
                warnings.append('heading_locator_title_truncated')
            heading = heading[:depth - 1] + [title[:500]]
        for offset in range(0, len(line), 800):
            segment = line[offset:offset + 800]
            if segment.strip():
                require(len(chunks) < 10000, 'PARSER_LIMIT', 'more than 10000 chunks requires a different approved pipeline')
                page_number = next((number for start, end, number in page_ranges if start <= position + offset < end), None)
                chunks.append({'text': segment, 'locator': {'page': page_number, 'bbox': None,
                               'char_start': position + offset, 'char_end': position + offset + len(segment),
                               'heading': list(heading), 'basis': 'canonical_utf8_text',
                               'canonical_hash': canonical_hash, 'original_hash': original_hash}})
        position += len(line)
    return text, canonical_hash, chunks, warnings
