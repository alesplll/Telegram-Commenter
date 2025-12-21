import os
import asyncio
import base64
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from dotenv import load_dotenv
import aiohttp
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO

# Загрузка переменных окружения
load_dotenv()

API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE = os.getenv('TELEGRAM_PHONE_NUMBER')
PASSWORD = os.getenv('TELEGRAM_PASSWORD')
TARGET_CHANNELS = [ch.strip() for ch in os.getenv('TARGET_CHANNEL', '').split(',') if ch.strip()]
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_URL = os.getenv('OPENROUTER_API_URL', 'https://openrouter.ai/api/v1')
MODEL_NAME = os.getenv('MODEL_NAME')  # Должна быть vision-модель типа qwen/qwen-2-vl-7b-instruct

SESSION_FILE = 'telegram_session.session'
TEMP_DIR = Path('./temp')
TEMP_DIR.mkdir(exist_ok=True)

MAX_COMMENTS_PER_HOUR = 8
MAX_COMMENTS_PER_DAY = 50
MAX_REPLIES_PER_THREAD = 3  # Новое ограничение на ветку

class RateLimiter:
    def __init__(self, max_per_hour, max_per_day, verbose=True):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.verbose = verbose
        self.hour_history = []
        self.day_history = []

    async def handle_comment(self):
        now = datetime.now()
        self.hour_history = [t for t in self.hour_history if now - t <= timedelta(hours=1)]
        self.day_history = [t for t in self.day_history if now - t <= timedelta(days=1)]
        
        print("Waiting 15 seconds before replying...")
        await asyncio.sleep(15)

        if len(self.hour_history) >= self.max_per_hour or len(self.day_history) >= self.max_per_day:
            if self.verbose:
                print("Rate limit exceeded. Waiting before posting next comment...")
            await asyncio.sleep(60)
            await self.handle_comment()
        else:
            self.hour_history.append(now)
            self.day_history.append(now)
            if self.verbose:
                print(f"Allowed to comment. Hour: {len(self.hour_history)}/{self.max_per_hour}, Day: {len(self.day_history)}/{self.max_per_day}")

async def download_media(client, message):
    """Скачивает медиа в BytesIO для base64"""
    if not message.media:
        return None
    
    file_bytes = BytesIO()
    await client.download_media(message.media, file=file_bytes)
    file_bytes.seek(0)
    return file_bytes

def media_to_base64(file_bytes):
    """Конвертирует BytesIO в base64 data URL"""
    media_type = "image/jpeg"  # по умолчанию
    if file_bytes.getvalue().startswith(b'\xFF\xD8'):  # JPEG
        media_type = "image/jpeg"
    elif file_bytes.getvalue().startswith(b'\x89PNG'):  # PNG
        media_type = "image/png"
    
    base64_image = base64.b64encode(file_bytes.getvalue()).decode('utf-8')
    return f"data:{media_type};base64,{base64_image}"

async def generate_comment(content, images_base64=[], reply_context=""):
    """Генерирует ответ с vision поддержкой"""
    if not content.strip():
        content = "[Сообщение без текста]"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://web.telegram.org/k/#@kishkeklak",  # Требуется OpenRouter
        "X-Title": "AitishnicaBot",  # Требуется OpenRouter
    }
    
    system_msg = (
        "Ты — подписчик личного Telegram-канала, русская айтишница 20 лет, красивая славянка. "
        "Отвечаешь на посты в комментариях: старайся часто использовать мат (блять, нахуй, пиздец, заебали и тд), "
        "жалуешься на мир/софт, но с технарским взглядом (сравнивай с математикой, теорией вероятностей, математической статистикой, кодом, алгоритмами, багами). "
        
        "ПРАВИЛА АНАЛИЗА ПОСТА:\n"
        "1. ЕСТЬ ФОТО? → ОСНОВНОЙ УПОР НА НЕЁ (70% ответа). Распознай текст на фото, людей, объекты. "
           "Если девушка/парень на фото — СДЕЛАЙ КОМПЛИМЕНТ + технарский комментарий.\n"
        "2. ТОЛЬКО ТЕКСТ? → распознай суть, дай ответ на пост.\n"
        "3. ФОТО+ТЕКСТ? → фото важнее текста.\n"
        
        "Стиль: коротко (2-4 предложения), разговорно ('короче', 'а чё'), предложения СВЯЗНЫЕ, "
        "одна ЧЕТКАЯ мысль. Будь саркастичной, умной, иногда милой."
    )

    # Строим messages с поддержкой vision
    messages = [{"role": "system", "content": system_msg}]
    
    user_content = []
    if reply_context:
        user_content.append({"type": "text", "text": f"Контекст (ответ на это): {reply_context}\n\n"})
    
    user_content.append({"type": "text", "text": f'Ответь на это сообщение как эта айтишница: "{content}"'})
    
    # Добавляем изображения
    for img_b64 in images_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": img_b64}
        })
    
    messages.append({
        "role": "user", 
        "content": user_content
    })

    data = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 500
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{OPENROUTER_API_URL}/chat/completions", 
                                  headers=headers, json=data) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
                else:
                    print(f"OpenRouter API error: {resp.status} - {await resp.text()}")
                    return "Пхах, блять, API опять лег, короче типичный пиздец."
        except Exception as e:
            print(f"Error generating comment: {e}")
            return "Окак, серверы опять наебнулись, заебали."

async def main():
    print("Starting Vision Telegram Auto Commenter...")
    print(f"Rate limits: {MAX_COMMENTS_PER_HOUR}/hour, {MAX_COMMENTS_PER_DAY}/day, {MAX_REPLIES_PER_THREAD}/thread")

    if not all([PASSWORD, OPENROUTER_API_KEY, MODEL_NAME]):
        raise Exception("Missing required env vars")

    rate_limiter = RateLimiter(MAX_COMMENTS_PER_HOUR, MAX_COMMENTS_PER_DAY)
    
    # Трекинг веток бота: {discussion_msg_id: count_replies}
    bot_threads = {}
    session_str = Path(SESSION_FILE).read_text() if Path(SESSION_FILE).exists() else ''

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.start(phone=PHONE, password=PASSWORD)
    
    Path(SESSION_FILE).write_text(client.session.save())
    print("Client started")

    if not TARGET_CHANNELS:
        print("No TARGET_CHANNEL in .env")
        return

    monitored_channels = {}
    for channel_username in TARGET_CHANNELS:
        entity = await client.get_entity(channel_username)
        full_channel = await client(GetFullChannelRequest(channel=entity))
        channel_id = str(entity.id)
        
        linked_chat_id = getattr(full_channel.full_chat, 'linked_chat_id', None)
        if linked_chat_id:
            discussion_chat = await client.get_entity(linked_chat_id)
            monitored_channels[channel_id] = discussion_chat
            print(f"Channel '{entity.title}' -> discussion {discussion_chat.id}")


    @client.on(events.NewMessage)
    async def handler(event):
        message = event.message

        # Игнорируем служебные и вообще всё без peer_id
        if not message.peer_id or not message or not getattr(message.peer_id, 'channel_id', None):
            return

        # ---------- 1. НОВЫЙ ПОСТ В КАНАЛЕ ----------
        if getattr(message, "post", False) and hasattr(message.peer_id, "channel_id"):
            message_channel_id = str(message.peer_id.channel_id)
            if message_channel_id not in monitored_channels:
                return

            discussion_entity = monitored_channels[message_channel_id]
            print(f"New channel post {message.id} in channel {message_channel_id}")

            await rate_limiter.handle_comment()

            images = []
            if message.media:
                img_bytes = await download_media(client, message)
                if img_bytes:
                    images.append(media_to_base64(img_bytes))
                    print("Downloaded image for vision (post)")

            comment = await generate_comment(message.text or "", images)
            print(f"Generated: {comment[:100]}...")

            # получаем id корневого сообщения в обсуждении
            discussion_message = await client(GetDiscussionMessageRequest(
                msg_id=message.id,
                peer=message.peer_id
            ))
            discussion_msg_id = discussion_message.messages[0].id

            sent_msg = await client.send_message(
                discussion_entity,  # entity, не id!
                comment,
                reply_to=discussion_msg_id
            )

            # регистрируем ветку (по корневому сообщению обсуждения)
            bot_threads[discussion_msg_id] = 1
            print(f"Bot reply #1 to post {message.id} -> discussion_msg {discussion_msg_id}, bot_msg {sent_msg.id}")
            return

        # ---------- 2. СООБЩЕНИЯ В ГРУППАХ ОБСУЖДЕНИЯ ----------
        # chat_id для сопоставления с discussion_entity.id
        peer = message.peer_id
        chat_id = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
        if not chat_id:
            return

        discussion_entity = None
        for channel_id, disc_entity in monitored_channels.items():
            if getattr(disc_entity, "id", None) == chat_id:
                discussion_entity = disc_entity
                break

        # если это не одна из наших групп обсуждения — выходим
        if not discussion_entity:
            return

        # это сообщение в нужной группе обсуждения
        print(f"Message in discussion group {chat_id}, msg_id={message.id}")

        # ---------- 2.1. Проверяем, это ли ответ в ветке бота ----------
        if (message.reply_to and
            getattr(message.reply_to, "reply_to_top_id", None) in bot_threads):

            thread_root_id = message.reply_to.reply_to_top_id
            reply_count = bot_threads.get(thread_root_id, 0)

            if reply_count >= MAX_REPLIES_PER_THREAD:
                print(f"Thread {thread_root_id} limit reached "
                      f"({reply_count}/{MAX_REPLIES_PER_THREAD})")
                return

            print(f"REPLY to bot thread {thread_root_id} "
                  f"(#{reply_count + 1}/{MAX_REPLIES_PER_THREAD})")

            await rate_limiter.handle_comment()

            images = []
            if message.media:
                img_bytes = await download_media(client, message)
                if img_bytes:
                    images.append(media_to_base64(img_bytes))
                    print("Downloaded reply image for vision")

            comment = await generate_comment(
                message.text or "",
                images,
                reply_context="Это ответ на твое предыдущее сообщение в ветке"
            )

            sent_msg = await client.send_message(
                discussion_entity,         # entity группы обсуждений
                comment,
                reply_to=message.id        # отвечаем на сообщение пользователя
            )

            bot_threads[thread_root_id] = reply_count + 1
            print(f"Bot reply #{bot_threads[thread_root_id]} in thread {thread_root_id}, "
                  f"bot_msg {sent_msg.id}")
            return

        # ---------- 2.2. Не ответ на ветку бота ----------
        print(
            f"Ignored reply in {chat_id}: "
            f"reply_to_top={getattr(message.reply_to, 'reply_to_top_id', None) if message.reply_to else None}"
        )


    print("Bot running... Ctrl+C to stop")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

