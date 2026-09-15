import asyncio
import concurrent.futures
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
import os
import sys
import qrcode
import customtkinter as ctk
from datetime import datetime

from pymax import Client, WebClient
from pymax.auth import QrAuthFlow
from pymax.exceptions import ApiError
from workflows import member_ids, collect_contacts, invite_contacts

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("green")


def api_error_text(error):
    # Never display raw API payloads, tokens, or unredacted phone numbers.
    detail = error.localized_message or error.message or "MAX отклонил запрос"
    detail = re.sub(r'https?://\S+', '[ссылка скрыта]', str(detail))
    detail = re.sub(r'\+?\d[\d ()-]{7,}\d', '[номер скрыт]', detail)
    detail = re.sub(r'(?i)(token|password|code|authorization)\s*[:=]\s*\S+', r'\1=[скрыто]', detail)
    code = str(error.error or "unknown")
    code = code if re.fullmatch(r'[a-zA-Z_.-]{1,80}', code) else "unknown"
    return f"MAX: {detail[:300]}\nКод: {code}; операция: {error.opcode}. Повторный запрос автоматически не отправляется."


def create_client(qr=False, **kwargs):
    # PyMax requires stderr even in a PyInstaller windowed build.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return (WebClient if qr else Client)(**kwargs)


def qr_image_data(url):
    code = qrcode.QRCode(box_size=1, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    scale = max(1, 360 // len(matrix))
    width = len(matrix) * scale
    pixels = bytearray()
    for row in matrix:
        line = b''.join((b'\x00\x00\x00' if cell else b'\xff\xff\xff') * scale for cell in row)
        pixels.extend(line * scale)
    return f"P6\n{width} {width}\n255\n".encode() + pixels


class QrDisplay:
    def __init__(self, events):
        self.events = events

    async def show_qr(self, url):
        self.events.put(("qr", url))


def phone(value):
    value = re.sub(r"[\s()\-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise ValueError("Номер должен быть в международном формате: +79123456789")
    return value


def user_id(value):
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value.strip()):
        raise ValueError("ID пользователя должен быть положительным целым числом")
    return int(value)


class Credentials:
    def __init__(self, events):
        self.events = events

    async def ask(self, title):
        answer = concurrent.futures.Future()
        self.events.put(("credential", (title, answer)))
        result = await asyncio.wrap_future(answer)
        if not result:
            raise ValueError("Вход отменён")
        return result.strip()

    async def get_code(self, phone):
        return await self.ask("Код подтверждения MAX")

    async def get_password(self, hint=None):
        return await self.ask("Пароль 2FA MAX")


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()
        self.client = None
        self.target = None
        self.busy = False
        self.closing = False
        self.pending = None
        self.qr_window = None
        self.qr_photo = None
        self.contact_ids = set()
        self.chat_choices = {}
        self.stop_job = threading.Event()
        self.active_job = False
        root.title("MAX Workspace / Тестовый аккаунт / v5")
        root.geometry("980x900")
        root.minsize(900, 860)
        root.configure(bg="#f4f6f8")
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ctk.CTkFrame(root, fg_color="#f4f6f8", corner_radius=0)
        frame.pack(fill="both", expand=True, padx=28, pady=24)
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 18))
        ctk.CTkLabel(header, text="MAX Workspace", font=("Segoe UI", 26, "bold"), text_color="#182126").pack(side="left")
        self.connection_label = ctk.CTkLabel(header, text="Не подключено", text_color="#68727b", font=("Segoe UI", 13))
        self.connection_label.pack(side="right")
        ctk.CTkLabel(frame, text="01  /  Аккаунт", font=("Segoe UI", 16, "bold"), text_color="#182126").pack(anchor="w", pady=(0, 10))
        self.auth_mode = tk.StringVar(value="qr")
        auth_row = ctk.CTkFrame(frame, fg_color="transparent")
        auth_row.pack(fill="x")
        self.auth_selector = ctk.CTkSegmentedButton(auth_row, values=["QR-код", "Номер телефона"], command=self.set_auth_mode, corner_radius=6, height=36, font=("Segoe UI", 14), selected_color="#16785e", selected_hover_color="#11624c")
        self.auth_selector.set("QR-код")
        self.auth_selector.pack(anchor="w")
        self.account_fields = ctk.CTkFrame(frame, fg_color="transparent")
        self.account_fields.pack(fill="x")
        self.account = self.field(self.account_fields, "Номер своего аккаунта", "+7")
        self.connect_button = ctk.CTkButton(frame, text="Подключить аккаунт", command=self.connect, height=40, width=220, corner_radius=6, font=("Segoe UI", 14), fg_color="#16785e", hover_color="#11624c")
        self.connect_button.pack(anchor="w", pady=8)
        ctk.CTkFrame(frame, height=1, fg_color="#dce2e6", corner_radius=0).pack(fill="x", pady=16)
        tabs = ctk.CTkTabview(frame, height=360, corner_radius=0, fg_color="#f4f6f8", segmented_button_selected_color="#16785e")
        tabs.pack(fill="x")
        bulk = tabs.add("Контакты из чата")
        manual = tabs.add("По номеру / ID")
        bulk.grid_columnconfigure((0, 1), weight=1, uniform="workflow")
        left = ctk.CTkFrame(bulk, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ctk.CTkFrame(bulk, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        self.workflow_buttons = []
        ctk.CTkLabel(left, text="01 / Чат → контакты", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        self.source_chat = self.chat_field(left, "Исходный чат")
        self.collect_count = self.field(left, "Количество новых контактов", "10")
        self.collect_button = self.action_button(left, "Добавить в контакты", self.collect_from_chat)
        ctk.CTkLabel(right, text="02 / Контакты → группа", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        self.target_chat = self.chat_field(right, "Целевая группа")
        self.invite_count = self.field(right, "Количество новых участников", "10")
        self.bulk_invite_button = self.action_button(right, "Проверить и пригласить", self.invite_from_contacts)
        tools = ctk.CTkFrame(bulk, fg_color="transparent")
        tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.refresh_button = self.action_button(tools, "Обновить чаты", self.refresh_chats, side="left")
        self.check_button = self.action_button(tools, "Проверить участников", self.check_target, side="left")
        self.stop_button = ctk.CTkButton(tools, text="Остановить", command=self.stop_job.set, width=130, height=36, corner_radius=6, fg_color="#9f3d48", hover_color="#82313a", state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.contacts_label = ctk.CTkLabel(bulk, text="Контактов аккаунта: 0", font=("Segoe UI", 13), text_color="#56636b")
        self.contacts_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        frame_for_log = frame
        frame = manual
        self.mode = tk.StringVar(value="phone")
        modes = ctk.CTkFrame(frame, fg_color="transparent")
        modes.pack(anchor="w")
        selector = ctk.CTkSegmentedButton(modes, values=["Номер телефона", "ID MAX"], command=lambda value: self.mode.set("phone" if value == "Номер телефона" else "id"), corner_radius=6, height=36, font=("Segoe UI", 14), selected_color="#16785e", selected_hover_color="#11624c")
        selector.set("Номер телефона")
        selector.pack(anchor="w")
        self.lookup = self.field(frame, "Участник")
        self.group = self.field(frame, "ID целевой группы MAX (не ссылка)")
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", pady=12)
        self.buttons = []
        for label, callback in [("Найти участника", self.find), ("Добавить в контакты", self.add), ("Пригласить в группу", self.invite)]:
            button = ctk.CTkButton(row, text=label, command=callback, state="disabled", height=40, width=220, corner_radius=6, font=("Segoe UI", 14), fg_color="#16785e" if not self.buttons else "#34424a", hover_color="#11624c" if not self.buttons else "#26343b")
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)
        frame = frame_for_log
        self.status = tk.StringVar(value="Не подключено")
        ctk.CTkLabel(frame, textvariable=self.status, wraplength=740, justify="left", anchor="w", font=("Segoe UI", 14), text_color="#34424a").pack(fill="x", pady=(6, 12))
        ctk.CTkFrame(frame, height=1, fg_color="#dce2e6", corner_radius=0).pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(frame, text="Журнал операций", font=("Segoe UI", 15, "bold"), text_color="#182126").pack(anchor="w", pady=(0, 6))
        self.log = ctk.CTkTextbox(frame, height=140, state="disabled", font=("Segoe UI", 13), wrap="word", corner_radius=6, fg_color="#ffffff", border_width=1, border_color="#dce2e6", text_color="#34424a")
        self.log.pack(fill="both", expand=True)
        self.poll_timer = root.after(100, self.poll)
        self.controls()

    def set_auth_mode(self, value):
        self.auth_mode.set("qr" if value == "QR-код" else "sms")
        self.controls()

    def chat_field(self, parent, label):
        ctk.CTkLabel(parent, text=label, font=("Segoe UI", 13), text_color="#56636b").pack(anchor="w", pady=(10, 2))
        box = ctk.CTkComboBox(parent, values=[""], height=40, corner_radius=6, font=("Segoe UI", 13))
        box.set("")
        box.pack(fill="x")
        return box

    def action_button(self, parent, label, command, side="top"):
        button = ctk.CTkButton(parent, text=label, command=command, height=38, width=185, corner_radius=6, font=("Segoe UI", 13), fg_color="#16785e", hover_color="#11624c", state="disabled")
        button.pack(side=side, anchor="w", pady=10, padx=(0, 8))
        self.workflow_buttons.append(button)
        return button

    def selected_chat(self, box):
        text = box.get().strip()
        if text in self.chat_choices:
            return self.chat_choices[text]
        if re.fullmatch(r'-?[1-9][0-9]{0,18}', text):
            return int(text)
        raise ValueError("Выберите чат из списка или введите числовой ID MAX")

    def requested_count(self, entry):
        text = entry.get().strip()
        if not text.isdigit() or not 1 <= int(text) <= 1000:
            raise ValueError("Количество для тестового задания: от 1 до 1000")
        return int(text)

    def update_chats(self, chats):
        choices = {}
        groups = []
        for chat in chats or []:
            kind = str(getattr(chat.type, 'value', chat.type)).upper()
            if kind not in ('CHAT', 'CHANNEL', 'GROUP'):
                continue
            label = f"{(chat.title or 'Без названия')[:38]} / {chat.id}"
            choices[label] = chat.id
            if kind in ('CHAT', 'GROUP'):
                groups.append(label)
        self.chat_choices = choices
        self.source_chat.configure(values=list(choices) or [""])
        self.target_chat.configure(values=groups or [""])
        return f"Доступно чатов: {len(choices)}"

    def refresh_chats(self):
        if self.busy or not self.client:
            return
        self.run(self.client.fetch_chats(), self.update_chats)

    def check_target(self):
        if self.busy or not self.client:
            return
        try:
            target = self.selected_chat(self.target_chat)
        except ValueError as error:
            messagebox.showerror("Группа", str(error))
            return
        contacts = set(self.contact_ids)
        self.stop_job.clear()
        self.run(member_ids(self.client, target, self.stop_job), lambda ids: f"Участников в целевой группе: {len(ids)}. Ваших контактов уже там: {len(contacts.intersection(ids))}.", 3600, stoppable=True)

    def collect_from_chat(self):
        if self.busy or not self.client:
            return
        try:
            source = self.selected_chat(self.source_chat)
            count = self.requested_count(self.collect_count)
        except ValueError as error:
            messagebox.showerror("Задание", str(error))
            return
        if not messagebox.askyesno("Добавление контактов", f"Добавить до {count} новых контактов из чата {source}?\nПодтвердите, что имеете согласие этих участников на такой перенос данных."):
            return
        self.stop_job.clear()
        self.run(collect_contacts(self.client, source, count, set(self.contact_ids), self.client.me.contact.id, self.stop_job, lambda kind, data: self.events.put((kind, data))), lambda text: text, 3600, stoppable=True)

    def invite_from_contacts(self):
        if self.busy or not self.client:
            return
        try:
            target = self.selected_chat(self.target_chat)
            count = self.requested_count(self.invite_count)
        except ValueError as error:
            messagebox.showerror("Задание", str(error))
            return
        if not self.contact_ids:
            messagebox.showinfo("Контакты", "Нет доступных контактов аккаунта")
            return
        if not messagebox.askyesno("Приглашения", f"Пригласить до {count} контактов в группу {target}, исключив уже присутствующих?\nПодтвердите согласие контактов на приглашение и наличие прав в группе."):
            return
        self.stop_job.clear()
        self.run(invite_contacts(self.client, target, count, sorted(self.contact_ids), self.client.me.contact.id, self.stop_job, lambda kind, data: self.events.put((kind, data))), lambda text: text, 3600, stoppable=True)

    def worker(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()

    def field(self, frame, title, initial=""):
        ctk.CTkLabel(frame, text=title, font=("Segoe UI", 13), text_color="#56636b").pack(anchor="w", pady=(8, 2))
        entry = ctk.CTkEntry(frame, height=40, corner_radius=6, font=("Segoe UI", 15), fg_color="#ffffff", border_color="#ccd5db", border_width=1, text_color="#182126")
        entry.insert(0, initial)
        entry.pack(fill="x")
        return entry

    def controls(self):
        if self.auth_mode.get() == "sms" and not self.client:
            self.root.minsize(900, 940)
            self.account_fields.pack(fill="x", before=self.connect_button)
        else:
            self.root.minsize(900, 860)
            self.account_fields.pack_forget()
        self.account.configure(state="normal" if self.auth_mode.get() == "sms" and not self.busy and not self.client else "disabled")
        self.auth_selector.configure(state="disabled" if self.busy or self.client else "normal")
        self.connect_button.configure(state="disabled" if self.busy or self.client else "normal")
        for button in self.workflow_buttons:
            button.configure(state="normal" if self.client and not self.busy else "disabled")
        self.stop_button.configure(state="normal" if self.busy and self.active_job else "disabled")
        for i, button in enumerate(self.buttons):
            button.configure(state="normal" if self.client and not self.busy and (i == 0 or self.target) else "disabled")

    def run(self, operation, success, timeout=45, stoppable=False):
        if self.busy:
            return
        self.busy = True
        self.active_job = stoppable
        self.status.set("Выполняется запрос…")
        self.controls()
        self.pending = asyncio.run_coroutine_threadsafe(asyncio.wait_for(operation, timeout), self.loop)
        self.pending.add_done_callback(lambda future: self.events.put(("result", (future, success))))

    def connect(self):
        if not messagebox.askokcancel("Тестовая интеграция", "Используется неофициальный API MAX. Возможны ограничения и блокировка. Продолжить вход в свой тестовый аккаунт?"):
            return
        qr = self.auth_mode.get() == "qr"
        number = None
        if not qr:
            try:
                number = phone(self.account.get())
            except ValueError as error:
                messagebox.showerror("Номер", str(error))
                return
        async def operation():
            directory = Path(os.environ["LOCALAPPDATA"]) / "MAX-Test" / "sessions"
            directory.mkdir(parents=True, exist_ok=True)
            provider = Credentials(self.events)
            if qr:
                flow = QrAuthFlow(QrDisplay(self.events), password_provider=provider)
                client = create_client(qr=True, work_dir=str(directory), session_name="test-web.db", auth_flow=flow)
            else:
                client = create_client(phone=number, work_dir=str(directory), session_name="test.db", sms_code_provider=provider, password_provider=provider)
            try:
                await client.connect()
                if not client.me:
                    raise RuntimeError("Авторизация не завершена")
                return client
            except BaseException:
                await client.close()
                raise
        def success(client):
            self.client = client
            self.connection_label.configure(text="Аккаунт подключён", text_color="#16785e")
            self.account.configure(state="disabled")
            self.contact_ids = {contact.id for contact in (getattr(client, 'contacts', None) or []) if contact}
            self.contacts_label.configure(text=f"Контактов аккаунта: {len(self.contact_ids)}")
            self.update_chats(getattr(client, 'chats', None))
            return "Аккаунт подключён"
        self.run(operation(), success, 180)

    def hide_qr(self):
        if self.qr_window is not None:
            self.qr_window.destroy()
            self.qr_window = None
        self.qr_photo = None

    def cancel_login(self):
        if self.pending and not self.pending.done():
            self.pending.cancel()
        self.hide_qr()

    def show_qr(self, url):
        if self.closing or not self.pending or self.pending.done():
            return
        self.hide_qr()
        self.qr_window = ctk.CTkToplevel(self.root)
        self.qr_window.title("MAX — подтверждение входа")
        self.qr_window.resizable(False, False)
        self.qr_window.transient(self.root)
        self.qr_window.protocol("WM_DELETE_WINDOW", self.cancel_login)
        self.qr_photo = tk.PhotoImage(data=qr_image_data(url), format="PPM", master=self.root)
        tk.Label(self.qr_window, image=self.qr_photo, bg="white", borderwidth=0).pack(padx=24, pady=24)
        ctk.CTkLabel(self.qr_window, text="Ожидание подтверждения MAX", wraplength=360, font=("Segoe UI", 15)).pack(pady=(0, 12))
        ctk.CTkButton(self.qr_window, text="Отменить вход", command=self.cancel_login, height=40, corner_radius=6).pack(pady=(0, 24))
        self.status.set("QR получен. Ожидание подтверждения на телефоне…")

    def find(self):
        self.target = None
        self.controls()
        try:
            mode = self.mode.get()
            value = phone(self.lookup.get()) if mode == "phone" else user_id(self.lookup.get())
        except ValueError as error:
            messagebox.showerror("Участник", str(error))
            return
        async def operation():
            return await (self.client.search_by_phone(value) if mode == "phone" else self.client.get_user(value))
        def success(user):
            if user is None:
                return "Участник не найден"
            self.target = user.id
            return f"Найден участник MAX, ID: {user.id}. Проверьте ID перед добавлением."
        self.run(operation(), success)

    def add(self):
        target = self.target
        if target and messagebox.askyesno("Добавление контакта", f"Добавить участника {target} в контакты текущего аккаунта MAX?"):
            def success(result):
                if not result:
                    return "Подтверждение добавления не получено"
                self.contact_ids.add(target)
                self.contacts_label.configure(text=f"Контактов аккаунта: {len(self.contact_ids)}")
                return "Сервер вернул обновлённый контакт"
            self.run(self.client.add_contact(target), success)

    def invite(self):
        try:
            text = self.group.get().strip()
            if not re.fullmatch(r"-?[1-9][0-9]{0,18}", text):
                raise ValueError("Укажите числовой ID группы MAX")
            group = int(text)
        except ValueError as error:
            messagebox.showerror("Группа", str(error))
            return
        target = self.target
        if target and messagebox.askyesno("Приглашение", f"Пригласить участника {target} в группу {group}?\n\nПодтвердите, что участник согласен на приглашение и у вас есть необходимые права."):
            self.run(self.client.invite_users_to_group(group, [target], show_history=False), lambda result: "Сервер вернул группу. Проверьте появление участника в MAX." if result else "Подтверждение приглашения не получено")

    def poll(self):
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind == "progress":
                self.status.set(data)
                self.log.configure(state="normal")
                self.log.insert("end", datetime.now().strftime("%H:%M:%S") + "  " + data + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
            elif kind == "contact_added":
                self.contact_ids.add(data)
                self.contacts_label.configure(text=f"Контактов аккаунта: {len(self.contact_ids)}")
            elif kind == "qr":
                self.show_qr(data)
            elif kind == "credential":
                title, future = data
                if not future.done():
                    answer = simpledialog.askstring(title, title, show="*", parent=self.root)
                    if not future.done():
                        future.set_result(answer)
            else:
                future, success = data
                try:
                    text = success(future.result())
                except ValueError as error:
                    text = str(error)
                except concurrent.futures.CancelledError:
                    text = "Вход отменён"
                except TimeoutError:
                    text = "Время ожидания запроса истекло. Проверяйте результат в MAX перед повтором."
                except ApiError as error:
                    text = api_error_text(error)
                except RuntimeError as error:
                    text = "QR-код истёк. Подключитесь заново." if str(error) == "QR authentication expired" else f"Запрос не завершён: {type(error).__name__}. Проверьте подключение MAX."
                except BaseException as error:
                    text = f"Запрос не завершён: {type(error).__name__}. Проверьте сеть, права и ограничения MAX."
                self.status.set(text)
                self.log.configure(state="normal")
                self.log.insert("end", datetime.now().strftime("%H:%M:%S") + "  " + text + "\n\n")
                self.log.see("end")
                self.log.configure(state="disabled")
                self.busy = False
                self.active_job = False
                self.hide_qr()
                self.controls()
        if not self.closing:
            self.poll_timer = self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        self.stop_job.set()
        self.hide_qr()
        self.root.after_cancel(self.poll_timer)
        if self.pending and not self.pending.done():
            self.pending.cancel()
        async def shutdown():
            if self.client:
                await self.client.close()
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        def finish():
            if future.done():
                self.loop.call_soon_threadsafe(self.loop.stop)
                for timer in self.root.tk.call('after', 'info'):
                    self.root.tk.call('after', 'cancel', timer)
                self.root.destroy()
            else:
                self.root.after(100, finish)
        finish()


if __name__ == "__main__":
    root = ctk.CTk()
    App(root)
    root.mainloop()
