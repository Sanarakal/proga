"""Read-only MAX message import, bounded downloads, atomic local persistence."""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp

from workspace_posts import MAX_MEDIA_BYTES, MAX_PHOTOS, validate_content


def media_url(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('MAX вернул неподдерживаемый адрес вложения.')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('Адрес вложения не является публичным.')
    return url


class PublicResolver(aiohttp.ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        result = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(item['host']).is_global for item in result):
            raise ValueError('Адрес вложения не является публичным.')
        return result


async def download_media(url, maximum=MAX_MEDIA_BYTES):
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=30)
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            for _ in range(6):
                url = media_url(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get('Location', ''))
                        continue
                    if response.status != 200:
                        raise ValueError('Вложение недоступно для скачивания. Пост не импортирован.')
                    if response.content_length is not None and response.content_length > maximum:
                        raise ValueError('Вложение превышает допустимый размер (50 МБ).')
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        if len(data) + len(chunk) > maximum:
                            raise ValueError('Вложение превышает допустимый размер (50 МБ).')
                        data.extend(chunk)
                    return bytes(data)
            raise ValueError('Слишком много перенаправлений при загрузке вложения.')
    except (aiohttp.ClientError, TimeoutError, OSError):
        # Download URLs may contain signed credentials; never expose them in logs.
        raise ValueError('Не удалось скачать вложение. Проверьте соединение и повторите импорт.') from None


def message_content(message):
    elements = []
    for entity in message.elements or []:
        raw = entity.model_dump(mode='json', by_alias=True, exclude_none=True)
        if 'from_' in raw:
            raw['from'] = raw.pop('from_')
        elements.append(raw)
    return validate_content(dict(text=message.text or '', elements=elements, photos=[]), ready=False)


def attachment_kind(attachment):
    return str(getattr(attachment.type, 'value', attachment.type))


@dataclass(frozen=True)
class ImportedPost:
    identity: str
    keyboard_omitted: bool


def check_message(message):
    content = message_content(message)
    unsupported = {attachment_kind(item) for item in (message.attaches or [])} - {'PHOTO', 'VIDEO', 'INLINE_KEYBOARD'}
    if unsupported:
        raise ValueError('Сообщение содержит неподдерживаемые вложения: ' + ', '.join(sorted(unsupported)) + '. Импорт без потери этих вложений пока недоступен.')
    media = [item for item in message.attaches or [] if attachment_kind(item) in ('PHOTO', 'VIDEO')]
    if len(media) > MAX_PHOTOS:
        raise ValueError(f'В сообщении больше {MAX_PHOTOS} вложений.')
    if not content['text'].strip() and not media:
        raise ValueError('Сообщение не содержит текста, фото или видео.')
    return content


async def import_message(engine, account, chat, message_id, title):
    client = engine.clients[account]
    message = await engine.request(account, client.get_message, chat, message_id)
    if not message or message.id != message_id or (message.chat_id is not None and message.chat_id != chat):
        raise ValueError('Исходное сообщение не найдено.')
    content = check_message(message)
    media = []
    total = 0
    for attachment in message.attaches or []:
        kind = attachment_kind(attachment)
        if kind == 'INLINE_KEYBOARD':
            continue
        if kind == 'PHOTO':
            url = attachment.base_url
        else:
            video = await engine.request(account, client.get_video_by_id, chat, message.id, attachment.video_id)
            if not video or not video.url:
                raise ValueError('MAX не предоставил файл видео. Пост не импортирован.')
            url = video.url
        data = await download_media(url)
        total += len(data)
        if total > 128 * 1024 * 1024:
            raise ValueError('Вложения поста превышают 128 МБ.')
        prepared = await asyncio.to_thread(engine.store.prepare_import_media, data, kind)
        media.append(prepared)
        content['photos'].append(prepared['id'])
    # Cancellation before the commit leaves no partially imported post or media.
    await asyncio.sleep(0)
    identity = engine.store.save_imported_post(title, content, media)
    return ImportedPost(identity, any(attachment_kind(a) == 'INLINE_KEYBOARD' for a in message.attaches or []))


async def message_history(engine, account, chat, before=None):
    client = engine.clients[account]
    kwargs = dict(backward=40)
    if before is not None:
        kwargs['from_'] = before
    messages = await engine.request(account, client.fetch_history, chat, **kwargs)
    return sorted(messages or [], key=lambda item: (item.time, item.id), reverse=True)


async def message_thumbnails(message):
    result = []
    for attachment in (message.attaches or [])[:MAX_PHOTOS]:
        kind = attachment_kind(attachment)
        url = getattr(attachment, 'base_url', None) if kind == 'PHOTO' else getattr(attachment, 'thumbnail', None) if kind == 'VIDEO' else None
        if not url:
            continue
        try:
            data = await download_media(url, maximum=10 * 1024 * 1024)
            from workspace_posts import PostStore
            prepared = await asyncio.to_thread(PostStore.prepare_import_media, data, 'PHOTO')
            result.append((kind, prepared['thumbnail']))
        except (ValueError, OSError):
            result.append((kind, None))
    return message.id, result
