"""Local-only import of group invitation links. Never evaluates spreadsheet formulas."""
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.formula.tokenizer import Tokenizer, TokenizerError


MAX_ROWS = 50000
MAX_CELLS = 500000
LINK_STATES = {
    'queued': 'В очереди', 'confirmed': 'Вступил', 'skipped': 'Уже состоит / в истории',
    'unavailable': 'Недоступна', 'pending': 'Не подтверждено',
    'retryable': 'Ошибка, повтор вручную',
    'global_skip': 'В общем реестре', 'reserved_skip': 'Закреплено другим заданием',
}


def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def normalize_link(value):
    text = str(value or '').strip()
    if any(c.isspace() or ord(c) < 32 for c in text):
        return None
    if text.startswith(('max.ru/', 'web.max.ru/')):
        text = 'https://' + text
    try:
        url = urlsplit(text)
        if (url.scheme not in ('http', 'https') or url.hostname not in ('max.ru', 'web.max.ru')
                or url.username or url.password or url.port is not None or url.query or url.fragment):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r'/join/[A-Za-z0-9_-]{1,512}/?', url.path):
        return None
    return 'https://max.ru' + url.path.rstrip('/')


@dataclass
class LinkImport:
    entries: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    duplicates: int = 0


def formula_link(value):
    if not isinstance(value, str) or not value.startswith('='):
        return None
    try:
        tokens = Tokenizer(value).items
        if (len(tokens) >= 3 and tokens[0].type == 'FUNC'
                and tokens[0].value.upper() == 'HYPERLINK('
                and tokens[1].type == 'OPERAND' and tokens[1].subtype == 'TEXT'
                and tokens[2].type in ('SEP', 'FUNC')):
            return tokens[1].value[1:-1].replace('""', '"')
    except TokenizerError:
        pass
    return None


def file_rows(path):
    if path.stat().st_size > 30 * 1024 * 1024:
        raise ValueError('Файл больше 30 МБ')
    if path.suffix.lower() == '.xlsx':
        with ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError('Слишком большой распакованный Excel-файл')
        # Normal mode retains hyperlink targets; bounded archive and cell counts limit memory.
        book = load_workbook(path, data_only=False, keep_links=False)
        try:
            if sum(s.max_row * s.max_column for s in book) > MAX_CELLS:
                raise ValueError('В Excel слишком много ячеек (максимум 500 000)')
            for sheet in book:
                for row in sheet.iter_rows():
                    yield sheet.title, row[0].row, [
                        (str(c.value or ''), c.hyperlink.target if c.hyperlink else formula_link(c.value))
                        for c in row]
        finally:
            book.close()
    elif path.suffix.lower() in ('.csv', '.txt'):
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            text = path.read_text(encoding='cp1251')
        if path.suffix.lower() == '.csv':
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
            except csv.Error:
                dialect = csv.excel
            rows = csv.reader(text.splitlines(), dialect)
        else:
            rows = ([line] for line in text.splitlines())
        for index, row in enumerate(rows, 1):
            yield '', index, [(cell, None) for cell in row]
    else:
        raise ValueError('Поддерживаются XLSX, CSV и TXT')


def read_links(filename):
    path = Path(filename)
    result, seen, columns = LinkImport(), set(), {}
    rows = file_rows(path)
    try:
        for sheet, number, cells in rows:
            if number > MAX_ROWS:
                raise ValueError('В файле больше 50 000 строк на лист')
            title = ''
            for value, _ in cells:
                if value.strip() and not value.startswith(('=', 'http', 'max.ru/', 'web.max.ru/')):
                    title = value.strip()[:250]
                    break
            header = next((i for i, (v, _) in enumerate(cells)
                           if v.strip().casefold() in ('ссылка', 'ссылка на чат', 'ссылка на группу', 'url', 'link')), None)
            if header is not None:
                columns[sheet] = header
                continue
            indexes = [columns[sheet]] if sheet in columns else range(len(cells))
            for index in indexes:
                if index >= len(cells):
                    continue
                value, target = cells[index]
                raw = target or value.strip()
                if not raw:
                    continue
                location = f'{sheet}!{number}' if sheet else str(number)
                candidates = [raw] if target or sheet in columns else re.findall(
                    r'(?:https?://|(?:web\.)?max\.ru/)[^\s<>"\)]+', raw)
                if not candidates and path.suffix.lower() == '.txt':
                    candidates = [raw]
                for candidate in candidates:
                    link = normalize_link(candidate)
                    if not link:
                        result.invalid.append({'link': candidate[:600], 'source': location})
                    elif link in seen:
                        result.duplicates += 1
                    else:
                        seen.add(link)
                        result.entries.append({'link': link, 'title': title or 'Группа MAX', 'source': location})
                    if len(result.entries) + len(result.invalid) + result.duplicates > MAX_ROWS:
                        raise ValueError('В файле больше 50 000 ссылок')
    except (ValueError, OSError):
        raise
    except Exception as error:
        raise ValueError('Не удалось прочитать файл. Проверьте формат и отсутствие пароля.') from error
    finally:
        rows.close()
    return result
