import asyncio


async def member_ids(client, chat_id, stop=None):
    ids = []
    seen_ids = set()
    seen_markers = set()
    marker = None
    for _ in range(2000):
        if stop and stop.is_set():
            raise ValueError('Задание остановлено до завершения проверки участников')
        members, next_marker = await asyncio.wait_for(client.get_chat_members(chat_id, marker=marker, count=100), 45)
        for member in members:
            identity = member.contact.id
            if identity not in seen_ids:
                ids.append(identity)
                seen_ids.add(identity)
        if not next_marker:
            return ids
        if next_marker in seen_markers:
            raise ValueError('MAX повторяет страницу участников. Полная проверка не завершена')
        seen_markers.add(next_marker)
        marker = next_marker
    raise ValueError('Слишком много страниц участников. Проверка не завершена')


async def collect_contacts(client, source, count, existing, own_id, stop, emit):
    candidates = await member_ids(client, source, stop)
    known = set(existing)
    added = 0
    skipped = 0
    for identity in candidates:
        if stop.is_set() or added >= count:
            break
        if identity in known or identity == own_id:
            skipped += 1
            continue
        result = await asyncio.wait_for(client.add_contact(identity), 45)
        if result is None or result.id != identity:
            raise ValueError(f'Добавлено {added}. MAX не подтвердил следующий контакт; задание остановлено')
        known.add(identity)
        added += 1
        emit('contact_added', identity)
        emit('progress', f'Добавлено контактов: {added} / {count}. Пропущено: {skipped}')
        if added < count and not stop.is_set():
            await asyncio.sleep(1)
    return f'Контакты: добавлено {added} / {count}; пропущено {skipped}.' + (' Задание остановлено.' if stop.is_set() else '')


async def invite_contacts(client, target, count, contacts, own_id, stop, emit):
    present = set(await member_ids(client, target, stop))
    candidates = list(dict.fromkeys(contacts))
    skipped = sum(identity in present or identity == own_id for identity in candidates)
    confirmed = 0
    for identity in candidates:
        if stop.is_set() or confirmed >= count:
            break
        if identity in present or identity == own_id:
            continue
        await asyncio.wait_for(client.invite_users_to_group(target, [identity], show_history=False), 45)
        # A successful API response alone does not prove membership.
        refreshed = set(await member_ids(client, target))
        if identity not in refreshed:
            raise ValueError(f'Подтверждено {confirmed}. Следующий участник не появился в группе; задание остановлено')
        present = refreshed
        confirmed += 1
        emit('progress', f'В группе подтверждено: {confirmed} / {count}. Уже в группе или свой аккаунт: {skipped}')
        if confirmed < count and not stop.is_set():
            await asyncio.sleep(1)
    return f'Приглашения: подтверждено {confirmed} / {count}; исключено {skipped}.' + (' Задание остановлено.' if stop.is_set() else '')
